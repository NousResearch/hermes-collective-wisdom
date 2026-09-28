"""Native review security regressions through registered callbacks."""
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=protected-access,redefined-outer-name,unused-import
import asyncio
import threading
import pytest

from restoration_support import SlackHarness, TelegramHarness
from test_restored_native_flow import Service
from test_install_execution import invocation_executor
from hermes_wisdom_plugin.runtime import InvocationService


@pytest.mark.parametrize('surface', [SlackHarness, TelegramHarness])
@pytest.mark.parametrize('forged_action', ['review', 'share', 'confirm'])
def test_forged_actions_cannot_install_and_real_install_still_inspects(
    invocation_executor, tmp_path, surface, forged_action,
):
    executor, _store, _client, _receipts, _plan, target, _authority = invocation_executor
    inspections = []

    class RealService:
        def native_owner(self):
            return executor._owner()
        def close(self):
            pass
        def check_owner(self):
            return executor._owner()
        def install_plan(self, reference, **kwargs):
            return executor.reviewer.plan(reference, **kwargs)
        def native_install_inspect(self, receipt):
            inspections.append(receipt)
            return InvocationService._native_package_inspect(
                self, executor.reviewer, receipt, 'Install',
            )
        def install_apply(self, receipt, **kwargs):
            return executor.apply(receipt, **kwargs)

    async def scenario():
        ui = surface(tmp_path / 'native', RealService())
        identity = ui.runtime.begin(ui.caller, 'skill-1@v2')['review_id']
        await ui.flush()
        assert ui.record(identity)['plan']['files']
        await ui.click(identity, forged_action)
        assert not target.exists()
        assert not inspections
        await ui.click(identity, 'install')
        assert target.exists() and len(inspections) == 1
    asyncio.run(scenario())


def test_cancel_during_candidate_preparation_revokes_review(tmp_path):
    started, release = threading.Event(), threading.Event()
    service = Service()
    def slow_prepare(identity):
        del identity
        started.set()
        assert release.wait(10)
        return 'local:draft-1'
    service.native_candidate_prepare = lambda identity: (_ for _ in ()).throw(
        ConnectionError('first local preparation unavailable')
    )

    async def scenario():
        ui = SlackHarness(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, 'candidate-1')['review_id']
        await ui.flush()
        service.native_candidate_prepare = slow_prepare
        preparing = asyncio.create_task(ui.click(identity, 'share'))
        assert await asyncio.to_thread(started.wait, 5)
        await ui.click(identity, 'cancel')

        release.set()
        await preparing

        assert ui.record(identity)['state'] == 'cancelled', (
            'Maybe Later was ignored during preparation'
        )
    asyncio.run(scenario())


def test_settings_effect_waits_for_display_ack(tmp_path):
    service = Service()
    changes = []
    service.notification_settings = lambda **kw: {
        'delivery_mode': 'agent', 'mode_writable': True, 'mute_control': {'revision': 7},
    }
    def mute(revision, duration, *, may_apply):
        assert may_apply()
        changes.append((revision, duration))
        return {'muted': True}
    service.choose_notification_mute = mute

    async def scenario():
        ui = SlackHarness(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, 'candidate-1')['review_id']
        await ui.flush()
        started, release = asyncio.Event(), asyncio.Event()
        original = ui.chat_update
        async def blocked_display(**kwargs):
            if (kwargs['text'].startswith('Collective Wisdom settings')
                    and 'State: completed' not in kwargs['text']):
                started.set()
                await release.wait()
                raise ConnectionError('settings card not delivered')
            return await original(**kwargs)
        ui.chat_update = blocked_display
        pending = asyncio.create_task(ui.click(identity, 'settings'))
        await asyncio.wait_for(started.wait(), 5)
        await ui.click(identity, 'settings.mute.1w')
        release.set()
        await pending

        assert not changes, 'Settings effect accepted before failed settings disclosure'
    asyncio.run(scenario())


def test_transient_candidate_failure_leaves_retry_or_cancel(tmp_path):
    service = Service()
    service.publication_review = lambda _identity: (_ for _ in ()).throw(
        ConnectionError('temporary failure')
    )
    async def scenario():
        ui = SlackHarness(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, 'candidate-1')['review_id']
        await ui.flush()
        await ui.click(identity, 'share')
        await ui.click(identity, 'cancel')

        assert ui.record(identity)['state'] != 'preparing', (
            'Review permanently stuck; all subsequent actions rejected'
        )
    asyncio.run(scenario())
