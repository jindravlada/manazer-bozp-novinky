#!/usr/bin/env python3
"""Zveřejní jen ověřený přehled v místní kopii kanálu. Na síť se nepřipojuje."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import overeni_prenosu as checks


class PublishError(Exception):
    """Ověřený přehled se do kanálu nezapíše."""


def publish_verified(root: Path, raw: bytes, *, today: date | None = None) -> str:
    """Zapíše další týden. Neplatný, předčasný, opakovaný nebo existující soubor odmítne."""
    channel = _channel(root)
    index_path = _within(channel, channel / "index.json")
    before_index = _regular_file(index_path)
    before_weeks = _week_names(channel)
    clock = _clock(today)
    document = checks.examine(raw, today=clock)
    week_id = document["id"]
    relation = _relation(week_id, channel)
    if relation:
        raise PublishError(relation)
    folder = _within(channel, channel / "prehledy")
    if not folder.is_dir() or folder.is_symlink():
        raise PublishError("kanal")
    target = _within(channel, folder / f"{week_id}.json")
    if target.exists() or target.is_symlink():
        raise PublishError("prepis")
    canonical = _dump(document)
    confirmed = checks.examine(canonical, today=clock)
    if confirmed["id"] != week_id or confirmed["items"] != document["items"]:
        raise PublishError("zapis")
    digest = hashlib.sha256(canonical).hexdigest()
    ref = {
        "id": week_id,
        "issued_at": document["issued_at"],
        "period_start": document["period"]["start"],
        "period_end": document["period"]["end"],
        "file": f"prehledy/{week_id}.json",
        "sha256": digest,
    }
    _create_exclusive(target, canonical)
    try:
        _append_index(index_path, before_index, ref, clock)
    except Exception:
        _remove_created(target, canonical)
        raise
    if _regular_file(target) != canonical or _week_names(channel) != before_weeks + (f"{week_id}.json",):
        _remove_created(target, canonical)
        _restore(index_path, before_index)
        raise PublishError("zapis")
    return f"zverejneno {week_id} polozek {len(document['items'])}"


def _clock(today: date | None) -> date:
    if os.environ.get("GITHUB_ACTIONS") == "true" or today is None:
        return datetime.now(ZoneInfo("Europe/Prague")).date()
    return today


def _relation(week_id: str, root: Path) -> str | None:
    ids = checks.published_ids(root)
    if week_id in ids:
        return "opakovani"
    if ids and week_id < ids[-1]:
        return "starsi"
    if week_id != checks.expected_week(root):
        return "nespravny_tyden"
    return None


def _append_index(path: Path, original: bytes, ref: dict, today: date) -> None:
    index = checks.parse_json(original)
    reviews = index["reviews"]
    if type(reviews) is not list or any(item.get("id") == ref["id"] for item in reviews):
        raise PublishError("opakovani")
    preserved = [dict(item) for item in reviews]
    sequence = index["sequence"]
    if type(sequence) is not int or isinstance(sequence, bool) or sequence >= 100_000:
        raise PublishError("zapis")
    reviews.append(ref)
    index["sequence"] = sequence + 1
    index["issued_at"] = today.isoformat()
    index["expires_at"] = (today + timedelta(days=90)).isoformat()
    if [dict(item) for item in reviews[:-1]] != preserved:
        raise PublishError("zapis")
    updated = _dump(index)
    if checks.parse_json(updated)["reviews"][:-1] != preserved:
        raise PublishError("zapis")
    _replace_regular(path, updated)


def _create_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError:
        raise PublishError("prepis") from None
    try:
        written = os.write(descriptor, data)
        if written != len(data):
            raise PublishError("zapis")
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        _remove_created(path, data)
        raise
    os.close(descriptor)


def _replace_regular(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise PublishError("zapis")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        written = os.write(descriptor, data)
        if written != len(data):
            raise PublishError("zapis")
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    os.replace(temporary, path)


def _remove_created(path: Path, expected: bytes) -> None:
    if path.is_symlink() or not path.is_file():
        return
    if path.read_bytes() != expected:
        return
    path.unlink()


def _restore(path: Path, original: bytes) -> None:
    if path.is_file() and not path.is_symlink() and path.read_bytes() != original:
        _replace_regular(path, original)


def _regular_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PublishError("kanal")
    return path.read_bytes()


def _week_names(root: Path) -> tuple[str, ...]:
    folder = root / "prehledy"
    names = []
    for path in folder.iterdir():
        if path.is_symlink() or not path.is_file():
            raise PublishError("kanal")
        names.append(path.name)
    return tuple(sorted(names))


def _channel(root: Path) -> Path:
    if root.is_symlink() or not root.is_dir():
        raise PublishError("kanal")
    return root.resolve()


def _within(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise PublishError("kanal")
    return resolved


def _dump(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
