/**
 * Unit tests for extensions/pi/agent-link.ts.
 *
 * The real TypeScript extension is loaded through jiti with the same aliases
 * Pi uses, registered against a fake ExtensionAPI, and executed against a fake
 * loopback HTTP server.  No model call is made and no parent host is needed.
 *
 * Run: node tests/pi/extension_unit.test.mjs
 */
import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { existsSync } from "node:fs";
import { appendFile, mkdir, mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { loadExtension, fakePi, makePiCtx, fireLifecycle, sleep, waitFor } from "./harness.mjs";

const TOKEN = "unit-test-token-0123456789";
const ORIGINAL_CREDENTIALS_ENV = process.env.AGENT_LINK_CREDENTIALS;

const { factory } = await loadExtension();
const { pi, tools, lifecycle, sent, logs } = fakePi();
factory(pi);

function respondJson(res, status, payload) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(payload));
}

async function startServer(handler, options = {}) {
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const request = {
        method: req.method,
        url: req.url,
        headers: req.headers,
        body: Buffer.concat(chunks).toString("utf8"),
      };
      requests.push(request);
      handler(req, res, request);
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const dir = await mkdtemp(path.join(tmpdir(), "agent-link-pi-"));
  const file = path.join(dir, "credentials.json");
  const endpoint = options.endpoint ?? `http://127.0.0.1:${server.address().port}`;
  await writeFile(
    file,
    JSON.stringify({
      protocol: options.protocol ?? "agent-link/1",
      endpoint,
      token: options.token ?? TOKEN,
      binding_id: "unit-b",
    }),
  );
  process.env.AGENT_LINK_CREDENTIALS = file;
  return {
    server,
    requests,
    file,
    async close() {
      if (ORIGINAL_CREDENTIALS_ENV === undefined) delete process.env.AGENT_LINK_CREDENTIALS;
      else process.env.AGENT_LINK_CREDENTIALS = ORIGINAL_CREDENTIALS_ENV;
      server.closeAllConnections?.();
      await new Promise((resolve) => server.close(resolve));
      await rm(dir, { recursive: true, force: true });
    },
  };
}

test("registers exactly the scoped child tools", () => {
  assert.deepEqual(
    [...tools.keys()].sort(),
    ["ask_parent", "check_parent_request", "report_parent"],
  );
  const askTimeout = tools.get("ask_parent").parameters.properties.timeout;
  assert.equal(askTimeout.maximum, 3600, "ask timeout stays within the host-service bound");
  assert.equal(askTimeout.minimum, 0.001);
});

test("report_parent posts to the scoped endpoint with the bearer token", async (t) => {
  const host = await startServer((_req, res) =>
    respondJson(res, 200, {
      result: { delivery_state: "host_accepted", idempotent: false },
      exit_code: 0,
    }),
  );
  t.after(() => host.close());
  const result = await tools.get("report_parent").execute("call-1", {
    message_id: "m-1",
    text: "progress",
  });
  const request = host.requests[0];
  assert.equal(request.method, "POST");
  assert.equal(request.url, "/v1/report");
  assert.equal(request.headers.authorization, `Bearer ${TOKEN}`);
  assert.deepEqual(JSON.parse(request.body), { message_id: "m-1", text: "progress" });
  assert.equal(result.details.delivery_state, "host_accepted");
  assert.match(result.content[0].text, /host_accepted/);
});

test("ask_parent returns the actual parent answer", async (t) => {
  const host = await startServer((_req, res) =>
    respondJson(res, 200, {
      result: {
        request_id: "r-1",
        status: "replied",
        answer: "option A",
        late: false,
        delivery_state: "host_accepted",
      },
      exit_code: 0,
    }),
  );
  t.after(() => host.close());
  const result = await tools.get("ask_parent").execute("call-2", {
    request_id: "r-1",
    message_id: "q-1",
    question: "A or B?",
    timeout: 5,
  });
  assert.equal(host.requests[0].url, "/v1/ask");
  assert.deepEqual(JSON.parse(host.requests[0].body), {
    request_id: "r-1",
    message_id: "q-1",
    question: "A or B?",
    timeout: 5,
  });
  assert.equal(result.details.answer, "option A");
  assert.match(result.content[0].text, /option A/);
  assert.doesNotMatch(result.content[0].text, /timed_out/);
});

test("ask_parent reports timed_out and keeps a stored late answer visible", async (t) => {
  const host = await startServer((_req, res) =>
    respondJson(res, 200, {
      result: { request_id: "r-2", status: "timed_out", answer: "late answer", late: true },
      exit_code: 3,
    }),
  );
  t.after(() => host.close());
  const result = await tools.get("ask_parent").execute("call-3", {
    request_id: "r-2",
    message_id: "q-2",
    question: "A or B?",
    timeout: 1,
  });
  assert.equal(result.details.status, "timed_out");
  assert.equal(result.details.late, true);
  assert.match(result.content[0].text, /timed_out/);
  assert.match(result.content[0].text, /late answer/);
  assert.match(result.content[0].text, /check_parent_request/);
});

test("check_parent_request reads the persisted request state", async (t) => {
  const host = await startServer((_req, res) =>
    respondJson(res, 200, {
      result: {
        request_id: "r-3",
        status: "timed_out",
        late: true,
        answer: "stored later",
        delivery_state: "host_accepted",
      },
      exit_code: 0,
    }),
  );
  t.after(() => host.close());
  const result = await tools.get("check_parent_request").execute("call-4", { request_id: "r-3" });
  assert.equal(host.requests[0].url, "/v1/get-request");
  assert.deepEqual(JSON.parse(host.requests[0].body), { request_id: "r-3" });
  assert.match(result.content[0].text, /status=timed_out/);
  assert.match(result.content[0].text, /stored later/);
});

test("host-service HTTP errors surface as tool errors", async (t) => {
  const host = await startServer((_req, res) =>
    respondJson(res, 404, { error: "not_found", message: "unknown request_id", exit_code: 6 }),
  );
  t.after(() => host.close());
  await assert.rejects(
    tools.get("check_parent_request").execute("call-5", { request_id: "nope" }),
    /not_found/,
  );
});

test("credentials reject non-loopback endpoints and wrong protocols", async (t) => {
  const dir = await mkdtemp(path.join(tmpdir(), "agent-link-pi-creds-"));
  const file = path.join(dir, "credentials.json");
  t.after(async () => {
    if (ORIGINAL_CREDENTIALS_ENV === undefined) delete process.env.AGENT_LINK_CREDENTIALS;
    else process.env.AGENT_LINK_CREDENTIALS = ORIGINAL_CREDENTIALS_ENV;
    await rm(dir, { recursive: true, force: true });
  });
  process.env.AGENT_LINK_CREDENTIALS = file;
  await writeFile(
    file,
    JSON.stringify({ protocol: "agent-link/1", endpoint: "http://192.0.2.10:8080", token: "x".repeat(32) }),
  );
  await assert.rejects(
    tools.get("report_parent").execute("call-6", { message_id: "m", text: "x" }),
    /loopback/,
  );
  await writeFile(
    file,
    JSON.stringify({ protocol: "agent-link/2", endpoint: "http://127.0.0.1:8080", token: "x".repeat(32) }),
  );
  await assert.rejects(
    tools.get("report_parent").execute("call-7", { message_id: "m", text: "x" }),
    /unsupported protocol/,
  );
  delete process.env.AGENT_LINK_CREDENTIALS;
  await assert.rejects(
    tools.get("report_parent").execute("call-8", { message_id: "m", text: "x" }),
    /AGENT_LINK_CREDENTIALS/,
  );
});

test("an aborted ask points back to the same request id", async (t) => {
  const host = await startServer(() => {
    // Never respond; the abort signal must end the wait.
  });
  t.after(() => host.close());
  const controller = new AbortController();
  const promise = tools.get("ask_parent").execute(
    "call-9",
    { request_id: "r-abort", message_id: "q-abort", question: "q", timeout: 0.001 },
    controller.signal,
  );
  setTimeout(() => controller.abort(), 50);
  await assert.rejects(promise, /did not finish/);
  await assert.rejects(promise, /same IDs|check the same IDs|persisted/);
});

// -- parent bridge watcher -------------------------------------------------

const WATCHER_ENV = ["AGENT_LINK_BRIDGE_FILE", "AGENT_LINK_BINDING", "AGENT_LINK_REGISTRY"];

/** Pin the watcher env for one test and restore the process env afterwards. */
function setWatcherEnv(t, vars) {
  const saved = {};
  for (const key of WATCHER_ENV) {
    saved[key] = process.env[key];
    if (key in vars) process.env[key] = vars[key];
    else delete process.env[key];
  }
  t.after(() => {
    for (const key of WATCHER_ENV) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
  });
}

function eventLine({ binding, threadId = "parent", kind = "report", messageId, prompt }) {
  return (
    JSON.stringify({
      v: 1,
      ts: "2026-01-01T00:00:00.000Z",
      source: "service",
      binding_id: binding,
      thread_id: threadId,
      kind,
      message_id: messageId,
      prompt,
    }) + "\n"
  );
}

async function writeRegistryEntry(
  dir,
  { binding, root, framework = "pi", sessionId, updatedAt = "2026-01-01T00:00:00.000Z" },
) {
  await writeFile(
    path.join(dir, `${binding}.json`),
    JSON.stringify({
      version: 1,
      binding_id: binding,
      run_id: `r-${binding}`,
      native_task_id: null,
      root,
      parent: { framework, session_id: sessionId },
      child: { framework: "pi", session_id: "child" },
      config_digest: "d".repeat(64),
      created_at: updatedAt,
      updated_at: updatedAt,
    }),
  );
}

/** Stop the watcher and wait until the bridge lock is actually released. */
async function shutdownWatcher(ctx, bridgeFile) {
  await fireLifecycle(lifecycle, "session_shutdown", ctx);
  await waitFor(() => !existsSync(bridgeFile + ".lock"));
}

test("watcher: env bridge injects events (idle plain, busy followUp)", async (t) => {
  const dir = await mkdtemp(path.join(tmpdir(), "agent-link-pi-watch-env-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const binding = "pi-env-b";
  const bridgeFile = path.join(dir, "omp-bridge", `${binding}.jsonl`);
  await mkdir(path.dirname(bridgeFile), { recursive: true });
  await writeFile(
    bridgeFile,
    eventLine({ binding, messageId: "m-1", prompt: "env-prompt-A" }),
  );
  setWatcherEnv(t, { AGENT_LINK_BRIDGE_FILE: bridgeFile, AGENT_LINK_BINDING: binding });
  const base = sent.length;
  let idle = true;
  const { ctx, notifies } = makePiCtx({ isIdle: () => idle });
  await fireLifecycle(lifecycle, "session_start", ctx);
  await waitFor(() => sent.length > base);
  assert.equal(sent[base].text, "env-prompt-A");
  assert.equal(sent[base].options, undefined, "an idle session gets a plain injection");
  idle = false;
  await appendFile(bridgeFile, eventLine({ binding, messageId: "m-2", prompt: "env-prompt-B" }));
  await waitFor(() => sent.length > base + 1);
  assert.equal(sent[base + 1].text, "env-prompt-B");
  assert.deepEqual(
    sent[base + 1].options,
    { deliverAs: "followUp" },
    "a busy session queues the event instead of interrupting",
  );
  assert.ok(notifies.length >= 2, "each injection is notified");
  await shutdownWatcher(ctx, bridgeFile);
});

test("watcher: auto-claim matches this session and pins thread_id", async (t) => {
  const dir = await mkdtemp(path.join(tmpdir(), "agent-link-pi-watch-claim-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const registry = path.join(dir, "registry");
  const root = path.join(dir, "root");
  const binding = "pi-auto-b";
  const bridgeFile = path.join(root, "omp-bridge", `${binding}.jsonl`);
  await mkdir(registry, { recursive: true });
  await mkdir(path.dirname(bridgeFile), { recursive: true });
  const sessionId = "pi-auto";
  await writeRegistryEntry(registry, { binding, root, sessionId });
  await writeFile(
    bridgeFile,
    eventLine({ binding, threadId: "not-me", messageId: "m-x", prompt: "auto-wrong" }) +
      eventLine({ binding, threadId: sessionId, messageId: "m-y", prompt: "auto-ok" }),
  );
  setWatcherEnv(t, { AGENT_LINK_REGISTRY: registry });
  const base = sent.length;
  const { ctx } = makePiCtx({ sessionId });
  await fireLifecycle(lifecycle, "session_start", ctx);
  await waitFor(() => sent.length > base);
  assert.deepEqual(sent.slice(base).map((entry) => entry.text), ["auto-ok"]);
  assert.ok(
    logs.some((line) => line.includes("skipping invalid event")),
    `foreign thread_id is skipped: ${logs}`,
  );
  await shutdownWatcher(ctx, bridgeFile);
});

test("watcher: auto-claim ignores other sessions and other frameworks", async (t) => {
  const dir = await mkdtemp(path.join(tmpdir(), "agent-link-pi-watch-mismatch-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const registry = path.join(dir, "registry");
  await mkdir(registry, { recursive: true });
  await writeRegistryEntry(registry, {
    binding: "pi-other-b",
    root: path.join(dir, "root-a"),
    sessionId: "someone-else",
  });
  await writeRegistryEntry(registry, {
    binding: "pi-omp-b",
    root: path.join(dir, "root-b"),
    framework: "omp",
    sessionId: "pi-mismatch",
  });
  setWatcherEnv(t, { AGENT_LINK_REGISTRY: registry });
  const base = sent.length;
  const logsBase = logs.length;
  const { ctx } = makePiCtx({ sessionId: "pi-mismatch" });
  await fireLifecycle(lifecycle, "session_start", ctx);
  await sleep(1200); // the immediate claim attempt runs; nothing may attach
  assert.equal(sent.length, base, "no event may be injected");
  assert.equal(logs.length, logsBase, `no watcher errors expected: ${logs.slice(logsBase)}`);
  assert.ok(
    !existsSync(path.join(dir, "root-a", "omp-bridge", "pi-other-b.jsonl.lock")) &&
      !existsSync(path.join(dir, "root-b", "omp-bridge", "pi-omp-b.jsonl.lock")),
    "no bridge lock may be taken",
  );
  await fireLifecycle(lifecycle, "session_shutdown", ctx);
});

test("watcher: auto-claim discovers a binding registered after session start", async (t) => {
  const dir = await mkdtemp(path.join(tmpdir(), "agent-link-pi-watch-late-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const registry = path.join(dir, "registry");
  const root = path.join(dir, "root");
  const binding = "pi-late-b";
  const bridgeFile = path.join(root, "omp-bridge", `${binding}.jsonl`);
  await mkdir(registry, { recursive: true });
  await mkdir(path.dirname(bridgeFile), { recursive: true });
  const sessionId = "pi-late";
  setWatcherEnv(t, { AGENT_LINK_REGISTRY: registry });
  const base = sent.length;
  const { ctx } = makePiCtx({ sessionId });
  await fireLifecycle(lifecycle, "session_start", ctx);
  await sleep(300); // no claim exists yet; the rescan timer is pending
  assert.equal(sent.length, base);
  await writeRegistryEntry(registry, { binding, root, sessionId });
  await writeFile(
    bridgeFile,
    eventLine({ binding, threadId: sessionId, messageId: "m-late", prompt: "late-1" }),
  );
  await waitFor(() => sent.length > base, { timeoutMs: 6000 });
  assert.equal(sent[base].text, "late-1");
  await shutdownWatcher(ctx, bridgeFile);
});

test("watcher: a failing injection blocks later events until it succeeds", async (t) => {
  const dir = await mkdtemp(path.join(tmpdir(), "agent-link-pi-watch-fail-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const binding = "pi-fail-b";
  const bridgeFile = path.join(dir, "omp-bridge", `${binding}.jsonl`);
  await mkdir(path.dirname(bridgeFile), { recursive: true });
  await writeFile(
    bridgeFile,
    eventLine({ binding, messageId: "m-a", prompt: "event-A" }) +
      eventLine({ binding, messageId: "m-b", prompt: "event-B" }) +
      eventLine({ binding, messageId: "m-c", prompt: "event-C" }),
  );
  setWatcherEnv(t, { AGENT_LINK_BRIDGE_FILE: bridgeFile, AGENT_LINK_BINDING: binding });
  const base = sent.length;
  const logsBase = logs.length;
  pi.failOn = "event-A";
  const { ctx } = makePiCtx();
  await fireLifecycle(lifecycle, "session_start", ctx);
  await waitFor(() =>
    logs.slice(logsBase).some((line) => line.includes("injection failed")),
  );
  assert.equal(sent.length, base, "the failing event blocks the ones behind it");
  await waitFor(() => sent.length >= base + 3);
  assert.deepEqual(
    sent.slice(base).map((entry) => entry.text),
    ["event-A", "event-B", "event-C"],
    "after the retry the order is preserved",
  );
  await shutdownWatcher(ctx, bridgeFile);
});
