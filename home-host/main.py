"""home-host entrypoint — run on the Mac at home with real internet.

Watches for REQ messages from the sky-client, forwards them to the Anthropic
API with the real key, and sends the response back over the same transport.

    ANTHROPIC_API_KEY=... SKY_PEER_NUMBER=+1... python3 home-host/main.py
    python3 home-host/main.py --transport file      # loopback via spool dir
"""

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import config
from shared import delta as deltalib
from shared.transport import FileTransport, IMessageTransport


def forward_to_anthropic(cfg, env, body_obj):
    """POST the request to the real API; returns (status, response_body)."""
    body_obj.pop("stream", None)  # de-stream here; sky-client re-synthesizes SSE
    data = json.dumps(body_obj).encode("utf-8")

    fwd_headers = env.get("headers") or {}
    headers = {
        "Content-Type": "application/json",
        "x-api-key": cfg["ANTHROPIC_API_KEY"],
        "anthropic-version": fwd_headers.get("anthropic-version") or "2023-06-01",
    }
    if fwd_headers.get("anthropic-beta"):
        headers["anthropic-beta"] = fwd_headers["anthropic-beta"]

    url = cfg["SKY_UPSTREAM_URL"].rstrip("/") + env.get("path", "/v1/messages")
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=env.get("method", "POST"))
    try:
        with urllib.request.urlopen(request, timeout=600) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except ValueError:
            body = {"type": "error",
                    "error": {"type": "api_error", "message": f"upstream HTTP {e.code}"}}
        return e.code, body


def handle_request(cfg, transport, msg_id, env, cache, cache_lock):
    started = time.time()
    try:
        with cache_lock:
            body_str = deltalib.materialize(env, cache)
    except deltalib.BaseMissing:
        print(f"[home-host] {msg_id}: delta base missing, asking for full body")
        transport.send("ERR", msg_id, {"code": "delta_base_missing"})
        return
    except deltalib.DeltaError as e:
        transport.send("ERR", msg_id, {
            "status": 502,
            "body": {"type": "error",
                     "error": {"type": "api_error", "message": f"skycode delta error: {e}"}},
        })
        return

    mode = "full" if "body_full" in env else "delta"
    try:
        status, body = forward_to_anthropic(cfg, env, json.loads(body_str))
        transport.send("RSP", msg_id, {"status": status, "body": body})
    except Exception as e:
        transport.send("ERR", msg_id, {
            "status": 502,
            "body": {"type": "error",
                     "error": {"type": "api_error", "message": f"skycode network error: {e}"}},
        })
        status = "ERR"
    print(f"[home-host] {msg_id}: {mode} req {len(body_str)}B -> {status} "
          f"in {time.time() - started:.1f}s")


def main():
    parser = argparse.ArgumentParser(description="skycode home-host")
    parser.add_argument("--transport", choices=["imessage", "file"], default="imessage")
    parser.add_argument("--spool", default="/tmp/sky-spool",
                        help="spool dir for --transport file")
    parser.add_argument("--include-from-me", action="store_true",
                        help="also read your own outgoing texts (single-Mac "
                             "self-texting e2e test)")
    args = parser.parse_args()

    required = ["ANTHROPIC_API_KEY"]
    if args.transport == "imessage":
        required.append("SKY_PEER_NUMBER")
    cfg = config.load(required=required)

    if args.transport == "imessage":
        transport = IMessageTransport(cfg, include_from_me=args.include_from_me)
        print(f"[home-host] transport: iMessage <-> {cfg['SKY_PEER_NUMBER']}")
    else:
        transport = FileTransport(args.spool, role="host")
        print(f"[home-host] transport: file spool at {args.spool}")
    print(f"[home-host] forwarding to {cfg['SKY_UPSTREAM_URL']}")

    cache = deltalib.BodyCache()
    cache_lock = threading.Lock()
    for kind, msg_id, env in transport.receive():
        if kind != "REQ":
            continue
        threading.Thread(
            target=handle_request,
            args=(cfg, transport, msg_id, env, cache, cache_lock),
            daemon=True,
        ).start()


if __name__ == "__main__":
    main()
