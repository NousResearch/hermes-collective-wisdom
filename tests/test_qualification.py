"""Qualification thresholds and real-text refinement over PluginState."""
# pylint: disable=missing-function-docstring
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from installed_test_support import JsonState

from hermes_wisdom_plugin.local_state import QualificationState
from hermes_wisdom_plugin.qualification import (
    LifecycleBridge, record_successful_use, record_mutation,
)


def environment(tmp_path):
    skill = tmp_path / 'skills' / 'local'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text(
        '---\nname: local\ndescription: Workflow\n---\n# Workflow\nInspect the logs.\n'
    )
    state = QualificationState(JsonState(tmp_path), tmp_path)
    state.bind_owner({'account_id': 'alice', 'organization_id': 'org'})
    args = {
        'store': state, 'skills_root': skill.parent, 'authorized': lambda: True,
        'skill_resolver': lambda name: skill, 'timezone_info': (timezone.utc, 'UTC'),
    }
    return skill, state, args


def test_high_usage_still_requires_seven_business_days(tmp_path):
    _, state, args = environment(tmp_path)
    start = datetime(2026, 8, 3, 9, tzinfo=timezone.utc)
    for offset in (0, 1, 2, 3, 4, 7):
        assert record_successful_use('local', at=start + timedelta(days=offset), **args) is None
    event = record_successful_use('local', at=start + timedelta(days=8), **args)
    assert event and len(state.candidates()) == 1
    assert state.candidates()[0]['qualification'] == 'high_usage'
    record_successful_use('local', at=start + timedelta(days=8), **args)
    assert len(state.candidates()) == 1


def test_real_text_refinement_reaches_authorized_observer(tmp_path):
    skill, _, args = environment(tmp_path)
    record_successful_use('local', **args)
    old = (skill / 'SKILL.md').read_text()
    new = old + '\nVerify the fix with a regression test before releasing.\n'
    (skill / 'SKILL.md').write_text(new)
    observations = []
    record_mutation('local', ambiguous_observer=lambda row: observations.append(row) or True,
                    session_id='own-session', task_id='own-task', **args)
    assert len(observations) == 1, 'A real content edit must not be silently classified as cosmetic'
    assert observations[0]['before_text'] == old
    assert observations[0]['after_text'] == new


@pytest.mark.parametrize("admitted", [False, True])
def test_refinement_model_review_requires_admitted_native_route(tmp_path, monkeypatch, admitted):
    skill, state, args = environment(tmp_path)
    record_successful_use('local', **args)
    before = (skill / 'SKILL.md').read_text()
    after = before + '\nVerify the fix with a regression test before releasing.\n'
    bridge = LifecycleBridge(
        tmp_path, ('default', tmp_path), state=state.state, llm=object(),
        get_config=lambda key, default=None: (
            True if key == 'qualification.allow_model_review' else default
        ),
    )
    controller = SimpleNamespace(store=state, on_session_end=lambda **_facts: [])
    monkeypatch.setattr(bridge, '_controller', controller)
    monkeypatch.setattr(bridge, '_get', lambda: controller)
    transmitted = []
    monkeypatch.setattr(bridge, '_resolve_ambiguous', transmitted.append)
    runtime = SimpleNamespace(tool_invocation=lambda **_facts: object() if admitted else None)
    bridge.admit_turn(platform='slack', session_id='s', task_id='t', native_runtime=runtime)
    (skill / 'SKILL.md').write_text(after)
    record_mutation(
        'local', ambiguous_observer=bridge._queue_ambiguous,  # pylint: disable=protected-access
        session_id='s', task_id='t', **args,
    )
    bridge.on_session_end(
        platform='slack', session_id='s', task_id='t',
        completed=True, failed=False, interrupted=False,
    )
    assert len(transmitted) == int(admitted)
    if admitted:
        assert transmitted[0]['before_text'] == before
        assert transmitted[0]['after_text'] == after
