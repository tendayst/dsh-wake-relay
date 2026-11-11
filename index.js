/**
 * dsh-wake-relay
 *
 * A local-only HTTP endpoint that lets an external process (a watcher daemon, a
 * build listener, a CI callback on the same machine) wake a Harness session and
 * start exactly one agent turn.
 *
 * Delivery mirrors `@deepseek-ai/dsh-schedule`: resolve the session through the
 * session controller, append a user message with `followup`, then flush the
 * session so persistence acknowledges it before we report success.
 *
 * Delivery is synchronous. HTTP 200 means "the message was accepted by the
 * session AND flushed". A failed delivery returns 503 and records nothing, so
 * the caller can retry verbatim with the same `event_id`. That single property
 * is what lets an external process own the retry loop: it never has to guess
 * whether a timeout meant "delivered" or "not delivered".
 *
 * Transport is HTTP on 127.0.0.1 only. The live port + token are published to
 * `$DSH_HOME/wake-endpoint.json` so callers discover them instead of keeping a
 * second copy of the config.
 *
 * Routing: a request may name its own `session`. Without it the bridge uses the
 * configured `sessionId`. An explicit id that cannot be resolved is an error,
 * never a silent fallback — waking the wrong context is worse than waking none.
 */
import { createServer } from "node:http";
import { randomBytes } from "node:crypto";
import {
  appendFileSync,
  mkdirSync,
  readFileSync,
  renameSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { createUserMessage } from "@deepseek-ai/dsh-llm";

export const inject = ["sessionController", "sessions"];

const DEFAULTS = {
  port: 45871,
  // Empty means "generate once and remember". An empty token disables the
  // endpoint on purpose in older builds; keeping that behaviour loses the
  // feature silently, which is worse than generating a secret.
  token: "",
  // Default target. Empty = only requests that carry an explicit `session` are
  // accepted (single-project installs usually pin this in cordis.patch.yml).
  sessionId: "",
  // Per-reason minimum spacing. It used to be global, which made every rule
  // starve every other rule for 90s; per-reason is the only sane shape when one
  // bridge serves many independent watchers.
  cooldownMs: 90_000,
  reasonWindowMs: 300_000,
  reasonMaxHits: 2,
  // Any two wakes, regardless of reason. Small on purpose: it only collapses
  // simultaneous bursts, it is not a rate limit.
  globalBurstMs: 3_000,
  // A stuck flush would block every later wake, since the HTTP side is serial
  // per socket and the caller is often a 30s-interval poller.
  flushTimeoutMs: 10_000,
  // Same event_id inside this window is a duplicate.
  dedupeWindowMs: 7 * 24 * 3600 * 1000,
  // Message body. Empty = built-in generic template (see renderMessage).
  template: "",
  // Optional last line, e.g. "按当前计划处置；不要临时改变风险参数".
  footnote: "",
  // Absolute path of an external watcher's state file, surfaced by GET /status.
  // Empty = `$DSH_HOME/wake-bridge-watcher/waiter-state.json` if it exists.
  watcherStatePath: "",
};

const DSH_HOME = process.env.DSH_HOME || join(homedir(), ".dsh");
const EVENTS_PATH = join(DSH_HOME, "wake-bridge-events.jsonl");
const SECRET_PATH = join(DSH_HOME, "wake-bridge-secret.json");
const ENDPOINT_PATH = join(DSH_HOME, "wake-endpoint.json");
const LOG_PATH = join(DSH_HOME, "wake-bridge.log");
const WATCHER_STATE_DEFAULT = join(DSH_HOME, "wake-bridge-watcher", "waiter-state.json");

function log(line) {
  try {
    mkdirSync(DSH_HOME, { recursive: true });
    appendFileSync(LOG_PATH, `[${new Date().toISOString()}] ${line}\n`, "utf8");
  } catch {
    /* logging must never fail a wake */
  }
}

function reply(res, code, payload) {
  const body = JSON.stringify(payload, null, 0);
  res.writeHead(code, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
  });
  res.end(body);
}

function sanitize(value, max) {
  if (typeof value !== "string") return "";
  return value.replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, max);
}

// ---------------------------------------------------------------------------
// durable fire log: append-only, folded back into memory on boot
// ---------------------------------------------------------------------------

/**
 * Every accepted fire appends one line. Nothing is ever rewritten, so a crash
 * between "delivered" and "recorded" cannot lose the whole dedupe table the way
 * a full `writeFileSync` of a JSON object can.
 */
function appendFire(record) {
  try {
    mkdirSync(DSH_HOME, { recursive: true });
    appendFileSync(EVENTS_PATH, `${JSON.stringify(record)}\n`, "utf8");
  } catch (error) {
    log(`could not append fire log: ${error && error.message ? error.message : String(error)}`);
  }
}

function foldFireLog(windowMs) {
  const seen = {};
  const reasonHits = {};
  let lastAt = 0;
  let lines = 0;
  let text = "";
  try {
    text = readFileSync(EVENTS_PATH, "utf8");
  } catch {
    return { seen, reasonHits, lastAt, lines, compact: false };
  }
  const cutoff = Date.now() - windowMs;
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    lines += 1;
    let rec;
    try {
      rec = JSON.parse(line);
    } catch {
      continue;
    }
    const at = Number(rec.at);
    if (!(at >= cutoff)) continue;
    if (rec.id) seen[rec.id] = at;
    if (rec.reason) (reasonHits[rec.reason] ||= []).push(at);
    if (at > lastAt) lastAt = at;
  }
  // Compact when the file has outgrown the window (rewrite, then rename).
  const tooBig = lines > 5000 || (text.length > 2 * 1024 * 1024);
  return { seen, reasonHits, lastAt, lines, compact: tooBig };
}

function compactFireLog(keepIds) {
  const tmp = `${EVENTS_PATH}.tmp`;
  const body = Object.entries(keepIds)
    .map(([id, at]) => JSON.stringify({ id, at }))
    .join("\n");
  try {
    writeFileSync(tmp, body ? `${body}\n` : "", "utf8");
    renameSync(tmp, EVENTS_PATH);
    log(`compacted fire log to ${Object.keys(keepIds).length} records`);
  } catch (error) {
    log(`compaction failed: ${error && error.message ? error.message : String(error)}`);
  }
}

function loadOrCreateSecret() {
  try {
    const raw = JSON.parse(readFileSync(SECRET_PATH, "utf8"));
    if (raw && typeof raw.token === "string" && raw.token) return raw.token;
  } catch {
    /* first run */
  }
  const token = `wake-${randomBytes(16).toString("hex")}`;
  try {
    mkdirSync(DSH_HOME, { recursive: true });
    writeFileSync(SECRET_PATH, JSON.stringify({ token, createdAt: new Date().toISOString() }, null, 1), {
      encoding: "utf8",
      mode: 0o600,
    });
  } catch (error) {
    log(`could not persist generated token: ${error && error.message ? error.message : String(error)}`);
  }
  log("generated a new token (persisted to wake-bridge-secret.json)");
  return token;
}

// ---------------------------------------------------------------------------
// message body
// ---------------------------------------------------------------------------

function expand(value, vars) {
  return String(value).replace(/\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}/g, (whole, key) => {
    if (key.startsWith("data.")) {
      const path = key.slice(5).split(".");
      let cur = vars.data;
      for (const p of path) {
        if (cur == null || typeof cur !== "object") return whole;
        cur = cur[p];
      }
      return cur == null ? whole : String(cur);
    }
    return vars[key] == null || vars[key] === "" ? whole : String(vars[key]);
  });
}

/**
 * The default body stays deliberately boring: it says "this is not the user",
 * names the event, and stops. Everything domain-specific (what to do about it)
 * belongs to the caller's `template` or `footnote`.
 */
function defaultTemplate(ev) {
  const lines = [
    "[EXTERNAL WAKE]",
    "This message was injected by an external process, not typed by the user.",
    "",
    `event_id : ${ev.event_id}`,
    `kind     : ${ev.kind}`,
    `reason   : ${ev.reason}`,
  ];
  if (ev.detail) lines.push(`detail   : ${ev.detail}`);
  if (ev.context_path) lines.push(`context  : ${ev.context_path}`);
  if (ev.data) lines.push(`data     : ${JSON.stringify(ev.data).slice(0, 1200)}`);
  return lines.join("\n");
}

function renderMessage(ev, cfg) {
  const base = ev.text || (cfg.template ? expand(cfg.template, ev) : defaultTemplate(ev));
  if (!cfg.footnote) return base;
  return `${base}\n\n${expand(cfg.footnote, ev)}`;
}

// ---------------------------------------------------------------------------

export function apply(ctx, config) {
  const cfg = { ...DEFAULTS, ...(config || {}) };
  if (!cfg.token) cfg.token = loadOrCreateSecret();

  const folded = foldFireLog(cfg.dedupeWindowMs);
  const state = { seen: folded.seen, reasonHits: folded.reasonHits, lastAt: folded.lastAt };
  if (folded.compact) compactFireLog(state.seen);

  let targetSession = cfg.sessionId || "";
  let listenPort = Number(cfg.port) || 0;
  const inflight = new Set();
  let server;

  function pruneReasonHits(now) {
    for (const [reason, hits] of Object.entries(state.reasonHits)) {
      state.reasonHits[reason] = hits.filter((ts) => now - Number(ts) < cfg.reasonWindowMs);
      if (state.reasonHits[reason].length === 0) delete state.reasonHits[reason];
    }
  }

  function makeMessage(ev) {
    const text = renderMessage(ev, cfg);
    try {
      return createUserMessage({ content: [{ type: "text", text }], source: { kind: "external-wake" } });
    } catch {
      return createUserMessage({ content: [{ type: "text", text }], source: { kind: "schedule" } });
    }
  }

  function withTimeout(promise, ms, label) {
    return Promise.race([
      promise,
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error(`${label} timed out after ${ms}ms`)), ms).unref?.(),
      ),
    ]);
  }

  async function deliver(ev) {
    const sessionId = ev.session || targetSession;
    if (!sessionId) return { ok: false, code: "no_session_bound" };
    try {
      const resolved = await ctx.sessionController.resolveAgent(sessionId);
      if (resolved && "error" in resolved) {
        const code = (resolved.error && resolved.error.code) || "session_not_found";
        log(`resolve failed session=${sessionId} code=${code}`);
        return { ok: false, code };
      }
      const message = makeMessage(ev);
      resolved.agent.followup(message);
      const flushed = await withTimeout(
        ctx.sessions.flush(resolved.agent.session),
        Number(cfg.flushTimeoutMs) || 10_000,
        "flush",
      );
      if (!flushed) throw new Error("session persistence did not acknowledge the wake");
      log(`delivered event_id=${ev.event_id} session=${sessionId} messageId=${message.id}`);
      return { ok: true, messageId: message.id, sessionId };
    } catch (error) {
      log(`delivery failed event_id=${ev.event_id} error=${error && error.message ? error.message : String(error)}`);
      return { ok: false, code: "delivery_failed" };
    }
  }

  function rateLimit(now, ev) {
    if (state.seen[ev.event_id]) return "duplicate";
    if (inflight.has(ev.event_id)) return "duplicate";
    if (now - Number(state.lastAt || 0) < cfg.globalBurstMs) return "burst";
    pruneReasonHits(now);
    const hits = state.reasonHits[ev.reason] || [];
    if (hits.length > 0 && now - Math.max(...hits) < cfg.cooldownMs) return "cooldown";
    if (hits.length >= cfg.reasonMaxHits) return "cooldown";
    return null;
  }

  async function handleWake(req, res, bodyText) {
    if (req.headers["x-wake-token"] !== cfg.token) {
      log("rejected wake: bad token");
      return reply(res, 401, { accepted: false, reason: "unauthorized" });
    }
    let ev;
    try {
      ev = JSON.parse(bodyText || "{}");
    } catch {
      return reply(res, 400, { accepted: false, reason: "bad_json" });
    }
    ev.event_id = sanitize(ev.event_id, 120);
    ev.kind = sanitize(ev.kind, 24) || "info";
    ev.reason = sanitize(ev.reason, 64) || "UNSPECIFIED";
    ev.detail = sanitize(ev.detail, 400);
    ev.context_path = sanitize(ev.context_path, 400);
    ev.session = sanitize(ev.session, 200);
    ev.text = sanitize(ev.text, 2000);
    if (ev.data && typeof ev.data === "object" && !Array.isArray(ev.data)) {
      // keep templates useful without letting a caller push a megabyte in
      const json = JSON.stringify(ev.data);
      ev.data = json.length <= 4000 ? ev.data : { truncated: true };
    } else if (ev.data !== undefined) {
      delete ev.data;
    }
    if (!ev.event_id) return reply(res, 400, { accepted: false, reason: "missing_event_id" });
    ev.time = new Date().toISOString();

    const now = Date.now();
    const limited = rateLimit(now, ev);
    if (limited) {
      log(`wake event_id=${ev.event_id} kind=${ev.kind} reason=${ev.reason} -> rejected:${limited}`);
      return reply(res, 429, { accepted: false, reason: limited });
    }

    inflight.add(ev.event_id);
    try {
      const result = await deliver(ev);
      if (!result.ok) {
        // Nothing was recorded: the same event_id may be retried verbatim.
        log(`wake event_id=${ev.event_id} reason=${ev.reason} -> failed:${result.code}`);
        return reply(res, 503, { accepted: false, reason: result.code, retryable: true });
      }
      state.seen[ev.event_id] = now;
      state.lastAt = now;
      state.reasonHits[ev.reason] = [...(state.reasonHits[ev.reason] || []), now];
      appendFire({ id: ev.event_id, at: now, reason: ev.reason, session: result.sessionId });
      log(`wake event_id=${ev.event_id} kind=${ev.kind} reason=${ev.reason} -> accepted`);
      return reply(res, 200, {
        accepted: true,
        delivered: true,
        messageId: result.messageId,
        sessionId: result.sessionId,
      });
    } finally {
      inflight.delete(ev.event_id);
    }
  }

  function handleBind(req, res, bodyText) {
    if (req.headers["x-wake-token"] !== cfg.token) {
      return reply(res, 401, { accepted: false, reason: "unauthorized" });
    }
    let payload;
    try {
      payload = JSON.parse(bodyText || "{}");
    } catch {
      return reply(res, 400, { accepted: false, reason: "bad_json" });
    }
    const next = sanitize(payload.sessionId, 200);
    if (!next) return reply(res, 400, { accepted: false, reason: "missing_session_id" });
    targetSession = next;
    publishEndpoint();
    log(`rebound to ${next}`);
    return reply(res, 200, { accepted: true, sessionId: next });
  }

  function readWatcherState() {
    const path = cfg.watcherStatePath || WATCHER_STATE_DEFAULT;
    let raw;
    try {
      if (statSync(path).size > 4 * 1024 * 1024) {
        return { path, error: "state file too large" };
      }
      raw = JSON.parse(readFileSync(path, "utf8"));
    } catch {
      return { path, present: false };
    }
    const rules = raw.rules && typeof raw.rules === "object" ? raw.rules : {};
    const pending = Array.isArray(raw.pending) ? raw.pending : [];
    return {
      path,
      present: true,
      pending: pending.length,
      pendingPreview: pending.slice(0, 5).map((item) => ({
        event_id: item.event_id || item.event?.event_id,
        reason: item.event?.reason,
        at: item.at || item.first_seen,
        attempts: item.attempts,
      })),
      rules: Object.keys(rules).length,
      armed: Object.values(rules).filter((r) => r && r.armed !== false).length,
    };
  }

  function handleStatus(req, res) {
    if (req.headers["x-wake-token"] !== cfg.token) {
      return reply(res, 401, { accepted: false, reason: "unauthorized" });
    }
    const now = Date.now();
    pruneReasonHits(now);
    return reply(res, 200, {
      ok: true,
      uptimeSeconds: Math.round(process.uptime()),
      port: listenPort,
      sessionId: targetSession || null,
      inflight: inflight.size,
      seenInWindow: Object.keys(state.seen).length,
      lastWakeAt: state.lastAt ? new Date(Number(state.lastAt)).toISOString() : null,
      reasonHits: state.reasonHits,
      watcher: readWatcherState(),
      pid: process.pid,
    });
  }

  function publishEndpoint() {
    try {
      mkdirSync(DSH_HOME, { recursive: true });
      writeFileSync(
        ENDPOINT_PATH,
        JSON.stringify(
          {
            port: listenPort,
            token: cfg.token,
            sessionId: targetSession,
            pid: process.pid,
            startedAt: new Date().toISOString(),
          },
          null,
          1,
        ),
        "utf8",
      );
    } catch {
      log("could not publish endpoint file");
    }
  }

  ctx.effect(() => {
    server = createServer((req, res) => {
      const chunks = [];
      let size = 0;
      req.on("data", (chunk) => {
        size += chunk.length;
        if (size > 64 * 1024) {
          req.destroy();
          return;
        }
        chunks.push(chunk);
      });
      req.on("end", () => {
        const bodyText = Buffer.concat(chunks).toString("utf8");
        const url = (req.url || "/").split("?")[0];
        if (req.method === "GET" && url === "/health") {
          return reply(res, 200, {
            ok: true,
            sessionId: targetSession,
            inflight: inflight.size,
            seen: Object.keys(state.seen).length,
            lastWakeAt: state.lastAt ? new Date(Number(state.lastAt)).toISOString() : null,
          });
        }
        if (req.method === "GET" && url === "/status") return handleStatus(req, res);
        if (req.method === "POST" && url === "/wake") {
          handleWake(req, res, bodyText).catch((error) => {
            log(`wake handler crashed: ${error && error.message ? error.message : String(error)}`);
            try {
              reply(res, 500, { accepted: false, reason: "internal" });
            } catch {
              /* socket already gone */
            }
          });
          return;
        }
        if (req.method === "POST" && url === "/bind") return handleBind(req, res, bodyText);
        return reply(res, 404, { accepted: false, reason: "not_found" });
      });
    });

    const start = (port, isRetry) => {
      server.listen(port, "127.0.0.1", () => {
        listenPort = server.address().port;
        publishEndpoint();
        // An unbound default is a legitimate setup (every request names its own
        // session), but it looks identical to "the plugin is broken" from the
        // outside, so the log has to say which one it is.
        const where = targetSession
          ? `-> default session ${targetSession}`
          : '— NO default session bound: every /wake request must carry "session"';
        log(
          `listening on 127.0.0.1:${listenPort} ${where}` +
            (isRetry ? " [fell back to an ephemeral port]" : ""),
        );
      });
    };

    // Two DSH processes on one machine would otherwise fight for the fixed port
    // and the loser would sit there silently unable to receive a single wake.
    server.on("error", (error) => {
      log(`server error: ${error && error.message ? error.message : String(error)}`);
      if (error && error.code === "EADDRINUSE" && listenPort !== 0) {
        log(`port ${listenPort} busy, retrying on an ephemeral port`);
        listenPort = 0;
        setTimeout(() => start(0, true), 50);
      }
    });
    start(listenPort, false);

    return () => {
      try {
        server.close();
      } catch {
        /* ignore */
      }
      // Two wake plugins can share one $DSH_HOME, and they all publish to the
      // same endpoint path. Only erase the discovery file if it still describes
      // this instance — otherwise the one that stops last blinds the other.
      try {
        const current = JSON.parse(readFileSync(ENDPOINT_PATH, "utf8"));
        if (Number(current.port) !== Number(listenPort)) {
          log(`stopped; left the endpoint file alone (it belongs to port ${current.port})`);
          return;
        }
      } catch {
        /* no endpoint file: nothing to guard */
      }
      try {
        writeFileSync(ENDPOINT_PATH, JSON.stringify({ port: null, stoppedAt: new Date().toISOString() }), "utf8");
      } catch {
        /* ignore */
      }
      log("stopped");
    };
  });
}
