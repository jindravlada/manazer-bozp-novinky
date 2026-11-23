"""Lokální zkoušky generátoru fiktivních přehledů. Na GitHub nic neposílá."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import generuj_prehled as generator

ROOT = Path(__file__).resolve().parents[1]
CLIENT_DIR = Path(
    os.environ.get(
        "NOVINKY_CLIENT_DIR",
        "/home/vjm/Data/ProjektyPython/manager-bozp/experiments/novinky_bozp",
    )
)


def copy_channel(destination: Path) -> None:
    destination.mkdir()
    for name in ("index.json", "index.html", ".nojekyll"):
        shutil.copyfile(ROOT / name, destination / name)
    prehledy = destination / "prehledy"
    prehledy.mkdir()
    for source in (ROOT / "prehledy").glob("*.json"):
        shutil.copyfile(source, prehledy / source.name)


class GeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = Path(self.id().replace(".", "_"))
        base = ROOT / ".tmp-test"
        base.mkdir(exist_ok=True)
        self.channel = base / self._temporary.name
        if self.channel.exists():
            shutil.rmtree(self.channel)
        copy_channel(self.channel)
        self.original_w46 = (self.channel / "prehledy" / "2026-W46.json").read_bytes()
        self.original_index = (self.channel / "index.json").read_bytes()

    def tearDown(self) -> None:
        shutil.rmtree(self.channel, ignore_errors=True)

    def test_closed_weeks(self) -> None:
        self.assertEqual(
            generator.latest_completed_week(date(2026, 11, 17))[:1],
            ("2026-W46",),
        )
        self.assertEqual(
            generator.latest_completed_week(date(2026, 11, 22))[0],
            "2026-W46",
        )
        self.assertEqual(
            generator.latest_completed_week(date(2026, 11, 23)),
            ("2026-W47", date(2026, 11, 16), date(2026, 11, 22)),
        )

    def test_existing_week_is_not_duplicated_or_rewritten(self) -> None:
        result = generator.generate(self.channel, today=date(2026, 11, 17))
        self.assertEqual(result, "beze_zmeny")
        self.assertEqual(
            (self.channel / "prehledy" / "2026-W46.json").read_bytes(),
            self.original_w46,
        )
        self.assertEqual((self.channel / "index.json").read_bytes(), self.original_index)
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())

    def test_missing_week_is_not_created(self) -> None:
        with self.assertRaises(generator.ChannelError) as caught:
            generator.generate(self.channel, today=date(2026, 11, 23))
        self.assertIn("vypnuté", str(caught.exception))
        self.assertIn("2026-W47", str(caught.exception))
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())
        self.assertEqual((self.channel / "index.json").read_bytes(), self.original_index)
        self.assertEqual(
            (self.channel / "prehledy" / "2026-W46.json").read_bytes(),
            self.original_w46,
        )
        with self.assertRaises(generator.ChannelError):
            generator._write_week(
                self.channel,
                "2026-W47",
                date(2026, 11, 16),
                date(2026, 11, 22),
                date(2026, 11, 23),
            )
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())
        draft = generator._fictional_review(
            "2026-W47",
            date(2026, 11, 16),
            date(2026, 11, 22),
            date(2026, 11, 23),
        )
        self.assertIn(generator.FICTIONAL_MARK, json.dumps(draft).casefold())

    def test_overwrite_is_disabled_and_keeps_existing_bytes(self) -> None:
        with self.assertRaises(generator.ChannelError):
            generator.generate(self.channel, today=date(2026, 11, 24), overwrite=True)
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "nastroje" / "generuj_prehled.py"),
                "--root",
                str(self.channel),
                "--today",
                "2026-11-24",
                "--overwrite",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("zakázán", completed.stderr)
        self.assertEqual(
            (self.channel / "prehledy" / "2026-W46.json").read_bytes(),
            self.original_w46,
        )
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())

    def test_missed_run_does_not_create_weeks(self) -> None:
        with self.assertRaises(generator.ChannelError) as caught:
            generator.generate(self.channel, today=date(2026, 11, 30))
        self.assertIn("2026-W47,2026-W48", str(caught.exception))
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())
        self.assertFalse((self.channel / "prehledy" / "2026-W48.json").exists())
        self.assertEqual((self.channel / "index.json").read_bytes(), self.original_index)

    def test_sha_mismatch_is_rejected(self) -> None:
        index_path = self.channel / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["reviews"][0]["sha256"] = "0" * 64
        index_path.write_text(json.dumps(index), encoding="utf-8")
        with self.assertRaises(generator.ChannelError) as caught:
            generator.verify_channel(self.channel)
        self.assertIn("otisk", str(caught.exception))

    def test_damaged_json_is_rejected(self) -> None:
        (self.channel / "index.json").write_bytes(b"{")
        with self.assertRaises(generator.ChannelError) as caught:
            generator.verify_channel(self.channel)
        self.assertIn("poškozený JSON", str(caught.exception))

    def test_export_contains_only_public_files(self) -> None:
        (self.channel / "nastroje").mkdir()
        (self.channel / "nastroje" / "tajne.py").write_text("token = 'x'", encoding="utf-8")
        site = self.channel.parent / f"{self.channel.name}-site"
        generator.export_site(self.channel, site)
        names = sorted(path.relative_to(site).as_posix() for path in site.rglob("*") if path.is_file())
        weeks = sorted(path.name for path in (self.channel / "prehledy").glob("*.json"))
        self.assertEqual(
            names,
            [
                ".nojekyll",
                "index.html",
                "index.json",
                *[f"prehledy/{name}" for name in weeks],
            ],
        )
        shutil.rmtree(site)

    def test_real_review_passes_and_invalid_review_is_rejected(self) -> None:
        repo_before = (
            (ROOT / "index.json").read_bytes(),
            (ROOT / "prehledy" / "2026-W45.json").read_bytes(),
            (ROOT / "prehledy" / "2026-W46.json").read_bytes(),
        )
        week_id = "2026-W40"
        start, end = generator._iso_bounds(week_id)
        title = "Hlášení pracovního úrazu"
        text = "Záznam o úrazu se podává ve stanovené lhůtě."
        practical = "Evidence úrazů má odlišit ohlášení a záznam."
        recommendation = "Ověřte postup v oficiálním zdroji SÚIP."
        url = "https://suip.gov.cz/hlaseni-pracovniho-urazu"
        document = {
            "format": generator.FORMAT_REVIEW,
            "format_version": 1,
            "id": week_id,
            "issued_at": (end + timedelta(days=1)).isoformat(),
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "status": "novinky",
            "ai_notice": generator.AI_NOTICE,
            "content_markdown": "\n".join(
                [
                    f"## {title}",
                    "",
                    text,
                    "",
                    practical,
                    "",
                    recommendation,
                    "",
                    url,
                ]
            ),
            "items": [
                {
                    "id": f"{week_id}-001",
                    "category": "ostatni",
                    "title": title,
                    "text": text,
                    "source": {
                        "name": "Státní úřad inspekce práce",
                        "url": url,
                        "published_at": start.isoformat(),
                    },
                    "effective_at": None,
                    "practical_meaning": practical,
                    "recommendation": recommendation,
                }
            ],
        }
        raw = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        self.assertNotIn("fiktiv", raw.decode("utf-8").casefold())
        (self.channel / "prehledy" / f"{week_id}.json").write_bytes(raw)
        index_path = self.channel / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["reviews"].insert(
            0,
            {
                "id": week_id,
                "issued_at": document["issued_at"],
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "file": f"prehledy/{week_id}.json",
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
        )
        index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        generator.verify_channel(self.channel)

        document["items"][0]["source"]["url"] = "http://suip.gov.cz/hlaseni-pracovniho-urazu"
        document["content_markdown"] = document["content_markdown"].replace("https://", "http://", 1)
        bad = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        (self.channel / "prehledy" / f"{week_id}.json").write_bytes(bad)
        index["reviews"][0]["sha256"] = hashlib.sha256(bad).hexdigest()
        index_path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(generator.ChannelError) as caught:
            generator.verify_channel(self.channel)
        self.assertIn("https", str(caught.exception))
        self.assertEqual(
            (
                (ROOT / "index.json").read_bytes(),
                (ROOT / "prehledy" / "2026-W45.json").read_bytes(),
                (ROOT / "prehledy" / "2026-W46.json").read_bytes(),
            ),
            repo_before,
        )

    def test_cli_does_not_overwrite_without_flag(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "nastroje" / "generuj_prehled.py"),
                "--root",
                str(self.channel),
                "--today",
                "2026-11-17",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "beze_zmeny")
        self.assertEqual((self.channel / "index.json").read_bytes(), self.original_index)

    def test_failed_check_stops_before_any_write_command(self) -> None:
        (self.channel / "index.json").write_bytes(b"{")
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "nastroje" / "generuj_prehled.py"),
                "--root",
                str(self.channel),
                "--check-only",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertIn("poškozený JSON", completed.stderr)

    def test_workflow_is_limited_and_does_not_overwrite_on_schedule(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "publikovat-prehled.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertNotIn("cron:", workflow)
        self.assertNotIn("Vytvořit chybějící fiktivní přehled", workflow)
        for line in workflow.splitlines():
            if "generuj_prehled.py" in line:
                self.assertTrue("--check-only" in line or "--export-site" in line, line)
        verified = (ROOT / ".github" / "workflows" / "zverejnit-overeny-prehled.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "15 9,10,11,12,13,14,15,16,17 * * 1"', verified)
        self.assertIn("zverejni_vzdaleny_prijem.py", verified)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("git rebase --abort", workflow)
        self.assertNotIn("--force", workflow)
        self.assertNotIn("overwrite", workflow.lower())
        self.assertLess(
            workflow.index("Ověřit kanál"),
            workflow.index("Zapsat změny do main"),
        )
        self.assertLess(
            workflow.index("Zapsat změny do main"),
            workflow.index("Nahrát web"),
        )
        self.assertIn("needs: publikovat", workflow)
        self.assertIn("if: success()", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotIn("PAT", workflow)
        self.assertNotIn("password", workflow.lower())
        for action in (
            "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803",
            "actions/configure-pages@983d7736d9b0ae728b81ab479565c72886d7745b",
            "actions/upload-pages-artifact@7b1f4a764d45c48632c6b24a0339c27f5614fb0b",
            "actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e",
        ):
            self.assertIn(action, workflow)
        self.assertNotRegex(workflow, r"uses: (?!actions/(checkout|configure-pages|upload-pages-artifact|deploy-pages)@)")
        moment = datetime(2026, 11, 23, 7, 15, tzinfo=timezone.utc)
        self.assertEqual(generator.prague_today(moment), date(2026, 11, 23))


if __name__ == "__main__":
    unittest.main()
