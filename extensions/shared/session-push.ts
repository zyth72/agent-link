/** Session-owned push endpoint. No timers, registry scanning or file inbox. */
import http from "node:http";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { chmod, mkdir, open, readFile, unlink } from "node:fs/promises";
import path from "node:path";
import os from "node:os";

const PROTOCOL = "agent-link-push/1";
const MAX_BODY = 65536;
const validId = (s: unknown): s is string => typeof s === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(s);
export type PushListener = { close(): Promise<void> };

export async function startSessionPush(options: {
  framework: "pi" | "omp";
  sessionId: string;
  inject(prompt: string): void | Promise<void>;
}): Promise<PushListener> {
  const { framework, sessionId, inject } = options;
  if (!validId(sessionId)) throw new Error("Agent Link: invalid session id");
  const configured = process.env.AGENT_LINK_SESSIONS || path.join(os.homedir(), ".agent-link", "sessions");
  const base = configured === "~" ? os.homedir() : configured.startsWith("~/") ? path.join(os.homedir(), configured.slice(2)) : configured;
  const directory = path.join(path.resolve(base), framework);
  await mkdir(directory, { recursive: true, mode: 0o700 });
  await chmod(directory, 0o700);
  const descriptorPath = path.join(directory, `${sessionId}.json`);
  const token = randomBytes(32).toString("hex");
  let closed = false;
  let queue = Promise.resolve();
  let pending = 0;
  const server = http.createServer((req, res) => {
    const respond = (status: number, body: object) => {
      if (!res.destroyed) res.writeHead(status, { "Content-Type": "application/json" }).end(JSON.stringify(body));
    };
    const authorization = Buffer.from(req.headers.authorization ?? "");
    const expected = Buffer.from(`Bearer ${token}`);
    if (authorization.length !== expected.length || !timingSafeEqual(authorization, expected)) {
      respond(403, { error: "unauthorized" }); req.resume(); return;
    }
    if (req.method !== "POST" || req.url !== "/v1/notify") {
      respond(404, { error: "unknown operation" }); req.resume(); return;
    }
    const size = Number(req.headers["content-length"]);
    if (!Number.isInteger(size) || size <= 0 || size > MAX_BODY) {
      respond(413, { error: "invalid body size" }); req.resume(); return;
    }
    let bytes = 0;
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => {
      bytes += chunk.length;
      if (bytes > MAX_BODY) { respond(413, { error: "body too large" }); req.destroy(); }
      else chunks.push(chunk);
    });
    req.on("error", () => {});
    req.on("end", () => {
      let event: Record<string, unknown>;
      try {
        event = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)));
        if (!event || typeof event !== "object" || Array.isArray(event) ||
            event.protocol !== PROTOCOL || event.framework !== framework || event.session_id !== sessionId ||
            !validId(event.binding_id) || !validId(event.run_id) || !validId(event.message_id) ||
            !["parent", "child"].includes(event.role as string) || typeof event.prompt !== "string" || !event.prompt.trim()) {
          throw new Error("invalid notification identity or body");
        }
      } catch { respond(400, { error: "invalid notification" }); return; }
      if (closed || pending >= 64) { respond(503, { error: "session unavailable or overloaded" }); return; }
      pending += 1;
      // Serialize submissions, including asynchronous framework rejection.
      queue = queue.then(async () => {
        if (closed) { pending -= 1; respond(409, { error: "session closed" }); return; }
        try {
          await inject(event.prompt as string);
          respond(200, { accepted: true, session_id: sessionId });
        } catch { respond(502, { error: "injection failed or uncertain" }); }
        finally { pending -= 1; }
      });
    });
  });
  server.requestTimeout = 10000;
  server.headersTimeout = 10000;
  server.timeout = 10000;
  const closeServer = async () => {
    server.closeAllConnections();
    await new Promise<void>(resolve => server.close(() => resolve()));
  };
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => { server.removeListener("error", reject); resolve(); });
  });
  const address = server.address();
  if (!address || typeof address === "string") { await closeServer(); throw new Error("no listener address"); }
  let ownsDescriptor = false;
  try {
    const fd = await open(descriptorPath, "wx", 0o600);
    ownsDescriptor = true;
    try { await fd.writeFile(JSON.stringify({ protocol: PROTOCOL, framework, session_id: sessionId,
      endpoint: `http://127.0.0.1:${address.port}`, token, pid: process.pid })); await fd.sync(); }
    finally { await fd.close(); }
  } catch (error) {
    await closeServer();
    if (ownsDescriptor) await unlink(descriptorPath).catch(() => {});
    throw error; // Duplicate/stale descriptor requires explicit operator recovery.
  }
  return { async close() {
    if (closed) return;
    closed = true;
    await closeServer();
    await queue;
    try {
      const own = JSON.parse(await readFile(descriptorPath, "utf8"));
      if (own.token === token) await unlink(descriptorPath);
    } catch { /* Missing or replaced descriptor belongs to another lifecycle. */ }
  }};
}
