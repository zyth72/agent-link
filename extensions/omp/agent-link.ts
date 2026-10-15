/** Agent Link tools and session-owned authenticated HTTP push listener.
 * No bridge-file polling. Starts on session_start, closes on session_shutdown.
 * AGENT_LINK_SESSIONS controls private descriptor discovery (default ~/.agent-link/sessions).
 * Acceptance proves framework injection only, not model processing.
 */
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import {
  ASK_RESPONSE_GRACE_MS,
  DEFAULT_REQUEST_TIMEOUT_MS,
  asText,
  callHost,
} from "../shared/agent-link-client";
import { startSessionPush, type PushListener } from "../shared/session-push";

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : undefined;
}

export default function agentLinkExtension(pi: ExtensionAPI) {
  const z = pi.zod;

  pi.registerTool({
    name: "report_parent",
    label: "Report to parent",
    description:
      "Report actual progress to your bound Agent Link parent. host_accepted only means the " +
      "bridge accepted the call, not that the parent processed it. Reuse the same message_id " +
      "only for an identical retry; a replay never re-delivers.",
    loadMode: "essential",
    parameters: z.object({
      message_id: z.string({ description: "Stable id; reuse only for an identical retry" }),
      text: z.string({ description: "Progress text for the parent" }),
    }),
    async execute(
      _toolCallId: string,
      params: { message_id: string; text: string },
      signal: AbortSignal | undefined,
    ) {
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
  });

  pi.registerTool({
    name: "ask_parent",
    label: "Ask parent",
    description:
      "Ask your bound Agent Link parent a question and wait for the actual reply. A timed_out " +
      "status means no on-time reply; never assume an answer. A late answer may still be stored: " +
      "use check_parent_request or repeat the ask with the same IDs and timeout to read it.",
    loadMode: "essential",
    parameters: z.object({
      request_id: z.string({ description: "Stable request id, unique per question" }),
      message_id: z.string({ description: "Message id for this question" }),
      question: z.string({ description: "The decision or information you need" }),
      timeout: z.number({ description: "Seconds to wait; must match on any replay" }),
    }),
    async execute(
      _toolCallId: string,
      params: { request_id: string; message_id: string; question: string; timeout: number },
      signal: AbortSignal | undefined,
    ) {
      const timeout = params.timeout;
      if (
        typeof timeout !== "number" ||
        !Number.isFinite(timeout) ||
        timeout < 0.001 ||
        timeout > 3600
      ) {
        throw new Error("timeout must be between 0.001 and 3600 seconds");
      }
      const timeoutMs = Math.ceil(timeout * 1000) + ASK_RESPONSE_GRACE_MS;
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
  });

  pi.registerTool({
    name: "check_parent_request",
    label: "Check parent request",
    description:
      "Read the persisted status and any late answer for one of your own Agent Link requests. " +
      "Use this after a timed_out ask; it cannot read requests from other bindings.",
    loadMode: "essential",
    parameters: z.object({
      request_id: z.string({ description: "The request id returned by ask_parent" }),
    }),
    async execute(
      _toolCallId: string,
      params: { request_id: string },
      signal: AbortSignal | undefined,
    ) {
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
  });

  pi.registerTool({
    name: "poll_parent_requests",
    label: "Poll parent requests",
    description:
      "List pending Agent Link requests your parent sent you (ask_child) that are waiting for " +
      "your answer. Answer one with answer_parent using its request_id.",
    loadMode: "essential",
    parameters: z.object({}),
    async execute(
      _toolCallId: string,
      _params: Record<string, never>,
      signal: AbortSignal | undefined,
    ) {
      const { result, exitCode } = await callHost(
        "child-requests",
        {},
        signal,
        DEFAULT_REQUEST_TIMEOUT_MS,
      );
      if (exitCode !== 0) {
        throw new Error(
          `Agent Link child-requests failed (exit ${exitCode}): ${JSON.stringify(result)}`,
        );
      }
      const rows = Array.isArray(result.parent_requests) ? result.parent_requests : [];
      const lines = rows.map((row) => {
        const record = asRecord(row);
        return `${String(record?.request_id ?? "?")}: ${String(record?.question ?? "")}`;
      });
      return {
        content: [
          {
            type: "text" as const,
            text:
              lines.length === 0
                ? "No pending parent requests."
                : `Pending parent requests: ${lines.join("; ")}`,
          },
        ],
        details: result,
      };
    },
  });

  pi.registerTool({
    name: "answer_parent",
    label: "Answer parent request",
    description:
      "Answer a parent-initiated Agent Link request (ask_child). The answer is persisted and " +
      "returned to the waiting parent; a second different answer is a conflict. A request past " +
      "its deadline stores your answer with late=true.",
    loadMode: "essential",
    parameters: z.object({
      request_id: z.string({ description: "The parent request id from the inbox message" }),
      message_id: z.string({ description: "New message id for this answer" }),
      answer: z.string({ description: "Your answer to the parent's question" }),
    }),
    async execute(
      _toolCallId: string,
      params: { request_id: string; message_id: string; answer: string },
      signal: AbortSignal | undefined,
    ) {
      const { result, exitCode } = await callHost(
        "answer",
        params,
        signal,
        DEFAULT_REQUEST_TIMEOUT_MS,
      );
      if (exitCode !== 0) {
        throw new Error(
          `Agent Link answer was rejected (exit ${exitCode}): ${JSON.stringify(result)}`,
        );
      }
      const late = result.late === true ? " late=true" : "";
      return {
        content: [
          {
            type: "text" as const,
            text: `Answered parent request ${params.request_id}; status=${String(result.status)}${late}.`,
          },
        ],
        details: result,
      };
    },
  });

  // State belongs to this extension instance, never shared across OMP children.
  let listener: PushListener | undefined;
  let lifecycle = Promise.resolve();
  pi.on("session_start", (_event, ctx) => {
    lifecycle = lifecycle.catch(() => {}).then(async () => {
      await listener?.close();
      listener = undefined;
      const sessionId = ctx.sessionManager.getSessionId();
      listener = await startSessionPush({
        framework: "omp", sessionId,
        inject: (prompt) => {
          return pi.sendUserMessage(prompt, { deliverAs: "aside", attribution: "agent" });
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
