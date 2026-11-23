"""Zkoušky místního zveřejnění. Veřejný kanál ani GitHub nemění."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import overeni_prenosu as checks
import test_overit_vzdaleny_prijem as remote_tests
import zverejni_prijem as publisher

ROOT = Path(__file__).resolve().parents[1]
ACCEPTED_DAY = date(2026, 11, 23)
REAL_DAY = date(2026, 11, 18)
PUBLISHED = remote_tests.PUBLISHED


def copy_channel(destination: Path) -> None:
    destination.mkdir()
    shutil.copyfile(ROOT / "index.json", destination / "index.json")
    folder = destination / "prehledy"
    folder.mkdir()
    for source in (ROOT / "prehledy").glob("*.json"):
        shutil.copyfile(source, folder / source.name)


class PublishTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".tmp-test"
        base.mkdir(exist_ok=True)
        self.channel = base / self.id().replace(".", "_")
        if self.channel.exists():
            shutil.rmtree(self.channel)
        copy_channel(self.channel)
        self.before_index = (self.channel / "index.json").read_bytes()
        self.before_weeks = {
            path.name: path.read_bytes() for path in (self.channel / "prehledy").glob("*.json")
        }

    def tearDown(self):
        shutil.rmtree(self.channel, ignore_errors=True)
        for name, digest in PUBLISHED.items():
            self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest)

    def test_verified_week_is_appended_once(self):
        raw = json.dumps(remote_tests.finished_week(), ensure_ascii=False).encode("utf-8")
        line = publisher.publish_verified(self.channel, raw, today=ACCEPTED_DAY)
        self.assertEqual(line, "zverejneno 2026-W47 polozek 1")
        stored = self.channel / "prehledy" / "2026-W47.json"
        self.assertTrue(stored.is_file())
        self.assertNotIn(b"distribution_test", stored.read_bytes())
        index = json.loads((self.channel / "index.json").read_text(encoding="utf-8"))
        before = json.loads(self.before_index)
        self.assertEqual(index["sequence"], before["sequence"] + 1)
        self.assertEqual(index["issued_at"], "2026-11-23")
        self.assertEqual(index["expires_at"], "2027-02-21")
        self.assertEqual(
            [item["id"] for item in index["reviews"]],
            [item["id"] for item in before["reviews"]] + ["2026-W47"],
        )
        self.assertEqual(index["reviews"][:-1], before["reviews"])
        self.assertEqual(index["reviews"][-1]["sha256"], hashlib.sha256(stored.read_bytes()).hexdigest())
        for name, content in self.before_weeks.items():
            self.assertEqual((self.channel / "prehledy" / name).read_bytes(), content)
        confirmed = checks.examine(stored.read_bytes(), today=ACCEPTED_DAY)
        self.assertEqual(confirmed["id"], "2026-W47")
        published = stored.read_bytes()
        index_after = (self.channel / "index.json").read_bytes()
        with self.assertRaises(publisher.PublishError) as caught:
            publisher.publish_verified(self.channel, raw, today=ACCEPTED_DAY)
        self.assertEqual(str(caught.exception), "opakovani")
        self.assertEqual(stored.read_bytes(), published)
        self.assertEqual((self.channel / "index.json").read_bytes(), index_after)

    def test_invalid_early_and_existing_files_stay_unpublished(self):
        valid = json.dumps(remote_tests.finished_week(), ensure_ascii=False).encode("utf-8")
        with self.assertRaises(checks.CheckError) as future:
            publisher.publish_verified(self.channel, valid, today=REAL_DAY)
        self.assertEqual(str(future.exception), "datum přehledu je v budoucnosti")

        missing = remote_tests.finished_week()
        del missing["content_markdown"]
        with self.assertRaises(checks.CheckError):
            publisher.publish_verified(
                self.channel,
                json.dumps(missing, ensure_ascii=False).encode("utf-8"),
                today=ACCEPTED_DAY,
            )
        unknown = remote_tests.finished_week()
        unknown["items"][0]["category"] = "neznama_kategorie"
        with self.assertRaises(checks.CheckError):
            publisher.publish_verified(
                self.channel,
                json.dumps(unknown, ensure_ascii=False).encode("utf-8"),
                today=ACCEPTED_DAY,
            )
        with self.assertRaises(checks.CheckError):
            publisher.publish_verified(self.channel, remote_tests.test_bytes(), today=ACCEPTED_DAY)

        later = remote_tests.finished_week()
        later["id"] = "2026-W48"
        later["issued_at"] = "2026-11-30"
        later["period"] = {"start": "2026-11-23", "end": "2026-11-29"}
        later["items"][0]["id"] = "2026-W48-001"
        later["items"][0]["source"]["published_at"] = "2026-11-24"
        with self.assertRaises(publisher.PublishError) as wrong:
            publisher.publish_verified(
                self.channel,
                json.dumps(later, ensure_ascii=False).encode("utf-8"),
                today=date(2026, 11, 30),
            )
        self.assertEqual(str(wrong.exception), "nespravny_tyden")

        target = self.channel / "prehledy" / "2026-W47.json"
        target.write_bytes(b"{}\n")
        with self.assertRaises(publisher.PublishError) as existing:
            publisher.publish_verified(self.channel, valid, today=ACCEPTED_DAY)
        self.assertEqual(str(existing.exception), "prepis")
        self.assertEqual(target.read_bytes(), b"{}\n")
        self.assertEqual((self.channel / "index.json").read_bytes(), self.before_index)
        self.assertFalse(any(self.channel.rglob("*.tmp")))

    def test_actions_clock_and_transfer_envelope_keep_guards(self):
        tomorrow = (checks._prague_today() + timedelta(days=1)).isoformat()
        raw = json.dumps(
            remote_tests.finished_week(issued_at=tomorrow),
            ensure_ascii=False,
        ).encode("utf-8")
        previous = os.environ.get("GITHUB_ACTIONS")
        os.environ["GITHUB_ACTIONS"] = "true"
        try:
            with self.assertRaises(checks.CheckError) as caught:
                publisher.publish_verified(self.channel, raw, today=ACCEPTED_DAY)
            self.assertEqual(str(caught.exception), "datum přehledu je v budoucnosti")
        finally:
            if previous is None:
                os.environ.pop("GITHUB_ACTIONS", None)
            else:
                os.environ["GITHUB_ACTIONS"] = previous
        self.assertEqual((self.channel / "index.json").read_bytes(), self.before_index)
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())

        fresh = self.channel / "obalka"
        copy_channel(fresh)
        line = publisher.publish_verified(fresh, remote_tests.dump(remote_tests.envelope("2026-W47")), today=ACCEPTED_DAY)
        self.assertEqual(line, "zverejneno 2026-W47 polozek 1")
        text = (publisher.__file__)
        source = Path(text).read_text(encoding="utf-8")
        self.assertNotIn("generuj_prehled", source)
        self.assertNotIn("api.github.com", source)
        self.assertNotIn("PRIJEM_READ_TOKEN", source)
        shutil.rmtree(fresh)

    def test_valid_envelope_uses_closed_week_only(self):
        raw = remote_tests.dump(remote_tests.envelope("2026-W47"))
        with self.assertRaises(checks.CheckError):
            publisher.publish_verified(self.channel, raw, today=REAL_DAY)
        self.assertEqual(checks.expected_week(self.channel), "2026-W47")
        self.assertFalse((self.channel / "prehledy" / "2026-W47.json").exists())


if __name__ == "__main__":
    unittest.main()
