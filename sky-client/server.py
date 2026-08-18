"""The local Anthropic-API-mimicking HTTP server Claude Code talks to.

Point Claude Code at it with ANTHROPIC_BASE_URL=http://localhost:8377.
POST /v1/messages goes over the transport to the home host; count_tokens and
models are stubbed locally so nothing else needs the round trip.
"""

import json
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from shared import delta as deltalib

import sse

PING_INTERVAL = 15.0

HEADER_ALLOWLIST = ("anthropic-version", "anthropic-beta")

MODELS = [
    {"type": "model", "id": "claude-fable-5", "display_name": "Claude Fable 5", "created_at": "2026-06-01T00:00:00Z"},
    {"type": "model", "id": "claude-opus-4-8", "display_name": "Claude Opus 4.8", "created_at": "2026-03-01T00:00:00Z"},
    {"type": "model", "id": "claude-sonnet-5", "display_name": "Claude Sonnet 5", "created_at": "2026-01-01T00:00:00Z"},
    {"type": "model", "id": "claude-haiku-4-5-20251001", "display_name": "Claude Haiku 4.5", "created_at": "2025-10-01T00:00:00Z"},
]


def anthropic_error(err_type, message):
    return {"type": "error", "error": {"type": err_type, "message": message}}


class SkyClient:
    """Owns the transport: sends REQs, routes RSPs/ERRs back to waiters."""

    def __init__(self, cfg, transport):
        self.cfg = cfg
        self.transport = transport
        self.cache = deltalib.BodyCache()
        self.pending = {}  # msg_id -> Queue
        self.lock = threading.Lock()
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def _recv_loop(self):
        for kind, msg_id, obj in self.transport.receive():
            if kind not in ("RSP", "ERR"):
                continue
            with self.lock:
                waiter = self.pending.get(msg_id)
            if waiter:
                waiter.put((kind, obj))

    def roundtrip(self, path, headers, body_str):
        """Send one API request over the transport; returns (status, body)."""
        msg_id = secrets.token_hex(4)
        waiter = queue.Queue()
        with self.lock:
            self.pending[msg_id] = waiter
        try:
            base_env = {
                "path": path,
                "method": "POST",
                "headers": {k: headers[k] for k in HEADER_ALLOWLIST if headers.get(k)},
            }
            with self.lock:
                env = {**base_env, **deltalib.encode_body(body_str, self.cache)}
            self.transport.send("REQ", msg_id, env)

            deadline = time.time() + self.cfg["SKY_RESPONSE_TIMEOUT"]
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return 504, anthropic_error(
                        "api_error",
                        "skycode: no reply from home host before SKY_RESPONSE_TIMEOUT",
                    )
                try:
                    kind, obj = waiter.get(timeout=remaining)
                except queue.Empty:
                    continue

                if kind == "ERR" and obj.get("code") == "delta_base_missing":
                    # Host lost our delta base (restart/eviction): resend full.
                    self.transport.send("REQ", msg_id, {**base_env, "body_full": body_str})
                    continue

                # Any real reply proves the host materialized this body, so
                # it's now a valid delta base on both sides.
                with self.lock:
                    self.cache.add(body_str)
                if kind == "ERR":
                    return obj.get("status", 502), obj.get(
                        "body", anthropic_error("api_error", "skycode: home host error")
                    )
                return obj.get("status", 200), obj.get("body")
        finally:
            with self.lock:
                self.pending.pop(msg_id, None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    client = None  # set by serve()

    def log_message(self, fmt, *args):
        print(f"[sky-client] {self.command} {self.path} {fmt % args}")

    def _write_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0].rstrip("/") == "/v1/models":
            self._write_json(200, {
                "data": MODELS,
                "has_more": False,
                "first_id": MODELS[0]["id"],
                "last_id": MODELS[-1]["id"],
            })
        else:
            # Connectivity probes just need a 200.
            self._write_json(200, {"ok": True})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        path = self.path.split("?")[0].rstrip("/")
        try:
            if path == "/v1/messages":
                self._handle_messages(raw)
            elif path == "/v1/messages/count_tokens":
                self._handle_count_tokens(raw)
            else:
                self._write_json(404, anthropic_error(
                    "not_found_error", f"skycode does not tunnel {path}"))
        except (BrokenPipeError, ConnectionResetError):
            pass  # Claude Code hung up; nothing to do.

    def _handle_count_tokens(self, raw):
        # Local heuristic (~4 chars/token) — only affects the context-size
        # display, not worth an iMessage round trip.
        try:
            body = json.loads(raw)
        except ValueError:
            return self._write_json(400, anthropic_error("invalid_request_error", "invalid JSON"))
        self._write_json(200, {"input_tokens": max(1, len(json.dumps(body)) // 4)})

    def _handle_messages(self, raw):
        try:
            body = json.loads(raw)
        except ValueError:
            return self._write_json(400, anthropic_error("invalid_request_error", "invalid JSON"))

        wanted_stream = bool(body.pop("stream", False))
        # Canonicalize with stream stripped so consecutive turns delta well.
        body_str = deltalib.canonicalize(body)
        headers = {k: self.headers.get(k) for k in HEADER_ALLOWLIST}

        if not wanted_stream:
            status, resp = self.client.roundtrip("/v1/messages", headers, body_str)
            return self._write_json(status, resp)

        # Streaming: open the SSE response immediately and ping while the
        # iMessage round trip is in flight so Claude Code keeps the
        # connection alive; then replay the complete message as a stream.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        result = queue.Queue()
        threading.Thread(
            target=lambda: result.put(self.client.roundtrip("/v1/messages", headers, body_str)),
            daemon=True,
        ).start()
        while True:
            try:
                status, resp = result.get(timeout=PING_INTERVAL)
                break
            except queue.Empty:
                self.wfile.write(sse.ping())
                self.wfile.flush()

        if status == 200 and isinstance(resp, dict) and resp.get("type") == "message":
            for chunk in sse.synthesize(resp):
                self.wfile.write(chunk)
        else:
            err = resp.get("error") if isinstance(resp, dict) else None
            self.wfile.write(sse.error(err or {
                "type": "api_error", "message": f"skycode: upstream returned {status}"}))
        self.wfile.flush()


def serve(cfg, transport):
    Handler.client = SkyClient(cfg, transport)
    server = ThreadingHTTPServer(("127.0.0.1", cfg["SKY_PORT"]), Handler)
    print(f"[sky-client] listening on http://localhost:{cfg['SKY_PORT']}")
    print(f"[sky-client] run Claude Code with: ANTHROPIC_BASE_URL=http://localhost:{cfg['SKY_PORT']} "
          "ANTHROPIC_API_KEY=skycode-dummy claude")
    server.serve_forever()
