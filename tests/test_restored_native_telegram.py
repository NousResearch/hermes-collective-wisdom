"""Offline Telegram SDK callbacks cover restored inspection and quiet dismissal."""
# pylint: disable=missing-function-docstring
import asyncio
import html
import json

from restoration_support import TelegramHarness
from test_restored_native_flow import Service


def test_telegram_complete_inspection_then_quiet_dismissal(tmp_path, monkeypatch):
    async def scenario():
        service = Service()
        plan = service.publication_review("draft")
        plan["files"][0]["content_utf8"] = "first\n" + "<&" * 1500 + "\nlast"
        monkeypatch.setattr(service, "publication_review", lambda _draft: plan)
        ui = TelegramHarness(tmp_path, service)
        response = ui.runtime.begin_publication(ui.caller, "draft")
        await ui.flush()
        identity = response["review_id"]
        assert "Share My Skill" in str(ui.posts[-1]["reply_markup"])
        await ui.click(identity, "details", actor_id=22)
        assert not ui.edits
        await ui.click(identity, "details")
        chunks = []
        while True:
            payload = ui.edits[-1]
            assert len(payload["text"]) < 4000
            decoded = html.unescape(payload["text"])
            chunks.append(decoded.split("Exact review data (JSON)\n", 1)[1])
            assert "confirm:" not in str(payload["reply_markup"])
            if not await ui.next_page(identity):
                break
        assert json.loads("".join(chunks)) == plan
        await ui.click(identity, "back")
        before = ui.edits[-1]["text"]
        assert not service.applied
        await ui.click(identity, "cancel")
        assert ui.edits[-1]["text"] == before
        assert ui.edits[-1]["reply_markup"] is None
        assert len(ui.posts) == 1
        await ui.click(identity, "confirm")
        assert not service.applied
    asyncio.run(scenario())
