"""A fake api.anthropic.com for e2e tests: echoes the last user message back
as a canned assistant response. Point home-host at it with SKY_UPSTREAM_URL.

Also runnable standalone:  python3 tests/mock_upstream.py 9999
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def canned_message(body):
    last_user = ""
    for m in reversed(body.get("messages", [])):
        if m.get("role") == "user":
            content = m.get("content")
            last_user = content if isinstance(content, str) else json.dumps(content)
            break
    return {
        "id": "msg_mock_0001",
        "type": "message",
        "role": "assistant",
        "model": body.get("model", "claude-fable-5"),
        "content": [{"type": "text", "text": f"echo: {last_user[:200]}"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 42, "output_tokens": 7},
    }


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        if self.path.rstrip("/") != "/v1/messages":
            return self._json(404, {"type": "error", "error": {
                "type": "not_found_error", "message": self.path}})
        if not self.headers.get("x-api-key"):
            return self._json(401, {"type": "error", "error": {
                "type": "authentication_error", "message": "missing x-api-key"}})
        self._json(200, canned_message(body))

    def _json(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_server(port=0):
    return ThreadingHTTPServer(("127.0.0.1", port), MockHandler)


if __name__ == "__main__":
    server = make_server(int(sys.argv[1]) if len(sys.argv) > 1 else 9999)
    print(f"mock upstream on http://127.0.0.1:{server.server_address[1]}")
    server.serve_forever()
