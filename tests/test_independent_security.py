"""Independent PR21 review probes. Assertions describe required safe behavior."""
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
@pytest.mark.parametrize('entry_action', ['review', 'share'])
def test_share_action_cannot_install_without_file_inspection(
    invocation_executor, tmp_path, surface, entry_action,
):
    executor, _store, client, _receipts, _plan, target, _authority = invocation_executor
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
        # 'review' is the positive control; 'share' is not offered on this card.
        await ui.click(identity, entry_action)
        while await ui.next_page(identity):
            pass
        await ui.click(identity, 'confirm')
        print('INSPECTION_GATE', surface.__name__, entry_action, {
              'inspection_calls': len(inspections),
              'package_files_present': 'package_files' in ui.record(identity),
              'state': ui.record(identity)['state'], 'installed': target.exists(),
              'remote_install_records': len(client.record_calls)})
        if entry_action == 'review':
            assert target.exists() and len(inspections) == 1
        else:
            assert not target.exists(), 'Installed exact receipt without ever disclosing file bytes'
    asyncio.run(scenario())


def test_cancel_during_candidate_preparation_revokes_review(tmp_path):
    started, release = threading.Event(), threading.Event()
    service = Service()
    def slow_prepare(_identity):
        started.set()
        assert release.wait(10)
        return 'local:draft-1'
    service.native_candidate_prepare = slow_prepare

    async def scenario():
        ui = SlackHarness(tmp_path, service)
        identity = ui.runtime.begin_candidate(ui.caller, 'candidate-1')['review_id']
        await ui.flush()
        preparing = asyncio.create_task(ui.click(identity, 'share'))
        assert await asyncio.to_thread(started.wait, 5)
        await ui.click(identity, 'cancel')
        during = ui.record(identity)['state']
        release.set()
        await preparing
        print('CANCEL_DURING_PREPARE', {'during': during, 'after': ui.record(identity)['state'],
              'operation': ui.record(identity)['operation'],
              'controls': [a.label for a in ui.view(identity).actions]})
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
        print('SETTINGS_WITHOUT_DISPLAY', {
            'changes': changes, 'state': ui.record(identity)['state'],
        })
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
        print('TRANSIENT_FAILURE', {'state': ui.record(identity)['state'], 'edits': len(ui.edits)})
        assert ui.record(identity)['state'] != 'preparing', (
            'Review permanently stuck; all subsequent actions rejected'
        )
    asyncio.run(scenario())
