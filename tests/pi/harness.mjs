/**
 * Shared helpers for the Pi Agent Link extension tests.
 *
 * Loads the real TypeScript extension through the same jiti aliases the Pi
 * runtime uses and returns a fake ExtensionAPI that records registered tools.
 * The extension's imports are aliased to the locally installed Pi packages, so
 * no dependency installation and no model call is needed.
 */
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
export const EXTENSION_PATH =
  process.env.AGENT_LINK_EXTENSION ?? path.join(REPO_ROOT, "extensions", "pi", "agent-link.ts");

export function findPiRoot() {
  const candidates = [];
  if (process.env.PI_CODING_AGENT_ROOT) candidates.push(process.env.PI_CODING_AGENT_ROOT);
  if (process.env.HOME) {
    candidates.push(
      path.join(process.env.HOME, ".npm-global", "lib", "node_modules", "@earendil-works", "pi-coding-agent"),
    );
  }
  for (const candidate of candidates) {
    if (candidate && existsSync(path.join(candidate, "dist", "index.js"))) return candidate;
  }
  throw new Error(
    "Cannot locate @earendil-works/pi-coding-agent; set PI_CODING_AGENT_ROOT to its package root",
  );
}

async function jitiLoader() {
  const piRoot = findPiRoot();
  const jitiEntry = path.join(piRoot, "node_modules", "jiti", "lib", "jiti.mjs");
  const codingAgent = path.join(piRoot, "dist", "index.js");
  const piAi = path.join(
    piRoot,
    "node_modules",
    "@earendil-works",
    "pi-ai",
    "dist",
    "compat.js",
  );
  for (const required of [jitiEntry, codingAgent, piAi]) {
    if (!existsSync(required)) throw new Error(`Missing test dependency: ${required}`);
  }
  const { createJiti } = await import(pathToFileURL(jitiEntry).href);
  const jiti = createJiti(import.meta.url, {
    alias: {
      "@earendil-works/pi-coding-agent": codingAgent,
      "@earendil-works/pi-ai": piAi,
    },
  });
  return { jiti, piRoot };
}

export async function loadExtension() {
  const { jiti, piRoot } = await jitiLoader();
  const extension = await jiti.import(EXTENSION_PATH);
  if (typeof extension.default !== "function") {
    throw new Error(`${EXTENSION_PATH} does not export a default extension factory`);
  }
  return { factory: extension.default, piRoot };
}

/** Load the shared push listener module directly (no extension needed). */
export async function loadSessionPush() {
  const { jiti } = await jitiLoader();
  return jiti.import(path.join(REPO_ROOT, "extensions", "shared", "session-push.ts"));
}

export function fakePi() {
  const tools = new Map();
  const lifecycle = new Map();
  const sent = [];
  const logs = [];
  const pi = {
    registerTool(tool) {
      if (!tool || typeof tool.name !== "string" || typeof tool.execute !== "function") {
        throw new Error("registerTool received an invalid tool definition");
      }
      tools.set(tool.name, tool);
      return tool;
    },
    on(event, handler) {
      if (!lifecycle.has(event)) lifecycle.set(event, []);
      lifecycle.get(event).push(handler);
    },
    sendUserMessage(text, options) {
      // Test hook: while `pi.failOn` is a non-empty string contained in the
      // text, the injection throws exactly once (the marker is consumed).
      if (pi.failOn && String(text).includes(pi.failOn)) {
        pi.failOn = null;
        throw new Error("simulated injection failure");
      }
      sent.push({ text: String(text), options });
    },
    failOn: null,
    logger: {
      error(message) {
        logs.push(String(message));
      },
    },
  };
  return { pi, tools, lifecycle, sent, logs };
}

/** Minimal ExtensionContext stand-in for the parent bridge watcher. */
export function makePiCtx(options = {}) {
  const notifies = [];
  const ctx = {
    isIdle: options.isIdle ?? (() => true),
    ui: {
      notify(message, level) {
        notifies.push({ message: String(message), level: String(level) });
      },
    },
    sessionManager: { getSessionId: () => options.sessionId ?? "pi-session" },
  };
  return { ctx, notifies };
}

export async function fireLifecycle(lifecycle, event, ctx) {
  for (const handler of lifecycle.get(event) ?? []) {
    await handler({}, ctx);
  }
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function waitFor(condition, { timeoutMs = 8000, intervalMs = 25 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await condition()) return;
    await sleep(intervalMs);
  }
  if (await condition()) return;
  throw new Error("waitFor timed out");
}
