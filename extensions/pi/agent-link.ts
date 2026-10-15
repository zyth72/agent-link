/**
 * Agent Link adapter for Pi.
 *
 * Two roles share this one extension:
 *
 * Child tools -- when the parent host service wrote an AGENT_LINK_CREDENTIALS
 * file, report_parent / ask_parent / check_parent_request behave exactly like
 * the OMP extension's tools (same names, labels, texts and semantics).  They
 * cannot choose a parent session, create bindings or reply.
 * See docs/HOST-PROTOCOL.md.
 *
 * Parent bridge watcher -- when AGENT_LINK_BRIDGE_FILE and AGENT_LINK_BINDING
 * are set, the session consumes that bridge file explicitly.  Without them the
 * watcher auto-claims the newest registry entry (AGENT_LINK_REGISTRY, default
 * ~/.agent-link/registry/) whose parent names this Pi session id, consuming
 * <root>/omp-bridge/<binding>.jsonl and pinning event.thread_id to this
 * session id.  The watcher itself (single-consumer lock, persisted cursor with
 * file identity, replace/truncate detection, order-preserving retry) is shared
 * with the OMP adapter in extensions/shared/bridge-watcher.ts.
 *
 * Injection follows Pi's semantics: an idle session gets a plain
 * pi.sendUserMessage (starting a turn); a busy session gets
 * deliverAs:"followUp" so the event queues instead of interrupting the
 * current turn.  Delivery is at-least-once: an event whose injection keeps
 * failing blocks later events until it succeeds so the order is preserved.
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
import {
  startBridgeWatchers,
  stopBridgeWatchers,
  type BridgeHost,
} from "../shared/bridge-watcher";

/** The ctx surface the parent watcher needs; satisfied by ExtensionContext. */
type BridgeContext = Pick<ExtensionContext, "isIdle" | "ui" | "sessionManager">;

function logError(pi: ExtensionAPI, message: string): void {
  try {
    // Pi's ExtensionAPI exposes no logger; use one only if the host provides it.
    if (!("logger" in pi)) return;
    const logger = pi.logger;
    if (!logger || typeof logger !== "object" || !("error" in logger)) return;
    const error = logger.error;
    if (typeof error !== "function") return;
    error.call(logger, message);
  } catch {
    // Logging must never break the watcher.
  }
}

/** Host-specific behavior for the shared bridge watcher (shared/bridge-watcher.ts). */
function bridgeHost(pi: ExtensionAPI, ctx: BridgeContext): BridgeHost<BridgeContext> {
  return {
    framework: "pi",
    roles: ["parent"],
    // Pi sessions are not sub-sessions here; module state keeps one watcher
    // per role per process regardless.
    isMain: () => true,
    sessionId: (context) => context.sessionManager.getSessionId(),
    setTimer: (_context, callback, ms) => setInterval(callback, ms),
    // The handle comes from the global setInterval above (this host owns the timer).
    clearTimer: (_context, handle) => clearInterval(handle as NodeJS.Timeout),
    inject: (context, prompt) => {
      // An idle session starts a turn; a busy session queues the event instead
      // of interrupting the current turn (deliverAs is required when busy).
      if (context.isIdle()) {
        pi.sendUserMessage(prompt);
      } else {
        pi.sendUserMessage(prompt, { deliverAs: "followUp" });
      }
    },
    notify: (context, message, level) => {
      try {
        context.ui.notify(message, level);
      } catch {
        // Notifying is best-effort.
      }
    },
    logError: (message) => logError(pi, message),
  };
}

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

  pi.on("session_start", (_event, ctx) => {
    void startBridgeWatchers(bridgeHost(pi, ctx), ctx);
  });

  pi.on("session_shutdown", () => {
    void stopBridgeWatchers();
  });
}
