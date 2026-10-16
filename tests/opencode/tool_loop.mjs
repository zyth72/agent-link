/**
 * Driver for tests/test_opencode_extension.py.
 *
 * Loads the real OpenCode plugin (v2 API: default export { id, setup }) under
 * Bun or Node with a fake plugin context and drives the five child tools
 * against a real `python3 -m agent_link.host` service.  Prints one JSON object
 * (last line) with the observed state.
 *
 * Usage: bun|node tests/opencode/tool_loop.mjs child-happy
 * Env:   AGENT_LINK_CREDENTIALS, AGENT_LINK_TEST_IDS (JSON)
 */
import { fakeOpencode, loadOpencodeExtension } from "./harness.mjs";

const scenario = process.argv[2];
const ids = JSON.parse(process.env.AGENT_LINK_TEST_IDS ?? "{}");
const plugin = await loadOpencodeExtension();
const fake = fakeOpencode();
const cleanup = await plugin.setup(fake.ctx);

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
  await cleanup();
  console.log(JSON.stringify(out));
} else {
  throw new Error(`unknown scenario ${scenario}`);
}
