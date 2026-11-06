"""The pending share-suggestion card's layout on every surface.

Question line above the controls, Not now then Share (primary, rightmost) in one right-aligned
row, and "Don't ask again for this skill" as a quiet link below, whole on every surface.
"""
# pylint: disable=missing-function-docstring
import html
import re

from hermes_wisdom_plugin.auto_suggest import _decision_label
from hermes_wisdom_plugin.auto_suggest_card import (
    DISMISS, NEVER, QUESTION, SHARE, auto_suggest_view, card_plan,
)
from hermes_wisdom_plugin.native_review import (
    MAX_ACTION_LABEL, NativeAction, NativeView, action_label, callback_value, control_label,
)
from hermes_wisdom_plugin.native_slack import blocks
from hermes_wisdom_plugin.native_telegram import (
    keyboard, plain_html, rich_html, rich_html_is_lossless,
)

LINK = "Don't ask again for this skill"


def _pending():
    return auto_suggest_view({"state": "pending", "plan": card_plan({
        "id": "s-1", "skill": "incident", "description": "Review incident notes.",
        "reason": "Teams handle incidents often.", "content_hash": "sha-1"})})


def test_pending_view_asks_the_question_with_the_new_controls():
    view = _pending()
    assert view.prompt == QUESTION == "Share it with your team?"
    assert view.notice == ""
    assert "Nothing is shared" not in view.to_text()
    assert view.to_text().rstrip().endswith(QUESTION)
    assert [(a.label, a.action, a.primary, a.link, a.row, a.align) for a in view.actions] == [
        ("Not now", DISMISS, False, False, 0, "right"),
        ("Share", SHARE, True, False, 0, "right"),
        (LINK, NEVER, False, True, 1, "right"),
    ]


def test_link_labels_render_whole_and_other_labels_stay_capped():
    assert len(LINK) > MAX_ACTION_LABEL
    assert action_label(LINK) != LINK
    assert control_label(NativeAction(LINK, NEVER, link=True)) == LINK
    assert control_label(NativeAction(LINK, NEVER)) == action_label(LINK)


def test_telegram_rich_card_right_aligns_share_and_shows_the_link():
    rendered = rich_html(_pending(), "r1")
    rows = re.findall(r'<tg-button-row align="(\w+)">(.*?)</tg-button-row>', rendered)
    assert [align for align, _ in rows] == ["right", "right"]
    share = html.escape(callback_value("r1", SHARE), quote=True)
    dismiss = html.escape(callback_value("r1", DISMISS), quote=True)
    never = html.escape(callback_value("r1", NEVER), quote=True)
    assert rows[0][1] == (
        f'<tg-button type="callback_data" data="{dismiss}">Not now</tg-button> '
        f'<tg-button type="callback_data" style="primary" data="{share}">Share</tg-button>')
    assert rows[1][1] == (f'<tg-button type="callback_data" style="link" data="{never}">'
                          f"{html.escape(LINK)}</tg-button>")
    assert f"<p>{QUESTION}</p>" in rendered and "<i>" not in rendered
    assert rendered.index(QUESTION) < rendered.index("<tg-button-row")
    assert rich_html_is_lossless(_pending(), "r1")


def test_telegram_plain_fallback_keeps_every_callback_and_the_question():
    rows = keyboard(_pending(), "r1").inline_keyboard
    assert [[(button.text, button.callback_data) for button in row] for row in rows] == [
        [("Not now", callback_value("r1", DISMISS)), ("Share", callback_value("r1", SHARE))],
        [(LINK, callback_value("r1", NEVER))],
    ]
    assert QUESTION in plain_html(_pending())


def test_slack_card_puts_the_question_above_share_and_the_link_on_its_own_row():
    rendered = blocks(_pending(), "r1")
    sections = [block["text"]["text"] for block in rendered
                if block["type"] == "section" and "text" in block]
    assert sections[-1] == QUESTION
    actions = [block for block in rendered if block["type"] == "actions"]
    assert [[(b["text"]["text"], b.get("style")) for b in block["elements"]]
            for block in actions] == [[("Not now", None), ("Share", "primary")], [(LINK, None)]]
    assert len({block["block_id"] for block in actions}) == 2
    assert rendered.index(actions[0]) == rendered.index(
        next(block for block in rendered if block.get("text", {}).get("text") == QUESTION)) + 1


def test_cards_without_links_or_alignment_render_as_before():
    view = NativeView("Title", "Summary", actions=[
        NativeAction("Share", "confirm", primary=True), NativeAction("Cancel", "cancel")])
    rendered = rich_html(view, "r1")
    assert rendered.count('<tg-button-row align="left">') == 1 and 'style="link"' not in rendered
    kinds = [block["type"] for block in blocks(view, "r1")]
    assert kinds == ["header", "section", "divider", "actions"]


def test_settled_copy_names_the_new_labels():
    assert [_decision_label({"event": "choice", "choice": choice})
            for choice in ("share", "dismiss", "never")] == [
        "you chose Share", "you chose Not now", f"you chose {LINK}"]
