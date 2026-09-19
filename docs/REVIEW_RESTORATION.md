# Reviewed Wisdom flow restoration

This change restores the useful private review experience on the current standalone
plugin. It does not restore the former recommendation/delivery infrastructure.

## What changes for a user

| Request | Restored behavior | Boundary |
| --- | --- | --- |
| 1. Useful sharing invitation | Qualified native candidates open “You created a skill that could help your team”, before the skill name, with an actionable Share My Skill button. | Qualification remains deterministic; no claim that a model assessed the team's needs. |
| 2. Compact checks | Actual complete passing security and hash-bound professionalism results get the requested collapsed Safe To Share summary. The first button expands the checks. | Missing, pending, advisory, unavailable, or incomplete results say Review checks. Local preparation normally still has pending Gateway checks. |
| 3. Ordered sharing controls | Checks, View More Details, Snooze Collective Wisdom, Maybe Later, Share My Skill, in that order. | Details are private native inspection, **not a Portal deeplink**. Opening snooze only opens settings. |
| 4. Installation/update invitation | An organization-context invitation, the install/update question, and View Details, Not Now, Mute Skill Recommendations, Install/Update Skill. Existing main-agent consent tools can pass a context-grounded explanation. | Partial: the current contract supplies no reliable organization display name. Generic notices do not invent personalized relevance or a skill description. Settings disclose that mute covers all recommendations, not recipient-only suggestions. |
| 5. Quiet dismissal | Maybe Later / Not Now remove controls while preserving the card's words. Replayed confirmation cannot apply. | Dismissal does not create a durable cooldown or mutate notification preferences. |
| 6. Read-only return | Private details expose the complete exact review, with Looks good. returning to the invitation without authorizing anything. | Larger packages need Previous/Next navigation; this is not a literal single-button screen for every package. |
| 7. Packaging failures | A real blocked local package reaches Share Packaging failed, with no settings shortcut and no upload/publication claim. Desktop's existing preparation error also uses the requested wording. | Arbitrary service/network errors are not mislabeled as local packaging failures by the native handler. |
| 8. Authorize sharing once | **Not implemented; not a regression.** | Share My Skill prepares locally; exact-content review and final confirmation remain mandatory. |

The native changes target private Slack and Telegram. The existing Desktop management
page and consent flow are retained, with the packaging-error wording corrected.
This does not claim complete invitation/action-bar parity on Desktop or unsupported
surfaces.

## Sharing without losing exact consent

1. Open a qualified sharing invitation and choose Share My Skill.
2. Preparation creates a local, owner-private draft; nothing is uploaded.
3. View More Details opens complete, bounded inspection. Looks good. returns only.
4. Share My Skill opens the exact approval review. Read each page in sequence.
5. Confirm appears on the last page only when the existing plan allows the effect.
   Its callback uses the original receipt and unmodified plan, not the visible summary
   or agent explanation. Server policy still chooses direct versus moderated sharing.

Install and update use the same read-only-details / separate-approval pattern. Their
inspection reuses the existing receipt verifier and includes complete UTF-8 bytes,
executable-script intent, and base64 for opaque files. Installing never authorizes setup.

Slack review bytes use literal plain-text sections, avoiding markdown interpretation
or stripped boundary whitespace. Telegram remains escaped HTML. Provider display
limits are unchanged: each inspection page is bounded, and the complete plan is never
trimmed to fit. Failed page rendering fails closed; it cannot enable a later forged
Confirm. Failed result rendering does not undo an already completed operation or
permit replay.

Large packages can require many pages. Native review expiry still applies; terminal
inspection is the practical alternative for very large packages. Successful callback
rendering is not a claim that a person read or understood every byte.

## Lean architecture / Teknium guidance compliance

- Reuse the existing native runtime, review controller, PluginState record, synchronous
  preparation/install/publication services, account guards, and settings routes.
- The existing bounded best-effort notice poll opens these review cards; there is no
  recommendation daemon, alternate catalog, queue, SQLite schema, retry worker,
  synthetic conversation turn, or new model client.
- Main-agent explanations remain advice. The tool instruction requires known context
  and inspected facts; presentation text is not an input to authorization or receipts.
- `native_inspection.py` is a small pure projection helper. It exists because the old
  renderer could truncate a complete plan and therefore correctly withheld Confirm.
  It serializes bounded pages and check summaries; it owns no storage, IO, or effects.
- The existing review record retains only the additional inspection/presentation state
  needed by that flow. Compare-and-replace protects pending transitions; the display
  context records provider acknowledgement before another approval action is accepted.
- No Agent/Gateway source changes, host patches, new dependencies, platform expansion,
  new CI framework, telemetry, installer, memory, or production changes.

### Complexity: distinguish function limits from file totals

Measured using the repository's existing `mccabe.PathGraphingAstVisitor`, from base
`e82409e380d8c926ff2625f120c3e21772791afc` to implementation commit
`6eb67275606cb733780d1f56b7f9b6734b2688a7`:

| File | Aggregate function complexity, base → head | Highest function, base → head |
| --- | --- | --- |
| native_review.py | 116 → 164 | 12 → 10 |
| native_runtime.py | 109 → 113 | 10 → 10 |
| native_inspection.py (new) | 0 → 10 | 0 → 4 |
| native_slack.py | 17 → 18 | 6 → 7 |
| review_state.py | 13 → 15 | 3 → 3 |
| native_tools.py | 20 → 20 | 5 → 5 |
| runtime.py | 191 → 191 | 9 → 9 |

The unchanged per-function ratchet passes, including resolving the previous `act`
exception. **The stronger file-aggregate baseline-or-lower target is not met:** these
files add 65 aggregate points. This is a genuine support-cost tradeoff for pagination,
checks, and restored interactions, not evidence of blanket architecture compliance.
The draft requires an explicit reviewer decision to accept that bounded exception or
reduce scope. No thresholds/baselines were raised and code was not split merely to
hide the delta. Desktop changes only one message and add no branch.

## Verification boundary

At the implementation commit, the offline suite passed **773 tests with 9 opt-in
integration skips**; baseline was 756 passed / 9 skipped. Pylint: 10.00/10. Existing
complexity ratchet: 836 functions checked, no new exception. The registered Desktop
component suite passed **16 tests**. Added tests invoke actual registered native
callback handlers, model tool routing, and mounted Desktop components rather than
only testing unused text helpers.

The native integration fixture verifies and applies a real exact install receipt
using an injected offline client, preserving binary bytes and script mode without
running the script. A real unsafe local package drives the packaging-failure card.
Additional Slack and Telegram tests cover disclosure, bounded details, quiet dismissal,
read-only return, ownership changes, failed display, sequential inspection, and replay.

Plugin Doctor through a stock host archive passed discovery/import/registration:
three tools and four hooks. Its seven undeclared-registration warnings also occur at
the base commit; they are not new warnings. The separate stock-host account probe
passed, and the pinned stock-host manual install lifecycle probe passed. See
[Testing](TESTING.md) for environment opt-ins and existing commands.

There was no live Slack/Telegram send, production Gateway publication, production
installation, or change to an active profile. Offline evidence does not establish
live service acceptance, private Portal links, recipient-only mute, or one-upfront
sharing consent.
