# Contributing

## Reviewable Extraction Slices

Pick a feature from [FEATURES.md](docs/FEATURES.md). State its ID, dependencies,
acceptance criteria, and intended write scope in the PR. Keep generic Hermes
host changes in a separate upstream PR and link the dependency here.

Copy coherent working code with its tests before adapting integration. Record
source symbols and test dispositions in the slice's verification note. Separate
mechanical extraction and adaptation into commits where practical; tasks share
delivery PRs according to docs/FIRST-RELEASE.md, not one PR per feature. Each
parallel worker owns a separate worktree and an exclusive write scope.

Use `codex/<short-topic>` for new development branches. Initial repository
bootstrapping uses `main`; normal work should use reviewed PRs.

Before changing implementation, consult the pinned source baseline and current
host API. The old implementation is behavioral evidence, not an instruction
to retain obsolete integration glue or legacy-state migrations.

## Checks

Run the README development commands. CI performs Pylint, focused tests, and
package builds without production credentials. Add a small regression test for
the behavior changed, plus a denial/recovery test when authority or persistence
is involved. See [TESTING.md](docs/TESTING.md) for later milestone checks.

Do not use broad lint suppressions or weaken assertions to force a green build.
Tests and dependencies must work from a clean environment, not only a developer
checkout that happens to contain the reverted core modules.

## Completion Record

For each feature, record the implementation commit/PR, automated command and
result, and live evidence if applicable. A feature can be implemented but still
await automated or live acceptance. A blocked check must name its dependency.

Use an artifact in `docs/verification/` for evidence that needs more space.
Exclude access tokens, private skill contents, personal identifiers, and raw
production logs. Git history is the audit trail; do not maintain duplicate
status spreadsheets or detached feature lists.

## Compatibility and Release

No runtime network download of code or schemas. Pin reviewed plugin and host
revisions. The full OpenAPI artifact may be fetched for development/CI only,
from an immutable Gateway revision and verified against its recorded hash.
Runtime-required schemas and canonical vectors must ship with the package.

Preserve upstream copyright notices when extracting code. The repository is
private; publishing releases or making it public is a separate decision.
