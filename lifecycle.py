"""Memory lifecycle: confidence scoring, retention decay, and supersession detection.

All three features feed a lifecycle multiplier applied post-retrieval:
    final_score = rrf_score × confidence × decay_factor

Superseded chunks are filtered at the query layer (include_superseded=False by default).
"""

import math
from datetime import date
from pathlib import Path
from store import Store


# ---------------------------------------------------------------------------
# Confidence by source folder
# ---------------------------------------------------------------------------

CONFIDENCE_BY_FOLDER: dict[str, float] = {
    'context':   1.00,  # deliberate decisions and preferences
    'memory':    0.95,  # curated memory files
    'wiki':      0.95,  # synthesised topic pages
    'sessions':  0.90,  # recorded work sessions
    'plans':     0.90,  # deliberate plans
    'reference': 0.90,  # reference material
    'proposals': 0.85,
    'todos':     0.85,  # task descriptions (may be stale if completed)
    'reports':   0.80,
    'inbox':     0.70,  # unprocessed / unreviewed notes
}
CONFIDENCE_DEFAULT = 0.80


def confidence_for_folder(folder: str) -> float:
    """Return the confidence weight for a given source folder."""
    return CONFIDENCE_BY_FOLDER.get(folder, CONFIDENCE_DEFAULT)


# ---------------------------------------------------------------------------
# Retention decay
# ---------------------------------------------------------------------------

# Decay constant: 180 days → decay_factor ≈ 0.6
# Derived from: 0.6 = e^(-λ × 180) → λ = -ln(0.6) / 180
DECAY_LAMBDA = -math.log(0.6) / 180  # ≈ 0.002837


def compute_decay_factor(last_updated: str | None) -> float:
    """Compute retention decay factor from a YYYY-MM-DD date string.

    Returns 1.0 for unknown or future dates, decays exponentially with age.
    At 30 days ≈ 0.92; at 180 days ≈ 0.60; at 365 days ≈ 0.36.
    """
    if not last_updated:
        return 1.0
    try:
        updated = date.fromisoformat(str(last_updated)[:10])
        days = (date.today() - updated).days
        if days <= 0:
            return 1.0
        return round(math.exp(-DECAY_LAMBDA * days), 4)
    except (ValueError, TypeError):
        return 1.0


# ---------------------------------------------------------------------------
# Lifecycle recalculation (startup + on-demand)
# ---------------------------------------------------------------------------

def recalculate_lifecycle(store: Store) -> dict:
    """Recompute confidence and decay_factor for all chunks from current date.

    Batches updates per source file rather than per chunk for efficiency.
    Returns a summary dict with 'sources_updated'.
    """
    rows = store._conn.execute(
        'SELECT DISTINCT source, folder, last_updated FROM chunks'
    ).fetchall()

    updated = 0
    for source, folder, last_updated in rows:
        new_confidence = confidence_for_folder(folder or 'root')
        new_decay = compute_decay_factor(last_updated)
        store._conn.execute(
            'UPDATE chunks SET confidence = ?, decay_factor = ? WHERE source = ?',
            (new_confidence, new_decay, source),
        )
        updated += 1

    store._conn.commit()
    print(f'[lifecycle] recalculated {updated} sources', flush=True)
    return {'sources_updated': updated}


# ---------------------------------------------------------------------------
# Supersession sweep (triggered per-file after indexing)
# ---------------------------------------------------------------------------

SUPERSESSION_THRESHOLD = 0.92


def supersession_sweep(store: Store, source: str) -> list[tuple[str, str]]:
    """Detect if a newly indexed file supersedes (or is superseded by) similar notes.

    Uses the first chunk of `source` as a representative query, searches for
    similar chunks in the same wing/room, and marks the older file as
    superseded_by the newer one when cosine similarity > SUPERSESSION_THRESHOLD.

    Only compares within the same wing+room to avoid false positives across topics.
    Returns list of (superseded_source, superseding_source) pairs.
    """
    row = store._conn.execute(
        'SELECT wing, room, last_updated FROM chunks WHERE source = ? LIMIT 1',
        (source,),
    ).fetchone()
    if not row or not row[0] or not row[1]:
        # Skip if wing/room not classified — too risky without topic scope
        return []

    wing, room, source_updated = row

    first_chunk = store._conn.execute(
        'SELECT content FROM chunks WHERE source = ? LIMIT 1',
        (source,),
    ).fetchone()
    if not first_chunk:
        return []

    # Search for similar chunks in same wing/room, excluding already-superseded notes
    similar_docs = store.search_vector(
        first_chunk[0], k=20, wing=wing, room=room, include_superseded=False,
    )

    # Deduplicate by source, keep highest similarity score
    candidates: dict[str, tuple[float, str | None]] = {}
    for doc in similar_docs:
        other_source = doc.metadata.get('source', '')
        if not other_source or other_source == source:
            continue
        sim = float(doc.metadata.get('similarity', 0.0))
        if sim < SUPERSESSION_THRESHOLD:
            continue
        if other_source not in candidates or candidates[other_source][0] < sim:
            other_row = store._conn.execute(
                'SELECT last_updated FROM chunks WHERE source = ? LIMIT 1',
                (other_source,),
            ).fetchone()
            candidates[other_source] = (sim, other_row[0] if other_row else None)

    if not candidates:
        return []

    superseded_pairs: list[tuple[str, str]] = []
    source_date = (source_updated or '')[:10] or '0000-01-01'

    for other_source, (sim, other_updated) in candidates.items():
        other_date = (other_updated or '')[:10] or '0000-01-01'

        if source_date >= other_date:
            # source is newer → mark other as superseded by source
            store._conn.execute(
                'UPDATE chunks SET superseded_by = ? WHERE source = ?',
                (Path(source).name, other_source),
            )
            superseded_pairs.append((other_source, source))
            print(
                f'[lifecycle] {Path(other_source).name} superseded by'
                f' {Path(source).name} (sim={sim:.3f})',
                flush=True,
            )
        else:
            # other is newer → mark source as superseded by other
            store._conn.execute(
                'UPDATE chunks SET superseded_by = ? WHERE source = ?',
                (Path(other_source).name, source),
            )
            superseded_pairs.append((source, other_source))
            print(
                f'[lifecycle] {Path(source).name} superseded by'
                f' {Path(other_source).name} (sim={sim:.3f})',
                flush=True,
            )

    if superseded_pairs:
        store._conn.commit()

    return superseded_pairs
