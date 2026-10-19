// Local stand-in for the Nous Portal, for the Collective Wisdom dev launcher.
//
// Binds 127.0.0.1 only. It issues HS256 tokens for fixed fake users, signed
// with a random per-process key that is never written anywhere, and serves the
// two Portal endpoints the Gateway and the plugin call:
//
//   GET  /api/oauth/account          validates a token it minted; returns the org
//   POST /api/v1/wisdom/publishers   display names for publisher ids
//   GET  /v1/models                  fake model list (OpenAI-compatible)
//   POST /v1/chat/completions        canned reply, so Hermes counts as signed in
//   GET  /healthz
//
// On start it writes one JSON line to stdout: {"url", "tokens": {name: jwt}}.
// Nothing here talks to any real Nous service.

import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";

const ORG_ID = "org-cw-local";
const ORG_NAME = "Local Test Team";
const USERS = {
  alice: { sub: "user-alice", name: "Alice (local)", role: "OWNER",
    scopes: ["wisdom:read", "wisdom:install", "wisdom:publish", "wisdom:moderate", "wisdom:admin"] },
  bob: { sub: "user-bob", name: "Bob (local)", role: "MEMBER",
    scopes: ["wisdom:read", "wisdom:install", "wisdom:publish"] },
};
const NAMES = Object.fromEntries(Object.values(USERS).map((u) => [u.sub, u.name]));
const TTL_SECONDS = 24 * 3600;
const key = randomBytes(32);

const b64 = (value) => Buffer.from(value).toString("base64url");
const sign = (data) => createHmac("sha256", key).update(data).digest();

function mint(user) {
  const now = Math.floor(Date.now() / 1000);
  const header = b64(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const payload = b64(JSON.stringify({
    sub: user.sub, org_id: ORG_ID, org_role: user.role, wisdom_scopes: user.scopes,
    iat: now, nbf: now - 5, exp: now + TTL_SECONDS, iss: "cw-local-fake-portal",
  }));
  return `${header}.${payload}.${b64(sign(`${header}.${payload}`))}`;
}

function verify(authorization) {
  const token = (authorization ?? "").replace(/^Bearer\s+/i, "").trim();
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const expected = sign(`${parts[0]}.${parts[1]}`);
  const given = Buffer.from(parts[2], "base64url");
  if (given.length !== expected.length || !timingSafeEqual(given, expected)) return null;
  const claims = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8"));
  return claims.exp > Date.now() / 1000 ? claims : null;
}

const send = (res, status, body) => {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
};

// A stand-in model. It never thinks; it just answers so the demo UI is usable.
const MODEL = "local-demo";
const REPLY = "This is a local demo model. Collective Wisdom works without it: " +
  "open Collective Wisdom in the sidebar to share and install skills.";

function chat(req, res) {
  let raw = "";
  req.on("data", (chunk) => { raw += chunk; });
  req.on("end", () => {
    let body = {};
    try { body = JSON.parse(raw); } catch { /* empty */ }
    const id = `chatcmpl-local-${Date.now()}`;
    const created = Math.floor(Date.now() / 1000);
    const usage = { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 };
    if (!body.stream) {
      return send(res, 200, { id, object: "chat.completion", created, model: MODEL, usage,
        choices: [{ index: 0, finish_reason: "stop", message: { role: "assistant", content: REPLY } }] });
    }
    res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
    const chunk = (delta, finish = null, extra = {}) => res.write(`data: ${JSON.stringify({
      id, object: "chat.completion.chunk", created, model: MODEL,
      choices: [{ index: 0, delta, finish_reason: finish }], ...extra })}\n\n`);
    chunk({ role: "assistant", content: REPLY });
    chunk({}, "stop", { usage });
    res.end("data: [DONE]\n\n");
  });
}

const server = createServer((req, res) => {
  const path = new URL(req.url ?? "/", "http://x").pathname;
  if (path === "/healthz") return send(res, 200, { ok: true });
  if (path === "/v1/models") {
    return send(res, 200, { object: "list", data: [{ id: MODEL, object: "model", owned_by: "local" }] });
  }
  if (path === "/v1/chat/completions" && req.method === "POST") return chat(req, res);
  const claims = verify(req.headers.authorization);
  if (path === "/api/oauth/account" && req.method === "GET") {
    if (!claims) return send(res, 401, { error: "invalid_token" });
    return send(res, 200, {
      user: { id: claims.sub, name: NAMES[claims.sub] ?? null },
      organisation: { id: ORG_ID, name: ORG_NAME },
    });
  }
  if (path === "/api/v1/wisdom/publishers" && req.method === "POST") {
    if (!claims) return send(res, 401, { error: "invalid_token" });
    let raw = "";
    req.on("data", (chunk) => { raw += chunk; });
    req.on("end", () => {
      let refs = [];
      try { refs = JSON.parse(raw).references ?? []; } catch { /* empty */ }
      // Ids are not known per version here; report the team with no name, which
      // the plugin renders as "A teammate" (it never falls back to raw ids).
      send(res, 200, refs.map((r) => ({
        skill_id: r.skill_id, version: r.version, published_by_user_id: "unknown",
        publisher_display_name: null, team_display_name: ORG_NAME,
      })));
    });
    return;
  }
  send(res, 404, { error: "not_found" });
});

server.listen(0, "127.0.0.1", () => {
  const { port } = server.address();
  const tokens = Object.fromEntries(Object.entries(USERS).map(([n, u]) => [n, mint(u)]));
  process.stdout.write(`${JSON.stringify({ url: `http://127.0.0.1:${port}`, tokens })}\n`);
});
