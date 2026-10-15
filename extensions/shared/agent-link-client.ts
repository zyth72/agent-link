/**
 * Agent Link child client shared by the Pi and OMP extension adapters.
 *
 * The child process receives only a loopback endpoint and bearer token via the
 * AGENT_LINK_CREDENTIALS file written by the parent's host service.  This
 * module has no framework imports (only node:fs/promises and the global fetch /
 * AbortSignal) so the same code runs under the Pi jiti loader and Bun.
 */
import { readFile } from "node:fs/promises";

export const CREDENTIALS_ENV = "AGENT_LINK_CREDENTIALS";
export const DEFAULT_REQUEST_TIMEOUT_MS = 30_000;
/** Extra client-side slack over the server-side ask deadline. */
export const ASK_RESPONSE_GRACE_MS = 15_000;

export type HostResult = { result: Record<string, unknown>; exitCode: number };

function invalidCredentials(reason: string): never {
  throw new Error(`Invalid Agent Link credentials: ${reason}`);
}

export async function loadEndpoint(): Promise<{ endpoint: URL; token: string }> {
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

export async function callHost(
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

export function asText(value: unknown): string {
  return value === null || value === undefined ? "<none>" : JSON.stringify(value);
}
