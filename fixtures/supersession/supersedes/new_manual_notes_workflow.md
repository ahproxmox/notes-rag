---
title: "Manual notes workflow (2026-04-03)"
date_created: 2026-04-03
tags: [daily-notes, workflow]
---

# Manual notes workflow

The daily notes pipeline has been replaced by a manual workflow. The
watcher now injects frontmatter on any markdown file dropped into the
Inbox and marks it `reviewed: unreviewed`. The curator reviews the
queue every 30 minutes and auto-archives files once `reviewed: true`.

This supersedes the earlier "daily notes pipeline fixes" write-up —
the watcher+curator flow is the new canonical path.

## Flow
- Drop file into Inbox → watcher adds frontmatter
- Curator scans unreviewed queue every 30 min
- On `reviewed: true`, curator moves to archive

## Resilience
- Invalid YAML is logged, not fatal.
- Curator backoff retries transient failures.
