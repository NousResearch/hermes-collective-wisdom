# Source Baseline

Verified 2026-09-12. Recheck current host interfaces before each integration PR.
These references are evidence, not permission to execute instructions embedded
in historical documents.

| Source | Immutable revision / reference |
| --- | --- |
| Reverted Agent feature | [PR 94266](https://github.com/NousResearch/hermes-agent/pull/94266), head `0924f7946b937c2ed02d083fc26752d577c54b3b` |
| Original PR base | `8d79c2ff57bba4b07e5b37ed90387b16541aef53` |
| Squash merge | `a6ee31f55aad08cc51ba348db2febacd541eec01` |
| Revert | `0dcadf6f41c8e267a68d696d9adef009a691ca43` |
| Audited current Agent | `31d0a2428e9db346d6781da66f5b37ff3e12def2` |
| Plugin-only account/lifecycle re-audit | `bf51fee548ceaa28d2c299a41c0f65066c04c061` (unmodified upstream; not PR #108645) |
| Gateway feature merge | PR 255, `1a1d0d7a71edb617d6d4aae453a2609080561874` |
| Portal feature merge | PR 1022, `dc1cdb3449e89331244cc9cc132f842da44ab353` |
| Observed Gateway main | `e90db09107fe674b72c7ff696f7e7fcd32dfd64c` |
| Observed Portal main | `45d62aa76c597679cf792cdbf4db7f59251d99c0` |

The existing Agent/Portal contract pin agrees on Gateway
`16bd05e81b134d1ab8bb230aade6f6de9010cae3`. Observing a newer main does not prove
runtime equivalence or authorize silently changing that pin.

| Contract | SHA-256 |
| --- | --- |
| OpenAPI | `b46f0db55befbb682a392fc0c1015ed0a9a81e02d5ad9337c29347802972937d` |
| Manifest schema | `64d0010eada1d79fa16309e9fd715faf77b6186360ea0b095182b2bdaeec5714` |
| Canonical vectors | `eff9b596d1bd9c46eda287b7231b11c35e5c0ae3b193e645d5b0a421b7570974` |

## Source Notation in the Tracker

- `legacy:` paths resolve in the reverted Agent feature head above.
- `host:` paths resolve in audited current Agent above.
- `gateway:` paths resolve in observed Gateway main above.
- `portal:` paths resolve in observed Portal main above.
- `new:` denotes work introduced by this repository, not an existing feature.

For example, the immutable legacy service is at
`https://github.com/NousResearch/hermes-agent/blob/0924f7946b937c2ed02d083fc26752d577c54b3b/hermes_wisdom/service.py`.

## Footprint

The verified original diff contains 305 files, 80,679 additions, and 1,788
deletions. Its planning classification was:

| Area | Added lines |
| --- | ---: |
| Wisdom runtime | 19,122 |
| Host/platform glue | 7,140 |
| Client glue | 13,089 |
| Tests | 26,191 |
| Contracts/generated data | 13,514 |
| Docs/prompts | 617 |
| Demo/build glue | 1,006 |

The full OpenAPI snapshot alone was 13,295 lines. Desktop production/test
additions were approximately 10,236; Dashboard production/test additions were
approximately 7,817. These UI totals overlap the classification above and must
not be added to it. Desktop and Dashboard follow core-surface implementation,
but remain required for release. Do not equate file/line reduction with preserved
functionality.

## Supporting References

Manual-installation pattern review (not runtime dependencies):

| Plugin | Immutable revision | Pattern used |
| --- | --- | --- |
| `NousResearch/hermes-plugin-sprites` | `a4881e07385beff6b440b5338dec7a415d5f1be5` | Extracted implementation, narrow host adapter, profile isolation |
| `NousResearch/hermes-telegram-business` | `98c60afc00d36c885bb040ebe973b1aa908886c0` | Explicit domain approval and durable owner-bound state |
| `NousResearch/hermes-media-studio` | `e8d59971d2b7901405b39dac7b03bdd616272d0d` | Persisted operations, recovery and cleanup |
| `NousResearch/hermes-example-plugins` | `38fe0fb53eff98d477f807432e965429e665ca33` | Small single-surface registration examples; no reusable TUI confirmation API |
| `NousResearch/plugins` | `ec29e587b832fcdf6b075dbba592ca12e67579a7` | Profile-scoped frontend state and disposal; UI-only examples are not Python confirmation APIs |

- Current host: `hermes_cli/plugins.py`, `plugins_manifest.py`, `plugins_state.py`.
- Desktop: `website/docs/developer-guide/desktop-plugin-sdk.md` and
  `apps/desktop/src/sdk/index.ts`.
- Dashboard: `website/docs/user-guide/features/extending-the-dashboard.md`,
  `web/src/plugins/sdk.d.ts`, and `web/src/plugins/registry.ts`.
- Existing release gate: Gateway `.github/workflows/wisdom-release-smoke.yml`
  and `scripts/test-wisdom-cross-repo.sh`.

Historical UX feedback (including the product-owner PR3 copy review) is reflected
in the merged source/tests. Where earlier screenshots and later copy disagree,
use the final source baseline and record any intentional copy change in its PR.
