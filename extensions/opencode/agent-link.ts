/**
 * Agent Link adapter for OpenCode.
 *
 * Plugin API: this targets the OpenCode v2 plugin API (verified by smoke test
 * against opencode 2.0.29, no model calls): a plugin is a .ts file under
 * ~/.config/opencode/plugin/ (auto-discovered) or a directory listed in the
 * config `plugin` array, whose default export is `{ id, setup(ctx) }`.  The
 * v1 factory API (`export const X = async (input) => hooks`) is rejected by
 * 2.0.x ("Plugin must export a default definition with an id and an effect or
 * setup function"), so no v1 fallback is provided.
 *
 * Two roles share this one plugin:
 *
 * Child tools -- with an AGENT_LINK_CREDENTIALS file from the parent host
 * service, report_parent / ask_parent / check_parent_request behave exactly
 * like the OMP/Pi extensions' tools (same names, texts and semantics);
 * answer_parent / poll_parent_requests serve parent-initiated requests
 * (ask_child).  They cannot choose a parent session, create bindings or reply
 * to their own requests.
 *
 * Bridge watchers -- with AGENT_LINK_BRIDGE_FILE + AGENT_LINK_BINDING set, the
 * plugin consumes that parent bridge file explicitly.  Without them, the
 * plugin auto-claims from the user-level registry (~/.agent-link/registry/,
 * override AGENT_LINK_REGISTRY): a binding whose parent.session_id matches the
 * tracked session makes it watch <root>/omp-bridge/<binding>.jsonl
 * (child->parent), and a binding whose child.session_id matches makes it watch
 * <root>/inbox/<binding>.jsonl (parent->child); both may be held at once.
 * Validated events are injected with ctx.session.prompt (delivery "queue").
 * Per-file single-consumer lock (<file>.lock) and persisted cursor
 * (<file>.cursor, with file identity) apply to each watched file; the watcher
 * itself is shared with the OMP/Pi adapters in extensions/shared/bridge-watcher.ts.
 *
 * Session identity: the plugin runs server-level per location and is not told
 * a session id at setup.  It tracks the most recently active session (the
 * session.prompt hook and session.created events for top-level sessions) and
 * the watchers follow that session: they start when the first session is
 * observed (env mode and auto-claim alike), and when activity moves to
 * another session the watchers are stopped and re-claimed for the new
 * session.  Injections always target the currently tracked session; with none
 * observed yet an injection fails and is retried (order is preserved by the
 * shared watcher).
 * Known limitations: several sessions of one location share this tracking
 * (last active wins), subagent sessions (session.created with parentID) never
 * become the tracked session, and one server process serving several
 * locations would share the module-level watcher state -- run one location
 * per server.
 *
 * Delivery is at-least-once: an event is replayed if the process dies after
 * injection but before the cursor is persisted, and a wholesale replacement of
 * the bridge file replays it from the start.  An event whose injection keeps
 * failing blocks later events until it succeeds so the order is preserved.
 */
import {
  ASK_RESPONSE_GRACE_MS,
  DEFAULT_REQUEST_TIMEOUT_MS,
  asText,
  callHost,
} from "../shared/agent-link-client.ts";
import {
  startBridgeWatchers,
  stopBridgeWatchers,
  type BridgeHost,
} from "../shared/bridge-watcher.ts";

/** Structural subset of the OpenCode v2 plugin context this adapter uses. */
type JsonSchema = Record<string, unknown>;

type ToolResult = { content?: string; metadata?: Record<string, unknown> };

type ToolExecuteContext = { sessionID?: string; signal?: AbortSignal };

type ToolDefinition = {
  name: string;
  description: string;
  input: JsonSchema;
  execute(input: Record<string, unknown>, context: ToolExecuteContext): Promise<ToolResult>;
};

type Registration = { dispose(): Promise<void> | void };

type PromptHookInput = { sessionID?: string };

type OpenCodeEvent = { type?: string; data?: Record<string, unknown> };

type OpenCodePluginContext = {
  tool: {
    transform(callback: (editor: { add(tool: ToolDefinition): void }) => void): Promise<unknown>;
  };
  session: {
    prompt(input: {
      sessionID: string;
      text: string;
      delivery?: "steer" | "queue";
    }): Promise<unknown>;
    hook(
      name: "prompt",
      callback: (input: PromptHookInput) => void | Promise<void>,
    ): Promise<Registration>;
  };
  event: { subscribe(): AsyncIterable<OpenCodeEvent> };
};

/** Mutable session tracking shared by the host callbacks. */
type BridgeContext = {
  oc: OpenCodePluginContext;
  tracked: { sessionId: string | undefined };
};

function logError(message: string): void {
  try {
    console.error(`[agent-link] ${message}`);
  } catch {
    // Logging must never break the plugin.
  }
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : undefined;
}

function objectSchema(properties: Record<string, JsonSchema>, required: string[]): JsonSchema {
  return { type: "object", properties, required, additionalProperties: false };
}

function stringParam(description: string): JsonSchema {
  return { type: "string", description };
}

/** The five child tools; names, texts and semantics match the OMP adapter. */
function toolDefinitions(): ToolDefinition[] {
  return [
    {
      name: "report_parent",
      description:
        "Report actual progress to your bound Agent Link parent. host_accepted only means the " +
        "bridge accepted the call, not that the parent processed it. Reuse the same message_id " +
        "only for an identical retry; a replay never re-delivers.",
      input: objectSchema(
        {
          message_id: stringParam("Stable id; reuse only for an identical retry"),
          text: stringParam("Progress text for the parent"),
        },
        ["message_id", "text"],
      ),
      async execute(input, context) {
        const { result, exitCode } = await callHost(
          "report",
          input,
          context.signal,
          DEFAULT_REQUEST_TIMEOUT_MS,
        );
        if (exitCode !== 0) {
          throw new Error(
            `Agent Link report was not delivered (exit ${exitCode}): ${JSON.stringify(result)}`,
          );
        }
        const replay = result.idempotent === true ? " (idempotent replay, not re-delivered)" : "";
        return {
          content: `Reported to parent; delivery_state=${String(result.delivery_state)}${replay}.`,
          metadata: result,
        };
      },
    },
    {
      name: "ask_parent",
      description:
        "Ask your bound Agent Link parent a question and wait for the actual reply. A timed_out " +
        "status means no on-time reply; never assume an answer. A late answer may still be stored: " +
        "use check_parent_request or repeat the ask with the same IDs and timeout to read it.",
      input: objectSchema(
        {
          request_id: stringParam("Stable request id, unique per question"),
          message_id: stringParam("Message id for this question"),
          question: stringParam("The decision or information you need"),
          timeout: { type: "number", description: "Seconds to wait; must match on any replay" },
        },
        ["request_id", "message_id", "question", "timeout"],
      ),
      async execute(input, context) {
        const timeout = input.timeout;
        if (
          typeof timeout !== "number" ||
          !Number.isFinite(timeout) ||
          timeout < 0.001 ||
          timeout > 3600
        ) {
          throw new Error("timeout must be between 0.001 and 3600 seconds");
        }
        const timeoutMs = Math.ceil(timeout * 1000) + ASK_RESPONSE_GRACE_MS;
        const { result, exitCode } = await callHost("ask", input, context.signal, timeoutMs);
        const late = result.late === true ? " late=true" : "";
        if (result.status === "replied") {
          return {
            content: `Parent replied${late}: ${String(result.answer ?? "")}`,
            metadata: result,
          };
        }
        if (result.status === "timed_out") {
          const stored =
            result.answer !== null && result.answer !== undefined
              ? `; a late answer is stored: ${String(result.answer)}`
              : "";
          return {
            content:
              `No on-time reply (status=timed_out${late})${stored}. ` +
              `Do not assume an answer; with request_id=${String(input.request_id)} call ` +
              "check_parent_request later or repeat the ask with identical IDs and timeout.",
            metadata: result,
          };
        }
        throw new Error(
          `Agent Link ask returned unexpected status (exit ${exitCode}): ${JSON.stringify(result)}`,
        );
      },
    },
    {
      name: "check_parent_request",
      description:
        "Read the persisted status and any late answer for one of your own Agent Link requests. " +
        "Use this after a timed_out ask; it cannot read requests from other bindings.",
      input: objectSchema(
        { request_id: stringParam("The request id returned by ask_parent") },
        ["request_id"],
      ),
      async execute(input, context) {
        const { result, exitCode } = await callHost(
          "get-request",
          input,
          context.signal,
          DEFAULT_REQUEST_TIMEOUT_MS,
        );
        if (exitCode !== 0) {
          throw new Error(
            `Agent Link get-request failed (exit ${exitCode}): ${JSON.stringify(result)}`,
          );
        }
        return {
          content:
            `status=${String(result.status)} late=${result.late === true} ` +
            `answer=${asText(result.answer)} delivery_state=${asText(result.delivery_state)}`,
          metadata: result,
        };
      },
    },
    {
      name: "poll_parent_requests",
      description:
        "List pending Agent Link requests your parent sent you (ask_child) that are waiting for " +
        "your answer. Answer one with answer_parent using its request_id.",
      input: objectSchema({}, []),
      async execute(_input, context) {
        const { result, exitCode } = await callHost(
          "child-requests",
          {},
          context.signal,
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
          content:
            lines.length === 0
              ? "No pending parent requests."
              : `Pending parent requests: ${lines.join("; ")}`,
          metadata: result,
        };
      },
    },
    {
      name: "answer_parent",
      description:
        "Answer a parent-initiated Agent Link request (ask_child). The answer is persisted and " +
        "returned to the waiting parent; a second different answer is a conflict. A request past " +
        "its deadline stores your answer with late=true.",
      input: objectSchema(
        {
          request_id: stringParam("The parent request id from the inbox message"),
          message_id: stringParam("New message id for this answer"),
          answer: stringParam("Your answer to the parent's question"),
        },
        ["request_id", "message_id", "answer"],
      ),
      async execute(input, context) {
        const { result, exitCode } = await callHost(
          "answer",
          input,
          context.signal,
          DEFAULT_REQUEST_TIMEOUT_MS,
        );
        if (exitCode !== 0) {
          throw new Error(
            `Agent Link answer was rejected (exit ${exitCode}): ${JSON.stringify(result)}`,
          );
        }
        const late = result.late === true ? " late=true" : "";
        return {
          content: `Answered parent request ${String(input.request_id)}; status=${String(result.status)}${late}.`,
          metadata: result,
        };
      },
    },
  ];
}

/** Host-specific behavior for the shared bridge watcher (shared/bridge-watcher.ts). */
function bridgeHost(): BridgeHost<BridgeContext> {
  return {
    framework: "opencode",
    roles: ["parent", "child"],
    // The plugin runs once per location at server level; there is no sub
    // session sharing this module state, so every setup is "main".
    isMain: () => true,
    sessionId: (ctx) => ctx.tracked.sessionId,
    setTimer: (_ctx, callback, ms) => setInterval(callback, ms),
    clearTimer: (_ctx, handle) => clearInterval(handle as NodeJS.Timeout),
    inject: async (ctx, prompt) => {
      const sessionId = ctx.tracked.sessionId;
      if (!sessionId) {
        throw new Error("no OpenCode session observed yet; cannot inject");
      }
      await ctx.oc.session.prompt({ sessionID: sessionId, text: prompt, delivery: "queue" });
    },
    // Server-level plugins have no UI surface; watcher activity is logged.
    notify: () => {},
    logError: (message) => logError(message),
  };
}

const plugin = {
  id: "agent-link",
  async setup(oc: OpenCodePluginContext) {
    try {
      await oc.tool.transform((editor) => {
        for (const tool of toolDefinitions()) editor.add(tool);
      });
    } catch (error) {
      logError(`tool registration failed: ${(error as Error).message}`);
    }

    const ctx: BridgeContext = { oc, tracked: { sessionId: undefined } };
    const host = bridgeHost();
    // Serialize watcher restarts so rapid session switches cannot interleave
    // stop/claim cycles.
    let refreshChain: Promise<void> = Promise.resolve();
    const refreshWatchers = (): void => {
      refreshChain = refreshChain
        .then(() => startBridgeWatchers(host, ctx))
        .catch((error: unknown) => {
          logError(`bridge watcher restart failed: ${(error as Error).message}`);
        });
    };
    const noteSession = (sessionId: string | undefined): void => {
      if (!sessionId || sessionId === ctx.tracked.sessionId) return;
      ctx.tracked.sessionId = sessionId;
      refreshWatchers();
    };

    let promptRegistration: Registration | undefined;
    try {
      promptRegistration = await oc.session.hook("prompt", (input) => {
        noteSession(typeof input.sessionID === "string" ? input.sessionID : undefined);
      });
    } catch (error) {
      logError(`session prompt hook failed: ${(error as Error).message}`);
    }

    let disposed = false;
    void (async () => {
      try {
        for await (const event of oc.event.subscribe()) {
          if (disposed) return;
          // Only top-level sessions retarget the watchers; subagent sessions
          // (session.created with parentID) share their parent's binding.
          if (event?.type === "session.created") {
            const data = asRecord(event.data);
            if (data && (data.parentID === undefined || data.parentID === null)) {
              noteSession(typeof data.sessionID === "string" ? data.sessionID : undefined);
            }
          }
        }
      } catch (error) {
        if (!disposed) logError(`event subscription failed: ${(error as Error).message}`);
      }
    })();

    // Watchers (env mode and auto-claim alike) start when the first session
    // is observed: injections need a session to target, and claiming without
    // one is impossible.  Restarting later follows the active session.
    return async () => {
      disposed = true;
      try {
        await promptRegistration?.dispose();
      } catch {
        // Disposing a hook on a gone location is harmless.
      }
      await stopBridgeWatchers();
      // The event loop ends when the host closes the subscription (the real
      // adapter closes it on plugin unload); do not await it here: a pending
      // iterator would block cleanup forever.
    };
  },
};

export default plugin;
