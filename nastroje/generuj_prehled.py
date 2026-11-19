#!/usr/bin/env python3
"""Ověří kanál novinek a drží fiktivní generátor vypnutý.

Ověření platného přehledu nevyžaduje slovo „fiktivní“. Fiktivní zkušební
zápis je oddělený a sám se nespustí. Existující týden se nepřepisuje.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

FORMAT_INDEX = "manazer-bozp-novinky-index"
FORMAT_REVIEW = "manazer-bozp-novinky-prehled"
FORMAT_VERSION = 1
FEED_ID = "manazer-bozp-novinky"
AI_NOTICE = (
    "Tento přehled byl vytvořen pomocí umělé inteligence. "
    "Může obsahovat nepřesnosti nebo opomenutí. "
    "Před použitím informací v praxi ověřte jejich správnost a aktuálnost "
    "v oficiálních zdrojích."
)
FICTIONAL_MARK = "fiktiv"
CATEGORIES = frozenset(
    {
        "pravni_predpis",
        "metodicky_pokyn",
        "kontrolni_cinnost",
        "technicka_norma",
        "evropsky_predpis",
        "ostatni",
    }
)
WEEK_ID_PATTERN = re.compile(r"20\d{2}-W(?:0[1-9]|[1-4]\d|5[0-3])")
HOST_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
)
PRAGUE = ZoneInfo("Europe/Prague")
MAX_VALIDITY_DAYS = 120
MAX_ISSUE_LAG_DAYS = 14
PUBLIC_FILES = (".nojekyll", "index.html", "index.json")


class ChannelError(Exception):
    """Kanál nelze ověřit nebo bezpečně doplnit."""


def prague_today(moment: datetime | None = None) -> date:
    current = moment or datetime.now(PRAGUE)
    if current.tzinfo is None:
        raise ChannelError("čas musí mít časové pásmo")
    return current.astimezone(PRAGUE).date()


def latest_completed_week(today: date) -> tuple[str, date, date]:
    """Vrátí poslední ISO týden, který před dneškem celý skončil."""
    probe = today
    for _ in range(14):
        probe -= timedelta(days=1)
        if probe.weekday() == 6:
            end = probe
            start = end - timedelta(days=6)
            iso = start.isocalendar()
            return f"{iso.year}-W{iso.week:02d}", start, end
    raise ChannelError("nelze určit uzavřený týden")


def generate(
    root: Path,
    *,
    today: date,
    overwrite: bool = False,
) -> str:
    """Nic nezapisuje. Chybějící týden je chyba, ne podnět k vygenerování."""
    if overwrite:
        raise ChannelError("přepis existujícího přehledu je v této fázi zakázán")
    root = root.resolve()
    verify_channel(root)
    existing = {path.stem for path in (root / "prehledy").glob("*.json")}
    missing = [week_id for week_id, _, _ in missing_completed_weeks(today, existing)]
    if missing:
        raise ChannelError(
            "automatické fiktivní generování je vypnuté a chybějící týden se nezapíše: "
            + ",".join(missing)
        )
    return "beze_zmeny"


def missing_completed_weeks(
    today: date,
    existing: set[str],
) -> list[tuple[str, date, date]]:
    """Chybějící uzavřené týdny od nejstaršího po poslední skončený."""
    found: list[tuple[str, date, date]] = []
    _, _, end = latest_completed_week(today)
    while len(found) <= 104:
        start = end - timedelta(days=6)
        iso = start.isocalendar()
        week_id = f"{iso.year}-W{iso.week:02d}"
        if week_id in existing:
            break
        found.append((week_id, start, end))
        end = start - timedelta(days=1)
    else:
        raise ChannelError("příliš mnoho chybějících týdnů")
    found.reverse()
    return found


def _write_week(root: Path, week_id: str, start: date, end: date, today: date) -> bool:
    """Fiktivní zápis je oddělený od ověření a zůstává vypnutý."""
    del root, week_id, start, end, today
    raise ChannelError("automatické fiktivní generování je vypnuté a chybějící týden se nezapíše")


def verify_channel(root: Path) -> None:
    root = root.resolve()
    index_path = _within(root, root / "index.json")
    index_raw = _read_file(index_path)
    index = _parse_json(index_raw)
    _expect(index, {
        "format",
        "format_version",
        "feed",
        "sequence",
        "issued_at",
        "expires_at",
        "reviews",
    })
    if index["format"] != FORMAT_INDEX or index["feed"] != FEED_ID:
        raise ChannelError("neznámý seznam")
    if index["format_version"] != FORMAT_VERSION:
        raise ChannelError("nepodporovaná verze seznamu")
    sequence = index["sequence"]
    if type(sequence) is not int or isinstance(sequence, bool) or not 1 <= sequence <= 100_000:
        raise ChannelError("neplatné pořadí seznamu")
    issued_at = _date(index["issued_at"], "issued_at")
    expires_at = _date(index["expires_at"], "expires_at")
    validity = (expires_at - issued_at).days
    if validity < 1 or validity > MAX_VALIDITY_DAYS:
        raise ChannelError("doba platnosti seznamu je mimo povolený rozsah")
    reviews = index["reviews"]
    if type(reviews) is not list or len(reviews) > 104:
        raise ChannelError("neplatný seznam přehledů")
    ids = []
    for item in reviews:
        ref = _review_ref(item)
        ids.append(ref["id"])
        file_path = _within(root, root / ref["file"])
        raw = _read_file(file_path)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != ref["sha256"]:
            raise ChannelError(f"otisk nesouhlasí: {ref['id']}")
        _verify_review(raw, ref)
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise ChannelError("přehledy nejsou jednoznačně seřazené")
    if ids and issued_at < max(_date(item["issued_at"], "issued_at") for item in reviews):
        raise ChannelError("seznam je starší než některý přehled")


def export_site(root: Path, destination: Path) -> None:
    """Zkopíruje jen veřejné soubory webu. Skripty ani workflow nebere."""
    verify_channel(root)
    destination.mkdir(parents=True, exist_ok=True)
    for name in PUBLIC_FILES:
        source = _within(root, root / name)
        if not source.is_file():
            raise ChannelError(f"chybí veřejný soubor {name}")
        shutil.copyfile(source, destination / name)
    prehledy = destination / "prehledy"
    prehledy.mkdir(exist_ok=True)
    for source in sorted((root / "prehledy").glob("*.json")):
        if not source.is_file() or source.is_symlink():
            raise ChannelError("týdenní soubor není obyčejný soubor")
        shutil.copyfile(source, prehledy / source.name)
    verify_channel(destination)


def _fictional_review(week_id: str, start: date, end: date, issued_at: date) -> dict:
    """Šablona zkušebního týdne. Ověření kanálu ji nepoužívá a samo ji nezapíše."""
    published = start + timedelta(days=2)
    document = {
        "format": FORMAT_REVIEW,
        "format_version": FORMAT_VERSION,
        "id": week_id,
        "issued_at": issued_at.isoformat(),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "status": "novinky",
        "ai_notice": AI_NOTICE,
        "items": [
            {
                "id": f"{week_id}-001",
                "category": "ostatni",
                "title": f"Fiktivní testovací záznam {week_id}",
                "text": (
                    "Tento záznam je smyšlený a slouží jen k technické zkoušce "
                    "automatické publikace. Nejde o skutečný předpis, metodiku "
                    "ani povinnost."
                ),
                "source": {
                    "name": "Fiktivní zdroj pro technickou zkoušku",
                    "url": f"https://example.com/manazer-bozp-novinky/fiktivni-test/{week_id}",
                    "published_at": published.isoformat(),
                },
                "effective_at": None,
                "practical_meaning": (
                    "Záznam ověřuje, že se týdenní soubor vytvoří a zveřejní. "
                    "V praxi se podle něj nic nemění."
                ),
                "recommendation": (
                    "Nic nezavádějte. Údaj není skutečná povinnost a nemá se "
                    "promítnout do dokumentace."
                ),
            }
        ],
    }
    if FICTIONAL_MARK not in _dump(document).decode("utf-8").lower():
        raise ChannelError("generátor by zapsal přehled bez fiktivního označení")
    return document


def _upsert_index(root: Path, ref: dict, today: date) -> None:
    index_path = root / "index.json"
    index = _parse_json(_read_file(index_path))
    week_id = ref["id"]
    reviews = index["reviews"]
    replaced = False
    for position, item in enumerate(reviews):
        if item["id"] == week_id:
            reviews[position] = ref
            replaced = True
            break
    if not replaced:
        reviews.append(ref)
    reviews.sort(key=lambda item: item["id"])
    index["sequence"] = int(index["sequence"]) + 1
    index["issued_at"] = today.isoformat()
    index["expires_at"] = (today + timedelta(days=90)).isoformat()
    _atomic_write(index_path, _dump(index))


def _verify_review(raw: bytes, ref: dict) -> None:
    document = _parse_json(raw)
    status = document.get("status")
    required = {
        "format",
        "format_version",
        "id",
        "issued_at",
        "period",
        "status",
        "ai_notice",
        "items",
    }
    if status == "bez_podstatnych_novinek":
        required.add("summary")
    _expect(document, required, optional={"content_markdown"})
    markdown = document.get("content_markdown")
    if markdown is not None:
        _text(markdown, 20_000, True, "content_markdown")
        _reject_active_content(markdown)
        _require_https_urls(markdown)
    if document["format"] != FORMAT_REVIEW or document["format_version"] != FORMAT_VERSION:
        raise ChannelError("neznámý týdenní přehled")
    if document["id"] != ref["id"]:
        raise ChannelError("identifikátor týdne neodpovídá seznamu")
    if document["ai_notice"] != AI_NOTICE:
        raise ChannelError("chybí povinné upozornění na zpracování AI")
    period = document["period"]
    _expect(period, {"start", "end"})
    start = _date(period["start"], "period.start")
    end = _date(period["end"], "period.end")
    if start.isoformat() != ref["period_start"] or end.isoformat() != ref["period_end"]:
        raise ChannelError("období týdne neodpovídá seznamu")
    issued_at = _date(document["issued_at"], "issued_at")
    if issued_at.isoformat() != ref["issued_at"]:
        raise ChannelError("datum vydání týdne neodpovídá seznamu")
    if issued_at < end or issued_at > end + timedelta(days=MAX_ISSUE_LAG_DAYS):
        raise ChannelError("datum vydání neodpovídá sledovanému období")
    expected_start, expected_end = _iso_bounds(ref["id"])
    if start != expected_start or end != expected_end:
        raise ChannelError("období neodpovídá ISO týdnu")
    items = document["items"]
    if type(items) is not list:
        raise ChannelError("items musí být seznam")
    if status == "bez_podstatnych_novinek":
        if items:
            raise ChannelError("prázdný týden nesmí obsahovat položky")
        _text(document["summary"], 1000, True, "summary")
        if markdown is not None and document["summary"] not in markdown:
            raise ChannelError("souhrn není v původním přehledu")
        return
    if status != "novinky" or not items or len(items) > 30:
        raise ChannelError("neplatný stav nebo počet položek")
    seen: set[str] = set()
    for item in items:
        item_id = _verify_item(item, week_id=ref["id"], start=start, end=end, issued_at=issued_at)
        if markdown is not None:
            _require_projection(item, markdown)
        if item_id in seen:
            raise ChannelError("duplicitní identifikátor položky")
        seen.add(item_id)


def _verify_item(item: object, *, week_id: str, start: date, end: date, issued_at: date) -> str:
    _expect(item, {
        "id",
        "category",
        "title",
        "text",
        "source",
        "effective_at",
        "practical_meaning",
        "recommendation",
    })
    item_id = item["id"]
    if type(item_id) is not str or not re.fullmatch(rf"{re.escape(week_id)}-\d{{3}}", item_id):
        raise ChannelError("neplatný identifikátor položky")
    if item["category"] not in CATEGORIES:
        raise ChannelError("neznámá kategorie")
    _text(item["title"], 180, False, "title")
    _text(item["text"], 4000, True, "text")
    _text(item["practical_meaning"], 1500, True, "practical_meaning")
    _text(item["recommendation"], 1500, True, "recommendation")
    source = item["source"]
    _expect(source, {"name", "url", "published_at"})
    _text(source["name"], 200, False, "source.name")
    _https_url(source["url"])
    published = _date(source["published_at"], "source.published_at")
    if published < start or published > end:
        raise ChannelError("datum zveřejnění zdroje je mimo sledované období")
    if item["effective_at"] is not None:
        effective = _date(item["effective_at"], "effective_at")
        if abs((effective - issued_at).days) > 365 * 20:
            raise ChannelError("datum účinnosti je mimo povolený rozsah")
    return item_id


def _review_ref(item: object) -> dict:
    _expect(item, {"id", "issued_at", "period_start", "period_end", "file", "sha256"})
    week_id = item["id"]
    if type(week_id) is not str or not WEEK_ID_PATTERN.fullmatch(week_id):
        raise ChannelError("neplatný identifikátor týdne")
    start = _date(item["period_start"], "period_start")
    end = _date(item["period_end"], "period_end")
    expected_start, expected_end = _iso_bounds(week_id)
    if start != expected_start or end != expected_end:
        raise ChannelError("období v seznamu neodpovídá ISO týdnu")
    if item["file"] != f"prehledy/{week_id}.json":
        raise ChannelError("nepovolená cesta souboru")
    digest = item["sha256"]
    if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ChannelError("neplatný otisk souboru")
    _date(item["issued_at"], "issued_at")
    return item


def _iso_bounds(week_id: str) -> tuple[date, date]:
    year = int(week_id[:4])
    week = int(week_id[6:])
    try:
        return date.fromisocalendar(year, week, 1), date.fromisocalendar(year, week, 7)
    except ValueError:
        raise ChannelError("neplatný identifikátor týdne") from None


def _https_url(value: object) -> None:
    text = _text(value, 300, False, "source.url")
    if "\\" in text or "@" in text:
        raise ChannelError("odkaz má nepovolený tvar")
    from urllib.parse import urlsplit

    parts = urlsplit(text)
    if parts.scheme != "https" or parts.username or parts.password or parts.port:
        raise ChannelError("odkaz musí být https bez přihlášení a portu")
    host = parts.hostname
    if host is None or parts.netloc != host or not HOST_PATTERN.fullmatch(host):
        raise ChannelError("nepovolené jméno serveru v odkazu")


def _text(value: object, max_len: int, multiline: bool, field: str) -> str:
    if type(value) is not str or len(value) > max_len or value != value.strip() or value == "":
        raise ChannelError(f"{field} má nepovolený text")
    for char in value:
        if unicodedata.category(char).startswith("C") and not (multiline and char in "\n\t"):
            raise ChannelError(f"{field} obsahuje nepovolený znak")
    return value


def _reject_active_content(value: str) -> None:
    lowered = value.casefold()
    if "<script" in lowered or "<iframe" in lowered or "javascript:" in lowered:
        raise ChannelError("přehled obsahuje nepovolený aktivní obsah")


def _require_https_urls(value: str) -> None:
    if "http://" in value.casefold():
        raise ChannelError("odkaz musí být https")
    for url in re.findall(r"https://[^\s)<>\]]+", value):
        _https_url(url.rstrip(".,;"))


def _require_projection(item: dict, markdown: str) -> None:
    for field in ("title", "practical_meaning", "recommendation"):
        if item[field] not in markdown:
            raise ChannelError("strukturovaný text není v původním přehledu")
    if item["source"]["url"] not in markdown:
        raise ChannelError("odkaz položky není v původním přehledu")


def _expect(value: object, required: set[str], optional: set[str] | None = None) -> dict:
    if type(value) is not dict:
        raise ChannelError("očekáván objekt JSON")
    allowed = set(required)
    if optional:
        allowed.update(optional)
    if not required <= set(value) or not set(value) <= allowed:
        missing = required - set(value)
        extra = set(value) - allowed
        detail = ", ".join(sorted(missing or extra))
        raise ChannelError(f"neočekávaná nebo chybějící pole: {detail}")
    return value


def _date(value: object, field: str) -> date:
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ChannelError(f"{field} musí být datum")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ChannelError(f"{field} není platné datum") from None


def _parse_json(data: bytes) -> object:
    if data.startswith(b"\xef\xbb\xbf"):
        raise ChannelError("JSON nesmí začínat BOM")
    try:
        text = data.decode("utf-8")
        value, end = json.JSONDecoder(object_pairs_hook=_pairs).raw_decode(text)
    except ChannelError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ChannelError("poškozený JSON") from None
    if text[end:].strip():
        raise ChannelError("data za koncem JSON")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ChannelError("duplicitní nebo neplatný klíč")
        result[key] = value
    return result


def _read_file(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise ChannelError(f"chybí soubor {path.name}")
    return path.read_bytes()


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _dump(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def _within(root: Path, path: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ChannelError("cesta vede mimo kanál")
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kontrola kanálu novinek bez generování")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--today", type=date.fromisoformat)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--export-site", type=Path)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)
    if args.overwrite:
        print("chyba: přepis existujícího přehledu je v této fázi zakázán", file=sys.stderr)
        return 1
    try:
        if args.check_only or args.export_site:
            if args.export_site:
                export_site(args.root, args.export_site)
            else:
                verify_channel(args.root)
            print("overeno")
            return 0
        result = generate(args.root, today=args.today or prague_today(), overwrite=args.overwrite)
    except ChannelError as exc:
        print(f"chyba: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
