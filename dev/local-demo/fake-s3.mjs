// Tiny in-memory S3 stand-in for the Collective Wisdom dev launcher.
//
// The Gateway only uploads and downloads whole objects (PutObject/GetObject,
// directly or through presigned URLs), so that is all this implements:
//   PUT  /<bucket>            create bucket (accepted, no-op)
//   PUT  /<bucket>/<key>      store bytes
//   GET  /<bucket>/<key>      return bytes
//   HEAD /<bucket>[/<key>]    exists check
//
// Binds 127.0.0.1 only, keeps everything in memory, and checks no signatures:
// it exists only for a throwaway local run. Prints {"url"} on start.

import { createHash } from "node:crypto";
import { createServer } from "node:http";

const objects = new Map();

// Decode the SDK's aws-chunked streaming body ("<hex>;chunk-signature=..\r\n<data>\r\n").
function decodeAwsChunked(buf) {
  const out = [];
  let i = 0;
  while (i < buf.length) {
    const lineEnd = buf.indexOf("\r\n", i);
    if (lineEnd < 0) break;
    const size = parseInt(buf.subarray(i, lineEnd).toString().split(";")[0], 16);
    if (!size) break;
    out.push(buf.subarray(lineEnd + 2, lineEnd + 2 + size));
    i = lineEnd + 2 + size + 2;
  }
  return Buffer.concat(out);
}

const server = createServer((req, res) => {
  const path = decodeURIComponent(new URL(req.url ?? "/", "http://x").pathname);
  const isObject = path.split("/").filter(Boolean).length >= 2;
  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", () => {
    if (req.method === "PUT") {
      if (isObject) {
        let body = Buffer.concat(chunks);
        const sha = String(req.headers["x-amz-content-sha256"] ?? "");
        if (sha.startsWith("STREAMING-") ||
            String(req.headers["content-encoding"] ?? "").includes("aws-chunked")) {
          body = decodeAwsChunked(body);
        }
        objects.set(path, body);
        res.writeHead(200, { ETag: `"${createHash("md5").update(body).digest("hex")}"` });
      } else {
        res.writeHead(200);
      }
      return res.end();
    }
    if (req.method === "GET" || req.method === "HEAD") {
      if (!isObject) { res.writeHead(200); return res.end(); }
      const body = objects.get(path);
      if (!body) {
        res.writeHead(404, { "content-type": "application/xml" });
        return res.end(req.method === "HEAD" ? undefined
          : "<Error><Code>NoSuchKey</Code><Message>not found</Message></Error>");
      }
      res.writeHead(200, { "content-length": body.length,
        "content-type": "application/octet-stream" });
      return res.end(req.method === "HEAD" ? undefined : body);
    }
    res.writeHead(405); res.end();
  });
});

server.listen(0, "127.0.0.1", () => {
  process.stdout.write(`${JSON.stringify({ url: `http://127.0.0.1:${server.address().port}` })}\n`);
});
