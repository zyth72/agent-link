/**
 * Agent Link adapter for OMP (oh-my-pi).
 *
 * Two roles share this one extension:
 *
 * Child tools -- when the parent host service wrote an AGENT_LINK_CREDENTIALS
 * file, report_parent / ask_parent / check_parent_request behave exactly like
 * the Pi extension's tools (same names, labels, texts and semantics).  They
 * cannot choose a parent session, create bindings or reply.
 *
 * Parent bridge watcher -- when AGENT_LINK_BRIDGE_FILE and AGENT_LINK_BINDING
 * are set, the main session polls the bridge JSONL file written by
 * agent_link.hosts.omp:OmpHost (or the omp_notifier CLI bridge) and injects
 * validated events into the session with pi.sendUserMessage.  A single
 * consumer lock (<bridge>.lock) keeps a second session from consuming the same
 * file; the persisted cursor (<bridge>.cursor, with file identity) resumes
 * across restarts and detects append/replace/truncate.
 *
 * Delivery is at-least-once: an event is replayed if the process dies after
 * injection but before the cursor is persisted, and a wholesale replacement of
 * the bridge file replays it from the start.  An event whose injection keeps
 * failing blocks later events until it succeeds so the order is preserved.
 */
import { randomUUID } from "node:crypto";
import { setTimeout as sleep } from "node:timers/promises";
import {
  open,
  readFile,
  rename,
  stat,
  unlink,
  writeFile,
  type FileHandle,
  type Stats,
} from "node:fs/promises";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import {
  ASK_RESPONSE_GRACE_MS,
  DEFAULT_REQUEST_TIMEOUT_MS,
  asText,
  callHost,
} from "../shared/agent-link-client";

/** Read window per tick and the maximum size of a single event line. */
const MAX_WINDOW_BYTES = 1024 * 1024;
const READ_CHUNK_BYTES = 64 * 1024;

type BridgeContext = {
  agent?: { kind?: string };
  setInterval(callback: () => void, ms: number): unknown;
  clearTimer(handle: unknown): void;
  ui: { notify(message: string, level: string): void };
};

type BridgeState = {
  pi: ExtensionAPI;
  ctx: BridgeContext;
  bridgeFile: string;
  binding: string;
  lockPath: string;
  token: string;
  timer: unknown;
  consumedPos: number;
  dev: number;
  ino: number;
  tail: Buffer;
  busy: boolean;
};

/** The one bridge file this process consumes; shared by all sessions. */
let active: BridgeState | null = null;

function logError(pi: ExtensionAPI, message: string): void {
  try {
    pi.logger?.error?.(message);
  } catch {
    // Logging must never break the watcher.
  }
}

async function writeCursor(state: BridgeState): Promise<void> {
  const cursorPath = state.bridgeFile + ".cursor";
  try {
    await writeFile(
      cursorPath + ".tmp",
      JSON.stringify({ offset: state.consumedPos, dev: state.dev, ino: state.ino }),
    );
    await rename(cursorPath + ".tmp", cursorPath);
  } catch (error) {
    // The in-memory position stays authoritative; the next tick retries.
    logError(state.pi, `Agent Link bridge watcher: cannot persist cursor: ${(error as Error).message}`);
  }
}

async function handleLine(state: BridgeState, line: string): Promise<"consumed" | "failed"> {
  let event: Record<string, unknown>;
  try {
    event = JSON.parse(line) as Record<string, unknown>;
  } catch {
    logError(state.pi, "Agent Link bridge watcher: skipping unparsable event line");
    return "consumed";
  }
  if (
    !event ||
    typeof event !== "object" ||
    event.v !== 1 ||
    typeof event.prompt !== "string" ||
    event.prompt === "" ||
    event.binding_id !== state.binding
  ) {
    logError(state.pi, `Agent Link bridge watcher: skipping invalid event ${line.slice(0, 120)}`);
    return "consumed";
  }
  try {
    await state.pi.sendUserMessage(event.prompt, { deliverAs: "aside", attribution: "agent" });
  } catch (error) {
    logError(
      state.pi,
      `Agent Link bridge watcher: injection failed, will retry: ${(error as Error).message}`,
    );
    return "failed";
  }
  try {
    state.ctx.ui.notify(`Agent Link ${event.kind ?? "message"}: ${event.message_id ?? ""}`, "info");
  } catch {
    // UI notification is best-effort; the event was injected either way.
  }
  return "consumed";
}

async function tick(): Promise<void> {
  const state = active;
  if (!state || state.busy) return;
  state.busy = true;
  let handle: FileHandle | undefined;
  try {
    let fileStat: Stats;
    try {
      fileStat = await stat(state.bridgeFile);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return;
      throw error;
    }
    if (fileStat.dev !== state.dev || fileStat.ino !== state.ino || fileStat.size < state.consumedPos) {
      // Replaced or truncated: restart from the beginning of the file.
      state.consumedPos = 0;
      state.tail = Buffer.alloc(0);
      state.dev = fileStat.dev;
      state.ino = fileStat.ino;
    }
    handle = await open(state.bridgeFile, "r");
    while (state.tail.length < MAX_WINDOW_BYTES) {
      const want = Math.min(READ_CHUNK_BYTES, MAX_WINDOW_BYTES - state.tail.length);
      const buffer = Buffer.alloc(want);
      const { bytesRead } = await handle.read(
        buffer,
        0,
        want,
        state.consumedPos + state.tail.length,
      );
      if (bytesRead <= 0) break;
      state.tail = Buffer.concat([state.tail, buffer.subarray(0, bytesRead)]);
    }
    for (;;) {
      const newline = state.tail.indexOf(0x0a);
      if (newline >= 0) {
        const line = state.tail.subarray(0, newline);
        state.tail = state.tail.subarray(newline + 1);
        state.consumedPos += newline + 1;
        const outcome = await handleLine(state, line.toString("utf8"));
        if (outcome === "failed") {
          // Put the failed line back at the head and stop; later events wait
          // behind it (order preserved) and the cursor is not advanced past it.
          state.tail = Buffer.concat([line, state.tail]);
          state.consumedPos -= newline + 1;
          await writeCursor(state);
          return;
        }
        continue;
      }
      if (state.tail.length < MAX_WINDOW_BYTES) break; // need more data
      // A single event line larger than the window: log once, skip it whole
      // and keep discarding until its terminating newline.
      logError(
        state.pi,
        `Agent Link bridge watcher: skipping oversized event line (> ${MAX_WINDOW_BYTES} bytes)`,
      );
      state.consumedPos += state.tail.length;
      state.tail = Buffer.alloc(0);
      let terminated = false;
      for (;;) {
        const buffer = Buffer.alloc(READ_CHUNK_BYTES);
        const { bytesRead } = await handle.read(buffer, 0, READ_CHUNK_BYTES, state.consumedPos);
        if (bytesRead <= 0) break;
        const chunk = buffer.subarray(0, bytesRead);
        const nextNewline = chunk.indexOf(0x0a);
        if (nextNewline >= 0) {
          state.tail = Buffer.from(chunk.subarray(nextNewline + 1));
          state.consumedPos += nextNewline + 1;
          terminated = true;
          break;
        }
        state.consumedPos += bytesRead;
      }
      if (!terminated) break; // EOF while discarding the oversized line
    }
    await writeCursor(state);
  } catch (error) {
    logError(state.pi, `Agent Link bridge watcher: tick failed: ${(error as Error).message}`);
  } finally {
    if (handle) {
      try {
        await handle.close();
      } catch {
        // Closing a already-gone file handle is harmless.
      }
    }
    state.busy = false;
  }
}

async function stopWatcher(): Promise<void> {
  const state = active;
  if (!state) return;
  active = null;
  try {
    state.ctx.clearTimer(state.timer);
  } catch {
    // The old session may already be gone; the lock still needs releasing.
  }
  try {
    const owner = await readFile(state.lockPath, "utf8");
    if (owner.trim() === state.token) await unlink(state.lockPath);
  } catch {
    // Missing or unreadable lock file: nothing of ours to release.
  }
}

async function startWatcher(pi: ExtensionAPI, ctx: BridgeContext): Promise<void> {
  // Sub sessions run in the same process and share module state; only the
  // main session may own the bridge file.
  if (ctx.agent?.kind !== "main") return;
  if (active) await stopWatcher();
  const bridgeFile = process.env.AGENT_LINK_BRIDGE_FILE;
  const binding = process.env.AGENT_LINK_BINDING;
  if (!bridgeFile || !binding) {
    const message =
      "Agent Link bridge watcher disabled: set AGENT_LINK_BRIDGE_FILE and AGENT_LINK_BINDING";
    logError(pi, message);
    try {
      ctx.ui.notify(message, "error");
    } catch {
      // Notifying is best-effort.
    }
    return;
  }
  const lockPath = bridgeFile + ".lock";
  const token = `${process.pid}:${randomUUID()}`;
  let acquired = false;
  try {
    const handle = await open(lockPath, "wx");
    try {
      await handle.writeFile(token);
    } finally {
      await handle.close();
    }
    acquired = true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") {
      const message = `Agent Link bridge watcher failed: cannot create ${lockPath}: ${(error as Error).message}`;
      logError(pi, message);
      try {
        ctx.ui.notify(message, "error");
      } catch {
        // Notifying is best-effort.
      }
      return;
    }
    // A lock left by a dead process is reclaimed once; a live owner wins and
    // this session refuses to become a second consumer.
    let owner = await readFile(lockPath, "utf8").catch(() => "");
    if (owner.trim() === "") {
      // The owner may be between creating the lock and writing its token;
      // give it a moment before treating the lock as stale.
      await sleep(50);
      owner = await readFile(lockPath, "utf8").catch(() => "");
    }
    const ownerPid = Number.parseInt(owner.split(":")[0] ?? "", 10);
    let alive = Number.isInteger(ownerPid) && ownerPid > 0;
    if (alive) {
      try {
        process.kill(ownerPid, 0);
      } catch (killError) {
        // EPERM means the process exists but belongs to someone else.
        alive = (killError as NodeJS.ErrnoException).code === "EPERM";
      }
    }
    if (alive) {
      const message = `Agent Link bridge watcher refused: ${bridgeFile} is already consumed by pid ${ownerPid}`;
      logError(pi, message);
      try {
        ctx.ui.notify(message, "error");
      } catch {
        // Notifying is best-effort.
      }
      return;
    }
    try {
      await unlink(lockPath);
    } catch {
      // Someone else cleaned it up first.
    }
    try {
      const handle = await open(lockPath, "wx");
      try {
        await handle.writeFile(token);
      } finally {
        await handle.close();
      }
      acquired = true;
    } catch (retryError) {
      const message = `Agent Link bridge watcher failed: cannot reclaim ${lockPath}: ${(retryError as Error).message}`;
      logError(pi, message);
      try {
        ctx.ui.notify(message, "error");
      } catch {
        // Notifying is best-effort.
      }
      return;
    }
  }
  if (!acquired) return;
  let consumedPos = 0;
  let dev = 0;
  let ino = 0;
  let fileStat: Stats | null = null;
  try {
    fileStat = await stat(bridgeFile);
    dev = fileStat.dev;
    ino = fileStat.ino;
  } catch {
    // No bridge file yet: the first tick starts from zero.
  }
  try {
    const cursor = JSON.parse(await readFile(bridgeFile + ".cursor", "utf8")) as Record<string, unknown>;
    if (
      cursor &&
      Number.isInteger(cursor.offset) &&
      (cursor.offset as number) >= 0 &&
      Number.isInteger(cursor.dev) &&
      Number.isInteger(cursor.ino) &&
      fileStat &&
      cursor.dev === fileStat.dev &&
      cursor.ino === fileStat.ino &&
      (cursor.offset as number) <= fileStat.size
    ) {
      consumedPos = cursor.offset as number;
    }
  } catch {
    // Missing or malformed cursor: start from the beginning.
  }
  const state: BridgeState = {
    pi,
    ctx,
    bridgeFile,
    binding,
    lockPath,
    token,
    timer: undefined,
    consumedPos,
    dev,
    ino,
    tail: Buffer.alloc(0),
    busy: false,
  };
  active = state;
  state.timer = ctx.setInterval(() => {
    void tick();
  }, 1000);
  void tick();
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

  pi.on("session_start", (_event, ctx) => {
    void startWatcher(pi, ctx as BridgeContext);
  });

  pi.on("session_shutdown", (_event, ctx) => {
    if ((ctx as BridgeContext).agent?.kind === "main") {
      void stopWatcher();
    }
  });
}
