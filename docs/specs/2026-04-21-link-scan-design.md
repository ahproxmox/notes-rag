---
tags: [spec]
title: "Link Scan — Supersession + Related Detection"
todo: 136
status: approved
created: 2026-04-21
---

# Link Scan — Supersession + Related Detection

User-triggered scan over a note being reviewed that surfaces two kinds of
candidate links against the existing corpus: **supersedes** (the new note
replaces an older one) and **related** (the notes cover the same topic
without replacement). Confirmed supersessions hide the old note from default
RAG results; confirmed related links cross-reference both sides.

This is a slice of the broader "memory lifecycle" exploration (Todo 136).
Confidence scoring, retention decay, and consolidation tiers were evaluated
and rejected for this workspace — see the decision notes at the bottom.

## Architecture

Home: this repo (ahproxmox/notes-rag), deployed on CT 111. No new service;
feature of the existing backend + review UI.

Components:

1. **Detection endpoint** — `POST /api/links/scan?path=N`. Runs hybrid search
   → threshold filter → LLM judge → categorised results.
2. **Commit endpoint** — `POST /api/links/confirm`. Mutates frontmatter on
   both sides, fires re-index. Idempotent.
3. **Rejection endpoint** — `POST /api/links/reject`. Appends a path to the
   note's `rejected_link_candidates` frontmatter so the pair isn't
   re-suggested.
4. **Review UI additions** — a "Scan for links" button plus two result
   panels (supersessions, related) with Confirm / Reject / Skip per card.
5. **Search filter update** — default hybrid search excludes notes where
   `superseded_by` is set. Opt-in via `include_superseded=true`.

Data flow (reviewing note N):

```
UI loads review page for N                 [no auto-scan]
  User clicks "Scan for links"
    → POST /api/links/scan?path=N
      → hybrid search top-K, threshold 0.6
      → LLM judge each candidate (Gemini Flash Lite)
      → categorise supersessions / related
    ← UI renders two panels with cards

  User clicks "Confirm" on a card
    → POST /api/links/confirm { type, source, target }
      → atomic frontmatter write on both files
      → fire-and-forget re-index
    ← card animates out, banner confirms

  User clicks "Reject" on a card
    → POST /api/links/reject { source, target }
      → append target to source.rejected_link_candidates
    ← card disappears (persisted)
```

External dependencies: Gemini Flash Lite via the existing provider (same key
and endpoint the wedding-platform and curator use). No new infra.

## Data model

All note-level state in YAML frontmatter. Paths are relative to workspace
root, matching existing MEMORY.md conventions.

**On an old note (the one being replaced):**

```yaml
superseded_by: project_manual_notes_workflow.md
superseded_at: 2026-04-21
```

**On the new note (the replacement):**

```yaml
supersedes:
  - project_daily_notes_fixes_2026_03_20.md
  - project_other_old_file.md
```

**For related links, on both notes:**

```yaml
related:
  - project_X.md
  - project_Y.md
```

**On any note that has had scans run against it:**

```yaml
rejected_link_candidates:
  - path/to/rejected_note.md
```

Rules:

- `supersedes` is an array — one rewrite can consolidate multiple predecessors.
- `superseded_by` is a single string — a note has one authoritative successor.
- `related` is symmetric: writing it on one side writes it on the other.
- `rejected_link_candidates` suppresses future suggestions for that pair.
- Missing fields mean "not superseded / no related / no rejections" — no
  migration needed for existing notes.

## Detection pipeline

Trigger: user clicks "Scan for links" on the review page for note N.

1. Load N's content + frontmatter.
2. Build exclusion set:
   - N itself
   - Anything in `N.supersedes`, `N.superseded_by`, `N.related`
   - Anything in `N.rejected_link_candidates`
   - Any candidate C where `C.superseded_by` is already set
3. Hybrid search top-K (K = 10, similarity threshold = 0.6) using N's body
   as the query. Drop anything in the exclusion set.
4. LLM judge: one Gemini Flash Lite call per remaining candidate C. Single
   call covers both relation types. Prompt:

   ```
   You are classifying the relationship between two notes from a
   personal knowledge base. Respond in strict JSON.

   Note A (existing):
   <C content, truncated to 2000 chars>

   Note B (new):
   <N content, truncated to 2000 chars>

   Does B supersede A (i.e. B is the current, correct version and A is
   now outdated)? Or do A and B cover the same topic without one
   replacing the other? Or neither?

   Return: {"relation": "supersedes" | "related" | "none",
            "reason": "<one sentence>"}
   ```

5. Categorise by `relation`. Drop `none`. Return:

   ```json
   {
     "supersessions": [{"path": "...", "reason": "...", "similarity": 0.82}],
     "related":       [{"path": "...", "reason": "...", "similarity": 0.71}]
   }
   ```

Cost: ~10 calls × Flash Lite pricing ≈ $0.001 per scan. Trivial.

Latency: K LLM calls in parallel, ~1–2 s. UI shows spinner.

Failure modes:

- LLM call fails for one candidate → skip it, log, continue. Always return
  partial results rather than erroring out.
- No candidates above threshold → empty result, UI shows "No link candidates
  found."
- Invalid JSON from LLM → treat as `none`, log for prompt tuning.

The 2000-char truncation is intentional for cost control. Very long notes
may lose context; revisit if accuracy suffers in practice.

## Search filter + commit endpoint

### Search filter

Existing hybrid search query augmented with:

```
WHERE superseded_by IS NULL OR superseded_by = ''
```

(or equivalent in the SQLite-vec index introduced in Todo 112).

New query param: `include_superseded=true` drops the filter. Default false.

Direct-path lookups (opening a note by title or path) bypass the filter —
if you know a superseded note exists and type its name, you get it. Only
default RAG results hide it.

### Commit endpoint

`POST /api/links/confirm`

```json
{
  "type": "supersedes" | "related",
  "source": "<path of the note being reviewed>",
  "target": "<path of the other note>"
}
```

**Handler (supersession):**

1. Read `source` and `target` frontmatter.
2. Guard: if `target.superseded_by` is already set, return 409 — don't
   re-mark.
3. Write `superseded_by: source` and `superseded_at: <today>` to `target`.
4. Append `target` to `source.supersedes[]` (create array if missing).
5. Fire-and-forget re-index of both files.
6. Return 200 with updated frontmatter of both.

**Handler (related):**

1. Read both frontmatters.
2. Append `target` to `source.related[]` and `source` to `target.related[]`.
   Skip if already present (idempotent).
3. Fire-and-forget re-index of both.
4. Return 200.

### Atomicity

The pair of frontmatter writes (steps 3+4 for supersession, step 2 for
related) is wrapped in a single file-mutation transaction — both files
updated via temp-file + atomic rename, or both rolled back. Uses the same
helper the review UI already uses for `reviewed: true` flips.

Re-index is fire-and-forget post-commit; a re-index failure does not roll
back the frontmatter writes (the content on disk is the source of truth
and will be picked up on the next indexer pass).

### Rollback / reversal

No dedicated endpoint. Edit the frontmatter by hand (delete `superseded_by`,
remove from `supersedes[]`) — rare enough that a UI affordance isn't worth
it yet.

## UI

Review page additions:

- **"Scan for links" button** at the top of the review panel, next to
  existing review actions. Disabled while a scan is in flight; shows
  spinner.
- **Results region** hidden until first scan completes. Two
  collapsed-by-default sections:
  - `Possible supersessions (N)` — orange accent
  - `Related notes (M)` — blue accent
- **Each card** shows:
  - Target note title (clickable → opens target in new tab, no page nav)
  - LLM reason, one line
  - Similarity score as a small badge
  - Three buttons: `Confirm` / `Reject` / `Skip`
- **Confirm** → POST `/api/links/confirm`, card animates out, top banner
  confirms.
- **Reject** → POST `/api/links/reject`, card disappears, persisted in
  `rejected_link_candidates` so the pair won't be re-suggested.
- **Skip** → card disappears, no persistence. Re-offered on next scan.
- **Empty state** — "No link candidates found for this note."

Minimal new frontend state. No routing change. One new API client function
per endpoint.

## Testing

### Fixtures

Three synthetic note-pair sets under `fixtures/supersession/`:

1. Known-supersedes pair — two notes on the same topic, one clearly
   replacing the other.
2. Known-related pair — two notes on the same topic, neither replacing
   the other.
3. Known-unrelated pair — different topics, must return `none`.

Fixtures are synthetic (not copies of real workspace notes) so the public
repo doesn't leak internal content.

### Unit / integration tests

- **Heuristic search** — fixture pairs with known high similarity return
  each other in top-K; unrelated pair does not.
- **LLM judge** — mock the LLM client; assert the prompt is constructed
  correctly. One live smoke test against real Flash Lite, snapshot the JSON
  (regenerate when prompts change).
- **Commit endpoint (supersession)** — POST, assert both files' frontmatter
  updated, assert re-index called, assert idempotency (409 on re-post).
- **Commit endpoint (related)** — POST, assert both sides get the array
  entry, assert no dupes on re-post.
- **Search filter** — seed two notes, mark one superseded via the commit
  endpoint, query default search, assert only the current one returns.
  Query with `include_superseded=true`, assert both return.
- **Rejection list** — confirm rejection is persisted, filtered out of
  subsequent scans.
- **Atomicity** — simulate a write failure on the second file; assert the
  first file is rolled back.

### Manual acceptance

Before calling done, review one real note in the live notes-rag UI, scan,
confirm a real supersession, verify the two affected frontmatters and that
the superseded note drops out of default search.

## Out of scope / not doing

Evaluated as part of the Todo 136 exploration and explicitly rejected:

- **Confidence scoring / retention decay.** This workspace is a homelab KB
  where project state and decisions remain valid long-term (a Caddy setup
  from last quarter isn't less true today). Time-based decay actively hurts
  retrieval quality here. Confidence scoring adds a signal we don't need —
  virtually all entries are deliberately curated, not auto-extracted.
- **Consolidation tiers (working / episodic / semantic / procedural).**
  Existing `tags`, `project`, `status`, and wing/room classification already
  cover most of what tiers would add. Another axis of frontmatter to
  maintain for query routing that hybrid search mostly handles already.
- **Fact-level (sub-note) supersession.** Requires fact extraction and
  entity linking, which is most of what Cognee would have provided, and
  Cognee was rejected (Todo 079). Note-level supersession matches the
  manual workflow already in use (`project_daily_notes_fixes_2026_03_20.md`
  → `project_manual_notes_workflow.md` in MEMORY.md).
- **Contradiction detection, prereq link suggestion, general
  knowledge-graph features.** Different problems, out of scope for v1.
