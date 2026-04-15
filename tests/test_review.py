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


from review import build_interview_prompt, build_review_content, _detect_note_intent


def test_detect_intent_decision():
    intent = _detect_note_intent('architecture.md', 'Decided to go with PostgreSQL vs MySQL. Pros: better JSON support.')
    assert intent == 'decision'


def test_detect_intent_idea():
    intent = _detect_note_intent('idea.md', 'What if we explored using a vector DB for search? Could be interesting.')
    assert intent == 'idea'


def test_detect_intent_task():
    intent = _detect_note_intent('task.md', 'Need to deploy the new service. Set up monitoring and install dependencies.')
    assert intent == 'task'


def test_detect_intent_list():
    intent = _detect_note_intent('shopping.md', '# Shopping\n- Milk\n- Eggs\n- Bread\n- Butter\n- Coffee')
    assert intent == 'list'


def test_build_prompt_single_note_contains_content():
    prompt = build_interview_prompt(
        notes=[{'filename': 'venues.md', 'body': '# Venues\n1) Poets Lane\n2) Lyrebird Falls'}],
        rag_context='Related: engagement ring planning notes from March',
        previous_reviews=[],
        question_count=0,
    )
    assert 'Poets Lane' in prompt
    assert 'Lyrebird Falls' in prompt
    assert 'engagement ring' in prompt


def test_build_prompt_grouped_notes():
    prompt = build_interview_prompt(
        notes=[
            {'filename': 'venues.md', 'body': '# Venues\n1) Poets Lane'},
            {'filename': 'budget.md', 'body': '# Budget\nVenue budget: $5000'},
        ],
        rag_context='',
        previous_reviews=[],
        question_count=0,
    )
    assert 'venues.md' in prompt
    assert 'budget.md' in prompt
    assert 'related notes together' in prompt.lower() or 'reviewing' in prompt.lower()


def test_build_prompt_avoids_previous_questions():
    prompt = build_interview_prompt(
        notes=[{'filename': 'venues.md', 'body': '# Venues\n1) Poets Lane'}],
        rag_context='',
        previous_reviews=[
            {'q': 'What prompted this list?', 'a': 'Started wedding planning'},
        ],
        question_count=0,
    )
    assert 'What prompted this list?' in prompt


def test_build_prompt_sparse_note_hint():
    prompt = build_interview_prompt(
        notes=[{'filename': 'idea.md', 'body': 'Solar panels'}],
        rag_context='',
        previous_reviews=[],
        question_count=0,
    )
    assert 'sparse' in prompt.lower() or 'expand' in prompt.lower()


def test_build_prompt_list_note_hint():
    prompt = build_interview_prompt(
        notes=[{'filename': 'list.md', 'body': '# Shopping\n- Milk\n- Eggs\n- Bread\n- Butter'}],
        rag_context='',
        previous_reviews=[],
        question_count=0,
    )
    assert 'list' in prompt.lower()


def test_build_prompt_includes_intent_guidance():
    prompt = build_interview_prompt(
        notes=[{'filename': 'decision-db.md', 'body': 'Decided to go with Postgres vs MySQL. Pros: JSON support.'}],
        rag_context='',
        previous_reviews=[],
        question_count=0,
    )
    assert 'DECISION' in prompt or 'decision' in prompt.lower()


def test_build_prompt_includes_rag_bridging_guidance():
    prompt = build_interview_prompt(
        notes=[{'filename': 'venues.md', 'body': '# Venues\n1) Poets Lane'}],
        rag_context='[budget.md] Venue budget is $5000',
        previous_reviews=[],
        question_count=0,
    )
    assert 'budget.md' in prompt
    assert 'extends' in prompt.lower() or 'supersedes' in prompt.lower() or 'relates' in prompt.lower()


def test_build_prompt_question_progression():
    # Q1 strategy should reference motivation/why
    prompt_q1 = build_interview_prompt(
        notes=[{'filename': 'note.md', 'body': 'Some content here'}],
        rag_context='', previous_reviews=[], question_count=0,
    )
    assert 'Q1' in prompt_q1

    # Q3 strategy should reference gaps/missing
    prompt_q3 = build_interview_prompt(
        notes=[{'filename': 'note.md', 'body': 'Some content here'}],
        rag_context='', previous_reviews=[], question_count=2,
    )
    assert 'Q3' in prompt_q3


def test_build_review_content_with_qa():
    qa = [
        {'q': 'What prompted this?', 'a': 'Wedding planning'},
        {'q': 'Any timeline?', 'a': 'Visiting next month'},
    ]
    content = build_review_content(qa)
    assert 'What prompted this?' in content
    assert 'Wedding planning' in content
    assert 'Any timeline?' in content
    assert 'Visiting next month' in content


def test_build_review_content_empty():
    content = build_review_content([])
    # Should still return something (just the date line)
    assert len(content) > 0
