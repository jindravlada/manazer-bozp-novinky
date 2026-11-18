#!/usr/bin/env python3
"""Jen ověří přenesený přehled. Nic nezapisuje a žádný týden nevytváří."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

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
MAX_ISSUE_LAG_DAYS = 14
MAX_DOCUMENT = 262_144
MAX_INDEX = 65_536
ENVELOPE_KEYS = {
    "experiment",
    "production_trusted",
    "id",
    "source_task",
    "source_run_date",
    "period_start",
    "period_end",
    "content_markdown",
}
EXPERIMENT_ID = "chatgpt-automation-transfer"
NUMBERED_HEADING = re.compile(r"^(\d+)\. (.+)$")
MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https://[^)\s]+)\)")
CZECH_DATE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
METADATA_LABELS = ("Stav", "Zveřejněno", "Účinnost", "Zdroj")
PRACTICAL_MARK = "**Praktický význam:**"
RECOMMENDATION_MARK = "**Manažer BOZP – návrh k posouzení:**"


class CheckError(Exception):
    """Přehled nelze bezpečně převzít."""


def parse_json(data: bytes) -> object:
    return _parse_json(data)


def examine(raw: bytes) -> dict:
    """Převede obálku a ověří ji. Do souborů nic nezapisuje."""
    if len(raw) > MAX_DOCUMENT:
        raise CheckError("nadměrný přehled")
    payload = _parse_json(raw)
    document = convert_transfer(payload)
    if document.get("content_markdown") != payload["content_markdown"]:
        raise CheckError("původní text se při převodu změnil")
    raw_review = _dump(document)
    _verify_review(
        raw_review,
        {
            "id": document["id"],
            "issued_at": document["issued_at"],
            "period_start": document["period"]["start"],
            "period_end": document["period"]["end"],
        },
    )
    return document


def published_ids(root: Path) -> list[str]:
    path = root / "index.json"
    if not path.is_file() or path.is_symlink():
        raise CheckError("chybí seznam")
    raw = path.read_bytes()
    if len(raw) > MAX_INDEX:
        raise CheckError("nadměrný přehled")
    index = _parse_json(raw)
    _expect(
        index,
        {"format", "format_version", "feed", "sequence", "issued_at", "expires_at", "reviews"},
    )
    if index["format"] != FORMAT_INDEX or index["feed"] != FEED_ID:
        raise CheckError("neznámý seznam")
    reviews = index["reviews"]
    if type(reviews) is not list or not reviews:
        raise CheckError("seznam nemá žádný týden")
    return [_review_ref(item)["id"] for item in reviews]


def expected_week(root: Path) -> str:
    ids = published_ids(root)
    _start, end = _iso_bounds(ids[-1])
    nxt = end + timedelta(days=1)
    iso = nxt.isocalendar()
    week_id = f"{iso.year}-W{iso.week:02d}"
    if WEEK_ID_PATTERN.fullmatch(week_id) is None:
        raise CheckError("neplatný identifikátor týdne")
    return week_id


def convert_transfer(payload: object) -> dict:
    """Sestaví týdenní dokument. Původní markdown nemění."""
    document = _expect(payload, ENVELOPE_KEYS)
    if document["experiment"] != EXPERIMENT_ID:
        raise CheckError("neznámý druh předaného přehledu")
    if document["production_trusted"] is not False:
        raise CheckError("předaný přehled není podepsaný a nesmí být označen jako důvěryhodný")
    week_id = document["id"]
    if type(week_id) is not str or not WEEK_ID_PATTERN.fullmatch(week_id):
        raise CheckError("neplatný identifikátor týdne")
    _text(document["source_task"], 200, False, "source_task")
    start = _date(document["period_start"], "period_start")
    end = _date(document["period_end"], "period_end")
    expected_start, expected_end = _iso_bounds(week_id)
    if start != expected_start or end != expected_end:
        raise CheckError("období neodpovídá ISO týdnu")
    issued_at = _date(document["source_run_date"], "source_run_date")
    if issued_at < end or (issued_at - end).days > MAX_ISSUE_LAG_DAYS:
        raise CheckError("datum vydání neodpovídá sledovanému období")
    markdown = document["content_markdown"]
    _text(markdown, 20_000, True, "content_markdown")
    _preamble, sections = _sections(markdown)
    items = []
    expected_number = 1
    for title, body in sections:
        numbered = NUMBERED_HEADING.fullmatch(title)
        if numbered is None:
            continue
        number = int(numbered.group(1))
        if number != expected_number:
            raise CheckError("položky přehledu nejsou souvisle číslované")
        items.append(_item(week_id, number, numbered.group(2), body))
        expected_number += 1
    if len(items) > 30:
        raise CheckError("neplatný stav nebo počet položek")
    if items:
        return _review(week_id, issued_at, start, end, "novinky", markdown, items)
    if len(markdown) > 1000:
        raise CheckError("týden bez položek nelze převzít bez zkrácení")
    _text(markdown, 1000, True, "summary")
    return _review(week_id, issued_at, start, end, "bez_podstatnych_novinek", markdown, [], markdown)


def _review(week_id, issued_at, start, end, status, markdown, items, summary=None) -> dict:
    document = {
        "format": FORMAT_REVIEW,
        "format_version": FORMAT_VERSION,
        "id": week_id,
        "issued_at": issued_at.isoformat(),
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "status": status,
        "ai_notice": AI_NOTICE,
        "content_markdown": markdown,
        "items": items,
    }
    if summary is not None:
        document["summary"] = summary
    return document


def _sections(markdown: str) -> tuple[str, list[tuple[str, str]]]:
    preamble: list[str] = []
    sections: list[tuple[str, str]] = []
    title: str | None = None
    body: list[str] = []
    for line in markdown.split("\n"):
        if line.startswith("### "):
            if title is None:
                preamble = body
            else:
                sections.append((title, "\n".join(body).strip()))
            title = line[4:].strip()
            if title == "":
                raise CheckError("prázdný nadpis položky")
            body = []
            continue
        body.append(line)
    if title is None:
        return markdown.strip(), []
    sections.append((title, "\n".join(body).strip()))
    return "\n".join(preamble).strip(), sections


def _item(week_id: str, number: int, title: str, body: str) -> dict:
    metadata, remainder = _metadata(body)
    narrative, practical, recommendation = _split_markers(remainder)
    source_name, source_url = _source(metadata["Zdroj"])
    text_parts = [f"Stav: {metadata['Stav']}"]
    if narrative:
        text_parts.append(narrative)
    return {
        "id": f"{week_id}-{number:03d}",
        "category": _category(metadata["Stav"]),
        "title": title,
        "text": "\n\n".join(text_parts),
        "source": {
            "name": source_name,
            "url": source_url,
            "published_at": _published(metadata["Zveřejněno"]),
        },
        "effective_at": _effective(metadata["Účinnost"]),
        "practical_meaning": practical,
        "recommendation": recommendation,
    }


def _metadata(body: str) -> tuple[dict[str, str], str]:
    lines = body.split("\n")
    found: dict[str, str] = {}
    index = 0
    for label in METADATA_LABELS:
        while index < len(lines) and lines[index].strip() == "":
            index += 1
        prefix = f"- **{label}:**"
        if index >= len(lines) or not lines[index].startswith(prefix):
            raise CheckError(f"v položce chybí údaj {label}")
        value = lines[index][len(prefix):].strip()
        if value == "":
            raise CheckError(f"v položce chybí údaj {label}")
        found[label] = value
        index += 1
    return found, "\n".join(lines[index:]).strip()


def _split_markers(body: str) -> tuple[str, str, str]:
    if body.count(PRACTICAL_MARK) != 1 or body.count(RECOMMENDATION_MARK) != 1:
        raise CheckError("v položce chybí praktický význam nebo doporučení")
    narrative, after_practical = body.split(PRACTICAL_MARK, 1)
    if RECOMMENDATION_MARK in narrative:
        raise CheckError("doporučení předchází praktický význam")
    practical, recommendation = after_practical.split(RECOMMENDATION_MARK, 1)
    narrative = narrative.strip()
    practical = practical.strip()
    recommendation = recommendation.strip()
    if practical == "" or recommendation == "":
        raise CheckError("v položce chybí praktický význam nebo doporučení")
    return narrative, practical, recommendation


def _source(value: str) -> tuple[str, str]:
    links = MARKDOWN_LINK.findall(value)
    if len(links) != 1:
        raise CheckError("zdroj položky musí obsahovat jeden https odkaz")
    name, url = links[0]
    name = name.strip()
    _text(name, 200, False, "source.name")
    _https_url(url)
    if "example.com" in url.casefold():
        raise CheckError("odkaz zkušebního zdroje nelze převzít jako odborný")
    return name, url


def _published(value: str) -> str:
    matches = list(CZECH_DATE.finditer(value))
    if len(matches) != 1:
        raise CheckError("datum zveřejnění nelze převzít")
    remainder = CZECH_DATE.sub("", value).strip(" .")
    if remainder:
        raise CheckError("datum zveřejnění nelze převzít")
    return _czech_date(matches[0]).isoformat()


def _effective(value: str) -> str | None:
    lowered = value.casefold()
    irrelevant = any(
        phrase in lowered for phrase in ("není relevantní", "není stanovena", "nestanovena")
    )
    matches = list(CZECH_DATE.finditer(value))
    if irrelevant:
        if matches:
            raise CheckError("účinnost současně popírá datum a uvádí je")
        return None
    if len(matches) != 1:
        raise CheckError("datum účinnosti nelze převzít")
    return _czech_date(matches[0]).isoformat()


def _czech_date(match: re.Match[str]) -> date:
    try:
        return date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    except ValueError:
        raise CheckError("neplatné datum v přehledu") from None


def _category(stav: str) -> str:
    lowered = stav.casefold()
    denies_amendment = any(
        phrase in lowered for phrase in ("nikoli novela", "nejde o novelu", "není novela")
    )
    if not denies_amendment and any(token in lowered for token in ("novela", "vyhlášena", "vyhlášen")):
        return "pravni_predpis"
    if "metodick" in lowered or "výklad" in lowered:
        return "metodicky_pokyn"
    if "kontrol" in lowered:
        return "kontrolni_cinnost"
    if "norm" in lowered:
        return "technicka_norma"
    if "evropsk" in lowered:
        return "evropsky_predpis"
    return "ostatni"


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
        raise CheckError("neznámý týdenní přehled")
    if document["id"] != ref["id"]:
        raise CheckError("identifikátor týdne neodpovídá seznamu")
    if document["ai_notice"] != AI_NOTICE:
        raise CheckError("chybí povinné upozornění na zpracování AI")
    period = document["period"]
    _expect(period, {"start", "end"})
    start = _date(period["start"], "period.start")
    end = _date(period["end"], "period.end")
    if start.isoformat() != ref["period_start"] or end.isoformat() != ref["period_end"]:
        raise CheckError("období týdne neodpovídá seznamu")
    issued_at = _date(document["issued_at"], "issued_at")
    if issued_at.isoformat() != ref["issued_at"]:
        raise CheckError("datum vydání týdne neodpovídá seznamu")
    if issued_at < end or issued_at > end + timedelta(days=MAX_ISSUE_LAG_DAYS):
        raise CheckError("datum vydání neodpovídá sledovanému období")
    expected_start, expected_end = _iso_bounds(ref["id"])
    if start != expected_start or end != expected_end:
        raise CheckError("období neodpovídá ISO týdnu")
    items = document["items"]
    if type(items) is not list:
        raise CheckError("items musí být seznam")
    if status == "bez_podstatnych_novinek":
        if items:
            raise CheckError("prázdný týden nesmí obsahovat položky")
        _text(document["summary"], 1000, True, "summary")
        if markdown is not None and document["summary"] not in markdown:
            raise CheckError("souhrn není v původním přehledu")
        return
    if status != "novinky" or not items or len(items) > 30:
        raise CheckError("neplatný stav nebo počet položek")
    seen: set[str] = set()
    for item in items:
        item_id = _verify_item(item, week_id=ref["id"], start=start, end=end, issued_at=issued_at)
        if markdown is not None:
            _require_projection(item, markdown)
        if item_id in seen:
            raise CheckError("duplicitní identifikátor položky")
        seen.add(item_id)


def _verify_item(item: object, *, week_id: str, start: date, end: date, issued_at: date) -> str:
    _expect(
        item,
        {"id", "category", "title", "text", "source", "effective_at", "practical_meaning", "recommendation"},
    )
    item_id = item["id"]
    if type(item_id) is not str or not re.fullmatch(rf"{re.escape(week_id)}-\d{{3}}", item_id):
        raise CheckError("neplatný identifikátor položky")
    if item["category"] not in CATEGORIES:
        raise CheckError("neznámá kategorie")
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
        raise CheckError("datum zveřejnění zdroje je mimo sledované období")
    if item["effective_at"] is not None:
        effective = _date(item["effective_at"], "effective_at")
        if abs((effective - issued_at).days) > 365 * 20:
            raise CheckError("datum účinnosti je mimo povolený rozsah")
    return item_id


def _review_ref(item: object) -> dict:
    _expect(item, {"id", "issued_at", "period_start", "period_end", "file", "sha256"})
    week_id = item["id"]
    if type(week_id) is not str or not WEEK_ID_PATTERN.fullmatch(week_id):
        raise CheckError("neplatný identifikátor týdne")
    if item["file"] != f"prehledy/{week_id}.json":
        raise CheckError("nepovolená cesta souboru")
    return {"id": week_id}


def _iso_bounds(week_id: str) -> tuple[date, date]:
    year = int(week_id[:4])
    week = int(week_id[6:])
    try:
        return date.fromisocalendar(year, week, 1), date.fromisocalendar(year, week, 7)
    except ValueError:
        raise CheckError("neplatný identifikátor týdne") from None


def _https_url(value: object) -> None:
    text = _text(value, 300, False, "source.url")
    if "\\" in text or "@" in text:
        raise CheckError("odkaz má nepovolený tvar")
    parts = urlsplit(text)
    if parts.scheme != "https" or parts.username or parts.password or parts.port:
        raise CheckError("odkaz musí být https bez přihlášení a portu")
    host = parts.hostname
    if host is None or parts.netloc != host or not HOST_PATTERN.fullmatch(host):
        raise CheckError("nepovolené jméno serveru v odkazu")


def _text(value: object, max_len: int, multiline: bool, field: str) -> str:
    if type(value) is not str or len(value) > max_len or value != value.strip() or value == "":
        raise CheckError(f"{field} má nepovolený text")
    for char in value:
        if unicodedata.category(char).startswith("C") and not (multiline and char in "\n\t"):
            raise CheckError(f"{field} obsahuje nepovolený znak")
    return value


def _expect(value: object, required: set[str], optional: set[str] | None = None) -> dict:
    if type(value) is not dict:
        raise CheckError("očekáván objekt JSON")
    allowed = set(required)
    if optional:
        allowed.update(optional)
    if not required <= set(value) or not set(value) <= allowed:
        raise CheckError("neočekávaná nebo chybějící pole: " + ", ".join(sorted((required - set(value)) or (set(value) - allowed))))
    return value


def _reject_active_content(value: str) -> None:
    lowered = value.casefold()
    if "<script" in lowered or "<iframe" in lowered or "javascript:" in lowered:
        raise CheckError("přehled obsahuje nepovolený aktivní obsah")


def _require_https_urls(value: str) -> None:
    if "http://" in value.casefold():
        raise CheckError("odkaz musí být https")
    for url in re.findall(r"https://[^\s)<>\]]+", value):
        _https_url(url.rstrip(".,;"))


def _require_projection(item: dict, markdown: str) -> None:
    for field in ("title", "practical_meaning", "recommendation"):
        if item[field] not in markdown:
            raise CheckError("strukturovaný text není v původním přehledu")
    if item["source"]["url"] not in markdown:
        raise CheckError("odkaz položky není v původním přehledu")
    for line in item["text"].splitlines():
        fragment = line.strip()
        if fragment.startswith("Stav: "):
            fragment = fragment[len("Stav: "):].strip()
        if fragment and fragment not in markdown:
            raise CheckError("text položky není v původním přehledu")


def _date(value: object, field: str) -> date:
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise CheckError(f"{field} musí být datum")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CheckError(f"{field} není platné datum") from None


def _parse_json(data: bytes) -> object:
    if data.startswith(b"\xef\xbb\xbf"):
        raise CheckError("JSON nesmí začínat BOM")
    try:
        text = data.decode("utf-8")
        value, end = json.JSONDecoder(object_pairs_hook=_pairs).raw_decode(text)
    except CheckError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CheckError("poškozený JSON") from None
    if text[end:].strip():
        raise CheckError("data za koncem JSON")
    return value


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise CheckError("duplicitní nebo neplatný klíč")
        result[key] = value
    return result


def _dump(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
