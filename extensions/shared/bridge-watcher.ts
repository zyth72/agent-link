/**
 * Shared bridge-file watcher for Agent Link framework adapters (OMP, Pi, ...).
 *
 * A session consumes at most one parent bridge
 * (<root>/omp-bridge/<binding>.jsonl, child -> parent events) and one child
 * inbox (<root>/inbox/<binding>.jsonl, parent -> child events) at a time.
 * Host-specific behavior — session-id lookup, message injection, timers, the
 * main-session gate — is injected through BridgeHost; everything else (single
 * consumer lock, persisted cursor with file identity, append/replace/truncate
 * handling, oversized/invalid line skipping, order-preserving failure retry,
 * registry auto-claim) lives here once.
 *
 * Delivery is at-least-once: an event is replayed if the process dies after
 * injection but before the cursor is persisted, and a wholesale replacement of
 * the bridge file replays it from the start.  An event whose injection keeps
 * failing blocks later events until it succeeds so the order is preserved.
 */
import { randomUUID } from "node:crypto";
import {
  open,
  readdir,
  readFile,
  rename,
  stat,
  unlink,
  writeFile,
  type FileHandle,
  type Stats,
} from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

/** Read window per tick and the maximum size of a single event line. */
const MAX_WINDOW_BYTES = 1024 * 1024;
const READ_CHUNK_BYTES = 64 * 1024;

export type WatcherRole = "parent" | "child";

/** Host-specific behavior the watcher cannot know about. */
export type BridgeHost<C> = {
  /** Framework id matched in registry claims ("omp", "pi", ...). */
  framework: string;
  /** Roles auto-claim scans for ("parent" consumes omp-bridge, "child" inbox). */
  roles: readonly WatcherRole[];
  /** Only the main session may consume bridge files. */
  isMain(ctx: C): boolean;
  /** This session's id, or undefined when the host cannot provide one. */
  sessionId(ctx: C): string | undefined;
  setTimer(ctx: C, callback: () => void, ms: number): unknown;
  clearTimer(ctx: C, handle: unknown): void;
  /** Deliver one validated event prompt into the session; throw = retry later. */
  inject(ctx: C, prompt: string): Promise<void> | void;
  /** Best-effort UI notification of watcher activity; may be a no-op. */
  notify(ctx: C, message: string, level: "info" | "error"): void;
  /** Error log channel; must never throw. */
  logError(message: string): void;
};

type BridgeState = {
  role: WatcherRole;
  bridgeFile: string;
  binding: string;
  /** Auto-claim mode additionally pins the session id events must target. */
  expectedThreadId?: string;
  lockPath: string;
  token: string;
  timer: unknown;
  consumedPos: number;
  dev: number;
  ino: number;
  tail: Buffer;
  busy: boolean;
  inject(prompt: string): Promise<void> | void;
  notify(message: string, level: "info" | "error"): void;
  logError(message: string): void;
  clearTimer(): void;
};

/** Bridge files this process consumes (parent bridge and/or child inbox). */
const watchers: Partial<Record<WatcherRole, BridgeState>> = {};

/** Pending auto-claim rescan while some role has not been claimed yet. */
let rescan: { timer: unknown; clear: () => void } | null = null;

/** Set while an auto-claim attempt is running (the rescan timer may overlap). */
let claimInFlight = false;

/** Registry env override shared with the Python side (agent_link/registry.py). */
const REGISTRY_ENV = "AGENT_LINK_REGISTRY";
const AUTO_CLAIM_POLL_MS = 2000;

function registryDir(): string {
  const override = process.env[REGISTRY_ENV];
  return override ?? join(homedir(), ".agent-link", "registry");
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : undefined;
}

/** Newest-updated registry entry naming this session for the given role. */
async function findClaim(
  framework: string,
  sessionId: string,
  role: WatcherRole,
): Promise<{ file: string; binding: string } | null> {
  const directory = registryDir();
  let names: string[];
  try {
    names = await readdir(directory);
  } catch {
    return null; // No registry yet: nothing claimable.
  }
  const matches: { updated: string; binding: string; root: string }[] = [];
  for (const name of names) {
    if (!name.endsWith(".json")) continue;
    let parsed: unknown;
    try {
      parsed = JSON.parse(await readFile(join(directory, name), "utf8"));
    } catch {
      continue; // Malformed entries are ignored.
    }
    const record = asRecord(parsed);
    const side = asRecord(record?.[role]);
    const bindingId = record?.binding_id;
    const root = record?.root;
    if (
      record?.version === 1 &&
      typeof bindingId === "string" &&
      typeof root === "string" &&
      side?.framework === framework &&
      side?.session_id === sessionId
    ) {
      const updated = record.updated_at;
      matches.push({
        updated: typeof updated === "string" ? updated : "",
        binding: bindingId,
        root,
      });
    }
  }
  // The binding whose service started most recently wins; an explicit env
  // override remains available when several live bindings share this session.
  matches.sort((a, b) => b.updated.localeCompare(a.updated));
  const chosen = matches[0];
  if (!chosen) return null;
  const dir = role === "parent" ? "omp-bridge" : "inbox";
  return { file: join(chosen.root, dir, `${chosen.binding}.jsonl`), binding: chosen.binding };
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
    state.logError(
      `Agent Link bridge watcher: cannot persist cursor: ${(error as Error).message}`,
    );
  }
}

async function handleLine(state: BridgeState, line: string): Promise<"consumed" | "failed"> {
  let event: Record<string, unknown>;
  try {
    event = JSON.parse(line) as Record<string, unknown>;
  } catch {
    state.logError("Agent Link bridge watcher: skipping unparsable event line");
    return "consumed";
  }
  if (
    !event ||
    typeof event !== "object" ||
    event.v !== 1 ||
    typeof event.prompt !== "string" ||
    event.prompt === "" ||
    event.binding_id !== state.binding ||
    (state.expectedThreadId !== undefined &&
      event[state.role === "parent" ? "thread_id" : "child_session_id"] !==
        state.expectedThreadId)
  ) {
    state.logError(`Agent Link bridge watcher: skipping invalid event ${line.slice(0, 120)}`);
    return "consumed";
  }
  try {
    await state.inject(event.prompt);
  } catch (error) {
    state.logError(
      `Agent Link bridge watcher: injection failed, will retry: ${(error as Error).message}`,
    );
    return "failed";
  }
  state.notify(`Agent Link ${event.kind ?? "message"}: ${event.message_id ?? ""}`, "info");
  return "consumed";
}

async function tick(state: BridgeState): Promise<void> {
  if (watchers[state.role] !== state || state.busy) return;
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
          // Put the failed line back at the head, including its terminating
          // newline (the split loop strips it): without it a same-process
          // retry would merge this line with the next one and both would be
          // skipped as unparseable.
          state.tail = Buffer.concat([line, Buffer.from("\n"), state.tail]);
          state.consumedPos -= newline + 1;
          await writeCursor(state);
          return;
        }
        continue;
      }
      if (state.tail.length < MAX_WINDOW_BYTES) break; // need more data
      // A single event line larger than the window: log once, skip it whole
      // and keep discarding until its terminating newline.
      state.logError(
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
    state.logError(`Agent Link bridge watcher: tick failed: ${(error as Error).message}`);
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

async function stopWatcher(role: WatcherRole): Promise<void> {
  const state = watchers[role];
  if (!state) return;
  delete watchers[role];
  try {
    state.clearTimer();
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

/** Stop both watchers and any pending auto-claim rescan. */
export async function stopBridgeWatchers(): Promise<void> {
  if (rescan) {
    try {
      rescan.clear();
    } catch {
      // The session may already be gone.
    }
    rescan = null;
  }
  await stopWatcher("parent");
  await stopWatcher("child");
}

/**
 * Start the watcher for this session: an explicit AGENT_LINK_BRIDGE_FILE +
 * AGENT_LINK_BINDING pair always wins; otherwise the session auto-claims from
 * the user-level registry by matching bindings that name its own session id
 * (framework must match the host's).  bind may run after the session started,
 * so unclaimed roles are rescanned periodically.
 */
export async function startBridgeWatchers<C>(host: BridgeHost<C>, ctx: C): Promise<void> {
  // Sub sessions share module state with the main session; only the main
  // session may own bridge files.
  if (!host.isMain(ctx)) return;
  await stopBridgeWatchers();
  const bridgeFile = process.env.AGENT_LINK_BRIDGE_FILE;
  const binding = process.env.AGENT_LINK_BINDING;
  if (bridgeFile && binding) {
    await attachWatcher(host, ctx, "parent", bridgeFile, binding, undefined);
    return;
  }
  if (bridgeFile || binding) {
    const message =
      "Agent Link bridge watcher disabled: set both AGENT_LINK_BRIDGE_FILE and AGENT_LINK_BINDING";
    host.logError(message);
    host.notify(ctx, message, "error");
    return;
  }
  const sessionId = host.sessionId(ctx);
  if (!sessionId) {
    host.logError(
      "Agent Link bridge watcher disabled: no AGENT_LINK_BRIDGE_FILE/AGENT_LINK_BINDING and no session id available for auto-claim",
    );
    return;
  }
  const tryClaim = async (): Promise<void> => {
    if (claimInFlight) return;
    claimInFlight = true;
    try {
      for (const role of host.roles) {
        if (!watchers[role]) {
          const claim = await findClaim(host.framework, sessionId, role);
          if (claim && !watchers[role]) {
            await attachWatcher(host, ctx, role, claim.file, claim.binding, sessionId);
          }
        }
      }
    } finally {
      claimInFlight = false;
    }
  };
  await tryClaim();
  if (host.roles.some((role) => !watchers[role])) {
    const timer = host.setTimer(ctx, () => void tryClaim(), AUTO_CLAIM_POLL_MS);
    rescan = { timer, clear: () => host.clearTimer(ctx, timer) };
  }
}

async function attachWatcher<C>(
  host: BridgeHost<C>,
  ctx: C,
  role: WatcherRole,
  bridgeFile: string,
  binding: string,
  expectedThreadId: string | undefined,
): Promise<void> {
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
      host.logError(message);
      host.notify(ctx, message, "error");
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
      host.logError(message);
      host.notify(ctx, message, "error");
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
      host.logError(message);
      host.notify(ctx, message, "error");
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
    const cursor = JSON.parse(await readFile(bridgeFile + ".cursor", "utf8")) as Record<
      string,
      unknown
    >;
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
    role,
    bridgeFile,
    binding,
    expectedThreadId,
    lockPath,
    token,
    timer: undefined,
    consumedPos,
    dev,
    ino,
    tail: Buffer.alloc(0),
    busy: false,
    inject: (prompt) => host.inject(ctx, prompt),
    notify: (message, level) => host.notify(ctx, message, level),
    logError: (message) => host.logError(message),
    clearTimer: () => host.clearTimer(ctx, state.timer),
  };
  watchers[role] = state;
  if (watchers.parent && watchers.child && rescan) {
    rescan.clear();
    rescan = null;
  }
  state.timer = host.setTimer(ctx, () => void tick(state), 1000);
  void tick(state);
}
