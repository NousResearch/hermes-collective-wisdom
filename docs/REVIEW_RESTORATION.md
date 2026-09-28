# Private invitation and review flows

Collective Wisdom uses the existing native review controller for private Slack and
Telegram invitations. It does not restore the removed recommendation/delivery
infrastructure. Desktop retains its management pages and exact-content controls.

## User-facing behavior

| Control or state | Behavior | Boundary |
| --- | --- | --- |
| Sharing invitation | Qualified candidates offer “You created a skill that could help your team”, the skill name, and Share My Skill. | Qualification is deterministic, not a model assessment of the team's needs. |
| Review checks | Expands the actual checks. Safe To Share requires complete passing security and hash-bound professionalism evidence. | Missing, pending, advisory, or unavailable checks never count as passed. Local preparation normally has pending Gateway checks. |
| View More Details | Opens private, bounded inspection. Looks good. returns without authorization. | This is not a Portal deeplink; larger packages need Previous/Next navigation. |
| Notification settings | Opens the existing profile delivery mode settings. | Opening settings changes nothing. Recommendation muting is no longer supported. |
| Install / Update invitation | Offers View Details, Not Now, and Install or Update, in that order. Uses reviewed editorial copy and a plain description under What does it do?. | No invented organization name or workload relevance. Main-agent explanations are labelled advice, never authorization. |
| Maybe Later / Not Now | Removes controls while retaining the invitation text, including cancellation during preparation. | Old callbacks cannot authorize effects. Dismissal does not create a durable cooldown. |
| Native packaging failure | A blocked local package displays Share Packaging failed without a settings shortcut. | Other preparation errors instead offer an accurately named retry/dismiss card, without exposing exception details. |
| Desktop preparation failure | Reports Share preparation failed, retaining the underlying error detail. | A transport or service failure is not falsely diagnosed as a packaging problem. Nothing is uploaded by preparation. |

## Exact consent and the existing contribution flow

Sharing starts with local, owner-private preparation, not an upload. Final submission
still requires separate exact-content approval under the organization's publication
policy. There is no once-for-all sharing authorization.

The existing `/wisdom suggest <local-skill>` workflow remains available: inspect files
and checks, edit through the terminal, re-review, acknowledge reviewable scanner
warnings, continue to sharing review, or keep the draft. The invitation flow must
preserve the same draft identity, owner, and warning-acknowledgement boundaries.
Acknowledging warnings is not publication consent; a changed package or scanner
verdict requires a fresh review.

Install, update, and publication separate read-only details from approval. Their
bounded exact-review pages must be read in sequence before Confirm is offered.
Install/update inspection verifies the receipt and discloses complete UTF-8 content,
executable intent, and base64 for opaque files. Installing never authorizes setup.
The consent plan and receipt are not replaced by a presentation summary or agent
explanation.

Slack exact-review sections use literal plain text, not interpreted Markdown.
Telegram uses escaped HTML, including its bounded fallback. Rendering must preserve
all required approval facts; oversized or failed disclosure cannot enable approval.

The owner is rechecked after an awaited provider post. If it changed or cannot be
established, the original unbound review is cancelled with full-record CAS and the
provider is asked to delete only the acknowledged message. Failed or unavailable
deletion cannot restore action authority. Already-transmitted data may have been
briefly visible; best-effort cleanup is not a guarantee it was never disclosed.
Each pending transition waits for provider acknowledgement before accepting another
approval action. Failure to display a completed result does not undo the effect or
make it replayable.

Large packages can require many pages. Native expiry still applies, and terminal
inspection remains the practical alternative for very large packages. Provider
acknowledgement proves successful rendering, not human comprehension.

## Implementation boundaries

- Reuse PluginState, native callbacks, synchronous preparation/receipt services,
  deterministic notice policy, account/route guards, settings, and main-agent tools.
- State transitions compare the complete expected review record, not only its state
  name. Newer draft, plan, scanner, and presentation changes must not be overwritten.
- Preserve exact owner/draft identity checks, scanner acknowledgement, and renderer
  losslessness from the existing contribution workflow.
- `native_inspection.py` is a pure bounded projection helper, not storage or an effect
  controller. No daemon, queue, SQLite schema, alternate catalog, model client,
  synthetic conversation turn, host patch, or new dependency is introduced.
- Inspected package bytes remain in existing PluginState review records. Expiry and
  dismissal revoke callbacks; they do **not** establish deletion or retention timing.
  No cleanup worker, migration, or production deletion is added.

The per-function complexity ratchet is not permission to ignore aggregate support
cost. The restoration increases controller/inspection complexity; the separate
file-aggregate exception remains a maintainer decision. Current measurements and
revision-bound validation belong in the PR, not a rolling history in this guide.
No complexity threshold or baseline is relaxed.

## Verification boundary

Offline tests exercise registered Slack/Telegram callbacks, actual receipt verification
and filesystem application with injected clients, binary/script preservation without
execution, sequential inspection, cancellation, owner changes, stale-record races,
failed display, and replay rejection. Desktop tests exercise mounted components.
See [Testing](TESTING.md) for reproducible commands and optional stock-host probes.

Passing tests does not establish live Slack/Telegram delivery or production Gateway
publication/installation. Full Desktop invitation parity, private Portal deeplinks,
once-for-all sharing consent remain outside this change.
