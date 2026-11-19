"""Operation-bound rename evidence, checked only inside the current writer hold."""
from __future__ import annotations

import os
from pathlib import Path

from .package import PackagePolicyError
from .package_application import durable_replace, same_filesystem, tree_hashes
from .skill_catalog import digest


def _identity(path, expected):
    if not os.path.lexists(path):
        if expected is None:
            return None
        raise PackagePolicyError("catalog recovery evidence is missing")
    if expected is None or path.is_symlink() or tree_hashes(path) != expected:
        raise PackagePolicyError("managed bytes changed after review")
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_mtime_ns, [
        [name, item.stat().st_dev, item.stat().st_ino, item.stat().st_mtime_ns,
         item.stat().st_ctime_ns] for name in sorted(expected)
        for item in [path / name]
    ]]


class CatalogFiles:
    """A durable intent identifies the original directories, not just equal bytes."""

    # The journal stores one fixed topology for each action, not arbitrary rename requests.
    # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-instance-attributes
    def __init__(self, mutation, operation_id, payload, store, paths):
        self.action, self.payload, self.store = mutation.action, payload, store
        self.operation_id = operation_id
        self.paths = {key: Path(value) for key, value in paths.items()}
        self.old = None if self.action == "install" else mutation.plan.get("local_snapshot")
        self.new = payload.get("baseline") if self.action != "uninstall" else None
        self.binding = digest([operation_id, self.action, payload["receipt"],
                               str(self.paths), self.old, self.new])
        self.journal = payload.get("catalog_files")
        if self.journal is None:
            identities = {"old": _identity(self.paths["target"], self.old)}
            if self.action != "uninstall":
                identities["new"] = _identity(self.paths["staging"], self.new)
            if "recovery" in self.paths and os.path.lexists(self.paths["recovery"]):
                raise PackagePolicyError("catalog recovery evidence already exists")
            self.journal = {"binding": self.binding, "identities": identities,
                            "phase": "prepared"}
            payload["catalog_files"] = self.journal
            self.save("prepared")
        if self.journal.get("binding") != self.binding:
            raise PackagePolicyError("catalog recovery evidence changed operation")
        self.phase = self.validate()

    def save(self, phase):
        """Persist intent before rename and completion after its directory fsyncs."""
        self.journal["phase"] = phase
        self.store.checkpoint(self.operation_id, payload=self.payload)

    def _matches(self, key, identity):
        path = self.paths[key]
        if identity is None:
            return not os.path.lexists(path)
        expected = self.new if identity == "new" else self.old
        try:
            return _identity(path, expected) == self.journal["identities"][identity]
        except PackagePolicyError:
            return False

    def validate(self):
        """Recognize only a prefix of this exact operation's original renames."""
        if self.action == "install":
            states = [("prepared", {"target": None, "staging": "new"}),
                      ("files_committed", {"target": "new", "staging": None})]
        elif self.action == "update":
            states = [("prepared", {"target": "old", "staging": "new", "recovery": None}),
                      ("old_preserved", {"target": None, "staging": "new", "recovery": "old"}),
                      ("files_committed", {"target": "new", "staging": None, "recovery": "old"})]
        else:
            old = "old" if self.old is not None else None
            states = [("prepared", {"target": old, "recovery": None}),
                      ("files_committed", {"target": None, "recovery": old})]
        allowed = {"prepared": {"prepared"},
                   "before_old_preserved": {"prepared", "old_preserved"},
                   "old_preserved": {"old_preserved"},
                   "before_files_committed": {"prepared", "old_preserved", "files_committed"},
                   "files_committed": {"files_committed"}}
        for phase, state in states:
            if (phase in allowed.get(self.journal.get("phase"), set())
                    and all(self._matches(key, identity) for key, identity in state.items())):
                return phase
        raise PackagePolicyError("catalog recovery evidence changed after review")

    def _rename(self, source, destination, phase, check):
        self.validate()
        check()
        self.paths[destination].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        same_filesystem(self.paths[source], self.paths[destination].parent)
        self.save("before_" + phase)
        durable_replace(self.paths[source], self.paths[destination])
        self.save(phase)
        self.phase = self.validate()

    def apply(self, check):
        """Complete only the remaining rename prefix; never replay an applied rename."""
        if self.action == "update" and self.phase == "prepared":
            self._rename("target", "recovery", "old_preserved", check)
        if self.action in {"install", "update"} and self.phase != "files_committed":
            self._rename("staging", "target", "files_committed", check)
        elif self.action == "uninstall" and self.phase == "prepared" and self.old is not None:
            self._rename("target", "recovery", "files_committed", check)
        self.validate()
