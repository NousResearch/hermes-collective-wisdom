# Verification

## Scope and evidence

The target is an external plugin on unmodified Hermes Agent, with Desktop
management pages using authenticated plugin REST and explicit native messaging
workflows. Dashboard/full six-surface parity and manual/live acceptance are
deferred. Retired SQLite/queue-era test counts do not validate this replacement.
Keep dated execution logs outside this requirements document.

Distinguish local fixture automation, actual-host dispatch (possibly with mocked
business services), real-service acceptance in disposable organizations, and
installed-client acceptance. Record the candidate source manifest, interpreter,
import paths, host/deployment revisions, commands, results and skips. Passing
one evidence level does not establish the next.

## Local checks

Use a disposable environment with this checkout's `.[dev]` dependencies, not an
installed Hermes environment. From the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH="$PWD" \
  python -m pytest -p no:cacheprovider -q -rs --tb=short
PYTHONPATH="$PWD:$PWD/tests" python -m pylint hermes_wisdom_plugin tests
python scripts/check_complexity.py
git diff --check
npm ci --ignore-scripts
npm test
```

Preserve pytest's configured import mode; clearing `addopts` can cause nested
test-basename collisions. Renderer tests use pinned React/SDK fixtures, not an
installed app or a deployed Gateway.

### Optional actual-host probes

Inspect each fixture's requirements before enabling it. Use a pinned unmodified
host checkout, a dependency-capable interpreter and disposable profiles:

```sh
WISDOM_AGENT_DIR=/path/to/pinned-agent \
WISDOM_HOST_PYTHON=/path/to/host/python \
PYTHONPATH="$PWD" python -m pytest -q -rs \
  tests/test_host_account_integration.py tests/test_host_install_integration.py

WISDOM_STOCK_AGENT_DIR=/path/to/pinned-agent \
WISDOM_STOCK_HOST_PYTHON=/path/to/host/python \
PYTHONPATH="$PWD" python -m pytest -q -rs \
  tests/test_host_gateway_session_binding.py

WISDOM_DESKTOP_HOST_DIR=/path/to/pinned-agent \
WISDOM_HOST_PYTHON=/path/to/host/python \
PYTHONPATH="$PWD" python -m pytest -q -rs tests/test_desktop_host_mount.py
```

Record subprocess imports separately from the interpreter location. A skip is
not a passing integration test. Never give fixtures production credentials.

## Retained behavioral gates

- **Authority:** fresh entitlement and account/profile/organization ownership;
  private owning-human admission; no background/subagent consent or effects.
- **Consent:** exact complete content/action binding, expiry before effects,
  re-review after byte changes, native actor/original-message binding, and no
  receipt transfer from closed views to new views.
- **Install/update/setup:** shared package application protects local edits and
  reviewed hashes. Inspection is not setup approval. Recheck owner/expiry after
  inspection and use the supported host approval/tool APIs. Distinguish unstarted
  denial from uncertain or already-started effects.
- **Contribution/publication:** complete-file-set review, disclosure and current
  Share authority; direct prepare → review → submit. Conflicts need re-review;
  ambiguous writes must not silently duplicate effects.
- **Qualification:** real stored snapshots, meaningful refinement, and opt-in
  before model-data disclosure. Advice never authorizes publication.
- **Notices:** candidate checks each eligible turn, feed polling only when due;
  independent policy/settings/mute/suppression gates; offered candidates do not
  starve later ones; stale account results are discarded.
- **Lifecycle:** actual emitted Desktop events, connection/profile fencing,
  disable/unload cleanup and native cancellation revocation. Unsupported
  proactive surfaces are successful no-ops.

Do not recreate notice claims, settlement telemetry, retry/rescue queues or
cross-device exactly-once guarantees. Missed notices can stay missed; explicit
Browse, Contributions and business workflows remain available. Map deleted
tests' behavioral assertions to retained coverage rather than restoring retired
APIs or silently dropping safety checks.

## Distribution and tooling

Run `tests/test_distribution.py`. Build in a disposable source copy with
`python -m build --outdir /path/to/external-artifacts`. Compare runtime/contract
bytes across source, wheel and sdist; extract the sdist separately and run its
affected tests/tooling with imports pointing at that extraction. Check Desktop
assets, manifests, scripts and lint/hook configuration separately. The wheel is
a Python library, not a native plugin installer. Do not smoke-test by installing
into a live profile.

The complexity checker uses pinned McCabe and a default cap of 10. Review
exceptions by exact `path::qualified_name`: remove retired/resolved entries,
lower improved bounds and justify above-cap functions. `--print-baseline` is
comparison evidence, not permission to accept new debt. Do not add abstractions
merely to move branches out of a headline function.

Hook installation is optional and separately authorized:

```sh
python scripts/install_complexity_hook.py
python scripts/check_complexity.py --staged
```

The installer refuses configured `core.hooksPath` and non-regular paths. An
existing non-pre-commit hook needs explicit `--replace-existing`; its backup is
not executed. Linked worktrees share hooks: tests must not install one there.

For an opt-in mutation campaign use a disposable `.[dev,mutation]` environment:

```sh
python scripts/run_receipt_mutations.py \
  --tool-python /absolute/path/to/disposable-venv/bin/python \
  --artifact-dir /absolute/path/to/new-external-artifact-directory \
  --budget-seconds 300 --max-children 4
```

This POSIX sandbox targets `InstallReceipts.start`, not the whole product.
Inspect logs/results and mutated-source import provenance. Distinguish timeout,
harness errors and survivors; do not infer a mutation score from ordinary tests.

## Deferred manual/live acceptance

First authorize the organization, clients and effects. Use newly initialized
disposable profiles and an inert skill; never reset existing user state as a test.

1. Exercise contribution/refinement, complete-file-set edit/review, submit,
   moderation and publication; verify member/admin separation.
2. As another member, browse, inspect, install and separately approve setup;
   publish another version and exercise manual and allowed automatic updates.
3. Exercise mute/suppression and explicit browsing after dismissed/missed advice,
   without requiring delivery guarantees or delivery-linked reporting.
4. Deny stale/altered reviews, wrong actors/messages and wrong profiles. Switch
   account/profile/connection mid-request; stale results must not cross over.
5. Use actual end-turn events; test Slack/Telegram commands and callbacks
   independently, original-message edits and supported thread behavior.
6. Disable/reload/reconnect; verify listener cleanup and no duplicate effects.
7. In installed Desktop, exercise catalog/installed/activity/contribution pages,
   native confirmations, OAuth remotes without sockets, keyboard/focus behavior,
   dialog labels, long text, themes and scrolling. Record visual evidence.

Mocks and credential-free Electron layout smokes do not establish deployed
workflow success. Report unavailable clients/services explicitly.
