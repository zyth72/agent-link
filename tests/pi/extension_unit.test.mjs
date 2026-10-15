/**
 * Unit tests for extensions/pi/agent-link.ts and the shared push listener.
 *
 * The real TypeScript extension is loaded through jiti with the same aliases
 * Pi uses, registered against a fake ExtensionAPI, and executed against a fake
 * loopback HTTP server.  The session-owned push listener is exercised over a
 * real loopback socket (real HTTP requests, no model, no parent host).
 *
 * Run: node tests/pi/extension_unit.test.mjs
 */
import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import {
  loadExtension,
  loadSessionPush,
  fakePi,
  makePiCtx,
  fireLifecycle,
  sleep,
  waitFor,
} from "./harness.mjs";

const TOKEN = "unit-test-token-0123456789";
const ORIGINAL_CREDENTIALS_ENV = process.env.AGENT_LINK_CREDENTIALS;

const { factory } = await loadExtension();
const { startSessionPush } = await loadSessionPush();
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
    ["answer_parent", "ask_parent", "check_parent_request", "report_parent"],
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

test("answer_parent posts the parent-initiated answer to /v1/answer", async (t) => {
  const host = await startServer((_req, res) =>
    respondJson(res, 200, {
      result: { status: "answered", late: false, idempotent: false },
      exit_code: 0,
    }),
  );
  t.after(() => host.close());
  const result = await tools.get("answer_parent").execute("call-answer", {
    request_id: "rq-1",
    message_id: "am-1",
    answer: "option A",
  });
  assert.equal(host.requests[0].url, "/v1/answer");
  assert.deepEqual(JSON.parse(host.requests[0].body), {
    request_id: "rq-1",
    message_id: "am-1",
    answer: "option A",
  });
  assert.equal(result.details.status, "answered");
  assert.match(result.content[0].text, /Answered parent request rq-1/);
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

// -- session-owned push listener -------------------------------------------

/** Point AGENT_LINK_SESSIONS at a fresh directory for one test. */
async function useSessions(t) {
  const dir = await mkdtemp(path.join(tmpdir(), "agent-link-sessions-"));
  const previous = process.env.AGENT_LINK_SESSIONS;
  process.env.AGENT_LINK_SESSIONS = dir;
  t.after(async () => {
    if (previous === undefined) delete process.env.AGENT_LINK_SESSIONS;
    else process.env.AGENT_LINK_SESSIONS = previous;
    await rm(dir, { recursive: true, force: true });
  });
  return dir;
}

/** Fire session_start through the awaited lifecycle and wait for the descriptor. */
async function startSession(t, sessionId, isIdle = () => true) {
  const { ctx } = makePiCtx({ sessionId, isIdle });
  await fireLifecycle(lifecycle, "session_start", ctx);
  const descriptorPath = path.join(process.env.AGENT_LINK_SESSIONS, "pi", `${sessionId}.json`);
  await waitFor(() => existsSync(descriptorPath));
  const descriptor = JSON.parse(await readFile(descriptorPath, "utf8"));
  return { ctx, descriptor, descriptorPath };
}

function stopSession(ctx) {
  return fireLifecycle(lifecycle, "session_shutdown", ctx);
}

function pushEvent(descriptor, overrides = {}) {
  return {
    protocol: "agent-link-push/1",
    framework: "pi",
    session_id: descriptor.session_id,
    binding_id: "b-1",
    run_id: "r-1",
    message_id: "m-1",
    role: "parent",
    prompt: "pushed prompt",
    ...overrides,
  };
}

async function postEvent(descriptor, event, options = {}) {
  const body = options.rawBody !== undefined ? options.rawBody : Buffer.from(JSON.stringify(event));
  const headers = { "Content-Type": "application/json", "Content-Length": String(body.length) };
  if (options.authorization !== null) {
    headers.Authorization = `Bearer ${options.authorization ?? descriptor.token}`;
  }
  return new Promise((resolve, reject) => {
    const request = http.request(
      `${descriptor.endpoint}${options.path ?? "/v1/notify"}`,
      { method: options.method ?? "POST", headers },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          let parsed = {};
          try {
            parsed = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          } catch {
            parsed = {};
          }
          resolve({ status: response.statusCode, body: parsed });
        });
      },
    );
    request.on("error", reject);
    request.end(body);
  });
}

test("push: rejects absent and wrong bearer tokens without injecting", async (t) => {
  await useSessions(t);
  const { ctx, descriptor } = await startSession(t, "pi-auth");
  t.after(() => stopSession(ctx));
  const base = sent.length;
  const absent = await postEvent(descriptor, pushEvent(descriptor), { authorization: null });
  assert.equal(absent.status, 403);
  const wrong = await postEvent(descriptor, pushEvent(descriptor), { authorization: "nope" });
  assert.equal(wrong.status, 403);
  assert.equal(sent.length, base, "rejected requests must not inject");
  const accepted = await postEvent(descriptor, pushEvent(descriptor, { prompt: "auth-ok" }));
  assert.equal(accepted.status, 200);
  assert.deepEqual(accepted.body, { accepted: true, session_id: "pi-auth" });
  assert.equal(sent[base].text, "auth-ok");
});

test("push: rejects cross-session and cross-framework events", async (t) => {
  await useSessions(t);
  const { ctx, descriptor } = await startSession(t, "pi-identity");
  t.after(() => stopSession(ctx));
  const base = sent.length;
  for (const event of [
    pushEvent(descriptor, { session_id: "someone-else" }),
    pushEvent(descriptor, { framework: "omp" }),
    pushEvent(descriptor, { role: "observer" }),
    pushEvent(descriptor, { protocol: "agent-link-push/99" }),
  ]) {
    const response = await postEvent(descriptor, event);
    assert.equal(response.status, 400, JSON.stringify(event));
  }
  assert.equal(sent.length, base, "identity mismatches must not inject");
});

test("push: rejects malformed, incomplete and oversized bodies", async (t) => {
  await useSessions(t);
  const { ctx, descriptor } = await startSession(t, "pi-malformed");
  t.after(() => stopSession(ctx));
  const base = sent.length;
  const malformed = await postEvent(descriptor, {}, { rawBody: Buffer.from("{not json") });
  assert.equal(malformed.status, 400);
  for (const event of [
    pushEvent(descriptor, { prompt: "" }),
    pushEvent(descriptor, { prompt: "   " }),
    pushEvent(descriptor, { message_id: "bad id!" }),
    pushEvent(descriptor, { run_id: "" }),
  ]) {
    assert.equal((await postEvent(descriptor, event)).status, 400, JSON.stringify(event));
  }
  const oversized = await postEvent(descriptor, {}, { rawBody: Buffer.alloc(65537, 0x61) });
  assert.equal(oversized.status, 413);
  const wrongPath = await postEvent(descriptor, pushEvent(descriptor), { path: "/v1/other" });
  assert.equal(wrongPath.status, 404);
  const wrongMethod = await postEvent(descriptor, pushEvent(descriptor), { method: "GET" });
  assert.equal(wrongMethod.status, 404);
  assert.equal(sent.length, base, "invalid bodies must not inject");
});

test("push: an idle session injects plain and a busy one queues a follow-up", async (t) => {
  await useSessions(t);
  let idle = true;
  const { ctx, descriptor } = await startSession(t, "pi-activity", () => idle);
  t.after(() => stopSession(ctx));
  const base = sent.length;
  assert.equal((await postEvent(descriptor, pushEvent(descriptor, { prompt: "idle-event" }))).status, 200);
  assert.equal(sent[base].text, "idle-event");
  assert.equal(sent[base].options, undefined, "an idle session gets a plain injection");
  idle = false;
  assert.equal((await postEvent(descriptor, pushEvent(descriptor, { message_id: "m-2", prompt: "busy-event" }))).status, 200);
  assert.equal(sent[base + 1].text, "busy-event");
  assert.deepEqual(
    sent[base + 1].options,
    { deliverAs: "followUp" },
    "a busy session queues the event instead of interrupting",
  );
});

test("push: an injection failure returns 502 and does not block later events", async (t) => {
  await useSessions(t);
  const { ctx, descriptor } = await startSession(t, "pi-failure");
  t.after(() => stopSession(ctx));
  const base = sent.length;
  pi.failOn = "boom-event";
  const failed = await postEvent(descriptor, pushEvent(descriptor, { message_id: "m-fail", prompt: "boom-event" }));
  assert.equal(failed.status, 502);
  assert.match(failed.body.error, /injection failed/);
  assert.equal(sent.length, base);
  const accepted = await postEvent(descriptor, pushEvent(descriptor, { message_id: "m-ok", prompt: "later-event" }));
  assert.equal(accepted.status, 200);
  assert.equal(sent[base].text, "later-event");
});

test("push: a pre-existing descriptor refuses a second listener", async (t) => {
  const sessions = await useSessions(t);
  const foreign = {
    protocol: "agent-link-push/1",
    framework: "pi",
    session_id: "pi-dup",
    endpoint: "http://127.0.0.1:1",
    token: "f".repeat(64),
    pid: 999,
  };
  await mkdir(path.join(sessions, "pi"), { recursive: true });
  const descriptorPath = path.join(sessions, "pi", "pi-dup.json");
  await writeFile(descriptorPath, JSON.stringify(foreign));
  const { ctx } = makePiCtx({ sessionId: "pi-dup" });
  await assert.rejects(fireLifecycle(lifecycle, "session_start", ctx), /EEXIST|exist/i);
  assert.deepEqual(
    JSON.parse(await readFile(descriptorPath, "utf8")),
    foreign,
    "a refused listener must not remove the foreign descriptor",
  );
  // The lifecycle chain recovers: a different session still registers.
  const fresh = await startSession(t, "pi-after-dup");
  t.after(() => stopSession(fresh.ctx));
  assert.equal((await postEvent(fresh.descriptor, pushEvent(fresh.descriptor))).status, 200);
});

test("push: session shutdown removes its own descriptor", async (t) => {
  await useSessions(t);
  const { ctx, descriptorPath } = await startSession(t, "pi-shutdown");
  assert.ok(existsSync(descriptorPath));
  await stopSession(ctx);
  await waitFor(() => !existsSync(descriptorPath));
  assert.ok(!existsSync(descriptorPath), "the owned descriptor must be removed");
});

test("push: shutdown leaves a descriptor replaced by another lifecycle", async (t) => {
  await useSessions(t);
  const { ctx, descriptor, descriptorPath } = await startSession(t, "pi-replaced");
  const replaced = { ...descriptor, token: "r".repeat(64) };
  await writeFile(descriptorPath, JSON.stringify(replaced));
  await stopSession(ctx);
  assert.deepEqual(
    JSON.parse(await readFile(descriptorPath, "utf8")),
    replaced,
    "a replaced descriptor belongs to another lifecycle and must survive",
  );
});

test("push: a new session closes the old listener and its endpoint goes stale", async (t) => {
  await useSessions(t);
  const first = await startSession(t, "pi-switch-a");
  assert.equal((await postEvent(first.descriptor, pushEvent(first.descriptor, { prompt: "first" }))).status, 200);
  const second = await startSession(t, "pi-switch-b");
  t.after(() => stopSession(second.ctx));
  assert.ok(!existsSync(first.descriptorPath), "the previous session descriptor is removed");
  await assert.rejects(
    postEvent(first.descriptor, pushEvent(first.descriptor)),
    /ECONNREFUSED|socket hang up|connect/i,
    "the stale endpoint must no longer accept events",
  );
  assert.equal((await postEvent(second.descriptor, pushEvent(second.descriptor, { prompt: "second" }))).status, 200);
  // Re-registering the first session id creates a brand new descriptor.
  const again = await startSession(t, "pi-switch-a");
  t.after(() => stopSession(again.ctx));
  assert.notEqual(again.descriptor.endpoint, first.descriptor.endpoint);
  assert.equal((await postEvent(again.descriptor, pushEvent(again.descriptor, { prompt: "again" }))).status, 200);
});

test("push: uses no timers, registry scanning or bridge files", async (t) => {
  const registry = await mkdtemp(path.join(tmpdir(), "agent-link-registry-"));
  const previousRegistry = process.env.AGENT_LINK_REGISTRY;
  process.env.AGENT_LINK_REGISTRY = registry;
  t.after(() => {
    if (previousRegistry === undefined) delete process.env.AGENT_LINK_REGISTRY;
    else process.env.AGENT_LINK_REGISTRY = previousRegistry;
  });
  const sessions = await useSessions(t);
  let timerCalls = 0;
  const { ctx } = makePiCtx({ sessionId: "pi-notimer" });
  ctx.setInterval = () => {
    timerCalls += 1;
    throw new Error("the push listener must not schedule interval timers");
  };
  ctx.clearTimer = () => {
    timerCalls += 1;
  };
  await fireLifecycle(lifecycle, "session_start", ctx);
  t.after(() => stopSession(ctx));
  assert.equal(timerCalls, 0, "no interval timer may be created");
  assert.deepEqual(await readdir(registry), [], "the registry must not be scanned or written");
  assert.deepEqual(await readdir(path.join(sessions, "pi")), ["pi-notimer.json"]);
  assert.ok(!existsSync(path.join(sessions, "omp-bridge")), "no bridge directory is created");
  assert.ok(!existsSync(path.join(sessions, "inbox")), "no inbox directory is created");
});

test("push module: concurrent submissions serialize and async failures do not wedge", async (t) => {
  await useSessions(t);
  const injected = [];
  let releaseFirst;
  const gate = new Promise((resolve) => {
    releaseFirst = resolve;
  });
  const listener = await startSessionPush({
    framework: "pi",
    sessionId: "mod-order",
    inject: async (prompt) => {
      injected.push(`start:${prompt}`);
      if (prompt === "A") await gate;
      if (prompt === "bad") throw new Error("async injection failure");
      injected.push(`end:${prompt}`);
    },
  });
  t.after(() => listener.close());
  const descriptor = JSON.parse(
    await readFile(path.join(process.env.AGENT_LINK_SESSIONS, "pi", "mod-order.json"), "utf8"),
  );
  const first = postEvent(descriptor, pushEvent(descriptor, { message_id: "m-a", prompt: "A" }));
  await waitFor(() => injected.includes("start:A"));
  const second = postEvent(descriptor, pushEvent(descriptor, { message_id: "m-b", prompt: "B" }));
  await sleep(30);
  assert.deepEqual(injected, ["start:A"], "the second submission must wait for the first");
  releaseFirst();
  const [firstResponse, secondResponse] = await Promise.all([first, second]);
  assert.equal(firstResponse.status, 200);
  assert.equal(secondResponse.status, 200);
  assert.deepEqual(injected, ["start:A", "end:A", "start:B", "end:B"]);
  // An async rejection is reported as 502 and the queue keeps accepting work.
  const bad = await postEvent(descriptor, pushEvent(descriptor, { message_id: "m-bad", prompt: "bad" }));
  assert.equal(bad.status, 502);
  const good = await postEvent(descriptor, pushEvent(descriptor, { message_id: "m-c", prompt: "C" }));
  assert.equal(good.status, 200);
  assert.deepEqual(injected.slice(-2), ["start:C", "end:C"]);
});
