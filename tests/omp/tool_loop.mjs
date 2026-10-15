/**
 * Driver for tests/test_omp_extension.py.
 *
 * Loads the real OMP extension under Bun (its native loader) with a fake
 * ExtensionAPI and drives either the child tools against a real
 * `python3 -m agent_link.host` service or the parent bridge watcher against a
 * real JSONL bridge file.  Prints one JSON object (last line) with the
 * observed state; `bridge-hold` prints `{"holding":true}` first and then
 * sleeps until it is killed.
 *
 * Usage: bun tests/omp/tool_loop.mjs child-happy|bridge|bridge-hold|bridge-switch
 * Env:   AGENT_LINK_CREDENTIALS, AGENT_LINK_TEST_IDS (JSON),
 *        AGENT_LINK_BRIDGE_FILE, AGENT_LINK_BINDING,
 *        AGENT_LINK_TEST_FAIL_FILE, AGENT_LINK_TEST_NOTIFY_THROW
 */
import { existsSync, readFileSync, statSync } from "node:fs";
import { fakeOmp, loadOmpExtension, makeCtx, sleep, waitFor } from "./harness.mjs";

const scenario = process.argv[2];
const ids = JSON.parse(process.env.AGENT_LINK_TEST_IDS ?? "{}");
const bridgeFile = process.env.AGENT_LINK_BRIDGE_FILE ?? process.env.AGENT_LINK_TEST_BRIDGE_FILE;
const factory = await loadOmpExtension();
const { pi, tools, lifecycle, sent, logs } = fakeOmp();
factory(pi);

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

async function fireLifecycle(event, ctx) {
  for (const handler of lifecycle.get(event) ?? []) {
    await handler({}, ctx);
  }
}

async function driveUntilStable(owner, { timeoutMs = 8000 } = {}) {
  // Wait until the watcher registered its timer (or logged a refusal) before
  // judging stability, otherwise an empty timer list looks "stable".
  await waitFor(() => owner.timers.length > 0 || logs.length > 0, { timeoutMs: 3000 });
  const deadline = Date.now() + timeoutMs;
  let previous = "";
  let stable = 0;
  while (Date.now() < deadline) {
    for (const timer of owner.timers) timer.callback();
    await sleep(50);
    const signature = JSON.stringify([sent.length, cursorOffset(), logs.length, owner.notifies.length]);
    if (signature === previous) {
      stable += 1;
      if (stable >= 2) break;
    } else {
      stable = 0;
      previous = signature;
    }
  }
}

async function run(label, toolName, params) {
  try {
    const result = await tools.get(toolName).execute(label, params);
    return { ok: true, text: result.content[0].text, details: result.details };
  } catch (error) {
    return { ok: false, error: String(error?.message ?? error) };
  }
}

if (scenario === "child-happy") {
  const out = {};
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
  const owner = makeCtx();
  await fireLifecycle("session_start", owner.ctx);
  const failMarker = process.env.AGENT_LINK_TEST_FAIL_FILE;
  if (failMarker) {
    // A failing injection stops the tick; wait for that tick to finish and to
    // persist the cursor over the events that preceded the failure, then stop
    // (no further ticks: the retry belongs to the next run).
    await waitFor(() => !existsSync(failMarker));
    await waitFor(() => (cursorOffset() ?? 0) > 0);
  } else {
    await driveUntilStable(owner);
  }
  await fireLifecycle("session_shutdown", owner.ctx);
  await sleep(150);
  console.log(
    JSON.stringify({
      sent,
      cursor: readCursor(),
      size: fileSize(),
      logs,
      notifies: owner.notifies,
      refused: [...logs, ...owner.notifies.map((notify) => notify.message)].some((line) =>
        line.includes("refused"),
      ),
      lockExists: lockToken() !== null,
    }),
  );
} else if (scenario === "bridge-hold") {
  const holder = makeCtx();
  await fireLifecycle("session_start", holder.ctx);
  await waitFor(() => (lockToken() ?? "") !== "");
  console.log(JSON.stringify({ holding: true, pid: process.pid, lock: lockToken() }));
  await sleep(600000);
} else if (scenario === "bridge-switch") {
  const first = makeCtx();
  await fireLifecycle("session_start", first.ctx);
  await waitFor(() => (lockToken() ?? "") !== "");
  const firstToken = lockToken();
  await driveUntilStable(first);
  const injectedBeforeSwitch = sent.length;
  const second = makeCtx();
  await fireLifecycle("session_start", second.ctx);
  await waitFor(() => {
    const token = lockToken();
    return token !== null && token !== "" && token !== firstToken;
  });
  const secondToken = lockToken();
  await driveUntilStable(second);
  await fireLifecycle("session_shutdown", second.ctx);
  await sleep(150);
  console.log(
    JSON.stringify({
      sent,
      injectedBeforeSwitch,
      firstCleared: first.cleared.length,
      firstTimers: first.timers.length,
      secondTimers: second.timers.length,
      lockTokens: [firstToken, secondToken],
      lockExists: lockToken() !== null,
      cursor: readCursor(),
      logs,
    }),
  );
} else if (scenario === "bridge-auto-claim" || scenario === "bridge-auto-claim-late") {
  // No AGENT_LINK_BRIDGE_FILE/AGENT_LINK_BINDING: the watcher must resolve the
  // binding itself from the registry by matching this session's id.
  const owner = makeCtx({ sessionId: process.env.AGENT_LINK_TEST_SESSION_ID });
  await fireLifecycle("session_start", owner.ctx);
  if (scenario === "bridge-auto-claim-late") {
    // The claim appears after session start; keep pumping timers until the
    // rescan attaches and consumes the events.
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline && sent.length === 0) {
      for (const timer of owner.timers) timer.callback();
      await sleep(50);
    }
  }
  await driveUntilStable(owner);
  const lockHeld = lockToken() !== null;
  await fireLifecycle("session_shutdown", owner.ctx);
  await sleep(150);
  console.log(
    JSON.stringify({
      sent,
      cursor: readCursor(),
      logs,
      lockHeld,
      lockAfterShutdown: lockToken() !== null,
    }),
  );
} else if (scenario === "bridge-fail-retry-same-process") {
  // One-shot injection failure inside one process: the next tick must retry
  // the exact same bytes (regression: the failed line must be restored with
  // its newline, otherwise it merges with the next line and both are lost).
  const owner = makeCtx();
  await fireLifecycle("session_start", owner.ctx);
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline && sent.length < 3) {
    for (const timer of owner.timers) timer.callback();
    await sleep(50);
  }
  await driveUntilStable(owner);
  await fireLifecycle("session_shutdown", owner.ctx);
  await sleep(150);
  console.log(JSON.stringify({ sent, cursor: readCursor(), logs }));
} else {
  throw new Error(`unknown scenario ${scenario}`);
}
