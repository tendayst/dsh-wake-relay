/**
 * Minimal client for dsh-wake-bridge.
 *
 * Reads `$DSH_HOME/wake-endpoint.json` (or an explicit {port, token}) and POSTs
 * one wake event. Nothing is retried here on purpose: the bridge returns 503
 * only when it recorded nothing, so the decision to retry — and how often —
 * belongs to whatever owns the schedule.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

export function dshHome() {
  return process.env.DSH_HOME || join(homedir(), ".dsh");
}

export function readEndpoint(home = dshHome()) {
  const raw = JSON.parse(readFileSync(join(home, "wake-endpoint.json"), "utf8"));
  if (!raw || !raw.port) {
    const error = new Error("wake endpoint is not published (host is down or the plugin is not loaded)");
    error.code = "no_endpoint";
    throw error;
  }
  return raw;
}

async function post(path, payload, endpoint, timeoutMs) {
  const ep = endpoint || readEndpoint();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(`http://127.0.0.1:${ep.port}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json; charset=utf-8", "x-wake-token": ep.token },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    const body = await res.json().catch(() => ({}));
    return { status: res.status, ok: res.status === 200, body };
  } catch (error) {
    return { status: 0, ok: false, body: { reason: error && error.name === "AbortError" ? "timeout" : "unreachable" } };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * POST one wake event.
 *
 * @returns {Promise<{status:number, ok:boolean, retryable:boolean, body:object}>}
 *   200 -> delivered and flushed.
 *   429 -> dropped by policy (duplicate / cooldown / burst). Do NOT retry.
 *   503 -> nothing was recorded. Same event_id may be retried verbatim.
 */
export async function wake(event, { endpoint, timeoutMs = 15_000 } = {}) {
  if (!event || !event.event_id) throw new Error("event_id is required");
  const result = await post("/wake", event, endpoint, timeoutMs);
  return { ...result, retryable: result.status === 503 };
}

export async function bind(sessionId, { endpoint, timeoutMs = 5_000 } = {}) {
  return post("/bind", { sessionId }, endpoint, timeoutMs);
}

export async function status({ endpoint, timeoutMs = 5_000 } = {}) {
  const ep = endpoint || readEndpoint();
  const res = await fetch(`http://127.0.0.1:${ep.port}/status`, {
    headers: { "x-wake-token": ep.token },
  });
  return { status: res.status, body: await res.json().catch(() => ({})) };
}
