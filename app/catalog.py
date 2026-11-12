"""League and bookmaker catalogue for the Discovery panel.

The API's /sports list has no country field (verified live). The country
here is OUR mapping from the sport key, and the UI labels it as such. Keys
this map does not know fall under "Other", so a new league is never hidden.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data"

INTERNATIONAL = "International"
OTHER = "Other"

# Prefix after "soccer_" -> country. Longest matching prefix wins.
_COUNTRY_PREFIX = {
    "argentina": "Argentina", "australia": "Australia", "austria": "Austria", "belgium": "Belgium",
    "brazil": "Brazil", "chile": "Chile", "china": "China", "denmark": "Denmark", "efl": "England",
    "england": "England", "epl": "England", "fa_cup": "England", "finland": "Finland", "france": "France",
    "germany": "Germany", "greece": "Greece", "italy": "Italy", "japan": "Japan", "korea": "South Korea",
    "league_of_ireland": "Ireland", "mexico": "Mexico", "netherlands": "Netherlands", "norway": "Norway",
    "poland": "Poland", "portugal": "Portugal", "russia": "Russia", "saudi_arabia": "Saudi Arabia",
    "spain": "Spain", "spl": "Scotland", "sweden": "Sweden", "switzerland": "Switzerland",
    "turkey": "Turkey", "usa": "USA",
    "uefa": INTERNATIONAL, "fifa": INTERNATIONAL, "conmebol": INTERNATIONAL, "concacaf": INTERNATIONAL,
    "africa": INTERNATIONAL,
}

# A generic shortlist (top European leagues and cups). Not tied to any jackpot.
POPULAR = (
    "soccer_epl", "soccer_efl_champ", "soccer_spain_la_liga", "soccer_italy_serie_a",
    "soccer_germany_bundesliga", "soccer_france_ligue_one", "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga", "soccer_uefa_champs_league", "soccer_uefa_europa_league",
    "soccer_england_league1", "soccer_germany_bundesliga2",
)


def country_of(sport_key: str) -> str:
    rest = sport_key.removeprefix("soccer_")
    best = ""
    for prefix in _COUNTRY_PREFIX:
        if (rest == prefix or rest.startswith(prefix + "_")) and len(prefix) > len(best):
            best = prefix
    return _COUNTRY_PREFIX[best] if best else OTHER


def soccer_leagues(sports: list[dict], include_inactive: bool = False) -> list[dict]:
    """Soccer leagues that have matches to pick (outright-only keys are left out)."""
    out = []
    for s in sports or []:
        if s.get("group") != "Soccer" or s.get("has_outrights"):
            continue
        if not include_inactive and not s.get("active"):
            continue
        key = s["key"]
        out.append({
            "key": key,
            "title": s.get("title") or key,
            "description": s.get("description") or "",
            "active": bool(s.get("active")),
            "country": country_of(key),
            "popular": key in POPULAR,
        })
    out.sort(key=lambda x: (x["country"] in (INTERNATIONAL, OTHER), x["country"], x["title"].lower()))
    return out


@lru_cache(maxsize=1)
def bookmakers() -> dict:
    return json.loads((DATA / "bookmakers.json").read_text(encoding="utf-8"))


def bookmaker_keys() -> set[str]:
    return {b["key"] for b in bookmakers()["bookmakers"]}


def paid_only_keys() -> set[str]:
    return {b["key"] for b in bookmakers()["bookmakers"] if b["paid_only"]}
