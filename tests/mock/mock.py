#!/usr/bin/env python3
"""Mock LLM endpoint and chat webhook for the e2e test (stdlib only, no network access).

Runs inside a python:3.12-alpine container on the test network:

    python3 mock.py        # listens on :8080

Upstreams it pretends to be:
    POST /v1/chat/completions   OpenAI-compatible chat completions (the workflow's LLM call)
    POST /slack/*, /generic/*   chat webhook sinks

Control endpoints (test harness only):
    GET  /_log      every request received (method, path, headers, body)
    POST /_reset    clear the log
    GET  /healthz   readiness

Failure modes are triggered by markers in the prompt, so one running stack can exercise them:
    MockLlm500        the LLM answers 500
    MockLlmMalformed  the LLM answers prose that is not JSON
    MockLlmFenced     valid JSON wrapped in prose and a ```json fence
"""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOG = []
LOCK = threading.Lock()


def prompt_text(body):
    """Concatenate every text the client sent, whatever the message shape."""
    parts = []

    def walk(x):
        if isinstance(x, str):
            parts.append(x)
        elif isinstance(x, list):
            for i in x:
                walk(i)
        elif isinstance(x, dict):
            for k, v in x.items():
                if k in ("model", "role", "type", "stream", "temperature"):
                    continue
                walk(v)

    walk(body.get("messages") or [])
    return "\n".join(parts)


def llm_answer(prompt):
    """Return (status, text). status is None for a normal answer."""
    if "MockLlm500" in prompt:
        return 500, None
    if "MockLlmMalformed" in prompt:
        return None, "Sure! Here is the triage: {summary: the database, likely_cause = unknown,,"
    names = list(dict.fromkeys(re.findall(r"alertname[\"'\\]*\s*[:=]\s*[\"'\\]*([A-Za-z][A-Za-z0-9_]+)", prompt)))
    obj = {
        "summary": f"MOCK summary: {', '.join(names) or 'no alertname'} needs attention",
        "likely_cause": f"MOCK likely cause for {names[0] if names else 'the alert'}",
        "first_checks": [f"Check recent deploys for {names[0] if names else 'the service'}",
                         "Check error rate and saturation dashboards"],
        "severity_assessment": "high" if re.search(r'"severity":\s*"critical"', prompt) else "medium",
    }
    if "MockLlmFenced" in prompt:
        return None, "Here is the JSON you asked for:\n```json\n" + json.dumps(obj, indent=2) + "\n```\nHope this helps."
    return None, json.dumps(obj)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _record(self, body_text):
        u = urlparse(self.path)
        try:
            parsed = json.loads(body_text) if body_text else None
        except ValueError:
            parsed = None
        entry = {"ts": time.time(), "method": self.command, "path": u.path, "query": parse_qs(u.query),
                 "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body_text, "json": parsed}
        if not u.path.startswith("/_"):
            with LOCK:
                LOG.append(entry)
        return u, entry

    def _send(self, status, obj=None, text=None, ctype="application/json"):
        data = (json.dumps(obj) if obj is not None else (text or "")).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u, _ = self._record("")
        if u.path == "/_log":
            with LOCK:
                return self._send(200, list(LOG))
        if u.path == "/healthz":
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found", "path": u.path})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        u, e = self._record(body)
        p = u.path
        j = e["json"] if isinstance(e["json"], dict) else {}
        if p == "/_reset":
            with LOCK:
                LOG.clear()
            return self._send(200, {"ok": True})
        if p == "/v1/chat/completions":
            if not e["headers"].get("authorization", "").startswith("Bearer "):
                return self._send(401, {"error": {"message": "missing bearer token"}})
            status, text = llm_answer(prompt_text(j))
            if status:
                return self._send(status, {"error": {"message": "mock upstream failure", "type": "server_error"}})
            return self._send(200, {"id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()),
                                    "model": j.get("model", "mock"),
                                    "choices": [{"index": 0, "finish_reason": "stop",
                                                 "message": {"role": "assistant", "content": text}}],
                                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}})
        if p.startswith("/slack/"):
            return self._send(200, text="ok", ctype="text/plain")
        if p.startswith("/generic/"):
            return self._send(202, {"ok": True})
        return self._send(404, {"error": "not found", "path": p})


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    print("mock listening on :8080", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
