/**
 * Closed-loop driver for tests/test_pi_extension.py.
 *
 * Loads the real Pi extension through jiti (same aliases as the Pi runtime),
 * captures the registered tools with a fake ExtensionAPI, and calls them
 * against a real `python3 -m agent_link.host` loopback service whose bound
 * notifier acts as the parent.  Prints one JSON object with each tool outcome.
 *
 * Usage: node tests/pi/tool_loop.mjs happy|replay|late
 * Env:   AGENT_LINK_CREDENTIALS, AGENT_LINK_TEST_IDS (JSON), PI_CODING_AGENT_ROOT
 */
import { loadExtension, fakePi } from "./harness.mjs";

const scenario = process.argv[2];
const ids = JSON.parse(process.env.AGENT_LINK_TEST_IDS ?? "{}");
const { factory } = await loadExtension();
const { pi, tools } = fakePi();
factory(pi);

async function run(label, toolName, params) {
  try {
    const result = await tools.get(toolName).execute(label, params);
    return { ok: true, text: result.content[0].text, details: result.details };
  } catch (error) {
    return { ok: false, error: String(error?.message ?? error) };
  }
}

const out = {};
if (scenario === "happy") {
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
} else if (scenario === "replay") {
  out.report = await run("report", "report_parent", {
    message_id: ids.report_message_id,
    text: "e2e progress",
  });
} else if (scenario === "late") {
  out.ask = await run("ask", "ask_parent", {
    request_id: ids.late_request_id,
    message_id: ids.late_message_id,
    question: "late-reply",
    timeout: 1,
  });
  await new Promise((resolve) => setTimeout(resolve, 2500));
  out.check = await run("check", "check_parent_request", { request_id: ids.late_request_id });
} else {
  throw new Error(`unknown scenario ${scenario}`);
}

console.log(JSON.stringify(out));
