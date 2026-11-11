# dsh-wake-relay

Let any external process wake a DeepSeek Harness session — build watchers, job runners, CI callbacks, scrapers, backup scripts, phone-push relays. Anything that can send one HTTP POST.

The point is not "wake an agent". Schedulers and hook plugins already do that. The point is **what happens when the wake cannot be delivered right now**:

```
200  -> delivered AND flushed. Done.
429  -> dropped by policy (duplicate / cooldown / burst). Do not retry.
503  -> nothing was recorded. Retry later with the SAME event_id.
```

That is the whole contract. Because 503 means *nothing happened* — no dedupe entry, no rate-limit hit — a retry with the same `event_id` is safe by construction, and the caller can own the retry loop without guessing whether a timeout meant "delivered" or "not delivered".

## Where the probe runs

Every other wake plugin puts the timer inside the host process. Host exits (upgrade, crash, you closed it) and probing stops with it — a condition that becomes true *and then false* during that window is gone forever.

This one splits the two halves:

```
external process (yours)                       host process (this plugin)
  probe every 30s                                verify token
  condition true -> append to local queue        dedupe + per-reason limits
  POST /wake (idempotent event_id)  ──────────▶  resolveAgent -> followup -> flush
  503 -> stays queued, retried until ttl  ◀────  200 only after flush
```

Two processes, two independent restart cycles:

- Host down → the watcher keeps sampling, events queue, and they are delivered when the host returns.
- Watcher down → the plugin is unaffected; already-queued wakes still deliver.
- Both down → events wait on the watcher's disk queue until `ttl_s` expires.

The cost of this split is that you need a resident external process. `watcher/` is one, but any script will do.

## Compared to in-host probe plugins

| | This | In-host probes (dsh-sentinel et al.) |
|---|---|---|
| Where the probe runs | external process | inside the host |
| Host is down | still sampling, queueing, retrying | nothing is probed |
| Condition vocabulary | any command exit code + JSON field assertions | fixed sensor types, mostly "did it change" |
| Failure semantics | 503 + nothing recorded, idempotent retry | internal requeue |
| UI | none (`GET /status` + log) | dock panel / dashboard |
| Multiple instances | falls back to an ephemeral port | lease-based leader election |
| Windows process probe | native `OpenProcess` | usually `pgrep` (unavailable) |

No UI is a deliberate choice: this plugin's user is a script.

## Install

```bash
dsh plugin --profile desktop add dsh-wake-relay     # from npm
dsh plugin --profile <your-profile> add ./dsh-wake-relay   # from a local clone
```

Restart DSH afterwards — the plugin tree is composed at process start.

### Two traps you only hit while installing

**1. Bundle entry ids must be globally unique.** Enable/disable state is persisted per entry id in the profile's `cordis.patch.yml`:

```yaml
- id: wake-bridge
  disabled: true
```

If another bundle's patch inserts the same id, it is swallowed silently — no error, no warning. The only reliable way to tell whether it loaded is to look for its runtime artifacts: a fresh `port` in `wake-endpoint.json`, a new `listening` line in `wake-bridge.log`, a generated `wake-bridge-secret.json`. `install_bundle` returns `applied` even when nothing loaded.

**2. An empty `sessionId` is a valid setup that will not answer a bare curl.** Requests must then carry `session`, or you get `503 no_session_bound`. The startup log says which state you are in:

```
listening on 127.0.0.1:45871 — NO default session bound: every /wake request must carry "session"
```

Pin `sessionId` in `cordis.patch.yml`, or pass `session` on every request.

> Installing from a local directory links the package (`pnpm link:`), and the linked copy cannot resolve host packages from the workspace root → `ERR_MODULE_NOT_FOUND`. Fix with a junction:
> ```
> mklink /J "…\dsh-wake-relay\node_modules\@deepseek-ai\dsh-llm" "C:\Users\<you>\.dsh\profiles\node_modules\@deepseek-ai\dsh-llm"
> ```
> After a failed load, `install_bundle` only reports `already-installed`; toggle `include:<id>` off and on to force a re-import.

## Config

Set on the bundle row in the profile's `cordis.patch.yml`, or in this package's `cordis.patch.yml`:

| Field | Default | Meaning |
|---|---:|---|
| `port` | 45871 | Bind address is always `127.0.0.1`. If the port is taken, falls back to an ephemeral port and republishes the endpoint file |
| `token` | empty | Empty = generated on first boot, persisted to `$DSH_HOME/wake-bridge-secret.json` (mode 0600) |
| `sessionId` | empty | Default target. Empty = only requests carrying an explicit `session` are accepted |
| `cooldownMs` | 90000 | Per-reason minimum spacing |
| `reasonWindowMs` / `reasonMaxHits` | 300000 / 2 | How many times one reason may fire inside the window |
| `globalBurstMs` | 3000 | Minimum gap between any two wakes; collapses simultaneous bursts only |
| `flushTimeoutMs` | 10000 | Per-delivery ceiling; a timed-out flush returns 503 |
| `dedupeWindowMs` | 604800000 | `event_id` dedupe window (7 days) |
| `template` | empty | Message body; supports `{{detail}}`, `{{reason}}`, `{{data.k}}` |
| `footnote` | empty | One line appended after the template |
| `watcherStatePath` | empty | External watcher state file folded into `GET /status` |

Template variables: `{{event_id}} {{kind}} {{reason}} {{detail}} {{context_path}} {{session}} {{time}}`, plus any `{{data.<path>}}`. Unknown variables are left verbatim.

## API

```
GET  /health                  public      -> {ok, sessionId, inflight, seen, lastWakeAt}
GET  /status                  (token)     -> counters, per-reason windows, external watcher queue summary
POST /wake     (x-wake-token) -> {accepted, delivered, messageId, sessionId}
POST /bind     (x-wake-token) -> {accepted, sessionId}   rebind the default (not persisted)
```

`POST /wake` body:

```json
{
  "event_id": "build-2026-11-12-7f3a",
  "kind": "signal",
  "reason": "BUILD_DONE",
  "detail": "17 passed, 0 failed",
  "data": { "anything": "reachable as {{data.anything}} in a template" },
  "context_path": "C:\\path\\to\\state.json",
  "session": "session-… (optional, overrides the default)",
  "text": "(optional) full message replacing the template, <= 2000 chars"
}
```

| Status | Meaning | Caller |
|---|---|---|
| 200 | delivered and flushed | done |
| 429 `duplicate` / `cooldown` / `burst` | dropped by policy | do **not** retry |
| 503 `delivery_failed` / `session_not_found` / `no_session_bound` | nothing recorded | retry with the **same** `event_id` |
| 401 / 400 | bad token / bad body | fix the caller |

> Non-ASCII bodies must be sent as UTF-8 bytes. PowerShell needs `[System.Text.Encoding]::UTF8.GetBytes($body)`, otherwise `detail` arrives as a row of `?`.

### Node

```js
import { wake } from "dsh-wake-relay/client";

const r = await wake({ event_id: `job-${Date.now()}`, reason: "JOB_DONE", detail: "…" });
if (r.retryable) setTimeout(() => wake(sameEvent), 30_000); // 503 means nothing was recorded
```

### Anything else

```bash
EP=$(cat ~/.dsh/wake-endpoint.json)   # {port, token, sessionId}
curl -s -X POST "http://127.0.0.1:$PORT/wake" \
  -H "x-wake-token: $TOKEN" -H "content-type: application/json; charset=utf-8" \
  --data-binary @event.json
```

## Optional rules engine (Python)

`watcher/` is a resident process plus a rule table, for the common case where you would otherwise write one polling script per condition:

```bash
python watcher/waiter.py --rules watcher/rules.example.json --list
python watcher/waiter.py --rules my-rules.json --once --dry-run   # evaluate, send nothing
pythonw watcher/waiter.py --rules my-rules.json                   # resident; rules hot-reload
```

| Probe | True when |
|---|---|
| `file_exists` / `file_contains` | path exists / content matches a regex |
| `pid` | process alive or gone (native on Windows, `kill(pid,0)` elsewhere) |
| `port` | TCP connect succeeds or fails |
| `http_json` | a JSON field compares (`== != > >= < <= contains matches`) |
| `command` | any command's exit code matches; stdout becomes `{detail}` |

Edge-triggered (fires on the false→true transition only), `rearm_s` debounce, `once`, a persisted retry queue with `ttl_s`, and `session: latest` for the newest ordinary session. The rule file reloads without a restart.

State lives in `$DSH_HOME/wake-bridge-watcher` (`waiter-state.json` / `waiter.log` / `waiter.pid`).

## Runtime files (`$DSH_HOME`)

| File | Content |
|---|---|
| `wake-endpoint.json` | `{port, token, sessionId, pid, startedAt}` — callers discover it, no second copy of the config |
| `wake-bridge-secret.json` | the generated token (0600) |
| `wake-bridge-events.jsonl` | accepted fires, append-only, 7-day window, compacted when it outgrows the window |
| `wake-bridge.log` | accept / reject / delivered / failed |
| `wake-bridge-watcher/` | external watcher edge state + retry queue |

## Known limits

- **The host must be online to deliver.** While it is down the watcher only accumulates events (within `ttl_s`); nothing is pre-staged for replay.
- **`/bind` is not persisted.** A restart returns to the configured `sessionId`.
- **The token is plaintext** in `wake-endpoint.json`. Loopback only — do not port-forward it, and tighten the file on shared machines.
- **One wake = one full inference + one permanent message.** For high-frequency conditions write state to disk instead of waking; when you do wake, bring the context with you (`context_path`).
- **`session: latest` is a guess.** It picks the newest ordinary session on disk. Long-running or unattended jobs must pin `session` explicitly, or the wake may land in a throwaway conversation.
- An explicit `session` that cannot be resolved returns 503. It is **never** silently redirected — waking the wrong context is worse than not waking.

## Tests

```bash
node selftest.mjs           # 21 checks: delivery, idempotency, limits, retry, templates, cross-restart dedupe, port fallback
python watcher/selftest.py  # 11 groups: probes, edges, retry queue, session routing, rule validation (offline)
```

## License

MIT.
