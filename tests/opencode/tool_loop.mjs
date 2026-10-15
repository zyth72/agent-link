/**
 * Driver for tests/test_opencode_extension.py.
 *
 * Loads the real OpenCode plugin (v2 API: default export { id, setup }) under
 * Bun or Node with a fake plugin context and drives either the child tools
 * against a real `python3 -m agent_link.host` service or the bridge watchers
 * against real JSONL bridge files.  Prints one JSON object (last line) with
 * the observed state; `bridge-hold` prints `{"holding":true}` first and then
 * sleeps until it is killed.
 *
 * Usage: bun|node tests/opencode/tool_loop.mjs child-happy|bridge|bridge-hold|bridge-switch|bridge-auto-claim|bridge-auto-claim-late|bridge-fail-retry-same-process
 * Env:   AGENT_LINK_CREDENTIALS, AGENT_LINK_TEST_IDS (JSON),
 *        AGENT_LINK_BRIDGE_FILE, AGENT_LINK_BINDING,
 *        AGENT_LINK_TEST_BRIDGE_FILE, AGENT_LINK_TEST_SESSION_ID,
 *        AGENT_LINK_TEST_FAIL_FILE
 */
import { existsSync, readFileSync, statSync } from "node:fs";
import { fakeOpencode, loadOpencodeExtension, sleep, waitFor } from "./harness.mjs";

const scenario = process.argv[2];
const ids = JSON.parse(process.env.AGENT_LINK_TEST_IDS ?? "{}");
const bridgeFile = process.env.AGENT_LINK_BRIDGE_FILE ?? process.env.AGENT_LINK_TEST_BRIDGE_FILE;
const plugin = await loadOpencodeExtension();
const fake = fakeOpencode();
const cleanup = await plugin.setup(fake.ctx);

function readCursor() {
  if (!bridgeFile || !existsSync(bridgeFile + ".cursor")) return null;
  try {
    return JSON.parse(readFileSync(bridgeFile + ".cursor", "utf8"));
  } catch {
    return null;
  }
}

function cursorOffset() {
  const cursor = readCursor();
  return cursor && Number.isInteger(cursor.offset) ? cursor.offset : null;
}

function fileSize() {
  return bridgeFile && existsSync(bridgeFile) ? statSync(bridgeFile).size : null;
}

function lockToken() {
  if (!bridgeFile || !existsSync(bridgeFile + ".lock")) return null;
  return readFileSync(bridgeFile + ".lock", "utf8").trim();
}

/**
 * The watcher ticks on a real 1s interval (the plugin uses global timers), so
 * stability means: no change in the observable signature for longer than one
 * full tick cycle.
 */
async function driveUntilStable({ timeoutMs = 15000, quietMs = 1400 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let previous = "";
  let since = Date.now();
  while (Date.now() < deadline) {
    await sleep(50);
    const signature = JSON.stringify([fake.sent.length, cursorOffset(), fake.logs.length]);
    if (signature !== previous) {
      previous = signature;
      since = Date.now();
    } else if (Date.now() - since >= quietMs) {
      return;
    }
  }
}

async function run(label, toolName, params) {
  try {
    const result = await fake.tools.get(toolName).execute(params, { sessionID: "tool-session" });
    return { ok: true, text: result.content, details: result.metadata };
  } catch (error) {
    return { ok: false, error: String(error?.message ?? error) };
  }
}

if (scenario === "child-happy") {
  const out = {};
  out.registeredTools = [...fake.tools.keys()];
  out.report = await run("report", "report_parent", {
    message_id: ids.report_message_id,
    text: "e2e progress",
  });
  out.ask = await run("ask", "ask_parent", {
    request_id: ids.request_id,
    message_id: ids.question_message_id,
    question: "auto-reply",
    timeout: 20,
  });
  out.check = await run("check", "check_parent_request", { request_id: ids.request_id });
  // Parent->child channel: poll the pending parent request and answer it.
  out.poll = await run("poll", "poll_parent_requests", {});
  out.answer = await run("answer", "answer_parent", {
    request_id: ids.parent_request_id,
    message_id: ids.parent_answer_message_id,
    answer: "child answer",
  });
  console.log(JSON.stringify(out));
} else if (scenario === "bridge") {
  // Env mode: watchers attach at setup; the prompt hook supplies the session
  // id injections target.
  await fake.firePromptHook("parent");
  const failMarker = process.env.AGENT_LINK_TEST_FAIL_FILE;
  if (failMarker) {
    // A failing injection stops the tick; wait for that tick to finish and to
    // persist the cursor over the events that preceded the failure, then stop
    // (no further ticks: the retry belongs to the next run).
    await waitFor(() => !existsSync(failMarker));
    await waitFor(() => (cursorOffset() ?? 0) > 0);
  } else {
    await driveUntilStable();
  }
  await cleanup();
  console.log(
    JSON.stringify({
      sent: fake.sent,
      cursor: readCursor(),
      size: fileSize(),
      logs: fake.logs,
      refused: fake.logs.some((line) => line.includes("refused")),
      lockExists: lockToken() !== null,
    }),
  );
} else if (scenario === "bridge-hold") {
  await fake.firePromptHook("holder");
  await waitFor(() => (lockToken() ?? "") !== "");
  console.log(JSON.stringify({ holding: true, pid: process.pid, lock: lockToken() }));
  await sleep(600000);
} else if (scenario === "bridge-switch") {
  await fake.firePromptHook("first");
  await waitFor(() => (lockToken() ?? "") !== "");
  const firstToken = lockToken();
  await driveUntilStable();
  const injectedBeforeSwitch = fake.sent.length;
  await fake.firePromptHook("second");
  await waitFor(() => {
    const token = lockToken();
    return token !== null && token !== "" && token !== firstToken;
  });
  const secondToken = lockToken();
  await driveUntilStable();
  const activeTimers = fake.intervals.size;
  const clearedTimers = fake.clearedIntervals.length;
  await cleanup();
  console.log(
    JSON.stringify({
      sent: fake.sent,
      injectedBeforeSwitch,
      clearedTimers,
      activeTimers,
      lockTokens: [firstToken, secondToken],
      lockExists: lockToken() !== null,
      cursor: readCursor(),
      logs: fake.logs,
    }),
  );
} else if (scenario === "bridge-auto-claim" || scenario === "bridge-auto-claim-late") {
  // No AGENT_LINK_BRIDGE_FILE/AGENT_LINK_BINDING: the watcher must resolve the
  // binding itself from the registry by matching the tracked session id.
  await fake.firePromptHook(process.env.AGENT_LINK_TEST_SESSION_ID);
  if (scenario === "bridge-auto-claim-late") {
    // The claim appears after the session became active; the 2s rescan timer
    // must attach and consume the events.
    await waitFor(() => fake.sent.length > 0, { timeoutMs: 30000 });
  }
  await driveUntilStable();
  const lockHeld = lockToken() !== null;
  await cleanup();
  console.log(
    JSON.stringify({
      sent: fake.sent,
      cursor: readCursor(),
      logs: fake.logs,
      lockHeld,
      lockAfterShutdown: lockToken() !== null,
    }),
  );
} else if (scenario === "bridge-fail-retry-same-process") {
  // One-shot injection failure inside one process: the next tick must retry
  // the exact same bytes (regression: the failed line must be restored with
  // its newline, otherwise it merges with the next line and both are lost).
  await fake.firePromptHook("parent");
  await waitFor(() => fake.sent.length >= 3, { timeoutMs: 30000 });
  await driveUntilStable();
  await cleanup();
  console.log(JSON.stringify({ sent: fake.sent, cursor: readCursor(), logs: fake.logs }));
} else {
  throw new Error(`unknown scenario ${scenario}`);
}
