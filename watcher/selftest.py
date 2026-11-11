# -*- coding: utf-8 -*-
"""Offline tests for the watcher: probes, edge logic, retry queue, session routing.

No real wake is sent: the HTTP server is a local mock and delivery is a fake.
"""
import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import waiter  # noqa: E402


def opts_for(tmp, dry=False):
    return argparse.Namespace(
        log_path=pathlib.Path(tmp) / "waiter.log",
        verbose=False,
        once=True,
        dry_run=dry,
        timeout=2.0,
    )


def rule(**kw):
    base = {
        "id": "r1",
        "enabled": True,
        "probe": {"type": "always"},
        "fire": {"reason": "TEST", "detail": "v={value}"},
        "session": "session-abc",
    }
    base.update(kw)
    return base


def main():
    ok = 0
    tmp = tempfile.mkdtemp(prefix="wake-bridge-test-")
    opts = opts_for(tmp)
    try:
        # --- value helpers -------------------------------------------------
        assert waiter.get_path({"a": {"b": [10, 20]}}, "a.b.1") == 20
        assert waiter.get_path({"a": 1}, "a.missing") is None
        assert waiter.compare("135", "<", 200) is True
        assert waiter.compare(10, ">=", "10") is True
        assert waiter.compare("abcdef", "contains", "cd") is True
        assert waiter.compare("abc", "matches", "^a.c$") is True
        print("ok  value helpers")
        ok += 1

        # --- probes --------------------------------------------------------
        path = pathlib.Path(tmp) / "flag.txt"
        path.write_text("READY", encoding="utf-8")
        assert waiter.probe_file_exists({"path": str(path)}, opts)[0] is True
        assert waiter.probe_file_contains({"path": str(path), "pattern": "READ"}, opts)[0] is True
        assert waiter.probe_file_contains({"path": str(path), "pattern": "NOPE"}, opts)[0] is False
        assert waiter.probe_pid({"pid": os.getpid(), "state": "running"}, opts)[0] is True
        assert waiter.probe_pid({"pid": 999_999, "state": "exited"}, opts)[0] is True
        assert waiter.probe_command({"cmd": [sys.executable, "-c", "import sys; sys.exit(0)"]}, opts)[0] is True
        assert waiter.probe_command({"cmd": [sys.executable, "-c", "import sys; sys.exit(3)"]}, opts)[0] is False
        print("ok  file / pid / command probes")
        ok += 1

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = json.dumps({"ok": True, "queued": "135"}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        p = {"type": "http_json", "url": f"http://127.0.0.1:{port}/x", "path": "queued", "op": ">", "value": 100}
        okp, detail, value = waiter.probe_http_json(p, opts)
        assert okp is True and value == "135", (okp, detail, value)
        server.shutdown()
        print("ok  http_json probe")
        ok += 1

        # --- edge: once fires exactly once --------------------------------
        waiter.PROBES["always"] = lambda probe, ctx: (True, "always true", "v")
        defaults = {"interval_s": 0, "rearm_s": 0, "ttl_s": 60}
        st = {}
        fired = waiter.evaluate_rule(rule(once=True), defaults, st, opts)
        assert len(fired) == 1 and fired[0]["event"]["event_id"] == "r1@1", fired
        assert st["done"] is True and st["seq"] == 1
        assert waiter.evaluate_rule(rule(once=True), defaults, st, opts) == []
        print("ok  once semantics")
        ok += 1

        # --- edge: rearm needs a false observation ------------------------
        flag = {"v": True}
        waiter.PROBES["flag"] = lambda probe, ctx: (flag["v"], "flag", flag["v"])
        st = {}
        r = rule(id="r2", once=False, probe={"type": "flag"})
        assert len(waiter.evaluate_rule(r, defaults, st, opts)) == 1
        assert waiter.evaluate_rule(r, defaults, st, opts) == []  # still true, not armed
        flag["v"] = False
        assert waiter.evaluate_rule(r, defaults, st, opts) == []  # false re-arms (rearm_s=0)
        assert st["armed"] is True
        flag["v"] = True
        second = waiter.evaluate_rule(r, defaults, st, opts)
        assert len(second) == 1 and second[0]["event"]["event_id"] == "r2@2", second
        print("ok  rearm / edge semantics")
        ok += 1

        # --- event payload ------------------------------------------------
        waiter.PROBES["num"] = lambda probe, ctx: (True, "num", 135.0)
        st = {}
        item = waiter.evaluate_rule(
            rule(fire={"reason": "P", "kind": "signal",
                       "detail": "queued {value}", "data": {"queued": "{value}"}},
                 probe={"type": "num"}, session="session-abc"),
            defaults, st, opts)[0]
        ev = item["event"]
        assert ev["session"] == "session-abc"
        assert ev["detail"] == "queued 135.0" and ev["data"]["queued"] == "135.0"
        assert item["expires_at"] > waiter.now()
        print("ok  event payload")
        ok += 1

        # --- retry queue: failure keeps the event, success drops it -------
        calls = []

        def fake_send(**kw):
            calls.append(kw)
            return None if len(calls) == 1 else {"accepted": True, "sessionId": kw.get("session")}

        real_send = waiter.wake_send
        waiter.wake_send = fake_send
        try:
            state = {"pending": [item]}
            waiter.deliver_pending(state, opts)
            assert len(state["pending"]) == 1 and state["pending"][0]["tries"] == 1
            state["pending"][0]["next_at"] = 0
            waiter.deliver_pending(state, opts)
            assert state["pending"] == [], state["pending"]
            assert calls[0]["event_id"] == ev["event_id"] and calls[0]["reason"] == "P"

            # duplicate / permanent rejection are dropped, not retried forever
            def reject(fixed):
                def _send(**kw):
                    return {"accepted": False, "reason": fixed}
                return _send

            for fixed in ("duplicate", "unauthorized"):
                state = {"pending": [dict(item, tries=0, next_at=0)]}
                waiter.wake_send = reject(fixed)
                waiter.deliver_pending(state, opts)
                assert state["pending"] == [], fixed
        finally:
            waiter.wake_send = real_send
        print("ok  retry queue")
        ok += 1

        # --- dry run never sends ------------------------------------------
        state = {"pending": [dict(item, tries=0, next_at=0)]}
        waiter.deliver_pending(state, opts_for(tmp, dry=True))
        assert state["pending"] == []
        print("ok  dry run")
        ok += 1

        # --- latest session routing ---------------------------------------
        from compression import zstd as _zstd

        root = pathlib.Path(tmp) / "sessions" / "--ws--"

        def write_session(name, age, is_seeded=False, origin=None):
            header = {"type": "session", "version": 4, "id": name,
                      "createdAt": 1, "cwd": tmp, "isSeeded": is_seeded}
            if origin:
                header["origin"] = origin
            d = root / name
            d.mkdir(parents=True)
            f = d / "session.v4.jsonl.zstd"
            f.write_bytes(_zstd.compress((json.dumps(header) + "\n").encode()))
            ts = waiter.now() - age
            os.utime(f, (ts, ts))

        write_session("session-old", 400)
        write_session("session-seeded", 0, is_seeded=True)   # subagent child, newest
        old_home = waiter._DSH_HOME
        waiter._DSH_HOME = tmp
        waiter._LATEST_CACHE.update(at=0, id=None)
        try:
            assert waiter.latest_session_id() == "session-old"
            assert waiter.resolve_session("latest") == "session-old"
            assert waiter.resolve_session("session-pinned") == "session-pinned"
        finally:
            waiter._DSH_HOME = old_home
            waiter._LATEST_CACHE.update(at=0, id=None)
        print("ok  session routing")
        ok += 1

        # --- client: payload shape, token header, missing endpoint --------
        seen_payload = {}

        class WakeHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length", 0)))
                seen_payload["body"] = json.loads(raw.decode("utf-8"))
                seen_payload["token"] = self.headers.get("x-wake-token")
                body = json.dumps({"accepted": True, "delivered": True, "sessionId": "s"}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        wserver = HTTPServer(("127.0.0.1", 0), WakeHandler)
        threading.Thread(target=wserver.serve_forever, daemon=True).start()
        home = pathlib.Path(tmp) / "home"
        home.mkdir()
        (home / "wake-endpoint.json").write_text(
            json.dumps({"port": wserver.server_address[1], "token": "tok"}), encoding="utf-8")
        old_home_env = os.environ.get("DSH_HOME")
        os.environ["DSH_HOME"] = str(home)
        try:
            reply = waiter.wake_send("R", detail="d", event_id="e1", session="session-abc",
                                     text="hello", data={"queued": 135})
            assert reply["accepted"] is True
            body = seen_payload["body"]
            assert seen_payload["token"] == "tok"
            assert body["event_id"] == "e1" and body["session"] == "session-abc" and body["text"] == "hello"
            assert body["data"] == {"queued": 135}
            (home / "wake-endpoint.json").write_text(json.dumps({"port": None}), encoding="utf-8")
            assert waiter.wake_send("R") is None
        finally:
            wserver.shutdown()
            if old_home_env is None:
                os.environ.pop("DSH_HOME", None)
            else:
                os.environ["DSH_HOME"] = old_home_env
        print("ok  client payload")
        ok += 1

        # --- rule file validation + CLI -----------------------------------
        bad = pathlib.Path(tmp) / "bad.json"
        bad.write_text(json.dumps({"rules": [{"id": "x", "probe": {"type": "nope"}, "fire": {"reason": "R"}}]}), encoding="utf-8")
        try:
            waiter.load_rules(bad)
        except ValueError as exc:
            assert "unknown probe" in str(exc) or "probe.type" in str(exc)
        else:
            raise AssertionError("bad rule file accepted")

        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        listed = subprocess.run([sys.executable, str(HERE / "waiter.py"), "--list",
                                 "--rules", str(HERE / "rules.example.json")],
                                capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
        assert listed.returncode == 0 and "build-finished" in listed.stdout, listed.stderr
        onced = subprocess.run([sys.executable, str(HERE / "waiter.py"), "--once", "--dry-run",
                                "--rules", str(HERE / "rules.example.json"),
                                "--state-dir", str(pathlib.Path(tmp) / "state")],
                               capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
        assert onced.returncode == 0, onced.stderr
        print("ok  rule validation + CLI")
        ok += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nALL PASS ({ok} groups)")


if __name__ == "__main__":
    main()
