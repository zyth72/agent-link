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
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { loadExtension, fakePi } from "./harness.mjs";

const TOKEN = "unit-test-token-0123456789";
const ORIGINAL_CREDENTIALS_ENV = process.env.AGENT_LINK_CREDENTIALS;

const { factory } = await loadExtension();
const { pi, tools } = fakePi();
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
