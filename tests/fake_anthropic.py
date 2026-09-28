"""A tiny local stand-in for the Anthropic Messages API, so the tests exercise the REAL SDK
(HTTP, retries, timeouts, error classes) without a network or an API key.

Point the SDK at it with ANTHROPIC_BASE_URL. `server.behaviour` is a function taking the
parsed request body and returning (status, json_body, delay_seconds)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def message(text: str, *, model: str = "claude-opus-5", stop: str = "end_turn", tin: int = 3000, tout: int = 800,
            extra: dict | None = None) -> dict:
    body = {"id": "msg_test", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": tin, "output_tokens": tout}}
    body.update(extra or {})
    return body


def verdict(decision="approve", mult=1.0, conf=0.6, reasons=("Looks like a clean breakout.",)) -> str:
    return json.dumps({"decision": decision, "size_multiplier": mult, "confidence": conf, "reasons": list(reasons)})


def error(status: int, kind: str, msg: str = "boom") -> tuple[int, dict, float]:
    return status, {"type": "error", "error": {"type": kind, "message": msg}}, 0.0


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        srv = self.server
        with srv.lock:
            srv.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        status, out, delay = srv.behaviour(body)
        if delay:
            time.sleep(delay)
        data = json.dumps(out).encode()
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.send_header("request-id", "req_test")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):  # the client timed out and hung up
            pass


class FakeAnthropic:
    def __init__(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.srv.requests = []
        self.srv.lock = threading.Lock()
        self.srv.behaviour = lambda body: (200, message(verdict()), 0.0)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    @property
    def requests(self) -> list[dict]:
        return self.srv.requests

    def respond(self, fn):
        self.srv.behaviour = fn

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
