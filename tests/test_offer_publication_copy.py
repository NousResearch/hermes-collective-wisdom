"""Exact Gateway metadata and publication results through native presentation."""
# pylint: disable=missing-function-docstring,line-too-long
from __future__ import annotations

import pytest

from test_install_review import Client, _reviewer
from review_test_support import _content
from hermes_wisdom_plugin.commands import _render_publication_outcome
from hermes_wisdom_plugin.native_review import (
    _inspection_pages, _suggest_terminal_view, project_view,
)
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.native_telegram import plain_html, keyboard
from hermes_wisdom_plugin.errors import WisdomValidationError


@pytest.mark.parametrize('version,updating', [(1, False), (2, True)])
def test_exact_version_metadata_reaches_both_native_cards(tmp_path, version, updating):
    client = Client()
    client.skill_value['created_by_user_id'] = 'original-creator'
    client.version_value.update({
        'author_description': 'Checks deployment rollback steps.',
        'verified_facts': {'schema_version': 1, 'provenance': 'plane_derived', 'scan_verdict': 'pass'},
        'published_by_user_id': 'version-publisher-42',
        'explanation': 'A repeatable rollback checklist.',
    })
    client.files[0] = ('SKILL.md', 'file', b'''---
name: managed-skill
description: Original author description.
metadata:
  hermes:
    editorial_name: Rollback Guide
    editorial_description: Checks deployment rollback steps.
    coworker_benefit: Helps colleagues restore service safely.
---
# Guide
''')
    envelope = _content(client.files)
    client.version_value.update(content_hash=envelope.content_hash, commit=envelope.commit)
    reviewer = _reviewer(tmp_path, client=client)
    plan = reviewer.plan(f'skill-1@v{version}')
    assert plan['published_by_user_id'] == 'version-publisher-42'
    record = {'operation': 'update' if updating else 'install', 'state': 'pending', 'plan': plan}
    view = project_view(record)
    rendered = str(blocks(view, 'review-id')) + plain_html(view) + str(keyboard(view, 'review-id'))
    for text in ('Rollback Guide', 'Checks deployment rollback steps.', 'Helps colleagues restore service safely.'):
        assert text in rendered
    assert 'original-creator' not in rendered and 'version-publisher-42' not in rendered
    assert 'Publisher name unavailable' in rendered
    assert [action.label for action in view.actions] == [
        'View Details', 'Not Now', 'Mute Skill Recommendations',
        'Update Skill' if updating else 'Install Skill',
    ]
    assert 'version-publisher-42' in str(plan)
    assert plan['version'] == version
    assert plan['content_hash'] == envelope.content_hash
    assert plan['editorial_name'] == 'Rollback Guide'
    assert reviewer.verify(plan).plan['editorial_name'] == 'Rollback Guide'
    assert 'version-publisher-42' not in ''.join(_inspection_pages(record))


def test_missing_version_publisher_is_explicit_not_creator(tmp_path):
    client = Client()
    client.skill_value['created_by_user_id'] = 'original-creator'
    plan = _reviewer(tmp_path, client=client).plan('skill-1@v1')
    view = project_view({'operation': 'install', 'state': 'pending', 'plan': plan})
    rendered = str(blocks(view, 'id')) + plain_html(view)
    assert 'Publisher name unavailable' in rendered
    assert 'original-creator' not in rendered


def test_changed_exact_version_publisher_invalidates_review(tmp_path):
    client = Client()
    client.version_value['published_by_user_id'] = 'publisher-a'
    reviewer = _reviewer(tmp_path, client=client)
    plan = reviewer.plan('skill-1@v1')
    client.version_value['published_by_user_id'] = 'publisher-b'
    with pytest.raises(WisdomValidationError, match='Install review changed'):
        reviewer.verify(plan)


def test_changed_package_editorial_copy_invalidates_review(tmp_path):
    client = Client()
    client.files[0] = ('SKILL.md', 'file', b'---\nmetadata:\n  hermes:\n    editorial_name: First\n---\n')
    envelope = _content(client.files)
    client.version_value.update(content_hash=envelope.content_hash, commit=envelope.commit)
    reviewer = _reviewer(tmp_path, client=client)
    plan = reviewer.plan('skill-1@v1')
    client.files[0] = ('SKILL.md', 'file', client.files[0][2].replace(b'First', b'Second'))
    with pytest.raises(WisdomValidationError, match='exact package content'):
        reviewer.verify(plan)


def test_canonical_frontmatter_name_survives_missing_or_invalid_editorial_copy(tmp_path):
    client = Client()
    client.files[0] = ('SKILL.md', 'file', b'''---
name: technical-rollback
description: Author's original description.
metadata:
  hermes:
    editorial_name: [not, a, title]
---
''')
    envelope = _content(client.files)
    client.version_value.update(content_hash=envelope.content_hash, commit=envelope.commit)
    reviewer = _reviewer(tmp_path, client=client)
    plan = reviewer.plan('skill-1@v1')
    assert plan['editorial_name'] == 'technical-rollback'
    assert plan['editorial_description'] == "Author's original description."
    assert plan['coworker_benefit'] is None
    assert reviewer.verify(plan).plan == plan
    view = project_view({'operation': 'install', 'state': 'pending', 'plan': plan})
    assert 'technical-rollback' in plain_html(view)
    assert "Author&#x27;s original description." in plain_html(view)


@pytest.mark.parametrize('result,heading,fragment', [
    ({'state': 'done', 'publication_state': 'published'}, 'Published', 'available to your team'),
    ({'state': 'done', 'publication_state': 'pending_moderation'}, 'Waiting for team review', 'not available'),
    ({'state': 'done', 'publication_state': 'changes_requested'}, 'Changes requested', 'reviewer'),
    ({'state': 'done', 'publication_state': 'declined'}, 'Rejected', 'reviewer'),
    ({'state': 'rejected', 'detail': 'Gateway rejected the publication draft before creation (HTTP 422).'}, 'Failed', 'draft'),
    ({'state': 'uncertain', 'phase': 'publication_unknown'}, 'Uncertain', 'do not'),
    ({'state': 'done', 'publication_state': 'published', 'phase': 'publication_unknown'}, 'Uncertain', 'do not'),
    ({'state': 'uncertain', 'publication_state': 'published'}, 'Uncertain', 'do not'),
    ({'state': 'rejected', 'phase': 'approval_rejected'}, 'Failed', 'stored scan'),
])
def test_publication_outcomes_are_distinct_in_native_and_cli(result, heading, fragment):
    native = _suggest_terminal_view({'skill_name': 'Example'}, 'completed' if result['state'] == 'done' else result['state'], result)
    native_text = str(blocks(native, 'id')) + plain_html(native)
    cli_text = _render_publication_outcome({'operation_id': 'op-1', **result})
    assert heading in native_text and heading in cli_text
    assert fragment.lower() in (native_text + cli_text).lower()


@pytest.mark.parametrize('result', [
    {'state': 'uncertain', 'publication_state': 'published', 'detail': 'stale success'},
    {'state': 'done', 'phase': 'publication_unknown',
     'publication_state': 'published', 'detail': 'stale success'},
])
def test_native_completed_record_does_not_promote_stale_publication_state(result):
    view = project_view({
        'operation': 'publish', 'state': 'completed',
        'plan': {'slug': 'example'}, 'result': result,
    })
    assert 'Published\n' not in view.to_text()
    assert 'Available to your team.' not in view.to_text()
