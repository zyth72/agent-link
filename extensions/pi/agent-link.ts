/** Agent Link tools and session-owned authenticated HTTP push listener.
 * No bridge-file polling. Starts on session_start, closes on session_shutdown.
 * AGENT_LINK_SESSIONS controls private descriptor discovery (default ~/.agent-link/sessions).
 * Acceptance proves framework injection only, not model processing.
 */
import { Type } from "@earendil-works/pi-ai";
import {
  defineTool,
  type ExtensionAPI,
  type ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import {
  ASK_RESPONSE_GRACE_MS,
  DEFAULT_REQUEST_TIMEOUT_MS,
  asText,
  callHost,
} from "../shared/agent-link-client";
import { startSessionPush, type PushListener } from "../shared/session-push";

export default function (pi: ExtensionAPI) {
  pi.registerTool(
    defineTool({
      name: "report_parent",
      label: "Report to parent",
      description:
        "Report actual progress to your bound Agent Link parent. host_accepted only means the " +
        "bridge accepted the call, not that the parent processed it. Reuse the same message_id " +
        "only for an identical retry; a replay never re-delivers.",
      parameters: Type.Object({
        message_id: Type.String({ description: "Stable id; reuse only for an identical retry" }),
        text: Type.String({ description: "Progress text for the parent" }),
      }),
      async execute(_toolCallId, params, signal) {
        const { result, exitCode } = await callHost(
          "report",
          params,
          signal,
          DEFAULT_REQUEST_TIMEOUT_MS,
        );
        if (exitCode !== 0) {
          throw new Error(
            `Agent Link report was not delivered (exit ${exitCode}): ${JSON.stringify(result)}`,
          );
        }
        const replay = result.idempotent === true ? " (idempotent replay, not re-delivered)" : "";
        return {
          content: [
            {
              type: "text" as const,
              text: `Reported to parent; delivery_state=${String(result.delivery_state)}${replay}.`,
            },
          ],
          details: result,
        };
      },
    }),
  );

  pi.registerTool(
    defineTool({
      name: "ask_parent",
      label: "Ask parent",
      description:
        "Ask your bound Agent Link parent a question and wait for the actual reply. A timed_out " +
        "status means no on-time reply; never assume an answer. A late answer may still be stored: " +
        "use check_parent_request or repeat the ask with the same IDs and timeout to read it.",
      parameters: Type.Object({
        request_id: Type.String({ description: "Stable request id, unique per question" }),
        message_id: Type.String({ description: "Message id for this question" }),
        question: Type.String({ description: "The decision or information you need" }),
        timeout: Type.Number({
          description: "Seconds to wait; must match on any replay",
          minimum: 0.001,
          maximum: 3600,
        }),
      }),
      async execute(_toolCallId, params, signal) {
        const timeoutMs = Math.ceil(params.timeout * 1000) + ASK_RESPONSE_GRACE_MS;
        const { result, exitCode } = await callHost("ask", params, signal, timeoutMs);
        const late = result.late === true ? " late=true" : "";
        if (result.status === "replied") {
          return {
            content: [
              {
                type: "text" as const,
                text: `Parent replied${late}: ${String(result.answer ?? "")}`,
              },
            ],
            details: result,
          };
        }
        if (result.status === "timed_out") {
          const stored =
            result.answer !== null && result.answer !== undefined
              ? `; a late answer is stored: ${String(result.answer)}`
              : "";
          return {
            content: [
              {
                type: "text" as const,
                text:
                  `No on-time reply (status=timed_out${late})${stored}. ` +
                  `Do not assume an answer; with request_id=${params.request_id} call ` +
                  "check_parent_request later or repeat the ask with identical IDs and timeout.",
              },
            ],
            details: result,
          };
        }
        throw new Error(
          `Agent Link ask returned unexpected status (exit ${exitCode}): ${JSON.stringify(result)}`,
        );
      },
    }),
  );

  pi.registerTool(
    defineTool({
      name: "check_parent_request",
      label: "Check parent request",
      description:
        "Read the persisted status and any late answer for one of your own Agent Link requests. " +
        "Use this after a timed_out ask; it cannot read requests from other bindings.",
      parameters: Type.Object({
        request_id: Type.String({ description: "The request id returned by ask_parent" }),
      }),
      async execute(_toolCallId, params, signal) {
        const { result, exitCode } = await callHost(
          "get-request",
          params,
          signal,
          DEFAULT_REQUEST_TIMEOUT_MS,
        );
        if (exitCode !== 0) {
          throw new Error(
            `Agent Link get-request failed (exit ${exitCode}): ${JSON.stringify(result)}`,
          );
        }
        return {
          content: [
            {
              type: "text" as const,
              text:
                `status=${String(result.status)} late=${result.late === true} ` +
                `answer=${asText(result.answer)} delivery_state=${asText(result.delivery_state)}`,
            },
          ],
          details: result,
        };
      },
    }),
  );

  pi.registerTool(
    defineTool({
      name: "answer_parent",
      label: "Answer parent request",
      description:
        "Answer a parent-initiated Agent Link request (ask_child). The answer is persisted and " +
        "returned to the waiting parent; a second different answer is a conflict. A request past " +
        "its deadline stores your answer with late=true. This is how a Pi RPC child answers after " +
        "receiving a question prompt; no polling is needed.",
      parameters: Type.Object({
        request_id: Type.String({ description: "The parent request id from the question prompt" }),
        message_id: Type.String({ description: "New message id for this answer" }),
        answer: Type.String({ description: "Your answer to the parent's question" }),
      }),
      async execute(_toolCallId, params, signal) {
        const { result, exitCode } = await callHost(
          "answer",
          params,
          signal,
          DEFAULT_REQUEST_TIMEOUT_MS,
        );
        if (exitCode !== 0) {
          throw new Error(
            `Agent Link answer was not delivered (exit ${exitCode}): ${JSON.stringify(result)}`,
          );
        }
        const replay = result.idempotent === true ? " (idempotent replay, not re-written)" : "";
        const late = result.late === true ? " late=true" : "";
        return {
          content: [
            {
              type: "text" as const,
              text:
                `Answered parent request ${params.request_id}; ` +
                `status=${String(result.status)}${late}${replay}.`,
            },
          ],
          details: result,
        };
      },
    }),
  );

  // State belongs to this extension instance, never shared across OMP children.
  let listener: PushListener | undefined;
  let lifecycle = Promise.resolve();
  pi.on("session_start", (_event, ctx) => {
    lifecycle = lifecycle.catch(() => {}).then(async () => {
      await listener?.close();
      listener = undefined;
      const sessionId = ctx.sessionManager.getSessionId();
      listener = await startSessionPush({
        framework: "pi", sessionId,
        inject: (prompt) => {
          if (ctx.isIdle()) pi.sendUserMessage(prompt);
          else pi.sendUserMessage(prompt, { deliverAs: "followUp" });
        },
      });
    });
    return lifecycle;
  });
  pi.on("session_shutdown", () => {
    lifecycle = lifecycle.catch(() => {}).then(async () => {
      await listener?.close(); listener = undefined;
    });
    return lifecycle;
  });
}
