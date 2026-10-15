/**
 * Driver for tests/test_omp_extension.py.
 *
 * Loads the real OMP extension under Bun (its native loader) with a fake
 * ExtensionAPI.  The child scenarios call the tools against a real
 * `python3 -m agent_link.host` service.  The push scenarios drive the
 * session-owned authenticated loopback listener: they fire the real
 * lifecycle, read the descriptor from AGENT_LINK_SESSIONS, send real HTTP
 * notifications and record what the fake Pi context received.  Prints one JSON
 * object (last line).
 *
 * Usage: bun tests/omp/tool_loop.mjs child-happy|push-basic|push-failure|push-switch|push-duplicate
 * Env:   AGENT_LINK_CREDENTIALS, AGENT_LINK_TEST_IDS (JSON),
 *        AGENT_LINK_SESSIONS, AGENT_LINK_TEST_SESSION_ID,
 *        AGENT_LINK_TEST_FAIL_FILE
 */
import { existsSync, writeFileSync } from "node:fs";
import {
  descriptorPath,
  fakeOmp,
  fireLifecycle,
  loadOmpExtension,
  makeCtx,
  postPush,
  pushEvent,
  readDescriptor,
  sleep,
} from "./harness.mjs";

const scenario = process.argv[2];
const ids = JSON.parse(process.env.AGENT_LINK_TEST_IDS ?? "{}");
const factory = await loadOmpExtension();
const { pi, tools, lifecycle, sent, logs } = fakeOmp();
factory(pi);

async function run(label, toolName, params) {
  try {
    const result = await tools.get(toolName).execute(label, params);
    return { ok: true, text: result.content[0].text, details: result.details };
  } catch (error) {
    return { ok: false, error: String(error?.message ?? error) };
  }
}

async function startPush(sessionId) {
  const owner = makeCtx({ sessionId });
  await fireLifecycle(lifecycle, "session_start", owner.ctx);
  return { owner, descriptor: readDescriptor("omp", sessionId) };
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
} else if (scenario === "push-basic") {
  const sessionId = process.env.AGENT_LINK_TEST_SESSION_ID ?? "omp-push";
  const { owner, descriptor } = await startPush(sessionId);
  const valid = await postPush(descriptor, pushEvent(descriptor, { message_id: "m-1", prompt: "push-1" }));
  const badToken = await postPush(
    descriptor,
    pushEvent(descriptor, { message_id: "m-2", prompt: "no" }),
    { authorization: "wrong" },
  );
  const noToken = await postPush(
    descriptor,
    pushEvent(descriptor, { message_id: "m-3", prompt: "no" }),
    { authorization: null },
  );
  const wrongSession = await postPush(
    descriptor,
    pushEvent(descriptor, { session_id: "other", message_id: "m-4", prompt: "no" }),
  );
  const wrongFramework = await postPush(
    descriptor,
    pushEvent(descriptor, { framework: "pi", message_id: "m-5", prompt: "no" }),
  );
  const malformed = await postPush(descriptor, pushEvent(descriptor, {}), { rawBody: "{bad" });
  const oversized = await postPush(descriptor, pushEvent(descriptor, {}), {
    rawBody: Buffer.alloc(65537, 97),
  });
  await fireLifecycle(lifecycle, "session_shutdown", owner.ctx);
  await sleep(100);
  console.log(
    JSON.stringify({
      sent,
      logs,
      valid,
      badToken,
      noToken,
      wrongSession,
      wrongFramework,
      malformed,
      oversized,
      descriptorExists: existsSync(descriptorPath("omp", sessionId)),
      descriptor,
    }),
  );
} else if (scenario === "push-failure") {
  const sessionId = "omp-fail";
  const marker = process.env.AGENT_LINK_TEST_FAIL_FILE;
  if (marker) writeFileSync(marker, "fail-me", "utf8");
  const { owner, descriptor } = await startPush(sessionId);
  const failure = await postPush(
    descriptor,
    pushEvent(descriptor, { message_id: "m-f", prompt: "fail-me" }),
  );
  const recovered = await postPush(
    descriptor,
    pushEvent(descriptor, { message_id: "m-ok", prompt: "after-failure" }),
  );
  await fireLifecycle(lifecycle, "session_shutdown", owner.ctx);
  await sleep(100);
  console.log(JSON.stringify({ sent, logs, failure, recovered }));
} else if (scenario === "push-switch") {
  const first = await startPush("omp-a");
  const firstAccepted = await postPush(
    first.descriptor,
    pushEvent(first.descriptor, { message_id: "m-a", prompt: "first" }),
  );
  const second = await startPush("omp-b");
  const firstRemoved = !existsSync(descriptorPath("omp", "omp-a"));
  let staleError = null;
  try {
    await postPush(first.descriptor, pushEvent(first.descriptor, { message_id: "m-a2", prompt: "stale" }));
  } catch (error) {
    staleError = String(error?.code ?? error?.message ?? error);
  }
  const secondAccepted = await postPush(
    second.descriptor,
    pushEvent(second.descriptor, { message_id: "m-b", prompt: "second" }),
  );
  await fireLifecycle(lifecycle, "session_shutdown", second.owner.ctx);
  await sleep(100);
  console.log(
    JSON.stringify({
      sent,
      logs,
      firstAccepted,
      secondAccepted,
      firstRemoved,
      staleError,
      firstEndpoint: first.descriptor.endpoint,
      secondEndpoint: second.descriptor.endpoint,
    }),
  );
} else if (scenario === "push-duplicate") {
  const sessionId = "omp-dup";
  let startError = null;
  try {
    await startPush(sessionId);
  } catch (error) {
    startError = String(error?.message ?? error);
  }
  console.log(
    JSON.stringify({
      startError,
      descriptor: readDescriptor("omp", sessionId),
    }),
  );
} else {
  throw new Error(`unknown scenario ${scenario}`);
}
