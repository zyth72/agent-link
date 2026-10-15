/**
 * Shared helpers for the OMP Agent Link extension tests.
 *
 * Runs the real TypeScript extension under Bun (its native loader, no
 * transpile step and no OMP session) with a fake ExtensionAPI that records
 * tool registrations, lifecycle handlers, injected messages, UI notifications
 * and log lines.  Also provides a small authenticated push client so tests can
 * drive the session-owned loopback listener with real HTTP requests.  No model
 * is involved.
 */
import { existsSync, readFileSync, unlinkSync } from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
export const EXTENSION_PATH =
  process.env.AGENT_LINK_OMP_EXTENSION ??
  path.join(REPO_ROOT, "extensions", "omp", "agent-link.ts");

export async function loadOmpExtension() {
  const extension = await import(pathToFileURL(EXTENSION_PATH).href);
  if (typeof extension.default !== "function") {
    throw new Error(`${EXTENSION_PATH} does not export a default extension factory`);
  }
  return extension.default;
}

export function fakeOmp() {
  const tools = new Map();
  const lifecycle = new Map();
  const sent = [];
  const logs = [];
  const labels = [];
  const pi = {
    zod: {
      object: (shape) => ({ kind: "object", shape }),
      string: (options) => ({ kind: "string", options }),
      number: (options) => ({ kind: "number", options }),
    },
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
    async sendUserMessage(text, options) {
      // Test hook: while the marker file exists, an injection whose text
      // contains its content fails exactly once (the marker is consumed).
      const failFile = process.env.AGENT_LINK_TEST_FAIL_FILE;
      if (failFile && existsSync(failFile)) {
        const marker = readFileSync(failFile, "utf8").trim();
        if (marker && String(text).includes(marker)) {
          unlinkSync(failFile);
          throw new Error(`simulated injection failure (${marker})`);
        }
      }
      sent.push({ text: String(text), options });
    },
    logger: {
      error(message) {
        logs.push(String(message));
      },
    },
    setLabel(label) {
      labels.push(String(label));
    },
  };
  return { pi, tools, lifecycle, sent, logs, labels };
}

export function makeCtx(options = {}) {
  const timers = [];
  const cleared = [];
  const notifies = [];
  const ctx = {
    agent: { kind: options.kind ?? "main" },
    cwd: options.cwd ?? REPO_ROOT,
    setInterval(callback, ms) {
      const handle = { index: timers.length, ms };
      timers.push({ handle, callback });
      return handle;
    },
    clearTimer(handle) {
      cleared.push(handle);
    },
    ui: {
      notify(message, level) {
        if (process.env.AGENT_LINK_TEST_NOTIFY_THROW) {
          throw new Error("simulated notify failure");
        }
        notifies.push({ message: String(message), level: String(level) });
      },
    },
    sessionManager: { getSessionId: () => options.sessionId },
  };
  return { ctx, timers, cleared, notifies };
}

export function sessionsDir() {
  return process.env.AGENT_LINK_SESSIONS;
}

export function descriptorPath(framework, sessionId) {
  return path.join(sessionsDir(), framework, `${sessionId}.json`);
}

export function readDescriptor(framework, sessionId) {
  return JSON.parse(readFileSync(descriptorPath(framework, sessionId), "utf8"));
}

export function pushEvent(descriptor, overrides = {}) {
  return {
    protocol: "agent-link-push/1",
    framework: descriptor.framework,
    session_id: descriptor.session_id,
    binding_id: "b-1",
    run_id: "r-1",
    message_id: "m-1",
    role: "parent",
    prompt: "pushed prompt",
    ...overrides,
  };
}

/** POST one notification to the listener; resolves {status, body}. */
export async function postPush(descriptor, event, options = {}) {
  const body =
    options.rawBody !== undefined
      ? Buffer.isBuffer(options.rawBody)
        ? options.rawBody
        : Buffer.from(options.rawBody)
      : Buffer.from(JSON.stringify(event));
  const headers = { "Content-Type": "application/json", "Content-Length": String(body.length) };
  if (options.authorization !== null) {
    headers.Authorization = `Bearer ${options.authorization ?? descriptor.token}`;
  }
  return new Promise((resolve, reject) => {
    const request = http.request(
      `${descriptor.endpoint}${options.path ?? "/v1/notify"}`,
      { method: options.method ?? "POST", headers },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          let parsed = {};
          try {
            parsed = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          } catch {
            parsed = {};
          }
          resolve({ status: response.statusCode, body: parsed });
        });
      },
    );
    request.on("error", reject);
    request.end(body);
  });
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
    if (condition()) return true;
    await sleep(intervalMs);
  }
  return condition();
}
