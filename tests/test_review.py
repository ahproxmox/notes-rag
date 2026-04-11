import tempfile
import os
import pytest
from review import parse_frontmatter, write_frontmatter, scan_unreviewed

SAMPLE_NOTE = """---
date_created: 2026-04-10
reviewed: unreviewed
tags: []
---
# Venues
1) Poets Lane
2) Lyrebird falls
"""

REVIEWED_NOTE = """---
date_created: 2026-04-10
reviewed: true
tags: [wedding, venues]
review_count: 1
---
# Venues
1) Poets Lane
2) Lyrebird falls

## Review 1
_2026-04-11_

- **Purpose:** Ceremony venue shortlist
"""

REVIEW2_NOTE = """---
date_created: 2026-04-10
reviewed: unreviewed
tags: [wedding, venues]
review_count: 1
---
# Venues
1) Poets Lane
2) Lyrebird falls
3) Circa 1876

## Review 1
_2026-04-11_

- **Purpose:** Ceremony venue shortlist
"""


def test_parse_frontmatter_unreviewed():
    fm, body = parse_frontmatter(SAMPLE_NOTE)
    assert fm['reviewed'] == 'unreviewed'
    assert fm['tags'] == []
    assert fm['date_created'] == '2026-04-10'
    assert '# Venues' in body


def test_parse_frontmatter_reviewed():
    fm, body = parse_frontmatter(REVIEWED_NOTE)
    assert fm['reviewed'] is True
    assert fm['tags'] == ['wedding', 'venues']
    assert fm['review_count'] == 1
    assert '## Review 1' in body


def test_parse_frontmatter_no_frontmatter():
    fm, body = parse_frontmatter('# Just a heading\nSome text')
    assert fm == {}
    assert '# Just a heading' in body


def test_write_frontmatter_first_review():
    fm, body = parse_frontmatter(SAMPLE_NOTE)
    result = write_frontmatter(
        fm, body,
        tags=['wedding', 'venues', 'planning'],
        review_num=1,
        review_content='_2026-04-11_\n\n- **Purpose:** Ceremony venue shortlist',
    )
    assert 'reviewed: true' in result
    assert 'review_count: 1' in result
    assert 'tags: [wedding, venues, planning]' in result
    assert '## Review 1' in result
    assert '# Venues' in result


def test_write_frontmatter_second_review():
    fm, body = parse_frontmatter(REVIEW2_NOTE)
    result = write_frontmatter(
        fm, body,
        tags=['wedding', 'venues', 'planning', 'updated'],
        review_num=2,
        review_content='_2026-04-12_\n\n- **New venue:** Circa 1876 added',
    )
    assert 'reviewed: true' in result
    assert 'review_count: 2' in result
    assert '## Review 1' in result
    assert '## Review 2' in result


def test_scan_unreviewed():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, 'note1.md'), 'w') as f:
            f.write(SAMPLE_NOTE)
        with open(os.path.join(tmpdir, 'note2.md'), 'w') as f:
            f.write(REVIEWED_NOTE)
        with open(os.path.join(tmpdir, 'note3.md'), 'w') as f:
            f.write(REVIEW2_NOTE)

        results = scan_unreviewed(tmpdir)
        filenames = [r['filename'] for r in results]
        assert 'note1.md' in filenames
        assert 'note2.md' not in filenames
        assert 'note3.md' in filenames
        note3 = next(r for r in results if r['filename'] == 'note3.md')
        assert note3['review_count'] == 1
