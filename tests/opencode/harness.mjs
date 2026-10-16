/**
 * Shared helpers for the OpenCode Agent Link plugin tests.
 *
 * Loads the real TypeScript plugin (extensions/opencode/agent-link.ts, v2
 * plugin API: default export { id, setup(ctx) }) with a fake plugin context
 * that records tool registrations.  The plugin no longer watches bridge files
 * or injects prompts; message transport lives on the Python host side.
 * Runs under Bun or Node (type stripping); no OpenCode server and no model is
 * involved.
 */
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
  const logs = [];
  // The plugin logs through console.error; capture it for diagnostics.
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
  };
  return { ctx, tools, logs };
}
