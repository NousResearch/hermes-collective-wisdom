// Local stand-in for Upstash QStash (background jobs), for the Collective
// Wisdom dev launcher. Replaces the `public.ecr.aws/upstash/qstash` image.
//
// The Portal only publishes jobs for billing/agent housekeeping the demo never
// triggers. This accepts every publish and drops it; nothing is delivered.
// Binds 127.0.0.1 on a random port and prints one JSON line: {"url": ...}.

import { createServer } from "node:http";

let counter = 0;
const server = createServer((req, res) => {
  req.resume();
  req.on("end", () => {
    const path = new URL(req.url ?? "/", "http://x").pathname;
    res.writeHead(200, { "content-type": "application/json" });
    if (req.method === "GET") return res.end("[]");
    if (path.includes("/batch")) return res.end(JSON.stringify([{ messageId: `local-${++counter}` }]));
    res.end(JSON.stringify({ messageId: `local-${++counter}`, scheduleId: `local-${counter}` }));
  });
});

server.listen(0, "127.0.0.1", () => {
  process.stdout.write(`${JSON.stringify({ url: `http://127.0.0.1:${server.address().port}` })}\n`);
});
