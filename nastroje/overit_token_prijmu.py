#!/usr/bin/env python3
"""Ruční zkouška tokenu příjmu. Nic nepublikuje a token nevypisuje.

Ve workflow smí běžet jen z větve main a jen s environmentem prijem-cteni.
Mimo GitHub Actions se na api.github.com nepřipojuje.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

OWNER = "jindravlada"
INTAKE = "manazer-bozp-prijem"
DISTRIBUTION = "manazer-bozp-novinky"
APPLICATION = "manazer-bozp"
READ_PATH = "README.md"
API_PRODUCTION = "https://api.github.com"
PERMISSION_PREFIX = "Resource not accessible by "
BODY_LIMIT = 65536
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SECRET_RE = re.compile(r"github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+|gho_[A-Za-z0-9]+")
WRITE_REPOS = (INTAKE, DISTRIBUTION, APPLICATION)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def emit(line: str) -> None:
    hidden = line
    for key in ("PRIJEM_READ_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(key, "")
        if value:
            hidden = hidden.replace(value, "[skryto]")
            stripped = value.strip()
            if stripped:
                hidden = hidden.replace(stripped, "[skryto]")
    hidden = SECRET_RE.sub("[skryto]", hidden)
    print(hidden, flush=True)


def resolve_api_base() -> str:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return API_PRODUCTION
    override = os.environ.get("NOVINKY_TOKEN_TEST_API", "").strip()
    if not override:
        raise RuntimeError("mimo Actions se produkcni API nevola")
    parsed = urllib.parse.urlparse(override)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("testovaci api neni lokalni")
    if parsed.username or parsed.password:
        raise RuntimeError("testovaci api nesmi nest pristupove udaje")
    return override.rstrip("/")


def branch_guard(token: str) -> str | None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return None
    ref = os.environ.get("GITHUB_REF", "")
    if ref != "refs/heads/main":
        if token:
            return "bezpecnostni_selhani: secret je dostupny mimo main"
        return "odmitnuto: spusteni mimo main, secret chybi"
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if github_token and secrets.compare_digest(token, github_token):
        return "jina_chyba: misto tokenu prijmu je pouzity GITHUB_TOKEN"
    return None


def _request(url: str, method: str, token: str, payload: dict | None) -> tuple[int | None, bytes]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "User-Agent": "ManazerBOZP-TokenTest/0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, response.read(BODY_LIMIT)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(BODY_LIMIT)
        except OSError:
            body = b""
        return exc.code, body
    except OSError:
        return None, b""


def _message(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    message = data.get("message")
    if not isinstance(message, str):
        return ""
    return message


def classify_write(status: int | None, body: bytes) -> str:
    if status in {200, 201, 202, 204}:
        return "zapis_prosel"
    message = _message(body)
    if (
        status == 403
        and message.startswith(PERMISSION_PREFIX)
        and len(message) <= 120
        and "github_pat_" not in message
        and "ghp_" not in message
    ):
        return "odmitnuto_opravneni"
    return "jina_chyba"


def _sha(base: str, repo: str, token: str) -> str | None:
    status, body = _request(
        f"{base}/repos/{OWNER}/{repo}/git/ref/heads/main",
        "GET",
        token,
        None,
    )
    if status != 200:
        emit(f"zapis {repo}: jina_chyba HTTP {status if status is not None else 'sit'}")
        return None
    try:
        data = json.loads(body.decode("utf-8"))
        sha = data["object"]["sha"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        emit(f"zapis {repo}: jina_chyba HTTP 200")
        return None
    if not isinstance(sha, str) or SHA_RE.fullmatch(sha) is None:
        emit(f"zapis {repo}: jina_chyba HTTP 200")
        return None
    return sha


def _write(base: str, repo: str, token: str) -> str:
    sha = _sha(base, repo, token)
    if sha is None:
        return "jina_chyba"
    nonce = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
    ref = f"refs/heads/bezpecnostni-test/nezapisovat-{nonce}"
    status, body = _request(
        f"{base}/repos/{OWNER}/{repo}/git/refs",
        "POST",
        token,
        {"ref": ref, "sha": sha},
    )
    kind = classify_write(status, body)
    if kind == "zapis_prosel":
        emit(f"bezpecnostni_selhani: vznikla vetev {OWNER}/{repo} {ref}")
        return kind
    code = status if status is not None else "sit"
    emit(f"zapis {repo}: {kind} HTTP {code}")
    return kind


def main() -> int:
    raw = os.environ.get("PRIJEM_READ_TOKEN", "")
    token = raw.strip()
    verdict = branch_guard(token)
    if verdict is not None:
        emit(verdict)
        return 2 if verdict.startswith("bezpecnostni_selhani") else 1
    if not token:
        emit("jina_chyba: secret chybi")
        return 1
    try:
        base = resolve_api_base()
    except RuntimeError as exc:
        emit(f"jina_chyba: {exc}")
        return 1

    status, _body = _request(
        f"{base}/repos/{OWNER}/{INTAKE}/contents/{READ_PATH}",
        "GET",
        token,
        None,
    )
    if status != 200:
        emit(f"cteni {INTAKE} {READ_PATH}: jina_chyba HTTP {status if status is not None else 'sit'}")
        return 1
    emit(f"cteni {INTAKE} {READ_PATH}: HTTP 200")

    for repo in WRITE_REPOS:
        kind = _write(base, repo, token)
        if kind == "zapis_prosel":
            return 2
        if kind != "odmitnuto_opravneni":
            return 1
    emit("vysledek: token cte prijem a zapisy jsou odmitnute")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        if os.environ.get("GITHUB_ACTIONS") != "true" and os.environ.get("NOVINKY_TOKEN_TEST_API"):
            raise
        emit("jina_chyba: neocekavane selhani")
        sys.exit(1)
