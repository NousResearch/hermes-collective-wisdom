"""Policy and Portal publisher contracts through the real notice and native producer."""
# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access
# pylint: disable=too-few-public-methods
from __future__ import annotations

import asyncio
from io import BytesIO
import json
from types import SimpleNamespace

import pytest

from installed_test_support import JsonState
from restoration_support import SlackHarness, TelegramHarness
from test_notices import Service, Page, event, notices
from test_restored_native_flow import Service as NativeService
from hermes_wisdom_plugin.client import AgentLedPolicyResponse
from hermes_wisdom_plugin.direct_services import DirectState
from hermes_wisdom_plugin.errors import WisdomAuthError, WisdomValidationError
from hermes_wisdom_plugin.publisher_names import publisher_names
from hermes_wisdom_plugin.runtime import InvocationService


class Response:
    status_code = 200
    def __init__(self, rows):
        self.headers = {}
        self.raw = BytesIO(json.dumps({'publishers': rows}).encode())
    def close(self):
        pass


class Transport:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
    def request(self, method, url, **options):
        self.calls.append((method, url, options))
        return Response(self.rows)


class Account:
    portal_base_url = 'https://portal.nousresearch.com'
    def credentials(self):
        return SimpleNamespace(access_token='private-fixture')


def policy(mode):
    payload = {
        'org_id': 'org', 'usage_evidence_window_days': 30,
        'min_aggregate_invocations': 1, 'consecutive_day_usage_counts': True,
        'repeated_edits_count': True, 'max_recommendations_per_user_per_week': 3,
        'publication_mode': 'open', 'install_popularity_threshold': 10,
        'notification_defaults': {'teammate_published': True, 'update_available': True,
                                  'skill_ready_to_share': True},
        'manager_review_email_cadence': 'daily', 'not_now_suppression_days': 7,
        'version': 1, 'updated_by_user_id': None,
    }
    if mode is not None:
        payload['recommendation_mode'] = mode
    return AgentLedPolicyResponse.model_validate(payload)


def producer(tmp_path, mode, *, muted=False, suppressed=False):
    owner = {'account_id': 'alice', 'organization_id': 'org'}
    service = Service([Page([event()], 'next', False)],
                      preferences={'muted': muted,
                                   'suppressed': {'feed:event-1': 'later'} if suppressed else {}})
    service._installation_owner = lambda: owner
    service.client = SimpleNamespace(agent_led_policy=lambda: policy(mode))
    service.notification_config = lambda: {'notifications': {
        'delivery_mode': 'agent', 'new_skills': 'immediate',
        'team_published': 'immediate', 'updates': 'immediate'}}
    service.notice_policy = lambda: InvocationService.notice_policy(service)
    emitted = []
    result = notices(tmp_path, service, [1000]).run(renderer=emitted.extend)
    return result, emitted, service


@pytest.mark.parametrize('mode,muted,suppressed,offered', [
    (None, False, False, 0), ('useful_only', False, False, 0),
    ('promote_all', False, False, 1), ('promote_all', True, False, 0),
    ('promote_all', False, True, 0),
])
def test_actual_gateway_policy_projection_and_personal_precedence(tmp_path, mode, muted,
                                                                    suppressed, offered):
    result, rows, service = producer(tmp_path, mode, muted=muted, suppressed=suppressed)
    assert result['offered'] == offered
    assert len(rows) == offered
    assert any(isinstance(call, tuple) and call[0] == 'preferences'
               for call in service.calls)
    assert DirectState(JsonState(tmp_path), tmp_path).known_publications()[0]['version'] == 2


def test_invalid_gateway_mode_and_owner_change_fail_quiet(tmp_path):
    with pytest.raises(ValueError):
        policy('surprise')
    result, rows, service = producer(tmp_path / 'invalid', 'promote_all')
    service.pages = [Page([event('unknown')], 'next', False)]
    service.client.agent_led_policy = lambda: SimpleNamespace(
        org_id='org', recommendation_mode='surprise',
        notification_defaults=SimpleNamespace(teammate_published=True,
                                              update_available=True, skill_ready_to_share=True))
    unknown = notices(tmp_path / 'invalid', service, [1001]).run(renderer=rows.extend)
    assert unknown['offered'] == 0 and len(rows) == 1
    result, rows, service = producer(tmp_path, 'promote_all')
    assert result['offered'] == 1 and rows
    state = DirectState(JsonState(tmp_path), tmp_path)
    state.set_identity({'account_id': 'bob', 'organization_id': 'other'})
    assert not state.known_publications()
    assert state.notice_state()['offered'] == {}
    service.pages = [Page([event('second')], 'later', False)]
    service._installation_owner = lambda: {'account_id': 'bob', 'organization_id': 'other'}
    next_result = notices(tmp_path, service, [1001]).run(renderer=lambda _: None)
    assert next_result['offered'] == 0 and next_result['error'] == 'WisdomAuthError'


def test_portal_batch_exact_ids_nullable_name_and_no_token_redirect():
    rows = [
        {'skill_id': 'skill-1', 'version': 2, 'published_by_user_id': 'publisher-1',
         'publisher_display_name': ' Alice ', 'team_display_name': 'Team'},
        {'skill_id': 'skill-2', 'version': 3, 'published_by_user_id': 'publisher-2',
         'publisher_display_name': None, 'team_display_name': 'Team'},
    ]
    transport = Transport(rows)
    refs = [{'skill_id': 'skill-1', 'version': 2}, {'skill_id': 'skill-2', 'version': 3}]
    names = publisher_names(Account(), refs, session=transport)
    assert names[('skill-1', 2)]['publisher_display_name'] == 'Alice'
    assert names[('skill-2', 3)]['publisher_display_name'] is None
    assert len(transport.calls) == 1
    method, url, options = transport.calls[0]
    assert method == 'POST' and url == 'https://portal.nousresearch.com/api/v1/wisdom/publishers'
    assert options['json'] == {'references': refs} and options['allow_redirects'] is False
    assert options['headers']['Authorization'] == 'Bearer private-fixture'
    assert 'private-fixture' not in url
    transport.rows = [dict(rows[0], version=4), rows[1]]
    assert not publisher_names(Account(), refs, session=transport)
    with pytest.raises(WisdomValidationError):
        publisher_names(Account(), [{'skill_id': 'skill-1', 'version': True}], session=transport)
    with pytest.raises(WisdomValidationError):
        publisher_names(Account(), refs * 5, session=transport)
    assert len(transport.calls) == 2
    bad = Account()
    bad.portal_base_url = 'https://portal.nousresearch.com.evil/path'
    with pytest.raises(WisdomAuthError):
        publisher_names(bad, refs, session=transport)


def test_publisher_failure_is_nullable_but_owner_switch_does_not_advance(tmp_path):
    service = Service([Page([event()], 'next', False)])
    service.notice_publishers = lambda _refs: {}
    rows = []
    assert notices(tmp_path, service, [1000]).run(renderer=rows.extend)['offered'] == 1
    assert 'publisher_display_name' not in rows[0]['publisher_fact']

    other = tmp_path / 'switch'
    switched = Service([Page([event()], 'next', False)])
    def owner_changed(_refs):
        raise WisdomAuthError('owner changed')
    switched.notice_publishers = owner_changed
    result = notices(other, switched, [1000]).run(renderer=lambda _: None)
    assert result['offered'] == 0 and result['error'] == 'WisdomAuthError'
    assert DirectState(JsonState(other), other).notice_state()['cursor'] is None


def test_real_publisher_service_fences_owner_after_portal_response():
    owner = [{'account_id': 'alice', 'organization_id': 'org'}]
    class SwitchingTransport(Transport):
        def request(self, method, url, **options):
            response = super().request(method, url, **options)
            owner[0] = {'account_id': 'bob', 'organization_id': 'org'}
            return response
    transport = SwitchingTransport([{
        'skill_id': 'skill-1', 'version': 2, 'published_by_user_id': 'publisher-1',
        'publisher_display_name': 'Alice', 'team_display_name': 'Team'}])
    service = InvocationService.__new__(InvocationService)
    service._installation_owner = lambda: owner[0]
    service.account = Account()
    service.client = SimpleNamespace(session=transport)
    with pytest.raises(WisdomAuthError, match='owner changed'):
        service.notice_publishers([{'skill_id': 'skill-1', 'version': 2}])
    assert len(transport.calls) == 1


@pytest.mark.parametrize('surface', [SlackHarness, TelegramHarness])
def test_batched_publisher_projection_reaches_native_card_without_id_fallback(tmp_path, surface):
    class Catalog(NativeService):
        def install_plan(self, reference, **kwargs):
            return {**super().install_plan(reference, **kwargs),
                    'published_by_user_id': 'publisher-1'}
    notice = Service([Page([event()], 'next', False)])
    notice.notice_publishers = lambda refs: {
        (refs[0]['skill_id'], refs[0]['version']): {
            'published_by_user_id': 'publisher-1',
            'publisher_display_name': 'Alice', 'team_display_name': 'Team'}}
    rows = []
    assert notices(tmp_path, notice, [1000]).run(renderer=rows.extend)['offered'] == 1
    async def scenario():
        ui = surface(tmp_path, Catalog())
        await ui.runtime._send_notice(ui.caller, rows)
        await ui.flush()
        assert 'Alice' in ui.posts[-1]['text']
        assert 'Team' in ui.posts[-1]['text']
        assert 'publisher-1' not in ui.posts[-1]['text']
        ui.runtime.close()
    asyncio.run(scenario())
