/**
 * Offline test for the bridge: fake session controller, real HTTP server,
 * throwaway DSH_HOME (never touches the live endpoint or the live session).
 *
 *   node selftest.mjs
 */
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

process.env.DSH_HOME = mkdtempSync(join(tmpdir(), "wake-bridge-test-"));
const { apply } = await import("./index.js");

const PORT = 45999;
const TOKEN = "test-token";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let captured = [];
const ctx = {
  sessionController: {
    resolveAgent: async (id) => {
      if (id === "session-missing") return { error: { code: "session/not-found" } };
      return {
        agent: {
          session: { id },
          followup: (message) => captured.push({ id, message }),
        },
      };
    },
  },
  sessions: { flush: async () => true },
  effect: (fn) => fn(),
};

apply(ctx, {
  port: PORT,
  token: TOKEN,
  sessionId: "session-default",
  cooldownMs: 1000,
  reasonWindowMs: 300000,
  reasonMaxHits: 10,
  globalBurstMs: 50,
});

async function call(path, payload, token = TOKEN) {
  const res = await fetch(`http://127.0.0.1:${PORT}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-wake-token": token },
    body: JSON.stringify(payload),
  });
  return { status: res.status, body: await res.json() };
}

for (let i = 0; i < 50; i += 1) {
  try {
    const res = await fetch(`http://127.0.0.1:${PORT}/health`);
    if (res.ok) break;
  } catch {
    /* not up yet */
  }
  await sleep(50);
}

let pass = 0;
function check(name, cond, detail) {
  if (!cond) throw new Error(`FAIL ${name}: ${JSON.stringify(detail)}`);
  console.log(`ok  ${name}`);
  pass += 1;
}

// 1. token gate + routing + delivery payload
let r = await call("/wake", { event_id: "e1", reason: "R1", detail: "hello" }, "wrong");
check("401 on bad token", r.status === 401 && r.body.reason === "unauthorized", r);

r = await call("/wake", { event_id: "e1", reason: "R1", detail: "hello", session: "session-a" });
check("200 delivered", r.status === 200 && r.body.delivered === true && r.body.sessionId === "session-a", r);
check("delivery reached followup", captured.length === 1 && captured[0].id === "session-a", captured.length);
check("message carries event", captured[0].message.content[0].text.includes("event_id : e1"), captured[0].message.content[0].text);

// 2. dedupe
r = await call("/wake", { event_id: "e1", reason: "R1", session: "session-a" });
check("duplicate rejected", r.status === 429 && r.body.reason === "duplicate", r);
check("duplicate did not deliver", captured.length === 1, captured.length);

// 3. burst guard, then per-reason cooldown
r = await call("/wake", { event_id: "e2", reason: "R2" });
check("burst rejected", r.status === 429 && r.body.reason === "burst", r);
await sleep(80);
r = await call("/wake", { event_id: "e2", reason: "R2", session: "session-b" });
check("first R2 delivered", r.status === 200 && r.body.sessionId === "session-b", r);
await sleep(80);
r = await call("/wake", { event_id: "e3", reason: "R2" });
check("per-reason cooldown", r.status === 429 && r.body.reason === "cooldown", r);
await sleep(80);
r = await call("/wake", { event_id: "e4", reason: "R3" });
check("other reason not starved", r.status === 200, r);

// 4. failed delivery records nothing -> retry with the same id works
await sleep(80);
r = await call("/wake", { event_id: "e5", reason: "R4", session: "session-missing" });
check("missing session is 503", r.status === 503 && r.body.retryable === true, r);
const before = captured.length;
r = await call("/wake", { event_id: "e5", reason: "R4", session: "session-ok" });
check("retry after failure accepted", r.status === 200 && r.body.sessionId === "session-ok", r);
check("retry delivered once", captured.length === before + 1, captured.length);

// 5. text override replaces the template
await sleep(80);
r = await call("/wake", { event_id: "e6", reason: "R5", text: "RAW TEXT", session: "session-ok" });
const last = captured[captured.length - 1].message.content[0].text;
check("text override", r.status === 200 && last === "RAW TEXT", last);

// 6. explicit session wins over the configured default
await sleep(80);
r = await call("/wake", { event_id: "e7", reason: "R6" });
check("configured default used", r.status === 200 && r.body.sessionId === "session-default", r);

// ---------------------------------------------------------------------------
// second instance: template expansion, /status, durable dedupe across "reboots"
// ---------------------------------------------------------------------------

const PORT2 = 45998;
const captured2 = [];
const ctx2 = {
  sessionController: {
    resolveAgent: async (id) => ({
      agent: { session: { id }, followup: (message) => captured2.push({ id, message }) },
    }),
  },
  sessions: { flush: async () => true },
  effect: (fn) => fn(),
};

apply(ctx2, {
  port: PORT2,
  token: TOKEN,
  sessionId: "session-default",
  cooldownMs: 0,
  reasonWindowMs: 300000,
  reasonMaxHits: 99,
  globalBurstMs: 0,
  template: "R={{reason}} D={{detail}} X={{data.k}} C={{context_path}}",
  footnote: "FOOT",
});

for (let i = 0; i < 50; i += 1) {
  try {
    const res = await fetch(`http://127.0.0.1:${PORT2}/health`);
    if (res.ok) break;
  } catch {
    /* not up yet */
  }
  await sleep(50);
}

async function call2(path, payload, token = TOKEN, method = "POST") {
  const res = await fetch(`http://127.0.0.1:${PORT2}${path}`, {
    method,
    headers: { "content-type": "application/json", "x-wake-token": token },
    body: method === "GET" ? undefined : JSON.stringify(payload),
  });
  return { status: res.status, body: await res.json() };
}

r = await call2("/wake", {
  event_id: "t1",
  reason: "TR",
  detail: "hello",
  context_path: "/tmp/state.json",
  data: { k: "v" },
});
check(
  "template + footnote + data expansion",
  r.status === 200 && captured2[0].message.content[0].text === "R=TR D=hello X=v C=/tmp/state.json\n\nFOOT",
  captured2[0].message.content[0].text,
);

// durable dedupe: a third instance on the same DSH_HOME must reject t1
const PORT3 = 45997;
const ctx3 = {
  sessionController: { resolveAgent: async (id) => ({ agent: { session: { id }, followup: () => {} } }) },
  sessions: { flush: async () => true },
  effect: (fn) => fn(),
};
apply(ctx3, { port: PORT3, token: TOKEN, sessionId: "session-default", globalBurstMs: 0, cooldownMs: 0 });
for (let i = 0; i < 50; i += 1) {
  try {
    const res = await fetch(`http://127.0.0.1:${PORT3}/health`);
    if (res.ok) break;
  } catch {
    /* not up yet */
  }
  await sleep(50);
}
const dup = await fetch(`http://127.0.0.1:${PORT3}/wake`, {
  method: "POST",
  headers: { "content-type": "application/json", "x-wake-token": TOKEN },
  body: JSON.stringify({ event_id: "t1", reason: "TR" }),
});
check("dedupe survives a restart", dup.status === 429, dup.status);

r = await call2("/status", null, TOKEN, "GET");
check(
  "status reports counters and watcher slot",
  r.status === 200 && r.body.seenInWindow >= 1 && "watcher" in r.body,
  r.body,
);
r = await call2("/status", null, "wrong", "GET");
check("status needs the token", r.status === 401, r.status);

// port collision must fall back instead of dying silently
const PORT4 = 45996;
const blocker = (await import("node:net")).createServer();
await new Promise((resolve) => blocker.listen(PORT4, "127.0.0.1", resolve));
const ctx4 = {
  sessionController: { resolveAgent: async (id) => ({ agent: { session: { id }, followup: () => {} } }) },
  sessions: { flush: async () => true },
  effect: (fn) => fn(),
};
apply(ctx4, { port: PORT4, token: TOKEN, sessionId: "session-default" });
await sleep(400);
const { readEndpoint } = await import("./client.mjs");
const ep = readEndpoint(process.env.DSH_HOME);
check("port collision falls back to an ephemeral port", ep.port !== PORT4 && ep.port > 0, ep);
const alive = await fetch(`http://127.0.0.1:${ep.port}/health`).then((x) => x.json());
check("fallback endpoint answers", alive.ok === true, alive);
await new Promise((resolve) => blocker.close(resolve));

console.log(`\nALL PASS (${pass})`);
process.exit(0);

