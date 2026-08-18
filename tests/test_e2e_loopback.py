"""Full-pipeline e2e: spawns the REAL sky-client and home-host processes,
wired together with the file-spool transport, against a mock Anthropic
upstream. Everything is exercised except the iMessage leg itself — no API
key, no Messages.app, runs anywhere.

    Claude Code (urllib here) -> sky-client proc -> spool files ->
    home-host proc -> mock upstream -> back again
"""

import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mock_upstream

STARTUP_TIMEOUT = 15


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port, timeout=STARTUP_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {port} never came up")


def post(port, path, body, timeout=30):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class TestE2ELoopback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="skycode-e2e-")
        spool = str(Path(cls.tmp.name) / "spool")
        cls.port = free_port()

        cls.upstream = mock_upstream.make_server()
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        upstream_url = f"http://127.0.0.1:{cls.upstream.server_address[1]}"

        cls.host_log = open(Path(cls.tmp.name) / "host.log", "w+")
        cls.client_log = open(Path(cls.tmp.name) / "client.log", "w+")

        cls.host_proc = subprocess.Popen(
            [sys.executable, "home-host/main.py", "--transport", "file", "--spool", spool],
            cwd=ROOT, stdout=cls.host_log, stderr=subprocess.STDOUT,
            env={"PATH": "/usr/bin:/bin", "PYTHONUNBUFFERED": "1","ANTHROPIC_API_KEY": "test-key",
                 "SKY_UPSTREAM_URL": upstream_url},
        )
        cls.client_proc = subprocess.Popen(
            [sys.executable, "sky-client/main.py", "--transport", "file", "--spool", spool],
            cwd=ROOT, stdout=cls.client_log, stderr=subprocess.STDOUT,
            env={"PATH": "/usr/bin:/bin", "PYTHONUNBUFFERED": "1","SKY_PORT": str(cls.port),
                 "SKY_RESPONSE_TIMEOUT": "30"},
        )
        wait_for_port(cls.port)

    @classmethod
    def tearDownClass(cls):
        for proc in (cls.client_proc, cls.host_proc):
            proc.terminate()
            proc.wait(timeout=5)
        cls.upstream.shutdown()
        cls.host_log.close()
        cls.client_log.close()
        cls.tmp.cleanup()

    def request_body(self, turns):
        messages = []
        for i in range(turns):
            messages.append({"role": "user", "content": f"turn-{i} question " + "x" * 500})
            if i < turns - 1:
                messages.append({"role": "assistant", "content": f"turn-{i} answer " + "y" * 500})
        return {"model": "claude-fable-5", "max_tokens": 100,
                "system": "test system prompt " + "s" * 300, "messages": messages}

    def host_log_text(self):
        self.host_log.flush()
        return Path(self.host_log.name).read_text()

    def test_01_non_streaming_roundtrip(self):
        status, data = post(self.port, "/v1/messages", self.request_body(1))
        self.assertEqual(status, 200)
        resp = json.loads(data)
        self.assertEqual(resp["type"], "message")
        self.assertIn("turn-0 question", resp["content"][0]["text"])

    def test_02_second_turn_goes_as_delta(self):
        status, data = post(self.port, "/v1/messages", self.request_body(2))
        self.assertEqual(status, 200)
        self.assertIn("turn-1 question", json.loads(data)["content"][0]["text"])

        deadline = time.time() + 5
        while time.time() < deadline and "delta req" not in self.host_log_text():
            time.sleep(0.2)
        self.assertIn("delta req", self.host_log_text())

    def test_03_streaming_roundtrip(self):
        body = dict(self.request_body(3), stream=True)
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/messages",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
            raw = resp.read().decode()

        events = [line.removeprefix("event: ")
                  for line in raw.split("\n") if line.startswith("event: ")]
        self.assertIn("message_start", events)
        self.assertIn("content_block_delta", events)
        self.assertEqual(events[-1], "message_stop")
        self.assertIn("turn-2 question", raw)

    def test_04_count_tokens_is_local(self):
        status, data = post(self.port, "/v1/messages/count_tokens", self.request_body(1))
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(data)["input_tokens"], 100)

    def test_05_models_endpoint(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/models", timeout=10) as resp:
            models = json.loads(resp.read())
        self.assertTrue(any(m["id"].startswith("claude-") for m in models["data"]))


if __name__ == "__main__":
    unittest.main()
