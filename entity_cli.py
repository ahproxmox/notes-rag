#!/usr/bin/env python3
"""Entity tracker CLI — manage the shared entity registry from the command line.

Usage:
    entity_cli.py add <slug> --type container --name 'CT 111 — notes-rag' \
                             --wing agents --summary '...' \
                             --attr ip=192.168.88.71 --attr port=8080 \
                             --alias rag --alias notes-rag
    entity_cli.py get <slug>
    entity_cli.py list [--type container] [--wing infra]
    entity_cli.py search <query>
    entity_cli.py delete <slug>
    entity_cli.py import <yaml-file>
    entity_cli.py export [--format yaml|json]

Default DB: /opt/rag/entities.db  (override with --db or ENTITIES_DB env var)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from entities import EntityStore

DEFAULT_DB = os.environ.get('ENTITIES_DB', '/opt/rag/entities.db')


def _parse_attrs(pairs: list[str]) -> dict:
    out: dict = {}
    for p in pairs or []:
        if '=' not in p:
            print(f'warn: skipping invalid --attr "{p}" (expected key=value)', file=sys.stderr)
            continue
        k, v = p.split('=', 1)
        # Best-effort type coercion: int, float, bool, else str
        vs = v.strip()
        if vs.lower() in ('true', 'false'):
            out[k.strip()] = (vs.lower() == 'true')
        else:
            try:
                out[k.strip()] = int(vs)
            except ValueError:
                try:
                    out[k.strip()] = float(vs)
                except ValueError:
                    out[k.strip()] = vs
    return out


def _print_entity(e: dict, verbose: bool = True):
    if not e:
        print('(not found)')
        return
    print(f"slug:    {e['slug']}")
    print(f"type:    {e['type']}")
    print(f"name:    {e['name']}")
    if e.get('wing'):
        print(f"wing:    {e['wing']}")
    if e.get('summary'):
        print(f"summary: {e['summary']}")
    if e.get('aliases'):
        print(f"aliases: {', '.join(e['aliases'])}")
    if e.get('attrs'):
        print('attrs:')
        for k, v in sorted(e['attrs'].items()):
            print(f'  {k}: {v}')
    if verbose:
        print(f"created: {e['created']}")
        print(f"updated: {e['updated']}")


def cmd_add(store: EntityStore, args):
    e = store.upsert(
        slug=args.slug,
        type=args.type,
        name=args.name,
        wing=args.wing,
        summary=args.summary,
        attrs=_parse_attrs(args.attr),
        aliases=args.alias or [],
    )
    print(f"upserted: {e['slug']}")
    _print_entity(e, verbose=False)


def cmd_get(store: EntityStore, args):
    e = store.get(args.slug)
    _print_entity(e)
    return 0 if e else 1


def cmd_list(store: EntityStore, args):
    entities = store.list(type=args.type, wing=args.wing)
    print(f'{len(entities)} entities')
    for e in entities:
        summary = (e.get('summary') or '')[:60]
        print(f"  {e['type']:10} {e['slug']:20} {summary}")


def cmd_search(store: EntityStore, args):
    results = store.search(args.query, k=args.k)
    print(f'{len(results)} matches')
    for e in results:
        summary = (e.get('summary') or '')[:60]
        print(f"  {e['type']:10} {e['slug']:20} {summary}")


def cmd_delete(store: EntityStore, args):
    if store.delete(args.slug):
        print(f'deleted: {args.slug}')
    else:
        print(f'not found: {args.slug}')
        return 1


def cmd_import(store: EntityStore, args):
    path = Path(args.file)
    if not path.exists():
        print(f'file not found: {path}', file=sys.stderr)
        return 1
    data = yaml.safe_load(path.read_text()) or {}
    entities = data.get('entities') or []
    if not entities:
        print('no entities in file (expected top-level "entities:" list)')
        return 1
    for row in entities:
        store.upsert(
            slug=row['slug'],
            type=row['type'],
            name=row['name'],
            wing=row.get('wing'),
            summary=row.get('summary'),
            attrs=row.get('attrs') or {},
            aliases=row.get('aliases') or [],
        )
    print(f'imported: {len(entities)} entities (total now: {store.count()})')


def cmd_export(store: EntityStore, args):
    entities = store.list()
    payload = {'entities': entities}
    if args.format == 'json':
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))


def main():
    p = argparse.ArgumentParser(description='Entity tracker CLI')
    p.add_argument('--db', default=DEFAULT_DB, help=f'Path to entities.db (default: {DEFAULT_DB})')
    sub = p.add_subparsers(dest='cmd', required=True)

    sp = sub.add_parser('add', help='Add or update an entity')
    sp.add_argument('slug')
    sp.add_argument('--type', required=True)
    sp.add_argument('--name', required=True)
    sp.add_argument('--wing')
    sp.add_argument('--summary')
    sp.add_argument('--attr', action='append', help='key=value (repeatable)')
    sp.add_argument('--alias', action='append', help='alias name (repeatable)')
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser('get', help='Fetch an entity by slug or alias')
    sp.add_argument('slug')
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser('list', help='List entities')
    sp.add_argument('--type')
    sp.add_argument('--wing')
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser('search', help='Fuzzy search over name/summary/aliases')
    sp.add_argument('query')
    sp.add_argument('-k', type=int, default=10)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser('delete', help='Delete an entity by slug')
    sp.add_argument('slug')
    sp.set_defaults(func=cmd_delete)

    sp = sub.add_parser('import', help='Bulk upsert from a YAML file')
    sp.add_argument('file')
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser('export', help='Dump all entities as YAML or JSON')
    sp.add_argument('--format', choices=['yaml', 'json'], default='yaml')
    sp.set_defaults(func=cmd_export)

    args = p.parse_args()
    store = EntityStore(args.db)
    try:
        rc = args.func(store, args)
        sys.exit(rc or 0)
    finally:
        store.close()


if __name__ == '__main__':
    main()
