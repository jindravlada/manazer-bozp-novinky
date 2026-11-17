"""Lokální zkoušky generátoru fiktivních přehledů. Na GitHub nic neposílá."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from datetime import date, datetime, timezone
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

    def test_new_fictional_week_is_added_once_and_hashes_match(self) -> None:
        first = generator.generate(self.channel, today=date(2026, 11, 23))
        self.assertEqual(first, "zapsano 2026-W47")
        created = self.channel / "prehledy" / "2026-W47.json"
        raw = created.read_bytes()
        payload = json.loads(raw)
        self.assertTrue(payload["items"][0]["title"].startswith("Fiktivní testovací záznam"))
        self.assertEqual(payload["issued_at"], "2026-11-23")
        self.assertIn("smyšlený", payload["items"][0]["text"])
        self.assertEqual(payload["ai_notice"], generator.AI_NOTICE)
        self.assertIn("example.com", payload["items"][0]["source"]["url"])
        index = json.loads((self.channel / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["sequence"], 2)
        entry = next(item for item in index["reviews"] if item["id"] == "2026-W47")
        self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(
            (self.channel / "prehledy" / "2026-W46.json").read_bytes(),
            self.original_w46,
        )
        second = generator.generate(self.channel, today=date(2026, 11, 23))
        self.assertEqual(second, "beze_zmeny")
        self.assertEqual(created.read_bytes(), raw)
        self.assertEqual(json.loads((self.channel / "index.json").read_text())["sequence"], 2)

    def test_overwrite_is_disabled_and_keeps_existing_bytes(self) -> None:
        generator.generate(self.channel, today=date(2026, 11, 23))
        week_path = self.channel / "prehledy" / "2026-W47.json"
        before = week_path.read_bytes()
        with self.assertRaises(generator.ChannelError):
            generator.generate(self.channel, today=date(2026, 11, 24), overwrite=True)
        self.assertEqual(week_path.read_bytes(), before)
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
        self.assertEqual(week_path.read_bytes(), before)

    def test_missed_run_catches_up_without_skipping_a_week(self) -> None:
        result = generator.generate(self.channel, today=date(2026, 11, 30))
        self.assertEqual(result, "zapsano 2026-W47,2026-W48")
        week_47 = json.loads((self.channel / "prehledy" / "2026-W47.json").read_text())
        week_48 = json.loads((self.channel / "prehledy" / "2026-W48.json").read_text())
        self.assertEqual(week_47["issued_at"], "2026-11-23")
        self.assertEqual(week_48["issued_at"], "2026-11-30")
        index = json.loads((self.channel / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["sequence"], 3)
        self.assertEqual(index["issued_at"], "2026-11-30")

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
        self.assertEqual(
            names,
            [
                ".nojekyll",
                "index.html",
                "index.json",
                "prehledy/2026-W45.json",
                "prehledy/2026-W46.json",
            ],
        )
        shutil.rmtree(site)

    def test_generated_week_passes_existing_client(self) -> None:
        sys.path.insert(0, str(CLIENT_DIR))
        import novinky_client as client

        generator.generate(self.channel, today=date(2026, 11, 23))
        index_raw = (self.channel / "index.json").read_bytes()
        index = client.parse_index_bytes(index_raw)
        self.assertEqual(index.sequence, 2)
        self.assertIn("2026-W47", [item.id for item in index.reviews])
        week_raw = (self.channel / "prehledy" / "2026-W47.json").read_bytes()
        review = client.parse_review_bytes(week_raw)
        self.assertEqual(review.id, "2026-W47")
        self.assertEqual(review.status, "novinky")
        self.assertEqual(client.AI_NOTICE, generator.AI_NOTICE)
        ref = next(item for item in index.reviews if item.id == "2026-W47")
        self.assertEqual(hashlib.sha256(week_raw).hexdigest(), ref.sha256)

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
        self.assertIn('cron: "15 8 * * 1"', workflow)
        self.assertIn("timezone: Europe/Prague", workflow)
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
