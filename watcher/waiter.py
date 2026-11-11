# -*- coding: utf-8 -*-
"""watcher — one process, many condition rules, at-least-once delivery.

One rule answers one question: "is the condition true now?" A rule that turns
false -> true queues one wake event. The event is retried until the bridge
accepts it (200 = delivered) or the rule's TTL expires, and pending events are
persisted, so a wake survives a Harness restart instead of being lost.

Conditions live in one JSON file (default: rules.json next to this script).
Anything a declarative probe cannot express goes through the `command` probe:
exit code 0 means "condition true", stdout becomes the detail line. That keeps
bespoke one-off watcher scripts from each owning their own retry/dedupe logic.

Time-based reminders do NOT belong here: use the native DSH schedule, which is
durable and replays a missed occurrence after downtime.

Usage
    python waiter.py --once            # evaluate every rule once, then exit
    python waiter.py --once --dry-run  # same, but never POST
    python waiter.py --list            # print the rule table
    python waiter.py                   # daemon loop (use pythonw / Task Scheduler)
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import time
import traceback
import urllib.request

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from wakeclient import wake as wake_send  # noqa: E402

_DSH_HOME = None  # selftest override; None means "use the environment"
_LATEST_CACHE = {"at": 0.0, "id": None}

MAX_LOG_BYTES = 2 * 1024 * 1024


def dsh_home() -> pathlib.Path:
    if _DSH_HOME:
        return pathlib.Path(_DSH_HOME)
    return pathlib.Path(os.environ.get("DSH_HOME") or (pathlib.Path.home() / ".dsh"))


def now() -> float:
    return time.time()


# --------------------------------------------------------------------------
# value helpers
# --------------------------------------------------------------------------

def _num(value):
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_path(obj, path):
    """Dotted path lookup: "a.b.0.c" over dicts and lists."""
    if not path:
        return obj
    cur = obj
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def compare(actual, op, expected):
    op = op or "=="
    if op == "contains":
        return str(expected) in str(actual)
    if op == "matches":
        return bool(re.search(str(expected), str(actual)))
    na, nb = _num(actual), _num(expected)
    if na is not None and nb is not None:
        a, b = na, nb
    else:
        a, b = str(actual), str(expected)
    if op in ("==", "="):
        return a == b
    if op == "!=":
        return a != b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    raise ValueError(f"unknown op {op!r}")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == 259  # STILL_ACTIVE
        return False
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------
# probes: (probe_dict, ctx) -> (ok, detail, value)
# --------------------------------------------------------------------------

def probe_file_exists(p, ctx):
    path = pathlib.Path(p["path"]).expanduser()
    ok = path.exists()
    return ok, f"{path} exists={ok}", ok


def probe_file_contains(p, ctx):
    path = pathlib.Path(p["path"]).expanduser()
    try:
        text = path.read_text(encoding=p.get("encoding", "utf-8"), errors="replace")
    except Exception as exc:
        return False, f"read failed: {exc}", None
    pattern = p.get("pattern", "")
    if p.get("regex", True):
        ok = bool(re.search(pattern, text, re.M))
    else:
        ok = pattern in text
    return ok, f"{path.name} matches {pattern!r}={ok}", ok


def probe_pid(p, ctx):
    pid = int(p.get("pid", 0))
    alive = _pid_alive(pid)
    want = p.get("state", "exited")
    ok = alive if want == "running" else not alive
    return ok, f"pid {pid} alive={alive}", alive


def probe_port(p, ctx):
    host = p.get("host", "127.0.0.1")
    port = int(p["port"])
    timeout = float(p.get("timeout_s", 2))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        code = sock.connect_ex((host, port))
    finally:
        sock.close()
    open_now = code == 0
    want = p.get("state", "open")
    ok = open_now if want == "open" else not open_now
    return ok, f"{host}:{port} open={open_now}", open_now


def probe_http_json(p, ctx):
    req = urllib.request.Request(p["url"], headers=p.get("headers") or {})
    with urllib.request.urlopen(req, timeout=float(p.get("timeout_s", 8))) as resp:
        raw = resp.read()
    data = json.loads(raw.decode(p.get("encoding", "utf-8")))
    value = get_path(data, p.get("path", ""))
    ok = compare(value, p.get("op", "=="), p.get("value"))
    return ok, f"{p.get('path', '.')}={value!r}", value


def probe_command(p, ctx):
    cmd = p["cmd"]
    shell = isinstance(cmd, str)
    proc = subprocess.run(
        cmd,
        cwd=p.get("cwd") or None,
        shell=shell,
        capture_output=True,
        text=True,
        encoding=p.get("encoding", "utf-8"),
        errors="replace",
        timeout=float(p.get("timeout_s", 20)),
    )
    want = int(p.get("expect_exit", 0))
    ok = proc.returncode == want
    detail = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return ok, (detail or f"exit={proc.returncode}")[:300], proc.returncode


PROBES = {
    "file_exists": probe_file_exists,
    "file_contains": probe_file_contains,
    "pid": probe_pid,
    "port": probe_port,
    "http_json": probe_http_json,
    "command": probe_command,
}


# --------------------------------------------------------------------------
# session routing
# --------------------------------------------------------------------------

def session_header(path: pathlib.Path):
    """First JSON line of a session log (partial zstd decode, ~8KB read)."""
    try:
        from compression import zstd  # Python 3.14+
    except Exception:
        return None
    try:
        raw = path.read_bytes()[:8192]
        out = zstd.ZstdDecompressor().decompress(raw, 800)
        return json.loads(out.decode("utf-8", "replace").splitlines()[0])
    except Exception:
        return None


def latest_session_id() -> str:
    """Newest ordinary session on disk (any profile, any purpose).

    Resolved here instead of in the plugin so the routing rule is inspectable and
    does not depend on a host service method. Subagent sessions (`isSeeded` or
    `origin: subagent`) are skipped: they are children of a chat, and waking one
    would run a turn nobody is watching. Cached for 20s.
    """
    if now() - _LATEST_CACHE["at"] < 20 and _LATEST_CACHE["id"]:
        return _LATEST_CACHE["id"]
    candidates = []
    try:
        for path in (dsh_home() / "sessions").glob("*/session-*/session.v4.jsonl.zstd"):
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except Exception:
        candidates = []
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    fallback = candidates[0][1].parent.name if candidates else None
    chosen = None
    for _, path in candidates[:20]:
        header = session_header(path)
        if header and (header.get("origin") == "subagent" or header.get("isSeeded")):
            continue
        chosen = path.parent.name
        break
    _LATEST_CACHE.update(at=now(), id=chosen or fallback)
    return chosen or fallback or ""


def resolve_session(selector) -> str:
    selector = selector or "latest"
    if selector == "latest":
        return latest_session_id()
    return selector


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

def _opt(rule, defaults, key, fallback=None):
    if key in rule:
        return rule[key]
    if key in defaults:
        return defaults[key]
    return fallback


def render(template, value, detail):
    if not isinstance(template, str):
        return template
    return template.replace("{value}", str(value)).replace("{detail}", str(detail))


def build_event(rule, defaults, detail, value, seq):
    fire = rule.get("fire") or {}
    reason = fire.get("reason") or rule["id"].upper()
    detail_text = render(fire.get("detail", ""), value, detail) or detail
    event = {
        "event_id": f"{rule['id']}@{seq}",
        "kind": fire.get("kind", "info"),
        "reason": reason,
        "detail": str(detail_text)[:400],
    }
    if fire.get("data") and isinstance(fire["data"], dict):
        rendered = {}
        for key, val in fire["data"].items():
            rendered[str(key)] = render(val, value, detail) if isinstance(val, str) else val
        event["data"] = rendered
    if fire.get("context_path"):
        event["context_path"] = str(fire["context_path"])
    if fire.get("text"):
        event["text"] = render(fire["text"], value, detail)[:2000]
    session = resolve_session(rule.get("session") or defaults.get("session"))
    if session:
        event["session"] = session
    return {
        "event": event,
        "rule": rule["id"],
        "tries": 0,
        "next_at": 0,
        "expires_at": now() + float(_opt(rule, defaults, "ttl_s", 3600)),
    }


def evaluate_rule(rule, defaults, st, opts):
    """Return zero or one pending item; mutates the rule's edge state."""
    if rule.get("enabled", True) is False:
        return []
    once = bool(rule.get("once", False))
    if once and st.get("done"):
        return []

    interval = float(_opt(rule, defaults, "interval_s", 30))
    if now() < float(st.get("next_due", 0)):
        return []
    st["next_due"] = now() + interval

    probe = rule.get("probe") or {}
    ptype = probe.get("type")
    fn = PROBES.get(ptype)
    if fn is None:
        raise ValueError(f"{rule['id']}: unknown probe type {ptype!r}")
    try:
        ok, detail, value = fn(probe, opts)
    except Exception as exc:
        ok, detail, value = False, f"probe error: {exc}", None

    st.setdefault("armed", True)
    if not ok:
        if st.get("false_since") is None:
            st["false_since"] = now()
        rearm_s = float(_opt(rule, defaults, "rearm_s", 60))
        if not st["armed"] and now() - float(st["false_since"]) >= rearm_s:
            st["armed"] = True
        return []

    st["false_since"] = None
    if not st["armed"]:
        return []

    seq = int(st.get("seq", 0)) + 1
    item = build_event(rule, defaults, detail, value, seq)
    st["armed"] = False
    st["seq"] = seq
    st["last_fire"] = now()
    if once:
        st["done"] = True
    return [item]


# --------------------------------------------------------------------------
# persistence + delivery
# --------------------------------------------------------------------------

def load_rules(path: pathlib.Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    defaults = data.get("defaults") or {}
    rules = data.get("rules") or []
    seen = set()
    for rule in rules:
        rid = rule.get("id")
        if not rid:
            raise ValueError("every rule needs an id")
        if rid in seen:
            raise ValueError(f"duplicate rule id {rid!r}")
        seen.add(rid)
        if (rule.get("probe") or {}).get("type") not in PROBES:
            raise ValueError(f"{rid}: probe.type must be one of {sorted(PROBES)}")
        if not (rule.get("fire") or {}).get("reason"):
            raise ValueError(f"{rid}: fire.reason is required")
    return defaults, rules


def load_state(path: pathlib.Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("rules", {})
            data.setdefault("pending", [])
            return data
    except Exception:
        pass
    return {"rules": {}, "pending": []}


def save_state(path: pathlib.Path, state):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _rotate_log(path: pathlib.Path):
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            os.replace(path, path.with_suffix(".log.1"))
    except OSError:
        pass


def log(opts, message):
    line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {message}"
    try:
        _rotate_log(opts.log_path)
        with open(opts.log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    if opts.verbose or opts.once:
        print(line, file=sys.stderr, flush=True)


def _backoff(tries: int) -> float:
    return min(300.0, 5.0 * (2 ** max(0, tries - 1)))


def deliver_pending(state, opts):
    pending = state.setdefault("pending", [])
    keep = []
    current = now()
    for item in pending:
        if float(item.get("next_at", 0)) > current:
            keep.append(item)
            continue
        event = item.get("event") or {}
        eid = event.get("event_id", "?")
        if current > float(item.get("expires_at", 1e18)):
            log(opts, f"drop expired {eid} rule={item.get('rule')}")
            continue
        if opts.dry_run:
            log(opts, f"[dry-run] {eid} rule={item.get('rule')} reason={event.get('reason')} session={event.get('session', '-')}")
            continue
        payload = {k: event[k] for k in ("reason", "detail", "kind", "event_id", "data",
                                         "context_path", "session", "text") if k in event}
        response = wake_send(timeout=opts.timeout, **payload)
        item["tries"] = int(item.get("tries", 0)) + 1
        if response is None:
            item["next_at"] = now() + _backoff(item["tries"])
            keep.append(item)
            log(opts, f"no endpoint, retry {eid} in {int(item['next_at'] - now())}s")
            continue
        reason = response.get("reason")
        if response.get("accepted"):
            log(opts, f"delivered {eid} rule={item.get('rule')} session={response.get('sessionId', '-')}")
            continue
        if reason == "duplicate":
            log(opts, f"already delivered {eid}")
            continue
        if reason in ("unauthorized", "bad_json", "missing_event_id"):
            log(opts, f"permanent reject {reason} {eid}")
            continue
        item["next_at"] = now() + _backoff(item["tries"])
        keep.append(item)
        log(opts, f"retry {reason} {eid} tries={item['tries']} in {int(item['next_at'] - now())}s")
    state["pending"] = keep


def tick(rules, defaults, state, opts):
    deliver_pending(state, opts)
    for rule in rules:
        st = state["rules"].setdefault(rule["id"], {})
        try:
            fired = evaluate_rule(rule, defaults, st, opts)
        except Exception:
            log(opts, f"rule {rule['id']} crashed: {traceback.format_exc(limit=1).strip()}")
            continue
        for item in fired:
            state.setdefault("pending", []).append(item)
            log(opts, f"fire {item['event']['event_id']} reason={item['event']['reason']} detail={item['event']['detail'][:120]}")
    deliver_pending(state, opts)


def _sleep_budget(rules, defaults, state, ceiling=30.0, floor=1.0):
    current = now()
    due = [current + ceiling]
    for rule in rules:
        if rule.get("enabled", True) is False:
            continue
        st = state["rules"].get(rule["id"], {})
        due.append(float(st.get("next_due", current)))
    for item in state.get("pending", []):
        due.append(float(item.get("next_at", current)))
    wait = min(due) - current
    return max(floor, min(ceiling, wait))


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------

def acquire_lock(state_dir: pathlib.Path) -> bool:
    lock = state_dir / "waiter.pid"
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
    except Exception:
        pid = 0
    if pid and pid != os.getpid() and _pid_alive(pid):
        print(f"waiter already running (pid {pid})", file=sys.stderr)
        return False
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return True


def build_parser():
    p = argparse.ArgumentParser(description="Condition-based wakes for the DSH wake bridge.")
    p.add_argument("--rules", default=str(_HERE / "rules.json"))
    p.add_argument("--state-dir", default=str(dsh_home() / "wake-bridge-watcher"))
    p.add_argument("--once", action="store_true", help="evaluate once and exit")
    p.add_argument("--dry-run", action="store_true", help="never POST, just log")
    p.add_argument("--list", action="store_true", help="print the rule table and exit")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--timeout", type=float, default=10.0)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    rules_path = pathlib.Path(args.rules).expanduser()
    state_dir = pathlib.Path(args.state_dir).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    args.log_path = state_dir / "waiter.log"

    try:
        defaults, rules = load_rules(rules_path)
    except Exception as exc:
        print(f"rules error: {exc}", file=sys.stderr)
        return 2

    if args.list:
        for rule in rules:
            print(f"{rule['id']:<28} enabled={rule.get('enabled', True)!s:<5} "
                  f"probe={(rule.get('probe') or {}).get('type')} "
                  f"reason={(rule.get('fire') or {}).get('reason')} "
                  f"session={rule.get('session') or defaults.get('session') or 'latest'}")
        return 0

    state_path = state_dir / "waiter-state.json"
    if not acquire_lock(state_dir):
        return 3
    state = load_state(state_path)

    if args.once:
        tick(rules, defaults, state, args)
        save_state(state_path, state)
        return 0

    log(args, f"started pid={os.getpid()} rules={len(rules)} file={rules_path}")
    try:
        try:
            rules_mtime = rules_path.stat().st_mtime
        except OSError:
            rules_mtime = 0.0
        while True:
            try:
                try:
                    mtime = rules_path.stat().st_mtime
                except OSError:
                    mtime = rules_mtime
                if mtime != rules_mtime:
                    # Hot reload: edit rules.json, no daemon restart needed.
                    try:
                        defaults, rules = load_rules(rules_path)
                        rules_mtime = mtime
                        log(args, f"rules reloaded ({len(rules)})")
                    except Exception as exc:
                        rules_mtime = mtime  # do not spam the same broken file every tick
                        log(args, f"rules reload failed, keeping previous: {exc}")
                tick(rules, defaults, state, args)
                save_state(state_path, state)
            except Exception:
                log(args, f"tick crashed: {traceback.format_exc(limit=2).strip()}")
            time.sleep(_sleep_budget(rules, defaults, state))
    except KeyboardInterrupt:
        log(args, "stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
