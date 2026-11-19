"""Existing presentation retains exact uninstall receipt across approval."""
# pylint: disable=missing-function-docstring,protected-access,import-outside-toplevel
from types import SimpleNamespace

from hermes_wisdom_plugin.desktop_api import DesktopWorkflowApi


def test_desktop_removal_retains_receipt_before_confirmation(tmp_path):
    events = []
    row = {"skill_id": "skill-1", "slug": "demo", "version": 1}
    service = SimpleNamespace(
        profile=("default", tmp_path), native_owner=lambda: {"owner": "one"},
        list_installations=lambda: [row], catalog_available=lambda: True,
        catalog_mutations=object(),
        uninstall_plan=lambda skill: events.append(("review", skill)) or {"receipt": "receipt-1"},
        uninstall_apply=lambda receipt, **_kwargs: events.append(("apply",
            receipt)) or {"uninstalled": True},
        uninstall=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("raw writer")),
    )
    api = DesktopWorkflowApi()
    view = api.open_view(service)["view_id"]
    review = api.uninstall_review(service, view, "skill-1")
    assert events == [("review", "skill-1")]
    api.uninstall_confirm(service, view, review["review_id"])
    assert events[-1] == ("apply", "receipt-1")


def test_native_removal_retains_exact_receipt_before_card():
    from hermes_wisdom_plugin.native_review import NativeReviewController, _apply_review_action

    events = []
    service = SimpleNamespace(
        catalog_available=lambda: True, catalog_mutations=object(),
        resolve_managed_skill=lambda ref: ref,
        list_installations=lambda: [{"skill_id": "skill-1", "slug": "demo", "version": 1,
                                     "state": "active"}],
        uninstall_plan=lambda _skill: events.append("review") or {"receipt": "native-receipt"},
        uninstall_apply=lambda receipt, **_k: events.append(receipt),
        uninstall=lambda *_a, **_k: events.append("raw writer"),
    )
    controller = object.__new__(NativeReviewController)
    controller._insert = lambda _s, _i, _a, plan: plan
    plan = controller.request_uninstall(service, None, "skill-1")
    assert events == ["review"]
    _apply_review_action("confirm", "uninstall", plan, service, lambda: True)
    assert events == ["review", "native-receipt"]


def test_cli_removal_reviews_before_prompt(monkeypatch):
    from hermes_wisdom_plugin.commands import _uninstall_cli
    from hermes_wisdom_plugin import commands

    events = []
    service = SimpleNamespace(
        catalog_available=lambda: True, catalog_mutations=object(),
        resolve_managed_skill=lambda name: name,
        uninstall_plan=lambda skill: events.append("review") or {"receipt": skill + "-receipt"},
        uninstall_apply=events.append,
        uninstall=lambda *_a: events.append("raw writer"),
    )
    monkeypatch.setattr(commands.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt: events.append("prompt") or "yes")
    _uninstall_cli(SimpleNamespace(skill_id="skill-1", yes=False, json=False), service)
    assert events == ["review", "prompt", "skill-1-receipt"]
