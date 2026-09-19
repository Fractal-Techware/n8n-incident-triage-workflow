"""Docker harness: a real, pinned n8n plus the stdlib mock server, bootstrapped headlessly.

One Stack is one isolated Docker network with:
  * `mock`  python:3.12-alpine running tests/mock/mock.py (the LLM endpoint and the chat webhook)
  * `n8n`   n8nio/n8n:2.39.7, no-new-privileges, port bound to 127.0.0.1 only

Bootstrap mirrors what you do as a user: import the credentials, import the SHIPPED workflow JSON
(only the Settings node is overlaid so the LLM and chat URLs point at the mock), then publish it.
No test ever reaches the internet: the workflow only talks to the mock container.
"""
import copy
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TESTS = os.path.join(ROOT, "tests")
TMP = os.path.join(TESTS, ".tmp")
WORKFLOW = os.path.join(ROOT, "workflows", "alertmanager-ai-triage.json")
N8N_IMAGE = "n8nio/n8n:2.39.7"
PY_IMAGE = "python:3.12-alpine"
LABEL = "ftwtriagefree=e2e"
PREFIX = "ftwtriagefree"
WEBHOOK_SECRET = "Bearer e2e-webhook-secret-7f3a"
LLM_KEY = "Bearer sk-e2e-not-a-real-key"
OWNER = {"email": "e2e@fractaltechware.test", "firstName": "E2E", "lastName": "Owner",
         "password": "E2e-Passw0rd-Triage"}


class TestFailure(AssertionError):
    pass


def sh(args, check=True, timeout=300):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise TestFailure(f"command failed ({p.returncode}): {' '.join(args)}\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}")
    return p


def wait_until(fn, timeout=60, interval=1.0, what="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            v = fn()
            if v:
                return v
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(interval)
    raise TestFailure(f"timed out after {timeout}s waiting for {what} (last: {last!r})")


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def sweep():
    """Remove anything this harness may have left behind (also runs after a crash)."""
    ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={LABEL}"],
                         capture_output=True, text=True).stdout.split()
    if ids:
        subprocess.run(["docker", "rm", "-f", "-v"] + ids, capture_output=True)
    for n in subprocess.run(["docker", "network", "ls", "-q", "--filter", f"label={LABEL}"],
                            capture_output=True, text=True).stdout.split():
        subprocess.run(["docker", "network", "rm", n], capture_output=True)


CREDENTIALS = [
    {"id": "ftwCredWebhookAu", "name": "Triage webhook auth", "type": "httpHeaderAuth",
     "data": {"name": "Authorization", "value": WEBHOOK_SECRET}},
    {"id": "ftwCredLlmKey01", "name": "Triage LLM API key", "type": "httpHeaderAuth",
     "data": {"name": "Authorization", "value": LLM_KEY}},
]


class Stack:
    def __init__(self, port=5699, settings_overlay=None):
        self.prefix = f"{PREFIX}-{os.urandom(3).hex()}"
        self.network = self.prefix
        self.port = port
        self.overlay = settings_overlay or {}
        self.containers = []
        self.work = os.path.join(TMP, self.prefix)
        self.base = f"http://127.0.0.1:{port}"
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.n8n = f"{self.prefix}-n8n"
        self.mock = f"{self.prefix}-mock"
        self.mock_port = None
        self.workflow_id = None

    def __enter__(self):
        try:
            self.start()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        for c in reversed(self.containers):
            sh(["docker", "rm", "-f", "-v", c], check=False)
        sh(["docker", "network", "rm", self.network], check=False)
        shutil.rmtree(self.work, ignore_errors=True)

    # ------------------------------------------------------------ lifecycle
    def start(self):
        os.makedirs(os.path.join(self.work, "workflows"), exist_ok=True)
        sh(["docker", "network", "create", "--label", LABEL, self.network])
        self._start_mock()
        self._prepare_files()
        self._start_n8n()
        self._bootstrap()

    def _start_mock(self):
        cmd = ["docker", "run", "-d", "--label", LABEL, "--name", self.mock, "--network", self.network,
               "--network-alias", "mock", "--security-opt", "no-new-privileges", "--user", "65534:65534",
               "-p", "127.0.0.1::8080",
               "-v", f"{os.path.join(TESTS, 'mock')}:/mock:ro", PY_IMAGE, "python", "-u", "/mock/mock.py"]
        self.containers.append(self.mock)
        sh(cmd)
        out = sh(["docker", "port", self.mock, "8080/tcp"]).stdout.strip().splitlines()[0]
        self.mock_port = int(out.rsplit(":", 1)[1])
        wait_until(lambda: self.mock_get("/healthz") is not None, 30, 0.5, "mock server")

    def _prepare_files(self):
        """Copy the shipped workflow, replacing only the Settings node's JSON with default+overlay."""
        wf = json.load(open(WORKFLOW))
        node = next(n for n in wf["nodes"] if n["name"] == "Settings")
        merged = deep_merge(json.loads(node["parameters"]["jsonOutput"]), self.overlay)
        node["parameters"]["jsonOutput"] = json.dumps(merged, indent=2)
        self.workflow_id = wf["id"]
        json.dump(wf, open(os.path.join(self.work, "workflows", "workflow.json"), "w"), indent=1)
        json.dump(CREDENTIALS, open(os.path.join(self.work, "credentials.json"), "w"))

    def _start_n8n(self):
        env = {"N8N_ENCRYPTION_KEY": "e2e-encryption-key-not-secret-000", "N8N_DIAGNOSTICS_ENABLED": "false",
               "N8N_PERSONALIZATION_ENABLED": "false", "N8N_SECURE_COOKIE": "false", "GENERIC_TIMEZONE": "UTC",
               "TZ": "UTC", "N8N_VERSION_NOTIFICATIONS_ENABLED": "false", "N8N_TEMPLATES_ENABLED": "false",
               "N8N_LOG_LEVEL": "info", "EXECUTIONS_DATA_PRUNE": "false", "N8N_RUNNERS_ENABLED": "true"}
        cmd = ["docker", "run", "-d", "--label", LABEL, "--name", self.n8n, "--network", self.network,
               "--security-opt", "no-new-privileges", "-p", f"127.0.0.1:{self.port}:5678"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [N8N_IMAGE]
        self.containers.append(self.n8n)
        sh(cmd)
        self.wait_ready()

    def wait_ready(self, timeout=600):
        def ready():
            try:
                with urllib.request.urlopen(f"{self.base}/healthz/readiness", timeout=3) as r:
                    return r.status == 200
            except Exception:  # noqa: BLE001
                return False
        wait_until(ready, timeout, 1, f"n8n readiness on {self.base}")

    def _bootstrap(self):
        sh(["docker", "exec", self.n8n, "mkdir", "-p", "/home/node/import"])
        sh(["docker", "cp", os.path.join(self.work, "workflows"), f"{self.n8n}:/home/node/import/"])
        sh(["docker", "cp", os.path.join(self.work, "credentials.json"),
            f"{self.n8n}:/home/node/import/credentials.json"])
        sh(["docker", "exec", self.n8n, "n8n", "import:credentials", "--input=/home/node/import/credentials.json"])
        sh(["docker", "exec", self.n8n, "n8n", "import:workflow", "--separate", "--input=/home/node/import/workflows"])
        self.rest("POST", "/rest/owner/setup", OWNER)
        self.rest("POST", "/rest/login", {"emailOrLdapLoginId": OWNER["email"], "password": OWNER["password"]})
        w = self.rest("GET", f"/rest/workflows/{self.workflow_id}")
        w = w.get("data", w)
        self.rest("POST", f"/rest/workflows/{self.workflow_id}/activate", {"versionId": w["versionId"]})
        self.wait_webhook("ftw-alertmanager-triage")

    def wait_webhook(self, path, timeout=90):
        """Readiness is reported before the workflow is active: wait until the webhook rejects on auth."""
        wait_until(lambda: self.webhook(path, {}, auth=None)[0] in (401, 403), timeout, 1,
                   f"webhook {path} registered")

    # ------------------------------------------------------------ HTTP helpers
    def rest(self, method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("browser-id", "e2e-browser")
        try:
            with self.opener.open(req, timeout=timeout) as r:
                txt = r.read().decode()
                return json.loads(txt) if txt else None
        except urllib.error.HTTPError as e:
            raise TestFailure(f"{method} {path} -> {e.code}: {e.read().decode()[:500]}") from None

    def webhook(self, path, payload, auth=WEBHOOK_SECRET, timeout=60):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(f"{self.base}/webhook/{path}", data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if auth:
            req.add_header("Authorization", auth)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def triage(self, payload):
        status, _ = self.webhook("ftw-alertmanager-triage", payload)
        if status != 200:
            raise TestFailure(f"webhook returned {status}")
        return status

    def mock_get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.mock_port}{path}", timeout=5) as r:
            return json.loads(r.read().decode())

    def reset_log(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.mock_port}/_reset", data=b"{}", method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()

    def log(self, path_prefix=None):
        entries = self.mock_get("/_log")
        return [e for e in entries if not path_prefix or e["path"].startswith(path_prefix)]

    def wait_for(self, path_prefix, count=1, timeout=60):
        """Wait until the mock has received `count` requests under `path_prefix` and return them."""
        return wait_until(lambda: (lambda es: es if len(es) >= count else None)(self.log(path_prefix)),
                          timeout, 0.5, f"{count} request(s) to {path_prefix}")

    def logs(self, tail=200):
        return sh(["docker", "logs", "--tail", str(tail), self.n8n], check=False).stderr
