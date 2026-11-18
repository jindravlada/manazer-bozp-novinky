#!/usr/bin/env python3
"""Ověří přehled ze soukromého příjmu a zveřejní jen platný další týden.

Chybějící soubor nic nemění. Neplatný, předčasný nebo už uložený týden
se nezapíše. Token se nevypisuje a do adresy se nedává.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date
from pathlib import Path

import overeni_prenosu as checks
import overit_token_prijmu as token_probe
import overit_vzdaleny_prijem as remote
import zverejni_prijem as publisher

WEEK_FILE = re.compile(r"prehledy/(20\d{2}-W(?:0[1-9]|[1-4]\d|5[0-3]))\.json")


def classify_worktree(lines: list[str]) -> str:
    """Povolí jen nový týdenní soubor a upravený seznam. Jinak zápis odmítne."""
    found: dict[str, str] = {}
    for line in lines:
        if line.strip() == "":
            continue
        if len(line) < 4 or " -> " in line:
            raise publisher.PublishError("zapis")
        status = line[:2]
        path = line[3:]
        if "/__pycache__/" in f"/{path}" or path.endswith(".pyc"):
            continue
        if path in found:
            raise publisher.PublishError("zapis")
        found[path] = status
    if not found:
        return "beze_zmeny"
    if set(found) == {"index.json"} or len(found) != 2 or "index.json" not in found:
        raise publisher.PublishError("zapis")
    if found["index.json"] not in {" M", "M "}:
        raise publisher.PublishError("zapis")
    week_path = next(path for path in found if path != "index.json")
    match = WEEK_FILE.fullmatch(week_path)
    if match is None or found[week_path] != "??":
        raise publisher.PublishError("zapis")
    return match.group(1)


def run(root: Path, argv: list[str] | None = None) -> int:
    if argv:
        token_probe.emit("odmitnuto prostor")
        return 1
    token = os.environ.get("PRIJEM_READ_TOKEN", "").strip()
    verdict = token_probe.branch_guard(token)
    if verdict is not None:
        token_probe.emit(verdict)
        return 2 if verdict.startswith("bezpecnostni_selhani") else 1
    if not token:
        token_probe.emit("odmitnuto secret_chybi")
        return 1
    index_before = (root / "index.json").read_bytes()
    weeks_before = remote._week_names(root)
    code = 1
    published = False
    try:
        try:
            clock = _test_clock()
            base = token_probe.resolve_api_base()
            week_id = checks.expected_week(root)
            url = remote.document_url(base, week_id)
        except publisher.PublishError as exc:
            token_probe.emit("odmitnuto " + str(exc))
        except checks.CheckError as exc:
            token_probe.emit("odmitnuto " + remote.map_reason(str(exc)))
        except RuntimeError:
            token_probe.emit("odmitnuto api")
        else:
            if token in url:
                token_probe.emit("odmitnuto api")
            else:
                status, body, oversized = remote.limited_get(url, token)
                if oversized:
                    token_probe.emit("odmitnuto nadmerne")
                elif status == 404:
                    token_probe.emit("ceka")
                    code = 0
                elif status != 200:
                    token_probe.emit(f"odmitnuto {remote.http_code(status)}")
                else:
                    try:
                        raw = remote.decode_github_file(body, f"prijem/{week_id}.json")
                        line = publisher.publish_verified(root, raw, today=clock)
                    except remote.RemoteReject as exc:
                        token_probe.emit(f"odmitnuto {exc.code}")
                    except checks.CheckError as exc:
                        token_probe.emit("odmitnuto " + remote.map_reason(str(exc)))
                    except publisher.PublishError as exc:
                        token_probe.emit("odmitnuto " + str(exc))
                    else:
                        published = True
                        code = 0
                        token_probe.emit(line)
    finally:
        changed = (root / "index.json").read_bytes() != index_before or remote._week_names(root) != weeks_before
        if published and not changed:
            token_probe.emit("odmitnuto zapis_chybi")
            code = 1
        elif changed and not published:
            token_probe.emit("odmitnuto zapis_zakazan")
            code = 1
    return code


def _test_clock() -> date | None:
    """Mimo Actions dovolí simulované datum. V Actions platí skutečné pražské datum."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return None
    override = os.environ.get("NOVINKY_TEST_TODAY", "").strip()
    if not override:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", override):
        raise checks.CheckError("neplatné zkušební datum")
    try:
        return date.fromisoformat(override)
    except ValueError:
        raise checks.CheckError("neplatné zkušební datum") from None


def main() -> int:
    return run(Path(".").resolve(), sys.argv[1:])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        if os.environ.get("GITHUB_ACTIONS") != "true" and os.environ.get("NOVINKY_TOKEN_TEST_API"):
            raise
        token_probe.emit("odmitnuto neocekavane_selhani")
        sys.exit(1)
