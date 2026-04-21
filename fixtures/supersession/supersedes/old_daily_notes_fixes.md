---
title: "Daily notes pipeline fixes (2026-03-20)"
date_created: 2026-03-20
tags: [daily-notes, fix]
---

# Daily notes pipeline fixes

The watcher was not injecting frontmatter correctly on files dropped into
the Inbox, so the curator would skip them. Patched the watcher to insert
`status: unreviewed` and the curator now picks them up within 30 min.

## What was changed
- watcher: inject `status: unreviewed` when missing
- curator: poll every 30 min for unreviewed notes

## Known issues
- Curator crashes if the file has invalid YAML; needs handling.
