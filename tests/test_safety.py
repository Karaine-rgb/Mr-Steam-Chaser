"""Guards: tests cannot reach the real API, the key never goes in a URL,
and only the credit gate can call the paid endpoint."""
import re
from pathlib import Path

import pytest

from app.config import load_settings
from app.odds_api import OddsApiClient, OddsApiConfigError, RealApiForbidden, build_client

ROOT = Path(__file__).resolve().parent.parent


async def test_real_client_refuses_during_tests():
    client = OddsApiClient("https://api.the-odds-api.com", "dummy-not-a-real-key")
    with pytest.raises(RealApiForbidden):
        await client.get_sports()
    with pytest.raises(RealApiForbidden):
        await client._get_odds_unchecked("soccer_epl", ["h2h"], ["pinnacle"])
    await client.aclose()


def test_real_client_needs_a_key():
    with pytest.raises(OddsApiConfigError):
        OddsApiClient("https://api.the-odds-api.com", "")


async def test_key_goes_in_header_not_url(client, mock_api):
    keyed = OddsApiClient("https://api.the-odds-api.com", "SECRET123",
                          transport=__import__("httpx").MockTransport(mock_api.handle), is_mock=True)
    await keyed.get_sports()
    await keyed._get_odds_unchecked("soccer_epl", ["h2h"], ["pinnacle"])
    for request in mock_api.requests:
        assert "SECRET123" not in str(request.url)
        assert "apikey" not in str(request.url).lower()
        assert request.headers["x-api-key"] == "SECRET123"
    await keyed.aclose()


def test_mock_setting_builds_mock_client():
    s = load_settings({"ODDS_API_MOCK": "1"})
    assert s.odds_api_mock and s.odds_api_key == ""
    assert build_client(s).is_mock


def test_settings_have_no_secret_defaults():
    s = load_settings({})
    assert s.odds_api_key == "" and s.database_url == "" and not s.odds_api_mock
    assert "SECRET" not in repr(load_settings({"ODDS_API_KEY": "SECRET"}))  # key hidden from repr


def test_only_credits_module_calls_paid_endpoint():
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "_get_odds_unchecked(" in text and path.name not in ("credits.py", "odds_api.py"):
            offenders.append(path.name)
    assert offenders == []


def _git_owner() -> str | None:
    """The current GitHub owner, read from the git remote at test time, so
    the name itself never has to be written into the repository."""
    import subprocess
    try:
        url = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.search(r"github\.com[/:]([^/]+)/", url)
    return m.group(1) if m else None


def _tracked_files() -> list[Path]:
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "--others", "--exclude-standard"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git not available")
    return [ROOT / line for line in out.splitlines() if line]


def test_no_secrets_or_account_names_in_repo():
    patterns = [
        re.compile(r"apiKey=[A-Za-z0-9]{16,}"),                      # a key in a URL
        re.compile(r"postgres(ql)?://[^@\s<>]+:[^@\s<>]+@[^\s<>]+"),  # a database URL with a password
        re.compile(r"github\.com/[\w.-]+/[\w.-]+", re.I),            # a hard-coded repository URL
    ]
    owner = _git_owner()
    if owner:
        patterns.append(re.compile(re.escape(owner.split("-")[0]), re.I))
    hits = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pat in patterns:
            if pat.search(text):
                hits.append(f"{path.relative_to(ROOT)}: {pat.pattern}")
    assert hits == []


def test_frontend_has_no_duplicate_function_names():
    """Plain scripts silently let a later function replace an earlier one of the
    same name (this bit once: two renderMatches functions)."""
    text = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    names = re.findall(r"^\s*(?:async\s+)?function\s+(\w+)", text, re.M)
    assert sorted({n for n in names if names.count(n) > 1}) == []


def test_frontend_loads_no_third_party_scripts():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert re.findall(r"<script[^>]+src=\"https?://", html) == []
