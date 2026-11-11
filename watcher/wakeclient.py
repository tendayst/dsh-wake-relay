# -*- coding: utf-8 -*-
"""Canonical client for the dsh-wake-bridge plugin.

One POST, no hidden throttling: the caller decides whether a rejected wake is
retried. The bridge is the only rate limiter (per-reason cooldown + 7-day
event_id dedupe), so a lost event is visible as a returned reason instead of
being swallowed inside this module.

Returns
    dict  the bridge's decoded JSON reply, e.g.
          {"accepted": True, "delivered": True, "messageId": "...", "sessionId": "..."}
          {"accepted": False, "reason": "cooldown"}            (dropped by policy)
          {"accepted": False, "reason": "delivery_failed", "retryable": True}
    None  the endpoint file is missing/unreadable: host not running, plugin
          disabled, or never installed. Retry later; nothing was sent.
"""
import json
import os
import pathlib
import time
import urllib.error
import urllib.request


def endpoint():
    home = pathlib.Path(os.environ.get("DSH_HOME") or (pathlib.Path.home() / ".dsh"))
    try:
        data = json.loads((home / "wake-endpoint.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    if not data.get("port") or not data.get("token"):
        return None
    return data


def available():
    return endpoint() is not None


def wake(reason, detail="", kind="info", event_id=None, data=None, context_path=None,
         session=None, text=None, timeout=5, dedupe_key=None):
    """POST one wake event.

    event_id: pass a deterministic id whenever a retry is possible. The bridge
    dedupes it for 7 days, so a retry of an already-delivered event comes back as
    {"accepted": False, "reason": "duplicate"} instead of waking twice.

    session: explicit target session id. Omit to use the bridge's configured
    default binding. "wake the wrong context" is a real failure mode, so this is
    always sent verbatim and never guessed.

    data: any flat JSON object. Templates reach into it as {{data.<key>}}.

    context_path: absolute path to a state/snapshot file the session should read.

    text: replaces the bridge's default [EXTERNAL WAKE] template. Use it when the
    event has a shape the template cannot express.

    dedupe_key: accepted for backwards compatibility with the old client only.
    """
    _ = dedupe_key
    ep = endpoint()
    if ep is None:
        return None
    payload = {
        "event_id": event_id or f"{reason}-{int(time.time() * 1000)}",
        "kind": kind or "info",
        "reason": reason,
        "detail": detail or "",
    }
    if data and isinstance(data, dict):
        payload["data"] = data
    if context_path:
        payload["context_path"] = str(context_path)
    if session:
        payload["session"] = session
    if text:
        payload["text"] = text

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{ep['port']}/wake",
        data=body,
        method="POST",
        headers={
            "content-type": "application/json; charset=utf-8",
            "x-wake-token": ep["token"],
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"accepted": False, "reason": f"http_{e.code}"}
    except Exception:
        return None


if __name__ == "__main__":
    print("endpoint:", endpoint())
    print("response:", wake("TEST_WAKE", detail="manual test from wakeclient.py"))
