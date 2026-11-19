"""Contract fake for the writer-local D11 recovery hook."""
# pylint: disable=missing-function-docstring,too-few-public-methods
from contextlib import contextmanager

from test_catalog_mutations import Permits
from hermes_wisdom_plugin.package import PackagePolicyError
from hermes_wisdom_plugin.package_application import tree_hashes
from hermes_wisdom_plugin.skill_catalog import digest


class RecoveryPermits(Permits):
    """Observe real files after admission, not a provider-supplied digest."""

    def __init__(self, target, root, action):
        super().__init__(target.relative_to(root).as_posix())
        self.path, self.action = target, action
        self.observation = None
        self.decisions = []

    @contextmanager
    def commit(self, admission, operation_id):
        if self.path.exists() != (self.action != "install"):
            raise PackagePolicyError("fresh commit presence changed")
        with super().commit(admission, operation_id) as hold:
            yield hold

    @contextmanager
    def recover(self, operation_id):
        assert not self.held
        self.held = True
        self.calls.append(("recover", operation_id))
        api = self

        class Recovery:
            """The concrete D11 response shape exposed by core's writer wrapper."""

            target_relative_path = api.target
            pre_revision_matches = api.path.exists() == (api.action != "install")
            observed_revision = (None if not api.path.exists() else digest(tree_hashes(api.path)))
            decision = None

            def decide(self, answer):
                assert api.held and self.decision is None
                api.decisions.append(answer)
                self.decision = answer["kind"]
                if self.decision == "reconciled":
                    assert answer["journal_digest"] == self.observed_revision
                    api.calls.append(("local", answer["result"]))
                elif self.decision == "abandon":
                    api.calls.append(("abandoned", answer["reason"]))

            def settle(self, result):
                assert api.held and self.decision == "retry_effect"
                api.calls.append(("local", result))

        try:
            hold = Recovery()
            if self.observation is not None:
                self.observation(hold)
            yield hold
            assert hold.decision is not None
        finally:
            self.held = False
            self.calls.append(("release", operation_id))
