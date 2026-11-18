"""Zkoušky automatického zveřejnění. Na GitHub nic neposílají."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_overit_vzdaleny_prijem as fixtures
import zverejni_prijem as publisher
import zverejni_vzdaleny_prijem as job

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "nastroje" / "zverejni_vzdaleny_prijem.py"
WORKFLOW = ROOT / ".github" / "workflows" / "zverejnit-overeny-prehled.yml"
FICTIONAL = ROOT / ".github" / "workflows" / "publikovat-prehled.yml"
TOKEN = "github_pat_lokalni_zkouska"
ACCEPTED_DAY = "2026-11-23"
PATH_W47 = "/repos/jindravlada/manazer-bozp-prijem/contents/prijem/2026-W47.json?ref=main"


def copy_channel(destination: Path) -> None:
    destination.mkdir()
    shutil.copyfile(ROOT / "index.json", destination / "index.json")
    folder = destination / "prehledy"
    folder.mkdir()
    for source in (ROOT / "prehledy").glob("*.json"):
        shutil.copyfile(source, folder / source.name)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        header = self.headers.get("Authorization", "")
        if header != "Bearer " + self.server.expected_token:
            self.server.bad_auth += 1
        if TOKEN in self.path:
            self.server.token_in_path += 1
        self.server.seen.append(self.path)
        spec = self.server.routes.get(self.path)
        status, payload = (404, b"{}") if spec is None else spec
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        self.server.posts += 1
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()


class RemotePublishTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".tmp-test"
        base.mkdir(exist_ok=True)
        self.channel = base / self.id().replace(".", "_")
        if self.channel.exists():
            shutil.rmtree(self.channel)
        copy_channel(self.channel)

    def tearDown(self):
        shutil.rmtree(self.channel, ignore_errors=True)
        for name, digest in fixtures.PUBLISHED.items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest)

    def test_missing_review_waits_and_valid_review_publishes_once(self):
        code, output, seen = self._serve({PATH_W47: (404, b"neni")})
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "ceka")
        self.assertNotIn("neni", output)
        self.assertEqual(seen, [PATH_W47])
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())

        raw = json.dumps(fixtures.finished_week(), ensure_ascii=False).encode("utf-8")
        body = fixtures.github_file(raw, "prijem/2026-W47.json")
        code, output, seen = self._serve({PATH_W47: (200, body)}, controlled_date=ACCEPTED_DAY)
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "zverejneno 2026-W47 polozek 1")
        self.assertEqual(seen, [PATH_W47])
        stored = (self.channel / "prehledy" / "2026-W47.json").read_bytes()
        self.assertNotIn(b"distribution_test", stored)
        for name in ("2026-W45.json", "2026-W46.json"):
            self.assertEqual(
                hashlib.sha256((self.channel / "prehledy" / name).read_bytes()).hexdigest(),
                fixtures.PUBLISHED[f"prehledy/{name}"],
            )

        next_path = "/repos/jindravlada/manazer-bozp-prijem/contents/prijem/2026-W48.json?ref=main"
        code, output, seen = self._serve({next_path: (404, b"{}")}, controlled_date="2026-11-30")
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "ceka")
        self.assertEqual(seen, [next_path])
        self.assertEqual((self.channel / "prehledy" / "2026-W47.json").read_bytes(), stored)

    def test_invalid_review_keeps_public_state(self):
        missing = fixtures.finished_week()
        del missing["content_markdown"]
        raw = json.dumps(missing, ensure_ascii=False).encode("utf-8")
        before = (self.channel / "index.json").read_bytes()
        code, output, _seen = self._serve(
            {PATH_W47: (200, fixtures.github_file(raw, "prijem/2026-W47.json"))},
            controlled_date=ACCEPTED_DAY,
        )
        self.assertEqual(code, 1)
        self.assertEqual(output.strip(), "odmitnuto chybi_puvodni_text")
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())
        self.assertEqual((self.channel / "index.json").read_bytes(), before)

        code, output, _seen = self._serve(
            {PATH_W47: (200, fixtures.github_file(fixtures.test_bytes(), "prijem/2026-W47.json"))},
            controlled_date=ACCEPTED_DAY,
        )
        self.assertEqual(code, 1)
        self.assertEqual(output.strip(), "odmitnuto testovaci")
        self.assertEqual((self.channel / "index.json").read_bytes(), before)

    def test_actions_ignore_simulated_date(self):
        previous_actions = os.environ.get("GITHUB_ACTIONS")
        previous_today = os.environ.get("NOVINKY_TEST_TODAY")
        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["NOVINKY_TEST_TODAY"] = ACCEPTED_DAY
        try:
            self.assertIsNone(job._test_clock())
            with self.assertRaises(publisher.checks.CheckError) as caught:
                publisher.publish_verified(
                    self.channel,
                    json.dumps(fixtures.finished_week(), ensure_ascii=False).encode("utf-8"),
                    today=job._test_clock(),
                )
            self.assertEqual(str(caught.exception), "datum přehledu je v budoucnosti")
        finally:
            if previous_actions is None:
                os.environ.pop("GITHUB_ACTIONS", None)
            else:
                os.environ["GITHUB_ACTIONS"] = previous_actions
            if previous_today is None:
                os.environ.pop("NOVINKY_TEST_TODAY", None)
            else:
                os.environ["NOVINKY_TEST_TODAY"] = previous_today
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())

    def test_worktree_guard_and_workflow_stay_narrow(self):
        self.assertEqual(job.classify_worktree([]), "beze_zmeny")
        self.assertEqual(
            job.classify_worktree([" M index.json", "?? prehledy/2026-W47.json"]),
            "2026-W47",
        )
        with self.assertRaises(publisher.PublishError):
            job.classify_worktree([" M index.json", " M prehledy/2026-W46.json"])
        with self.assertRaises(publisher.PublishError):
            job.classify_worktree([" M prehledy/2026-W45.json", "?? prehledy/2026-W47.json"])
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('cron: "15 9-17 * * 1"', text)
        self.assertIn("timezone: Europe/Prague", text)
        self.assertIn("environment: prijem-cteni", text)
        self.assertEqual(text.count("secrets.PRIJEM_READ_TOKEN"), 1)
        self.assertEqual(text.count("contents: write"), 1)
        self.assertEqual(text.count("pages: write"), 1)
        self.assertIn("git push --ff-only origin HEAD:main", text)
        self.assertIn("publikovano == 'ano'", text)
        self.assertNotIn("generuj_prehled", text)
        self.assertNotIn("--force", text)
        self.assertNotIn("rebase", text)
        self.assertNotIn("workflow_dispatch", text)
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("generuj_prehled", source)
        self.assertNotIn("github_pat_", source)
        fictional = FICTIONAL.read_text(encoding="utf-8")
        self.assertIn("generuj_prehled.py", fictional)
        self.assertNotIn("zverejni_vzdaleny_prijem", fictional)

    def _serve(
        self,
        routes: dict[str, tuple[int, bytes]],
        controlled_date: str | None = None,
    ) -> tuple[int, str, list[str]]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.routes = routes
        server.expected_token = TOKEN
        server.bad_auth = 0
        server.token_in_path = 0
        server.posts = 0
        server.seen = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            code, output = _run(self.channel, base, controlled_date=controlled_date)
            if server.bad_auth or server.token_in_path or server.posts:
                raise AssertionError("autorizace, cesta nebo zapis nesplnuji ocekavani")
            return code, output, list(server.seen)
        finally:
            server.shutdown()
            server.server_close()


def _run(channel_root: Path, base: str, controlled_date: str | None = None) -> tuple[int, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {
            "PRIJEM_READ_TOKEN",
            "GITHUB_TOKEN",
            "GITHUB_ACTIONS",
            "GITHUB_REF",
            "NOVINKY_TOKEN_TEST_API",
            "NOVINKY_TEST_TODAY",
        }
    }
    env["NOVINKY_TOKEN_TEST_API"] = base
    env["PRIJEM_READ_TOKEN"] = TOKEN
    if controlled_date is not None:
        env["NOVINKY_TEST_TODAY"] = controlled_date
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        cwd=channel_root,
        env=env,
    )
    output = completed.stdout + completed.stderr
    if TOKEN in output or "Bearer " in output:
        raise AssertionError("vystup obsahuje token")
    return completed.returncode, output


if __name__ == "__main__":
    unittest.main()
