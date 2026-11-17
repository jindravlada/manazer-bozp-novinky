"""Lokální zkoušky bezpečnostního testu tokenu. Na GitHub nic neposílají."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import overit_token_prijmu as probe

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "overit-token-prijmu.yml"
SCRIPT = ROOT / "nastroje" / "overit_token_prijmu.py"
TOKEN = "github_pat_lokalni_zkouska"
SHA = "0123456789abcdef0123456789abcdef01234567"
CANARY = "TAJEMSTVI_README_NEVYPISOVAT"
PERMISSION = {
    "message": "Resource not accessible by personal access token",
    "documentation_url": "https://invalid.test/canary-docs",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _authorized(self) -> None:
        header = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.expected_token
        if header != expected:
            self.server.bad_auth += 1
        if TOKEN in self.path or "github_pat_" in self.path:
            self.server.token_in_path += 1
        self.server.seen.append(f"{self.command} {self.path}")

    def _read_body(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length:
            self.rfile.read(length)

    def _send(self, spec) -> None:
        if spec is None:
            spec = (500, b"")
        status, body = spec
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        if status in {301, 302, 303, 307, 308}:
            self.send_header("Location", "http://127.0.0.1:9/presmerovano")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._authorized()
        self._send(self.server.routes.get(("GET", self.path)))

    def do_POST(self):
        self._authorized()
        self._read_body()
        self._send(self.server.routes.get(("POST", self.path)))


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            key: os.environ.get(key)
            for key in ("GITHUB_ACTIONS", "GITHUB_REF", "GITHUB_TOKEN", "NOVINKY_TOKEN_TEST_API", "PRIJEM_READ_TOKEN")
        }

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_classify_permission_denial_and_other_errors(self):
        denied = json.dumps(PERMISSION).encode()
        self.assertEqual(probe.classify_write(403, denied), "odmitnuto_opravneni")
        self.assertEqual(probe.classify_write(201, b"{}"), "zapis_prosel")
        self.assertEqual(probe.classify_write(200, b"{}"), "zapis_prosel")
        self.assertEqual(probe.classify_write(401, denied), "jina_chyba")
        self.assertEqual(probe.classify_write(404, denied), "jina_chyba")
        self.assertEqual(probe.classify_write(422, denied), "jina_chyba")
        limited = json.dumps({"message": "API rate limit exceeded"}).encode()
        self.assertEqual(probe.classify_write(403, limited), "jina_chyba")
        self.assertEqual(probe.classify_write(403, b""), "jina_chyba")
        self.assertEqual(probe.classify_write(None, b""), "jina_chyba")

    def test_production_api_is_not_called_outside_actions(self):
        os.environ.pop("GITHUB_ACTIONS", None)
        os.environ.pop("NOVINKY_TOKEN_TEST_API", None)
        with self.assertRaises(RuntimeError):
            probe.resolve_api_base()

    def test_actions_ignore_override_and_reject_remote_test_api(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["NOVINKY_TOKEN_TEST_API"] = "http://evil.example/api"
        self.assertEqual(probe.resolve_api_base(), probe.API_PRODUCTION)
        os.environ.pop("GITHUB_ACTIONS", None)
        os.environ["NOVINKY_TOKEN_TEST_API"] = "http://evil.example/api"
        with self.assertRaises(RuntimeError):
            probe.resolve_api_base()
        os.environ["NOVINKY_TOKEN_TEST_API"] = "http://127.0.0.1:9/api"
        self.assertEqual(probe.resolve_api_base(), "http://127.0.0.1:9/api")

    def test_secret_on_another_branch_stops_before_network(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["GITHUB_REF"] = "refs/heads/pokus"
        os.environ["PRIJEM_READ_TOKEN"] = TOKEN
        code, output = _run_script(extra_env={
            "GITHUB_ACTIONS": "true",
            "GITHUB_REF": "refs/heads/pokus",
            "PRIJEM_READ_TOKEN": TOKEN,
        })
        self.assertEqual(code, 2)
        self.assertIn("bezpecnostni_selhani: secret je dostupny mimo main", output)
        self.assertFalse(_leaks(output))

    def test_missing_secret_on_another_branch_does_not_call_network(self):
        code, output = _run_script(extra_env={
            "GITHUB_ACTIONS": "true",
            "GITHUB_REF": "refs/heads/pokus",
        })
        self.assertEqual(code, 1)
        self.assertIn("odmitnuto: spusteni mimo main, secret chybi", output)

    def test_read_succeeds_and_three_writes_are_denied(self):
        code, output, seen = _conversation({
            "readme": (200, CANARY),
            "refs": (200, {"object": {"sha": SHA}}),
            "create": (403, PERMISSION),
        })
        self.assertEqual(code, 0, output)
        self.assertIn(f"cteni manazer-bozp-prijem README.md: HTTP 200", output)
        for repo in probe.WRITE_REPOS:
            self.assertIn(f"zapis {repo}: odmitnuto_opravneni HTTP 403", output)
        self.assertIn("vysledek: token cte prijem a zapisy jsou odmitnute", output)
        self.assertEqual(sum(1 for item in seen if item.startswith("POST ")), 3)
        self.assertNotIn(CANARY, output)
        self.assertNotIn(SHA, output)
        self.assertFalse(_leaks(output))

    def test_unexpected_write_stops_and_does_not_touch_other_repos(self):
        code, output, seen = _conversation({
            "readme": (200, {"ok": True}),
            "refs": (200, {"object": {"sha": SHA}}),
            "create": (201, {"ref": "refs/heads/bezpecnostni-test/nezapisovat-x"}),
        })
        self.assertEqual(code, 2)
        self.assertIn("bezpecnostni_selhani: vznikla vetev jindravlada/manazer-bozp-prijem ", output)
        self.assertNotIn("manazer-bozp-novinky", output)
        posts = [item for item in seen if item.startswith("POST ")]
        self.assertEqual(posts, ["POST /repos/jindravlada/manazer-bozp-prijem/git/refs"])
        self.assertFalse(any("manazer-bozp-novinky" in item or item.endswith("/manazer-bozp/git/refs") for item in seen))
        self.assertFalse(_leaks(output))

    def test_other_api_errors_are_not_treated_as_denial(self):
        for status in (401, 404, 422):
            with self.subTest(status=status):
                code, output, _seen = _conversation({
                    "readme": (200, {"ok": True}),
                    "refs": (200, {"object": {"sha": SHA}}),
                    "create": (status, {"message": "Resource not accessible by personal access token"}),
                })
                self.assertEqual(code, 1)
                self.assertIn(f"zapis manazer-bozp-prijem: jina_chyba HTTP {status}", output)
                self.assertNotIn("vysledek:", output)

    def test_failed_read_does_not_attempt_write(self):
        code, output, seen = _conversation({
            "readme": (401, {"message": "Bad credentials"}),
            "refs": (200, {"object": {"sha": SHA}}),
            "create": (403, PERMISSION),
        })
        self.assertEqual(code, 1)
        self.assertIn("cteni manazer-bozp-prijem README.md: jina_chyba HTTP 401", output)
        self.assertFalse(any(item.startswith("POST ") for item in seen))
        self.assertNotIn("Bad credentials", output)

    def test_redirect_is_not_followed(self):
        code, output, seen = _conversation({
            "readme": (302, b""),
            "refs": (200, {"object": {"sha": SHA}}),
            "create": (403, PERMISSION),
        })
        self.assertEqual(code, 1)
        self.assertIn("jina_chyba HTTP 302", output)
        self.assertEqual(seen, ["GET /repos/jindravlada/manazer-bozp-prijem/contents/README.md"])
        self.assertNotIn("presmerovano", output)

    def test_workflow_is_manual_read_only_and_does_not_publish(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("environment: prijem-cteni", text)
        self.assertEqual(text.count("secrets.PRIJEM_READ_TOKEN"), 1)
        self.assertIn("contents: read", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("python3 nastroje/overit_token_prijmu.py", text)
        for banned in (
            "schedule:",
            "cron:",
            "pull_request",
            "workflow_run",
            "workflow_call",
            "repository_dispatch",
            "push:",
            "contents: write",
            "pages:",
            "set -x",
            "bash -x",
            "curl",
            "index.json",
            "deploy-pages",
            "publikuj_prijem",
            "generuj_prehled",
            "actions/upload-artifact",
            "ACTIONS_STEP_DEBUG",
            "echo ",
        ):
            self.assertNotIn(banned, text)
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("set -x", source)
        self.assertNotIn("token[:", source)
        self.assertNotIn("print(token", source)


def _leaks(output: str) -> bool:
    return TOKEN in output or "TESTVALUE" in output or "Bearer " in output


def _run_script(extra_env: dict[str, str], cwd: Path | None = None) -> tuple[int, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PRIJEM_READ_TOKEN", "GITHUB_TOKEN", "GITHUB_ACTIONS", "GITHUB_REF", "NOVINKY_TOKEN_TEST_API"}
    }
    env.update(extra_env)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        cwd=cwd or ROOT,
        env=env,
    )
    return completed.returncode, completed.stdout + completed.stderr


def _conversation(routes_spec: dict) -> tuple[int, str, list[str]]:
    def body_of(value):
        if isinstance(value, bytes):
            return value
        return json.dumps(value).encode("utf-8")

    routes = {
        ("GET", "/repos/jindravlada/manazer-bozp-prijem/contents/README.md"): (
            routes_spec["readme"][0],
            body_of(routes_spec["readme"][1]),
        ),
    }
    for repo in probe.WRITE_REPOS:
        routes[("GET", f"/repos/jindravlada/{repo}/git/ref/heads/main")] = (
            routes_spec["refs"][0],
            body_of(routes_spec["refs"][1]),
        )
        routes[("POST", f"/repos/jindravlada/{repo}/git/refs")] = (
            routes_spec["create"][0],
            body_of(routes_spec["create"][1]),
        )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.routes = routes
    server.expected_token = TOKEN
    server.bad_auth = 0
    server.token_in_path = 0
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        code, output = _run_script(extra_env={
            "NOVINKY_TOKEN_TEST_API": base,
            "PRIJEM_READ_TOKEN": TOKEN,
        })
        if server.bad_auth or server.token_in_path:
            raise AssertionError("autorizace nebo cesta zadosti nesplnuje ocekavani")
        return code, output, list(server.seen)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    unittest.main()
