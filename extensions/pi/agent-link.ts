/**
 * Agent Link child adapter for Pi.
 *
 * The child process receives only a loopback endpoint and bearer token via the
 * AGENT_LINK_CREDENTIALS file written by the parent's host service.  These
 * tools can report progress, ask the bound parent and read this child's own
 * persisted request.  They cannot choose a parent session, create bindings or
 * reply.  See docs/HOST-PROTOCOL.md.
 */
import { readFile } from "node:fs/promises";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const CREDENTIALS_ENV = "AGENT_LINK_CREDENTIALS";
const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
/** Extra client-side slack over the server-side ask deadline. */
const ASK_RESPONSE_GRACE_MS = 15_000;

type HostResult = { result: Record<string, unknown>; exitCode: number };

function invalidCredentials(reason: string): never {
  throw new Error(`Invalid Agent Link credentials: ${reason}`);
}

async function loadEndpoint(): Promise<{ endpoint: URL; token: string }> {
  const path = process.env[CREDENTIALS_ENV];
  if (!path) {
    throw new Error(
      `${CREDENTIALS_ENV} is not set; the parent host service writes the child credential file`,
    );
  }
  let raw: unknown;
  try {
    raw = JSON.parse(await readFile(path, "utf8"));
  } catch (error) {
    throw new Error(`Cannot read Agent Link credentials at ${path}: ${(error as Error).message}`);
  }
  const credentials = raw as Record<string, unknown>;
  if (credentials.protocol !== "agent-link/1") invalidCredentials("unsupported protocol");
  let endpoint: URL;
  try {
    endpoint = new URL(String(credentials.endpoint ?? ""));
  } catch {
    invalidCredentials("endpoint is not a URL");
  }
  if (
    endpoint.protocol !== "http:" ||
    endpoint.hostname !== "127.0.0.1" ||
    endpoint.username ||
    endpoint.password ||
    endpoint.pathname !== "/" ||
    endpoint.search ||
    endpoint.hash
  ) {
    invalidCredentials("endpoint must be plain http://127.0.0.1:<port>/ (loopback only)");
  }
  const port = Number(endpoint.port);
  if (!Number.isInteger(port) || port < 1 || port > 65535) invalidCredentials("endpoint port");
  if (typeof credentials.token !== "string" || credentials.token.length < 16) {
    invalidCredentials("token is missing or too short");
  }
  return { endpoint, token: credentials.token as string };
}

async function callHost(
  operation: string,
  params: Record<string, unknown>,
  signal: AbortSignal | undefined,
  timeoutMs: number,
): Promise<HostResult> {
  const { endpoint, token } = await loadEndpoint();
  const signals = [signal, AbortSignal.timeout(timeoutMs)].filter(
    (value): value is AbortSignal => value !== undefined,
  );
  let response: Response;
  try {
    response = await fetch(new URL(`/v1/${operation}`, endpoint), {
      method: "POST",
      redirect: "error",
      signal: AbortSignal.any(signals),
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(params),
    });
  } catch (error) {
    const name = (error as Error).name;
    if (name === "AbortError" || name === "TimeoutError") {
      throw new Error(
        `Agent Link ${operation} did not finish within ${Math.round(timeoutMs / 1000)}s. ` +
          "The request may still be persisted: check the same IDs instead of changing them.",
      );
    }
    throw new Error(`Agent Link ${operation} request failed: ${(error as Error).message}`);
  }
  const body = await response.text();
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(body) as Record<string, unknown>;
  } catch {
    throw new Error(
      `Agent Link ${operation} returned non-JSON HTTP ${response.status}: ${body.slice(0, 200)}`,
    );
  }
  if (!response.ok) {
    const code = String(payload.error ?? "error");
    throw new Error(
      `Agent Link ${operation} rejected (HTTP ${response.status}, ${code}): ` +
        `${String(payload.message ?? body.slice(0, 200))}`,
    );
  }
  if (typeof payload.exit_code !== "number" || typeof payload.result !== "object") {
    throw new Error(`Agent Link ${operation} returned an unexpected envelope: ${body.slice(0, 200)}`);
  }
  return { result: payload.result as Record<string, unknown>, exitCode: payload.exit_code };
}

function asText(value: unknown): string {
  return value === null || value === undefined ? "<none>" : JSON.stringify(value);
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
}
