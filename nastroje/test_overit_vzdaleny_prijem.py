"""Zkoušky odděleného ověření příjmu. Na GitHub nic neposílají."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import overeni_prenosu as checks
import overit_vzdaleny_prijem as remote

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "nastroje" / "overit_vzdaleny_prijem.py"
VALIDATOR = ROOT / "nastroje" / "overeni_prenosu.py"
WORKFLOW = ROOT / ".github" / "workflows" / "overit-vzdaleny-prijem.yml"
TOKEN = "github_pat_lokalni_zkouska"
CANARY = "SOUKROMY_TEXT_NEVYPISOVAT"
PUBLISHED = {
    "index.json": "665aa2bf2bd0c072fb07d33e6de680bb721f62964613486b64beff1ea0505839",
    "prehledy/2026-W45.json": "e103b0fccffd4996936696b34737032d6476fa39fb0066eb8117ebab36216b24",
    "prehledy/2026-W46.json": "ee09bef9d61ed9fc1f2afd23f07d1913bbe2656baf435b370294f601057204b3",
}


def bounds(week_id: str) -> tuple[date, date]:
    year = int(week_id[:4])
    week = int(week_id[6:])
    return date.fromisocalendar(year, week, 1), date.fromisocalendar(year, week, 7)


def envelope(week_id: str, **changes: object) -> dict:
    start, end = bounds(week_id)
    published = start + timedelta(days=2)
    markdown = (
        "### 1. Zkušební položka\n\n"
        "- **Stav:** nezávazná metodická informace ke zkušební struktuře\n"
        f"- **Zveřejněno:** {published.day}. {published.month}. {published.year}.\n"
        "- **Účinnost:** není relevantní.\n"
        "- **Zdroj:** [Zkušební zdroj](https://invalid.test/novinky/1).\n\n"
        "Zkušební text položky. Není to odborné zjištění.\n\n"
        "**Praktický význam:** zkušební význam položky.\n\n"
        "**Manažer BOZP – návrh k posouzení:** zkušební doporučení položky."
    )
    document = {
        "experiment": "chatgpt-automation-transfer",
        "production_trusted": False,
        "id": week_id,
        "source_task": "zkusebni kontrola struktury",
        "source_run_date": (end + timedelta(days=1)).isoformat(),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "content_markdown": markdown,
    }
    document.update(changes)
    return document


def dump(document: dict) -> bytes:
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def github_file(raw: bytes, week_id: str) -> bytes:
    return json.dumps(
        {
            "type": "file",
            "encoding": "base64",
            "size": len(raw),
            "content": base64.b64encode(raw).decode("ascii"),
            "path": f"prijem/{week_id}.json",
            "url": "https://api.github.com/ignored",
        }
    ).encode("utf-8")


def copy_public(destination: Path) -> None:
    destination.mkdir()
    for name in ("index.json", "index.html", ".nojekyll"):
        shutil.copyfile(ROOT / name, destination / name)
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
        if status in {301, 302, 303, 307, 308}:
            self.send_response(status)
            self.send_header("Location", "http://127.0.0.1:9/presmerovano")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
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


class SeparatedIntakeTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".tmp-test"
        base.mkdir(exist_ok=True)
        self.channel = base / self.id().replace(".", "_")
        if self.channel.exists():
            shutil.rmtree(self.channel)
        copy_public(self.channel)
        self.before = (self.channel / "index.json").read_bytes()
        self.weeks = remote._week_names(self.channel)

    def tearDown(self):
        self.assertEqual((self.channel / "index.json").read_bytes(), self.before)
        self.assertEqual(remote._week_names(self.channel), self.weeks)
        shutil.rmtree(self.channel, ignore_errors=True)

    def test_modules_do_not_import_generator_or_intake_writer(self):
        self.assertNotIn("generuj_prehled", sys.modules)
        self.assertNotIn("prijmi_prehled", sys.modules)
        for path in (SCRIPT, VALIDATOR):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("generuj_prehled", text)
            self.assertNotIn("prijmi_prehled", text)
            self.assertNotIn("write_bytes", text)
            self.assertNotIn("fiktiv", text)

    def test_published_channel_is_unchanged(self):
        for name, digest in PUBLISHED.items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(checks.expected_week(self.channel), "2026-W47")

    def test_valid_review_is_not_published(self):
        code, line = remote.check_bytes(self.channel, dump(envelope("2026-W47")), "2026-W47")
        self.assertEqual((code, line), (0, "overeno 2026-W47 polozek 1 nepublikovano"))
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())

    def test_invalid_structure_danger_wrong_week_and_size(self):
        missing = envelope("2026-W47")
        del missing["id"]
        self.assertEqual(remote.check_bytes(self.channel, dump(missing), "2026-W47")[1], "odmitnuto chybejici_nebo_navic")
        dangerous = envelope("2026-W47")
        dangerous["content_markdown"] += "\n<script>alert(1)</script>"
        self.assertEqual(remote.check_bytes(self.channel, dump(dangerous), "2026-W47")[1], "odmitnuto nebezpecny_obsah")
        self.assertEqual(remote.check_bytes(self.channel, dump(envelope("2026-W48")), "2026-W47")[1], "odmitnuto nespravny_tyden")
        self.assertEqual(
            remote.check_bytes(self.channel, b"{" + b"x" * checks.MAX_DOCUMENT, "2026-W47")[1],
            "odmitnuto nadmerne",
        )

    def test_missing_file_redirect_invalid_and_absent_token(self):
        path = "/repos/jindravlada/manazer-bozp-prijem/contents/prijem/2026-W47.json?ref=main"
        code, output, seen = _serve({path: (404, CANARY.encode())})
        self.assertEqual(code, 1)
        self.assertEqual(output.strip(), "odmitnuto http_404")
        self.assertNotIn(CANARY, output)
        self.assertEqual(seen, [path])

        code, output, seen = _serve({path: (302, b"")})
        self.assertEqual(output.strip(), "odmitnuto presmerovani")
        self.assertEqual(seen, [path])
        self.assertNotIn("presmerovano", output)

        code, output, _seen = _serve({path: (401, b"github_pat_v_odpovedi")})
        self.assertEqual(output.strip(), "odmitnuto http_401")
        self.assertNotIn("github_pat_v_odpovedi", output)

        code, output = _run(self.channel, "http://127.0.0.1:9", drop_token=True)
        self.assertEqual(code, 1)
        self.assertEqual(output.strip(), "odmitnuto secret_chybi")

    def test_valid_remote_file_and_oversize_do_not_leak(self):
        path = "/repos/jindravlada/manazer-bozp-prijem/contents/prijem/2026-W47.json?ref=main"
        code, output, _seen = _serve({path: (200, github_file(dump(envelope("2026-W47")), "2026-W47"))})
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "overeno 2026-W47 polozek 1 nepublikovano")
        self.assertNotIn("Zkušební text", output)
        payload = b"CANARYOVERSIZE" + b"Q" * remote.HTTP_LIMIT
        code, output, _seen = _serve({path: (200, payload)})
        self.assertEqual(output.strip(), "odmitnuto nadmerne")
        self.assertNotIn("CANARYOVERSIZE", output)

    def test_workflow_is_read_only(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("environment: prijem-cteni", text)
        self.assertEqual(text.count("secrets.PRIJEM_READ_TOKEN"), 1)
        self.assertIn("contents: read", text)
        self.assertNotIn("contents: write", text)
        self.assertNotIn("schedule:", text)
        self.assertNotIn("pull_request", text)
        self.assertNotIn("pages:", text)


def _leaks(output: str) -> bool:
    return TOKEN in output or "Bearer " in output


def _run(channel_root: Path, base: str, drop_token: bool = False) -> tuple[int, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PRIJEM_READ_TOKEN", "GITHUB_TOKEN", "GITHUB_ACTIONS", "GITHUB_REF", "NOVINKY_TOKEN_TEST_API"}
    }
    env["NOVINKY_TOKEN_TEST_API"] = base
    if not drop_token:
        env["PRIJEM_READ_TOKEN"] = TOKEN
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        cwd=channel_root,
        env=env,
    )
    output = completed.stdout + completed.stderr
    if _leaks(output):
        raise AssertionError("vystup obsahuje token")
    return completed.returncode, output


def _serve(routes: dict[str, tuple[int, bytes]]) -> tuple[int, str, list[str]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.routes = routes
    server.expected_token = TOKEN
    server.bad_auth = 0
    server.token_in_path = 0
    server.posts = 0
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    channel_root = ROOT / ".tmp-test" / "http-oddelene"
    if channel_root.exists():
        shutil.rmtree(channel_root)
    copy_public(channel_root)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        code, output = _run(channel_root, base)
        if server.bad_auth or server.token_in_path or server.posts:
            raise AssertionError("autorizace, cesta nebo zapis nesplnuji ocekavani")
        if (channel_root / "prehledy" / "2026-W47.json").exists():
            raise AssertionError("vznikl verejny prehled")
        return code, output, list(server.seen)
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(channel_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
