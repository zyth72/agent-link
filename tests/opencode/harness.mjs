/**
 * Shared helpers for the OpenCode Agent Link plugin tests.
 *
 * Loads the real TypeScript plugin (extensions/opencode/agent-link.ts, v2
 * plugin API: default export { id, setup(ctx) }) with a fake plugin context
 * that records tool registrations, prompt-hook handlers, injected prompts,
 * event emissions and log lines.  Runs under Bun or Node (type stripping);
 * no OpenCode server and no model is involved.
 */
import { existsSync, readFileSync, unlinkSync } from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
export const EXTENSION_PATH =
  process.env.AGENT_LINK_OPENCODE_EXTENSION ??
  path.join(REPO_ROOT, "extensions", "opencode", "agent-link.ts");

export async function loadOpencodeExtension() {
  const extension = await import(pathToFileURL(EXTENSION_PATH).href);
  const plugin = extension.default;
  if (!plugin || typeof plugin.id !== "string" || typeof plugin.setup !== "function") {
    throw new Error(`${EXTENSION_PATH} does not default-export a v2 plugin { id, setup }`);
  }
  return plugin;
}

export function fakeOpencode() {
  const tools = new Map();
  const promptHooks = [];
  const sent = [];
  const logs = [];
  const eventWaiters = [];
  const eventQueue = [];
  // The plugin logs through console.error and times through the global
  // interval functions (server-level plugin ctx has neither); capture both.
  if (!console.error.__agentLinkPatched) {
    const original = console.error;
    const patched = (...args) => {
      fakeOpencode.activeLogs?.push(args.map(String).join(" "));
      original.apply(console, args);
    };
    patched.__agentLinkPatched = true;
    console.error = patched;
  }
  fakeOpencode.activeLogs = logs;
  const intervals = new Set();
  const clearedIntervals = [];
  if (!globalThis.setInterval.__agentLinkPatched) {
    const realSet = globalThis.setInterval;
    const realClear = globalThis.clearInterval;
    const patchedSet = (callback, ms, ...rest) => {
      const handle = realSet(callback, ms, ...rest);
      fakeOpencode.activeIntervals?.add(handle);
      return handle;
    };
    patchedSet.__agentLinkPatched = true;
    const patchedClear = (handle) => {
      if (fakeOpencode.activeIntervals?.delete(handle)) {
        fakeOpencode.activeCleared?.push(handle);
      }
      return realClear(handle);
    };
    globalThis.setInterval = patchedSet;
    globalThis.clearInterval = patchedClear;
  }
  fakeOpencode.activeIntervals = intervals;
  fakeOpencode.activeCleared = clearedIntervals;
  const ctx = {
    tool: {
      async transform(callback) {
        callback({
          add(tool) {
            if (!tool || typeof tool.name !== "string" || typeof tool.execute !== "function") {
              throw new Error("editor.add received an invalid tool definition");
            }
            tools.set(tool.name, tool);
          },
        });
      },
      async list() {
        return [...tools.values()].map((tool) => ({ id: tool.name, ...tool }));
      },
    },
    session: {
      async hook(name, callback) {
        if (name !== "prompt") throw new Error(`unexpected hook ${name}`);
        promptHooks.push(callback);
        return {
          dispose() {
            const index = promptHooks.indexOf(callback);
            if (index >= 0) promptHooks.splice(index, 1);
          },
        };
      },
      async prompt(input) {
        // Test hook: while the marker file exists, an injection whose text
        // contains its content fails exactly once (the marker is consumed).
        const failFile = process.env.AGENT_LINK_TEST_FAIL_FILE;
        if (failFile && existsSync(failFile)) {
          const marker = readFileSync(failFile, "utf8").trim();
          if (marker && String(input.text).includes(marker)) {
            unlinkSync(failFile);
            throw new Error(`simulated injection failure (${marker})`);
          }
        }
        sent.push({ ...input, text: String(input.text) });
      },
    },
    event: {
      subscribe() {
        return {
          [Symbol.asyncIterator]() {
            return {
              next() {
                if (eventQueue.length > 0) {
                  return Promise.resolve({ done: false, value: eventQueue.shift() });
                }
                return new Promise((resolve) => eventWaiters.push(resolve));
              },
              return() {
                return Promise.resolve({ done: true, value: undefined });
              },
            };
          },
        };
      },
    },
  };
  const emitEvent = (event) => {
    const waiter = eventWaiters.shift();
    if (waiter) waiter({ done: false, value: event });
    else eventQueue.push(event);
  };
  const firePromptHook = async (sessionID) => {
    for (const hook of [...promptHooks]) await hook({ sessionID });
  };
  return { ctx, tools, promptHooks, sent, logs, emitEvent, firePromptHook, intervals, clearedIntervals };
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function waitFor(condition, { timeoutMs = 15000, intervalMs = 25 } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (await condition()) return;
    if (Date.now() >= deadline) throw new Error("waitFor timed out");
    await sleep(intervalMs);
  }
}
