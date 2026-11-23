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
TEST_WORKFLOW = ROOT / ".github" / "workflows" / "overit-zkousku-prijmu.yml"
ACCEPTED_DAY = date(2026, 11, 23)
REAL_DAY = date(2026, 11, 18)
TOKEN = "github_pat_lokalni_zkouska"
CANARY = "SOUKROMY_TEXT_NEVYPISOVAT"
PUBLISHED = {
    "index.json": "658f06388c536db8afd004742a1be6dafb75592b097e468aa2c81bb56127218b",
    "prehledy/2026-W41.json": "275ddeaa25fbfddda2ef0bf3635845d7f3f484a89df2220cc1ce0fc88130ae42",
    "prehledy/2026-W42.json": "6d53f56562db5fbcb85e07608872f4bb941f669a43c10178e74d730267193c62",
    "prehledy/2026-W43.json": "40b91559f621f8ade5491da0e1fc6e7a0c9441b8af11b643066437b087219592",
    "prehledy/2026-W44.json": "c11c4ef70ec7b7a43c1d661b683a0627d7e36be3a057254f6a235ad18fc2e7dc",
    "prehledy/2026-W45.json": "7923ab0dca5a586bf6e161b80b79810ec624cec0b8dfec0c39f318976deee480",
    "prehledy/2026-W46.json": "03dfd0252b95b84795a56bd02388283f6f97d6769965fb001faf35b82ebd34ac",
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


def test_markdown() -> str:
    return (
        "### 1. Zkušební fiktivní položka\n\n"
        "- **Stav:** zkušební položka bez odborného významu\n"
        "- **Zveřejněno:** 18. 11. 2026.\n"
        "- **Účinnost:** není relevantní.\n"
        "- **Zdroj:** [Zkušební zdroj](https://invalid.test/novinky/fiktivni-zkouska-nepublikovat).\n\n"
        "Toto není právní informace ani zpráva o skutečném předpisu. Slouží jen ke zkoušce přenosu.\n\n"
        "**Praktický význam:** Zkušební význam bez dopadu na praxi.\n\n"
        "**Manažer BOZP – návrh k posouzení:** Nezveřejňovat. FIKTIVNI_ZKOUSKA_NEPUBLIKOVAT"
    )


def test_payload(**changes: object) -> dict:
    document = {
        "experiment": "chatgpt-automation-transfer",
        "production_trusted": False,
        "id": "2026-W47",
        "source_task": "FIKTIVNI_ZKOUSKA_NEPUBLIKOVAT",
        "source_run_date": "2026-11-23",
        "period_start": "2026-11-16",
        "period_end": "2026-11-22",
        "distribution_test": True,
        "content_markdown": test_markdown(),
    }
    document.update(changes)
    return document


def test_bytes() -> bytes:
    return json.dumps(test_payload(), ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def finished_week(**changes: object) -> dict:
    title = "Zkušební položka"
    text = "Zkušební text položky. Není to odborné zjištění."
    practical = "zkušební význam položky."
    recommendation = "zkušební doporučení položky."
    url = "https://invalid.test/novinky/1"
    markdown = "\n\n".join((title, text, practical, recommendation, url))
    document = {
        "format": "manazer-bozp-novinky-prehled",
        "format_version": 1,
        "id": "2026-W47",
        "issued_at": "2026-11-23",
        "period": {"start": "2026-11-16", "end": "2026-11-22"},
        "status": "novinky",
        "ai_notice": checks.AI_NOTICE,
        "content_markdown": markdown,
        "items": [
            {
                "id": "2026-W47-001",
                "category": "ostatni",
                "title": title,
                "text": text,
                "source": {
                    "name": "Zkušební zdroj",
                    "url": url,
                    "published_at": "2026-11-18",
                },
                "effective_at": None,
                "practical_meaning": practical,
                "recommendation": recommendation,
            }
        ],
    }
    document.update(changes)
    return document


def github_file(raw: bytes, path: str, claimed_sha: str = "sha-ze-serveru") -> bytes:
    return json.dumps(
        {
            "type": "file",
            "encoding": "base64",
            "size": len(raw),
            "content": base64.b64encode(raw).decode("ascii"),
            "path": path,
            "sha": claimed_sha,
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
        code, line = remote.check_bytes(
            self.channel, dump(envelope("2026-W47")), "2026-W47", today=ACCEPTED_DAY
        )
        self.assertEqual((code, line), (0, "overeno 2026-W47 polozek 1 nepublikovano"))
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())

    def test_invalid_structure_danger_wrong_week_and_size(self):
        missing = envelope("2026-W47")
        del missing["id"]
        self.assertEqual(
            remote.check_bytes(self.channel, dump(missing), "2026-W47", today=ACCEPTED_DAY)[1],
            "odmitnuto chybejici_nebo_navic",
        )
        dangerous = envelope("2026-W47")
        dangerous["content_markdown"] += "\n<script>alert(1)</script>"
        self.assertEqual(
            remote.check_bytes(self.channel, dump(dangerous), "2026-W47", today=ACCEPTED_DAY)[1],
            "odmitnuto nebezpecny_obsah",
        )
        self.assertEqual(
            remote.check_bytes(self.channel, dump(envelope("2026-W48")), "2026-W47", today=date(2026, 11, 30))[1],
            "odmitnuto nespravny_tyden",
        )
        self.assertEqual(
            remote.check_bytes(self.channel, b"{" + b"x" * checks.MAX_DOCUMENT, "2026-W47")[1],
            "odmitnuto nadmerne",
        )

    def test_finished_week_requires_original_text_and_known_category(self):
        code, line = remote.check_bytes(
            self.channel, dump(finished_week()), "2026-W47", today=ACCEPTED_DAY
        )
        self.assertEqual((code, line), (0, "overeno 2026-W47 polozek 1 nepublikovano"))
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())
        missing = finished_week()
        del missing["content_markdown"]
        self.assertEqual(
            remote.check_bytes(self.channel, dump(missing), "2026-W47", today=ACCEPTED_DAY)[1],
            "odmitnuto chybi_puvodni_text",
        )
        unknown = finished_week()
        unknown["items"][0]["category"] = "neznama_kategorie"
        self.assertEqual(
            remote.check_bytes(self.channel, dump(unknown), "2026-W47", today=ACCEPTED_DAY)[1],
            "odmitnuto kategorie",
        )
        shifted = finished_week()
        shifted["period"] = {"start": "2026-11-17", "end": "2026-11-22"}
        self.assertEqual(
            remote.check_bytes(self.channel, dump(shifted), "2026-W47", today=ACCEPTED_DAY)[1],
            "odmitnuto nesoulad_obdobi",
        )
        self.assertEqual(
            remote.check_bytes(self.channel, dump(finished_week()), "2026-W47", today=REAL_DAY)[1],
            "odmitnuto budouci_datum",
        )
        drifted = finished_week()
        drifted["items"][0]["title"] = "Jiný název"
        self.assertEqual(
            remote.check_bytes(self.channel, dump(drifted), "2026-W47", today=ACCEPTED_DAY)[1],
            "odmitnuto nesoulad_textu",
        )

    def test_production_conversion_rejects_every_test_flag(self):
        for value in (True, False, "true", 1):
            with self.assertRaises(checks.CheckError) as caught:
                checks.convert_transfer(test_payload(distribution_test=value))
            self.assertEqual(str(caught.exception), "testovaci prehled nelze publikovat")
        code, line = remote.check_bytes(self.channel, test_bytes(), "2026-W47", today=ACCEPTED_DAY)
        self.assertEqual(line, "odmitnuto testovaci")
        self.assertEqual(code, 1)
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())

    def test_distribution_test_is_not_publishable_and_honors_clock(self):
        self.assertEqual(hashlib.sha256(test_bytes()).hexdigest(), remote.TEST_SHA256)
        verdict = checks.assess_distribution_test(test_bytes(), today=ACCEPTED_DAY)
        self.assertFalse(verdict.publishable)
        self.assertEqual(verdict.week_id, "2026-W47")
        self.assertEqual(verdict.item_count, 1)
        with self.assertRaises(checks.CheckError) as caught:
            checks.ReadVerdict("2026-W47", 1, True)
        self.assertEqual(str(caught.exception), "testovaci prehled nelze publikovat")
        with self.assertRaises(checks.CheckError) as future:
            checks.assess_distribution_test(test_bytes(), today=REAL_DAY)
        self.assertEqual(str(future.exception), "datum přehledu je v budoucnosti")
        code, line = remote.check_test_bytes(self.channel, test_bytes(), today=ACCEPTED_DAY)
        self.assertEqual((code, line), (0, "overeno 2026-W47 polozek 1 nepublikovano"))
        self.assertEqual(
            remote.check_test_bytes(self.channel, test_bytes(), today=REAL_DAY)[1],
            "odmitnuto budouci_datum",
        )
        mutated = test_bytes().replace("Zkušební".encode(), b"CANARYTEXT")
        self.assertEqual(remote.check_test_bytes(self.channel, mutated, today=ACCEPTED_DAY)[1], "odmitnuto neocekavany_obsah")
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())

    def test_spaces_stay_fixed_and_actions_ignore_clock(self):
        base = "http://127.0.0.1:9"
        production = remote.document_url(base, "2026-W47")
        trial = remote.test_document_url(base)
        self.assertTrue(production.endswith("/contents/prijem/2026-W47.json?ref=main"))
        self.assertNotIn("zkouska", production)
        self.assertTrue(trial.endswith("/contents/zkouska/2026-W47.json?ref=zkouska-cteni"))
        self.assertEqual(remote.selected_space([]), "prijem")
        self.assertEqual(remote.selected_space(["--zkouska"]), "zkouska")
        with self.assertRaises(remote.RemoteReject):
            remote.selected_space(["--zkouska", "--publikovat"])
        with self.assertRaises(remote.RemoteReject):
            remote.selected_space(["--ref", "zkouska-cteni"])
        self.assertNotIn("potvrdit", SCRIPT.read_text(encoding="utf-8"))
        path = "/repos/jindravlada/manazer-bozp-prijem/contents/zkouska/2026-W47.json?ref=zkouska-cteni"
        body = github_file(test_bytes(), "zkouska/2026-W47.json", claimed_sha="jiný-otisk-ze-serveru")
        code, output, seen = _serve({path: (200, body)}, argv=["--zkouska"], controlled_date="2026-11-23")
        self.assertEqual(output.strip(), "overeno 2026-W47 polozek 1 nepublikovano")
        self.assertEqual(seen, [path])
        self.assertNotIn("FIKTIVNI_ZKOUSKA_NEPUBLIKOVAT", output)
        self.assertNotIn("jiný-otisk-ze-serveru", output)
        wrong = github_file(test_bytes(), "prijem/2026-W47.json")
        code, output, _seen = _serve({path: (200, wrong)}, argv=["--zkouska"], controlled_date="2026-11-23")
        self.assertEqual(output.strip(), "odmitnuto cesta")
        altered = github_file(test_bytes() + b" ", "zkouska/2026-W47.json", claimed_sha=remote.TEST_SHA256)
        code, output, _seen = _serve({path: (200, altered)}, argv=["--zkouska"], controlled_date="2026-11-23")
        self.assertEqual(output.strip(), "odmitnuto neocekavany_obsah")
        self.assertNotIn("Zkušební", output)
        previous_actions = os.environ.get("GITHUB_ACTIONS")
        previous_today = os.environ.get("NOVINKY_TEST_TODAY")
        os.environ["GITHUB_ACTIONS"] = "true"
        os.environ["NOVINKY_TEST_TODAY"] = "2026-11-23"
        try:
            self.assertEqual(checks.processing_date(), checks._prague_today())
            tomorrow = (checks._prague_today() + timedelta(days=1)).isoformat()
            future = json.dumps(
                test_payload(source_run_date=tomorrow),
                ensure_ascii=False,
            ).encode("utf-8")
            with self.assertRaises(checks.CheckError) as caught:
                checks.assess_distribution_test(future, today=ACCEPTED_DAY)
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
        os.environ["NOVINKY_TEST_TODAY"] = "1999-01-01"
        try:
            self.assertEqual(checks.processing_date(), date(1999, 1, 1))
        finally:
            os.environ.pop("NOVINKY_TEST_TODAY", None)

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
        code, output, _seen = _serve(
            {path: (200, github_file(dump(envelope("2026-W47")), f"prijem/{'2026-W47'}.json"))},
            controlled_date="2026-11-23",
        )
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
        self.assertNotIn("--zkouska", text)
        trial = TEST_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", trial)
        self.assertIn("environment: prijem-cteni", trial)
        self.assertEqual(trial.count("secrets.PRIJEM_READ_TOKEN"), 1)
        self.assertIn("contents: read", trial)
        self.assertIn("python3 nastroje/overit_vzdaleny_prijem.py --zkouska", trial)
        self.assertNotIn("contents: write", trial)
        self.assertNotIn("schedule:", trial)
        self.assertNotIn("pull_request", trial)
        self.assertNotIn("workflow_dispatch:\n    inputs:", trial)
        self.assertNotIn("sha:", trial)
        self.assertNotIn(remote.TEST_SHA256, trial)


def _leaks(output: str) -> bool:
    return TOKEN in output or "Bearer " in output


def _run(
    channel_root: Path,
    base: str,
    drop_token: bool = False,
    argv: list[str] | None = None,
    controlled_date: str | None = None,
    actions: bool = False,
) -> tuple[int, str]:
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
            "PRIJEM_PROSTOR",
        }
    }
    env["NOVINKY_TOKEN_TEST_API"] = base
    if not drop_token:
        env["PRIJEM_READ_TOKEN"] = TOKEN
    if controlled_date is not None:
        env["NOVINKY_TEST_TODAY"] = controlled_date
    if actions:
        env["GITHUB_ACTIONS"] = "true"
        env["GITHUB_REF"] = "refs/heads/main"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *(argv or [])],
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


def _serve(
    routes: dict[str, tuple[int, bytes]],
    argv: list[str] | None = None,
    controlled_date: str | None = None,
    actions: bool = False,
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
    channel_root = ROOT / ".tmp-test" / "http-oddelene"
    if channel_root.exists():
        shutil.rmtree(channel_root)
    copy_public(channel_root)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        code, output = _run(
            channel_root,
            base,
            argv=argv,
            controlled_date=controlled_date,
            actions=actions,
        )
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
