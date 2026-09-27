"""Public contribution service wrappers preserve real kernel authority fences."""
# pylint: disable=protected-access

from pathlib import Path

import pytest
from installed_test_support import JsonState
from test_contribution_review import RecordingLlm, reviewer
from test_publication import publication

from hermes_wisdom_plugin.errors import WisdomAuthError
from hermes_wisdom_plugin.runtime import InvocationService


@pytest.mark.parametrize("operation", ["save", "model_review"])
def test_service_rechecks_authority_inside_contribution_kernel(tmp_path, monkeypatch, operation):
    """Revocation in the kernel's owner read stops draft replacement/model disclosure."""
    llm = RecordingLlm()
    kernel, _source, owner = reviewer(tmp_path, llm=llm)
    prepared = kernel.prepare("reviewed-skill")
    prior_calls = list(llm.calls)
    before = kernel.store.draft(prepared["draft_id"])
    service = InvocationService(
        kernel.store.root, ("default", Path(owner["profile_home"])),
        state=JsonState(kernel.store.root),
    )
    monkeypatch.setattr(service, "_installation_owner", lambda: dict(owner))
    monkeypatch.setattr(service, "_contribution_reviewer", lambda: kernel)
    monkeypatch.setattr(service, "check_owner", lambda: None)
    active = [True]

    def revoke_during_kernel_owner_read():
        active[0] = False
        return dict(owner)

    kernel.owner_provider = revoke_during_kernel_owner_read
    with pytest.raises(WisdomAuthError, match="no longer current"):
        if operation == "save":
            service.prepared_save(
                prepared["draft_id"], author_description="Changed private description",
                files=prepared["files"], expected_hashes=prepared["hashes"],
                may_apply=lambda: active[0],
            )
        else:
            service.prepared_review(
                prepared["draft_id"], allow_model_review=True,
                reviewed_hashes=prepared["hashes"], may_apply=lambda: active[0],
            )

    assert not active[0]
    assert kernel.store.draft(prepared["draft_id"]) == before
    assert llm.calls == prior_calls


@pytest.mark.parametrize("action", ["review", "submit", "reopen", "decline"])
def test_publication_service_carries_authority_past_kernel_owner_read(
    tmp_path, monkeypatch, action,
):
    """The public service cannot drop the selected invocation before remote effects."""
    kernel, prepared, client, owner = publication(tmp_path)
    review = kernel.review(prepared["draft_id"])
    target = review["receipt"] if action == "submit" else prepared["draft_id"]
    if action == "reopen":
        client.state.fail_after_effect.add("submit")
        pending = kernel.submit(review["receipt"])
        target = pending["operation_id"]
        client.state.calls.clear()
    elif action == "decline":
        pending = kernel.submit_private_draft(review["receipt"])
        target = pending["operation_id"]
        client.state.calls.clear()
    def snapshot():
        if action == "review":
            return kernel.store.draft(target)
        if action == "submit":
            return kernel.store.publication_review(target)
        return kernel.store.publication(target)

    before = snapshot()
    service = InvocationService(
        kernel.store.root, ("default", Path(owner["profile_home"])),
        state=JsonState(kernel.store.root),
    )
    monkeypatch.setattr(service, "_publication_service", lambda: kernel)
    monkeypatch.setattr(service, "check_owner", lambda: None)
    active = [True]

    def revoke_during_owner_read():
        active[0] = False
        return dict(owner)

    kernel.owner_provider = revoke_during_owner_read
    with pytest.raises(WisdomAuthError, match="no longer current"):
        getattr(service, f"publication_{action}")(target, may_apply=lambda: active[0])

    assert not active[0]
    assert not client.state.calls
    assert snapshot() == before
