// Turns two freshly signed-up local Portal accounts into the demo team and
// mints each one a Hermes login, through the Portal's own server code.
//
//   npx tsx dev/local-demo/portal-team.mts <nas-app-dir> <alice.json> <bob.json>
//
// <alice.json>/<bob.json> are scripts/mint-account.ts output. Alice's personal
// org becomes "Local Test Team" (slug cw-local) with alice as OWNER; bob joins
// as MEMBER, the same rows an accepted invitation writes. Tokens come from the
// Portal's normal issuer (createRefreshSession + issueOAuthAccessToken), so the
// Gateway and Hermes see exactly what a real `hermes auth` login produces.
// Prints one JSON line. Only ever pointed at the launcher's throwaway database.

import { readFileSync } from "node:fs";
import { register } from "node:module";
import { pathToFileURL } from "node:url";

const [nasDir, aliceFile, bobFile] = process.argv.slice(2);
if (!nasDir || !aliceFile || !bobFile) {
  console.error("usage: portal-team.mts <nas-app-dir> <alice.json> <bob.json>");
  process.exit(2);
}
const dbUrl = new URL(process.env["DATABASE_URL"] ?? "");
if (dbUrl.hostname !== "127.0.0.1") {
  console.error("refusing: DATABASE_URL is not the local throwaway database");
  process.exit(1);
}
// Same module hooks the Portal's own scripts use (`server-only`, `~/` alias).
register(pathToFileURL(`${nasDir}/scripts/lib/server-only-hooks.mjs`));

type Minted = { userId: string; orgId: string };
const alice = JSON.parse(readFileSync(aliceFile, "utf8")) as Minted;
const bob = JSON.parse(readFileSync(bobFile, "utf8")) as Minted;
const ORG_SLUG = "cw-local";
const ORG_NAME = "Local Test Team";
const CLIENT_ID = "hermes-cli";
const TTL_SECONDS = 12 * 3600;

const { db } = await import(`${nasDir}/src/server/db.ts`);
try {
  await db.$transaction(async (tx: typeof db) => {
    await tx.organisation.update({
      where: { id: alice.orgId },
      data: { name: ORG_NAME, slug: ORG_SLUG, isPersonal: false },
    });
    await tx.orgMembership.upsert({
      where: { userId_orgId: { userId: alice.userId, orgId: alice.orgId } },
      update: { role: "OWNER" },
      create: { userId: alice.userId, orgId: alice.orgId, role: "OWNER" },
    });
    await tx.orgMembership.upsert({
      where: { userId_orgId: { userId: bob.userId, orgId: alice.orgId } },
      update: { role: "MEMBER" },
      create: { userId: bob.userId, orgId: alice.orgId, role: "MEMBER" },
    });
  });

  const { createRefreshSession } = await import(`${nasDir}/src/server/oauth/device-flow.ts`);
  const { issueOAuthAccessToken } = await import(`${nasDir}/src/server/oauth/access-token-issuer.ts`);
  const tokens: Record<string, unknown> = {};
  for (const [name, user] of [["alice", alice], ["bob", bob]] as const) {
    const session = await createRefreshSession({ userId: user.userId, orgId: alice.orgId, clientId: CLIENT_ID });
    const token = await issueOAuthAccessToken({
      userId: user.userId,
      orgId: alice.orgId,
      clientId: CLIENT_ID,
      scope: undefined,
      sessionId: session.sessionId,
      expiresInSeconds: TTL_SECONDS,
    });
    tokens[name] = {
      access_token: token.accessToken,
      refresh_token: session.refreshToken,
      expires_in: token.expiresIn,
      user_id: user.userId,
    };
  }
  console.log(JSON.stringify({ orgId: alice.orgId, orgSlug: ORG_SLUG, orgName: ORG_NAME, tokens }));
} finally {
  await db.$disconnect();
}
