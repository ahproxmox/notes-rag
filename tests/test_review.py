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


def test_write_frontmatter_preserves_passthrough_fields():
    note = """---
date_created: 2026-04-10
reviewed: unreviewed
tags: []
source: discord
---
# Test note
Some content
"""
    fm, body = parse_frontmatter(note)
    result = write_frontmatter(fm, body, tags=["test"], review_num=1, review_content="_2026-04-11_")
    assert "source: discord" in result


def test_parse_frontmatter_with_hr_in_body():
    note = """---
date_created: 2026-04-10
reviewed: unreviewed
tags: []
---
# Note with divider

Some content

---

More content
"""
    fm, body = parse_frontmatter(note)
    assert fm.get("reviewed") == "unreviewed"
    assert "---" in body
    assert "More content" in body


from review import SessionManager, group_notes


def test_session_create_single_note():
    mgr = SessionManager()
    session = mgr.create(['note1.md'], notes_data=[{
        'filename': 'note1.md',
        'path': '/tmp/note1.md',
        'body': '# Venues\n1) Poets Lane',
        'review_count': 0,
        'previous_reviews': [],
    }])
    assert session['session_id'] is not None
    assert len(session['notes']) == 1
    assert session['notes'][0]['filename'] == 'note1.md'


def test_session_create_grouped_notes():
    mgr = SessionManager()
    session = mgr.create(['note1.md', 'note2.md'], notes_data=[
        {'filename': 'note1.md', 'path': '/tmp/note1.md',
         'body': '# Venues\n1) Poets Lane', 'review_count': 0, 'previous_reviews': []},
        {'filename': 'note2.md', 'path': '/tmp/note2.md',
         'body': '# Budget\nVenue budget: $5000', 'review_count': 0, 'previous_reviews': []},
    ])
    assert len(session["notes"]) == 2


def test_session_add_qa():
    mgr = SessionManager()
    session = mgr.create(['note1.md'], notes_data=[{
        'filename': 'note1.md', 'path': '/tmp/note1.md',
        'body': '# Venues', 'review_count': 0, 'previous_reviews': [],
    }])
    sid = session['session_id']
    mgr.add_qa(sid, 'What prompted this list?', 'We started wedding planning last month')
    state = mgr.get(sid)
    assert len(state['qa']) == 1
    assert state['qa'][0]['q'] == 'What prompted this list?'
    assert state['qa'][0]['a'] == 'We started wedding planning last month'


def test_session_get_nonexistent():
    mgr = SessionManager()
    assert mgr.get('nonexistent') is None


def test_session_remove():
    mgr = SessionManager()
    session = mgr.create(['note1.md'], notes_data=[{
        'filename': 'note1.md', 'path': '/tmp/note1.md',
        'body': '# Venues', 'review_count': 0, 'previous_reviews': [],
    }])
    sid = session['session_id']
    mgr.remove(sid)
    assert mgr.get(sid) is None


def test_group_notes_by_similarity():
    notes = [
        {'filename': 'venues.md', 'preview': 'Poets Lane, Lyrebird Falls'},
        {'filename': 'budget.md', 'preview': 'Venue budget $5000'},
        {'filename': 'recipe.md', 'preview': 'Focaccia recipe with rosemary'},
    ]
    similarity = {
        ('venues.md', 'budget.md'): 0.75,
        ('venues.md', 'recipe.md'): 0.1,
        ('budget.md', 'recipe.md'): 0.05,
    }
    groups = group_notes(notes, similarity, threshold=0.4)
    group_files = [set(g['filenames']) for g in groups]
    assert {'venues.md', 'budget.md'} in group_files
    assert {'recipe.md'} in group_files
