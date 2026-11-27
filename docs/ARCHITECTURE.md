# Architecture

Current scope: a replacement-only external plugin on unmodified Hermes Agent.
See [Features](FEATURES.md) for workflows and [Verification](TESTING.md) for
evidence levels.

## Ownership

| Layer | Owns |
| --- | --- |
| Hermes host | PluginState namespace, settings, credentials, lifecycle, caller routing and SDK surfaces |
| Python plugin | Domain records, qualification, exact reviews, package application and explicit operation status |
| Surface adapters | Commands, native cards/callbacks and Desktop rendering; not independent business logic |
| Gateway/Portal | Scoped remote access, content storage, publication policy and moderation |

`InvocationService` composes services for one captured profile/account. Relative
imports support the native directory loader's synthetic package namespace.
Host credentials are read through the existing account adapter, never copied
into plugin state. Ownership and entitlement are rechecked at effect boundaries.

## State and effects

Domain owners use the **injected public PluginState namespace**:

- `DirectState`: enrollment, setup reviews/progress and publication records.
- `InstalledState`: installed versions and shared install/update operations.
- `QualificationState`: local snapshots, usage/refinement and candidates.
- `ReviewState`: exact explicit native review records.

Short aggregate guards protect read/modify/write operations. Existing effect
locks coordinate filesystem/setup operations; network or human waits do not run
under aggregate state locks. There is no SQLite store, migration/importer,
compatibility facade or dual-write path.

Install and update reuse `package_application.py`, preserving reviewed hashes
and refusing unexpected local edits. Setup is separately reviewed and approved.
An informational notice or prior install approval never authorizes setup.

Publication binds an owner, immutable file snapshot, canonical hashes and the
observed policy to one review. Remote effects recheck current authority, including
after slow head reads. Unknown outcomes remain explicit and reconcile against
owner-private remote state rather than silently submitting a second draft.
Those business-operation records are not advisory delivery queues.

## Optional skill-catalog adapter

`catalog_registration.py` feature-detects the host registrar and `skills.catalog` grant before
importing the experimental provider base. The adapter maps discovery and review data to plain
JSON; the host stamps source identity and owns target reservations. Importing or registering
never opens credentials or contacts Gateway. Existing hosts keep their current surfaces.

`CatalogReviews` binds generic tokens to the exact native install/update/uninstall receipt,
owner, implementation revision and complete finding set. `CatalogProvider` delegates confirmation
to the existing executors. `NativeCatalog` admits legacy effects through the same core facade.
`CatalogMutation` repeats byte checks inside the core-owned target hold, journals local completion,
then settles pending with `outcome: null` and the local installation revision. Gateway calls and
terminal settlement happen outside the hold; retained terminal wire results precede receipt
completion and support read-only replay after target reuse.

Installed projection never claims an author's publication-only source. After disable, matching
installed trees can be reclaimed on re-enable, while edited/replaced trees remain local. Catalog
updates refuse local edits without writing a second preservation target. No Hub install bundle,
model permission, automatic setup or new CLI surface is introduced.

## Best-effort notices and native reviews

Each eligible end turn checks qualified local candidates; feed polling runs only
when due. Policy, profile notification settings, mute and suppression are separate
gates. Persist only owner-bound cursor/poll state and offered markers. Discard
stale account results; unsupported surfaces do nothing. No claims, settlement,
retry/rescue queues, model-history injection or cross-device guarantee exists.

Explicit native tools consume host-admitted private turns. Native actors and
Portal accounts are different identities. Reviews bind exact content/actions,
owner, actor, route, original SDK-returned message identity and expiry. A model
can request a review but cannot authorize its effect. CLI/TUI retain explicit
terminal workflows; unsupported renderers cannot claim to display native cards.

## Desktop and distribution

`desktop/plugin.js` uses the SDK and `ctx.rest`; `dashboard/plugin_api.py` mounts
under the existing authenticated plugin namespace. It is not a new server or a
Dashboard UI. Page handles and action receipts are fenced by ownership, view and
profile/connection lifetime. Setup execution retains a native handoff.

Native Git installation is the user distribution path. Wheel/sdist builds check
packaging and contract-byte parity; a wheel alone is not native plugin discovery.
Manual installed-client and real-service acceptance remain separate from fixtures
and pinned-host automation. Historical extraction plans do not override this scope.
