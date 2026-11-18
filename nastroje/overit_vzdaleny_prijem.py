#!/usr/bin/env python3
"""Ověří JSON ze soukromého příjmu. Nic nezapisuje a token nevypisuje.

Na větvi main přijímacího repozitáře soubor přehledu teď není.
Chybějící soubor se nenahrazuje smyšleným přehledem.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import overeni_prenosu as checks
import overit_token_prijmu as token_probe

OWNER = "jindravlada"
INTAKE = "manazer-bozp-prijem"
HTTP_LIMIT = 400_000
API_VERSION = "2022-11-28"
TEST_REF = "zkouska-cteni"
TEST_PATH = "zkouska/2026-W47.json"
TEST_WEEK = "2026-W47"
# Otisk je součástí tohoto souboru. Ze stažené odpovědi se nepřebírá.
TEST_SHA256 = "4d3e305d0a3e0cfa3348ba258993b1fca43c0e0ee224f919e9c8bc301d4724ef"
EXACT_REASONS = {
    "poškozený JSON": "poskozeny_json",
    "data za koncem JSON": "poskozeny_json",
    "JSON nesmí začínat BOM": "poskozeny_json",
    "duplicitní nebo neplatný klíč": "duplicitni_klic",
    "očekáván objekt JSON": "typ",
    "období neodpovídá ISO týdnu": "nesoulad_obdobi",
    "neplatný identifikátor týdne": "iso_tyden",
    "přehled obsahuje nepovolený aktivní obsah": "nebezpecny_obsah",
    "odkaz musí být https": "nebezpecny_obsah",
    "odkaz musí být https bez přihlášení a portu": "nebezpecny_obsah",
    "odkaz má nepovolený tvar": "nebezpecny_obsah",
    "zdroj položky musí obsahovat jeden https odkaz": "nebezpecny_obsah",
    "odkaz zkušebního zdroje nelze převzít jako odborný": "nebezpecny_obsah",
    "nepovolené jméno serveru v odkazu": "nebezpecny_obsah",
    "datum vydání neodpovídá sledovanému období": "nesoulad_obdobi",
    "datum zveřejnění zdroje je mimo sledované období": "nesoulad_obdobi",
    "items musí být seznam": "typ",
    "neznámý druh předaného přehledu": "struktura",
    "předaný přehled není podepsaný a nesmí být označen jako důvěryhodný": "struktura",
    "nadměrný přehled": "nadmerne",
    "původní text se při převodu změnil": "struktura",
    "testovaci prehled nelze publikovat": "testovaci",
    "neplatný příznak testu": "testovaci",
    "datum přehledu je v budoucnosti": "budouci_datum",
    "chybí původní text přehledu": "chybi_puvodni_text",
    "neznámá kategorie": "kategorie",
    "strukturovaný text není v původním přehledu": "nesoulad_textu",
    "neplatné zkušební datum": "datum",
}


class RemoteReject(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


def map_reason(message: str) -> str:
    if message in EXACT_REASONS:
        return EXACT_REASONS[message]
    if message.startswith("neočekávaná nebo chybějící pole:"):
        detail = message.split(":", 1)[1].strip()
        if re.fullmatch(r"[A-Za-z0-9_., ]+", detail):
            return "chybejici_nebo_navic"
        return "struktura"
    if message.startswith("v položce chybí údaj "):
        return "chybejici_nebo_navic"
    if message.endswith("má nepovolený text") or message.endswith("obsahuje nepovolený znak"):
        return "delka_nebo_text"
    if "musí být datum" in message or "není platné datum" in message:
        return "typ"
    return "struktura"


def selected_space(argv: list[str]) -> str:
    if not argv:
        return "prijem"
    if argv == ["--zkouska"]:
        return "zkouska"
    raise RemoteReject("prostor")


def _api_base_or_reject(base: str) -> None:
    parsed = urllib.parse.urlparse(base)
    if parsed.username or parsed.password or parsed.scheme not in {"https", "http"}:
        raise RemoteReject("api")
    if base != token_probe.API_PRODUCTION and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RemoteReject("api")


def document_url(base: str, week_id: str) -> str:
    if checks.WEEK_ID_PATTERN.fullmatch(week_id) is None:
        raise checks.CheckError("neplatný identifikátor týdne")
    _api_base_or_reject(base)
    return f"{base}/repos/{OWNER}/{INTAKE}/contents/prijem/{week_id}.json?ref=main"


def test_document_url(base: str) -> str:
    _api_base_or_reject(base)
    return f"{base}/repos/{OWNER}/{INTAKE}/contents/{TEST_PATH}?ref={TEST_REF}"


def _week_names(root: Path) -> tuple[str, ...]:
    folder = root / "prehledy"
    return tuple(sorted(path.name for path in folder.glob("*.json") if path.is_file()))


def _relation(week_id: str, root: Path, expected: str) -> str | None:
    ids = checks.published_ids(root)
    if week_id in ids:
        return "opakovani"
    if ids and week_id < ids[-1]:
        return "starsi"
    if week_id != expected:
        return "nespravny_tyden"
    return None


def check_bytes(root: Path, raw: bytes, expected: str, *, today: date | None = None) -> tuple[int, str]:
    if len(raw) > checks.MAX_DOCUMENT:
        return 1, "odmitnuto nadmerne"
    try:
        document = checks.examine(raw, today=today)
        relation = _relation(document["id"], root, expected)
        if relation:
            return 1, f"odmitnuto {relation}"
    except checks.CheckError as exc:
        return 1, "odmitnuto " + map_reason(str(exc))
    return 0, f"overeno {document['id']} polozek {len(document['items'])} nepublikovano"


def check_test_bytes(root: Path, raw: bytes, *, today: date | None = None) -> tuple[int, str]:
    if len(raw) > checks.MAX_DOCUMENT:
        return 1, "odmitnuto nadmerne"
    if hashlib.sha256(raw).hexdigest() != TEST_SHA256:
        return 1, "odmitnuto neocekavany_obsah"
    try:
        verdict = checks.assess_distribution_test(raw, today=today)
        if verdict.publishable or verdict.week_id != TEST_WEEK:
            return 1, "odmitnuto testovaci"
        relation = _relation(verdict.week_id, root, checks.expected_week(root))
        if relation:
            return 1, f"odmitnuto {relation}"
    except checks.CheckError as exc:
        return 1, "odmitnuto " + map_reason(str(exc))
    return 0, f"overeno {verdict.week_id} polozek {verdict.item_count} nepublikovano"


def decode_github_file(body: bytes, expected_path: str) -> bytes:
    if len(body) > HTTP_LIMIT:
        raise RemoteReject("nadmerne")
    try:
        payload = checks.parse_json(body)
    except checks.CheckError as exc:
        raise RemoteReject(map_reason(str(exc))) from None
    if type(payload) is not dict:
        raise RemoteReject("struktura")
    for key in ("type", "encoding", "size", "content", "path"):
        if key not in payload:
            raise RemoteReject("struktura")
    if payload["type"] != "file" or payload["path"] != expected_path:
        raise RemoteReject("cesta")
    if payload["encoding"] != "base64":
        raise RemoteReject("nadmerne")
    size = payload["size"]
    content = payload["content"]
    if type(size) is not int or isinstance(size, bool) or type(content) is not str:
        raise RemoteReject("struktura")
    if size > checks.MAX_DOCUMENT or len(content) > HTTP_LIMIT:
        raise RemoteReject("nadmerne")
    compact = "".join(content.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise RemoteReject("poskozeny_json") from None
    if len(decoded) != size or len(decoded) > checks.MAX_DOCUMENT:
        raise RemoteReject("nadmerne" if len(decoded) > checks.MAX_DOCUMENT else "struktura")
    return decoded


def limited_get(url: str, token: str) -> tuple[int | None, bytes, bool]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "User-Agent": "ManazerBOZP-PrijemKontrola/0",
            "X-GitHub-Api-Version": API_VERSION,
        },
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        token_probe._NoRedirect(),
    )
    try:
        with opener.open(request, timeout=20) as response:
            body = response.read(HTTP_LIMIT + 1)
            return response.status, body, len(body) > HTTP_LIMIT
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(HTTP_LIMIT + 1)
        except OSError:
            body = b""
        return exc.code, body, len(body) > HTTP_LIMIT
    except OSError:
        return None, b"", False


def http_code(status: int | None) -> str:
    if status in {301, 302, 303, 307, 308}:
        return "presmerovani"
    if status in {401, 403, 404, 500}:
        return f"http_{status}"
    if status is None:
        return "sit"
    return "http_jine"


def run(root: Path, argv: list[str] | None = None) -> int:
    try:
        space = selected_space([] if argv is None else argv)
    except RemoteReject as exc:
        token_probe.emit(f"odmitnuto {exc.code}")
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
    weeks_before = _week_names(root)
    code = 1
    try:
        try:
            base = token_probe.resolve_api_base()
            if space == "zkouska":
                week_id = TEST_WEEK
                expected_path = TEST_PATH
                url = test_document_url(base)
            else:
                week_id = checks.expected_week(root)
                expected_path = f"prijem/{week_id}.json"
                url = document_url(base, week_id)
        except RemoteReject as exc:
            token_probe.emit(f"odmitnuto {exc.code}")
            return 1
        except checks.CheckError as exc:
            token_probe.emit("odmitnuto " + map_reason(str(exc)))
            return 1
        except RuntimeError:
            token_probe.emit("odmitnuto api")
            return 1
        if token in url:
            token_probe.emit("odmitnuto api")
            return 1
        status, body, oversized = limited_get(url, token)
        if oversized:
            token_probe.emit("odmitnuto nadmerne")
            return 1
        if status != 200:
            token_probe.emit(f"odmitnuto {http_code(status)}")
            return 1
        try:
            raw = decode_github_file(body, expected_path)
        except RemoteReject as exc:
            token_probe.emit(f"odmitnuto {exc.code}")
            return 1
        if space == "zkouska":
            code, line = check_test_bytes(root, raw)
        else:
            code, line = check_bytes(root, raw, week_id)
        token_probe.emit(line)
    finally:
        changed = (root / "index.json").read_bytes() != index_before or _week_names(root) != weeks_before
        if changed:
            token_probe.emit("odmitnuto zapis_zakazan")
            code = 1
    return code


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
