// Local stand-in for Upstash's Redis REST API, for the Collective Wisdom dev
// launcher. Replaces the third-party `hiett/serverless-redis-http` image.
//
// The Portal talks to Redis through @upstash/redis, which sends commands as
// JSON over HTTP. This translates them to the plain Redis protocol and forwards
// them to the official `redis` container on loopback. No dependencies.
//
//   POST /             one command:  ["SET", "k", "v"]       -> {"result": ...}
//   POST /pipeline     many:         [["GET","a"],["GET","b"]] -> [{"result": ...}, ...]
//   POST /multi-exec   same as /pipeline, wrapped in MULTI/EXEC
//
// Usage: node fake-upstash.mjs <redis-port>
// Binds 127.0.0.1 on a random port and prints one JSON line: {"url": ...}.

import { createServer } from "node:http";
import { connect } from "node:net";

const REDIS_PORT = Number(process.argv[2]);
if (!REDIS_PORT) {
  console.error("usage: node fake-upstash.mjs <redis-port>");
  process.exit(2);
}

// ── Minimal RESP client: one connection, commands answered in order ─────────
class Redis {
  constructor(port) {
    this.queue = [];
    this.buffer = Buffer.alloc(0);
    this.socket = connect(port, "127.0.0.1");
    this.socket.on("data", (chunk) => {
      this.buffer = Buffer.concat([this.buffer, chunk]);
      this.drain();
    });
    this.socket.on("error", (error) => {
      for (const pending of this.queue.splice(0)) pending.reject(error);
    });
  }

  send(args) {
    const parts = [`*${args.length}\r\n`];
    for (const arg of args) {
      const value = Buffer.from(arg === null || arg === undefined ? "" : String(arg));
      parts.push(`$${value.length}\r\n`, value, "\r\n");
    }
    return new Promise((resolve, reject) => {
      this.queue.push({ resolve, reject });
      for (const part of parts) this.socket.write(part);
    });
  }

  drain() {
    while (this.queue.length) {
      const parsed = parse(this.buffer, 0);
      if (!parsed) return;
      this.buffer = this.buffer.subarray(parsed.end);
      const pending = this.queue.shift();
      pending.resolve(parsed.value);
    }
  }
}

class RedisError {
  constructor(message) { this.message = message; }
}

function parse(buf, start) {
  const lineEnd = buf.indexOf("\r\n", start);
  if (lineEnd < 0) return null;
  const type = String.fromCharCode(buf[start]);
  const line = buf.toString("utf8", start + 1, lineEnd);
  const next = lineEnd + 2;
  switch (type) {
    case "+": return { value: line, end: next };
    case "-": return { value: new RedisError(line), end: next };
    case ":": return { value: Number(line), end: next };
    case "$": {
      const length = Number(line);
      if (length < 0) return { value: null, end: next };
      if (buf.length < next + length + 2) return null;
      return { value: buf.toString("utf8", next, next + length), end: next + length + 2 };
    }
    case "*": {
      const count = Number(line);
      if (count < 0) return { value: null, end: next };
      const items = [];
      let cursor = next;
      for (let i = 0; i < count; i += 1) {
        const item = parse(buf, cursor);
        if (!item) return null;
        items.push(item.value);
        cursor = item.end;
      }
      return { value: items, end: cursor };
    }
    default:
      throw new Error(`unexpected RESP type ${JSON.stringify(type)}`);
  }
}

// ── Upstash response shape ──────────────────────────────────────────────────
// With the `Upstash-Encoding: base64` header (the SDK default) every string
// in a result is base64; numbers and nulls pass through.
function encode(value, base64) {
  if (!base64) return value;
  if (typeof value === "string") return Buffer.from(value).toString("base64");
  if (Array.isArray(value)) return value.map((item) => encode(item, base64));
  return value;
}

function toEntry(value, base64) {
  if (value instanceof RedisError) return { error: value.message };
  return { result: encode(value, base64) };
}

const redis = new Redis(REDIS_PORT);

async function run(path, body, base64) {
  if (path === "/" || path === "") {
    return { status: 200, json: toEntry(await redis.send(body), base64) };
  }
  if (path === "/pipeline") {
    const results = [];
    for (const command of body) results.push(toEntry(await redis.send(command), base64));
    return { status: 200, json: results };
  }
  if (path === "/multi-exec") {
    await redis.send(["MULTI"]);
    for (const command of body) await redis.send(command);
    const results = await redis.send(["EXEC"]);
    return { status: 200, json: (results ?? []).map((value) => toEntry(value, base64)) };
  }
  // Path-style single command: /get/key, /set/key/value
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { status: 200, json: toEntry(await redis.send(parts), base64) };
}

const server = createServer((req, res) => {
  let raw = "";
  req.on("data", (chunk) => { raw += chunk; });
  req.on("end", async () => {
    const path = new URL(req.url ?? "/", "http://x").pathname.replace(/\/+$/, "") || "/";
    const base64 = String(req.headers["upstash-encoding"] ?? "").toLowerCase() === "base64";
    try {
      const body = raw ? JSON.parse(raw) : [];
      const { status, json } = await run(path, body, base64);
      const entry = Array.isArray(json) ? null : json;
      res.writeHead(entry?.error ? 400 : status, { "content-type": "application/json" });
      res.end(JSON.stringify(json));
    } catch (error) {
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: String(error?.message ?? error) }));
    }
  });
});

server.listen(0, "127.0.0.1", () => {
  process.stdout.write(`${JSON.stringify({ url: `http://127.0.0.1:${server.address().port}` })}\n`);
});
