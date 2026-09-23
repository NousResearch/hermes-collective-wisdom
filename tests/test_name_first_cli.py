"""Name-first terminal routing without weakening exact review authority."""
# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access,unused-argument
import argparse
from typing import Any, cast
from types import SimpleNamespace

import pytest

from hermes_wisdom_plugin.commands import build_parser, run_cli
from hermes_wisdom_plugin.errors import WisdomConflict, WisdomNotFound
from hermes_wisdom_plugin.package import source_fingerprint
from hermes_wisdom_plugin.runtime import InvocationService


def args(*words):
    parser = argparse.ArgumentParser()
    build_parser(parser)
    return parser.parse_args(words)


class NamedService:
    def __init__(self):
        self.calls = []

    def require_setup(self):
        pass

    def resolve_skill(self, name):
        self.calls.append(("resolve", name))
        return {"skill": {"id": "opaque-1", "slug": name}, "versions": []}

    def show(self, reference):
        self.calls.append(("show", reference))
        return {"skill": {"slug": "Friendly Skill"}, "versions": []}

    def versions(self, reference):
        self.calls.append(("versions", reference))
        return [{"version": 2}]

    def list_installations(self):
        self.calls.append(("installed",))
        return [{"skill_id": "opaque-1", "slug": "Friendly Skill", "state": "active", "version": 2}]

    def check_update(self, reference):
        self.calls.append(("check", reference))
        return {"skill_id": "opaque-1", "state": "current"}

    def update_plan(self, reference, **kwargs):
        self.calls.append(("update", reference))
        return {"slug": "Friendly Skill", "version": 3, "files": [], "allowed": False}

    def uninstall(self, reference):
        self.calls.append(("uninstall", reference))
        return {"uninstalled": True, "slug": "Friendly Skill"}

    def installed_setup_inspect(self, reference, *, version=None):
        self.calls.append(("setup", reference))
        return {"slug": "Friendly Skill", "state": "required"}


def test_catalog_reads_resolve_exact_name_and_human_output_omits_id(capsys):
    service = NamedService()
    assert run_cli(args("show", "Friendly Skill"), service) == 0
    assert service.calls == [("show", "Friendly Skill")]
    assert "Friendly Skill" in capsys.readouterr().out
    assert run_cli(args("versions", "Friendly Skill", "--json"), service) == 0
    assert service.calls[-1] == ("versions", "Friendly Skill")


def test_runtime_catalog_methods_bind_name_to_id():
    service = cast(Any, InvocationService.__new__(InvocationService))
    calls = []
    detail = {"skill": {"id": "opaque-1", "slug": "Friendly Skill"}}
    service._discovery = lambda: SimpleNamespace(
        resolve_skill=lambda name: calls.append(("resolve", name)) or detail,
        versions=lambda skill_id: calls.append(("versions", skill_id)) or [],
    )
    assert service.show("Friendly Skill") == detail
    assert service.versions("Friendly Skill") == []
    assert calls == [("resolve", "Friendly Skill"), ("resolve", "Friendly Skill"),
                     ("versions", "opaque-1")]


def test_managed_name_uses_exact_active_installation_for_removal_and_setup(capsys):
    service = NamedService()
    assert run_cli(args("uninstall", "Friendly Skill", "--yes"), service) == 0
    assert service.calls[-1] == ("uninstall", "opaque-1")
    assert "opaque-1" not in capsys.readouterr().out
    assert run_cli(args("installed-setup", "inspect", "Friendly Skill"), service) == 0
    assert service.calls[-1] == ("setup", "opaque-1")


def test_runtime_managed_name_rejects_inactive_or_unknown_and_exact_duplicate():
    service = cast(Any, InvocationService.__new__(InvocationService))
    rows = [{"skill_id": "opaque-1", "slug": "Friendly Skill", "state": "active"}]
    service.list_installations = lambda: rows
    service.check_owner = lambda: None
    assert service.resolve_managed_skill("Friendly Skill") == "opaque-1"
    assert service.resolve_managed_skill("opaque-1") == "opaque-1"
    with pytest.raises(WisdomNotFound):
        service.resolve_managed_skill("missing")
    rows.append({"skill_id": "opaque-2", "slug": "Friendly Skill", "state": "active"})
    with pytest.raises(WisdomConflict, match="ambiguous"):
        service.resolve_managed_skill("Friendly Skill")


def test_ambiguous_managed_name_refuses_removal(capsys):
    class Ambiguous(NamedService):
        def list_installations(self):
            return [{"skill_id": "one", "slug": "Friendly Skill", "state": "active"},
                    {"skill_id": "two", "slug": "Friendly Skill", "state": "active"}]

    service = Ambiguous()
    assert run_cli(args("uninstall", "Friendly Skill", "--yes"), service) != 0
    assert not any(call[0] == "uninstall" for call in service.calls)
    assert "ambiguous" in capsys.readouterr().err.lower()


def test_draft_name_resolution_refuses_ambiguous_and_binds_operation():
    owner = {"profile_name": "demo"}
    rows = {
        "drafts": {
            "local:one": {"id": "local:one", "slug": "Friendly Skill", "owner": owner,
                          "state": "prepared"},
        },
        "publications": {
            "op-1": {"id": "op-1", "local_draft_id": "local:one", "owner": owner,
                     "status": "pending"},
        },
        "publication_reviews": {
            "receipt-1": {"id": "receipt-1", "draft_id": "local:one", "owner": owner,
                          "consumed_at": None},
        },
    }
    service = cast(Any, InvocationService.__new__(InvocationService))
    service._direct_state = SimpleNamespace(_load=lambda: rows)
    service._installation_owner = lambda: owner
    service.check_owner = lambda: None
    assert service.resolve_local_draft("Friendly Skill") == "local:one"
    assert service.resolve_publication_operation("Friendly Skill") == "op-1"
    assert service.resolve_publication_receipt("Friendly Skill") == "receipt-1"
    rows["drafts"]["local:two"] = {"id": "local:two", "slug": "Friendly Skill",
                                   "owner": owner, "state": "prepared"}
    with pytest.raises(WisdomConflict, match="ambiguous"):
        service.resolve_local_draft("Friendly Skill")
    # Drafts can share a name while only one has a pending publication.
    assert service.resolve_publication_operation("Friendly Skill") == "op-1"
    assert service.resolve_publication_receipt("Friendly Skill") == "receipt-1"
    rows["publications"]["op-2"] = {"id": "op-2", "local_draft_id": "local:two",
                                     "owner": owner, "status": "pending"}
    rows["publication_reviews"]["receipt-2"] = {
        "id": "receipt-2", "draft_id": "local:two", "owner": owner, "consumed_at": None}
    with pytest.raises(WisdomConflict, match="ambiguous"):
        service.resolve_publication_operation("Friendly Skill")
    with pytest.raises(WisdomConflict, match="ambiguous"):
        service.resolve_publication_receipt("Friendly Skill")
    with pytest.raises(WisdomNotFound):
        service.resolve_local_draft("missing")


def test_name_uses_current_source_when_prior_published_draft_is_retained(tmp_path):
    name = "journey-smoke"
    source = tmp_path / name
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: journey-smoke\n---\n\nv1\n")
    old_hash = source_fingerprint(source)
    (source / "SKILL.md").write_text("---\nname: journey-smoke\n---\n\nv2\n")
    current_hash = source_fingerprint(source)
    owner = {"profile_name": "demo"}
    rows = {"drafts": {
        "local:old": {"id": "local:old", "slug": name, "owner": owner,
                      "source_path": str(source), "source_hash": old_hash, "state": "prepared"},
        "local:new": {"id": "local:new", "slug": name, "owner": owner,
                      "source_path": str(source), "source_hash": current_hash, "state": "prepared"},
    }}
    service = cast(Any, InvocationService.__new__(InvocationService))
    service._direct_state = SimpleNamespace(_load=lambda: rows)
    service._installation_owner = lambda: owner
    service.check_owner = lambda: None
    service._contribution_reviewer = lambda: SimpleNamespace(source=lambda _name: source)
    assert service.resolve_local_draft(name) == "local:new"
    (source / "SKILL.md").write_text("---\nname: journey-smoke\n---\n\nv3\n")
    with pytest.raises(WisdomConflict, match="ambiguous"):
        service.resolve_local_draft(name)


def test_prepared_and_publication_names_bind_internal_ids(capsys):
    class Local(NamedService):
        def resolve_local_draft(self, name):
            assert name == "Friendly Skill"
            return "local:one"

        def resolve_publication_operation(self, name):
            assert name == "Friendly Skill"
            return "op-1"

        def resolve_publication_receipt(self, name):
            assert name == "Friendly Skill"
            return "exact-receipt"

        def prepared_review(self, draft_id, *, allow_model_review=False):
            self.calls.append(("prepared", draft_id))
            return {"slug": "Friendly Skill", "draft_id": draft_id, "files": []}

        def publication_review(self, draft_id, *, accept_warnings=False):
            self.calls.append(("review", draft_id, accept_warnings))
            return {"slug": "Friendly Skill", "receipt": "exact-receipt"}

        def publication_status(self, operation_id):
            self.calls.append(("status", operation_id))
            return {"state": "uncertain", "operation_id": operation_id}

        def publication_submit(self, receipt):
            self.calls.append(("submit", receipt))
            return {"state": "done", "publication_state": "published"}

    service = Local()
    assert run_cli(args("prepared", "review", "Friendly Skill"), service) == 0
    assert run_cli(args("publication", "review", "Friendly Skill"), service) == 0
    assert run_cli(args("publication", "submit", "Friendly Skill"), service) == 0
    assert run_cli(args("publication", "status", "Friendly Skill"), service) == 0
    assert service.calls == [("prepared", "local:one"), ("review", "local:one", False),
                             ("submit", "exact-receipt"), ("status", "op-1")]
    output = capsys.readouterr().out
    assert "local:one" not in output
    assert "op-1" not in output
