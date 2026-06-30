"""
Shared pytest fixtures for VLR Stats Dashboard.

Creates a minimal but structurally-valid data directory so every Flask route
can exercise its happy path without a live internet connection or a full
season's worth of scraped data.
"""
import json
import os
import shutil

import pytest


# ---------------------------------------------------------------------------
# Minimal fixture data
# ---------------------------------------------------------------------------

PLAYERS_CSV = (
    "Player,PlayerID,Team,TeamID,Tier,Region,Year,"
    "Rating,ACS,KD,KAST,ADR,KPR,APR,FKPR,FDPR,HS%,Rounds,Events,IsSub,_sort_KAST,_sort_HS%\n"
    "s1mple,101,NAVI,201,Tier 1,EMEA,2024,"
    "1.32,255.4,1.45,75.3,165.2,0.82,0.21,0.12,0.09,24.5,312,3,0,75.3,24.5\n"
    "TenZ,102,NRG,202,Tier 1,Americas,2024,"
    "1.21,238.1,1.31,72.1,158.7,0.79,0.18,0.11,0.08,22.1,289,2,0,72.1,22.1\n"
    "Leo,103,LOUD,203,Tier 1,Americas,2024,"
    "1.18,231.6,1.28,71.4,153.4,0.77,0.17,0.10,0.08,20.8,301,4,0,71.4,20.8\n"
)

PLAYER_EVENTS = {
    "101": [{
        "Event": "VCT 2024 Masters Madrid",
        "Tier": "Tier 1", "Region": "International", "Year": 2024,
        "Team": "NAVI", "TeamID": "201",
        "Rating": 1.32, "ACS": 255.4, "KD": 1.45,
        "KAST": 75.3, "ADR": 165.2, "KPR": 0.82, "APR": 0.21,
        "FKPR": 0.12, "FDPR": 0.09, "HS%": "24.5%", "Rounds": 312
    }],
    "102": [{
        "Event": "VCT 2024 Americas League",
        "Tier": "Tier 1", "Region": "Americas", "Year": 2024,
        "Team": "NRG", "TeamID": "202",
        "Rating": 1.21, "ACS": 238.1, "KD": 1.31,
        "KAST": 72.1, "ADR": 158.7, "KPR": 0.79, "APR": 0.18,
        "FKPR": 0.11, "FDPR": 0.08, "HS%": "22.1%", "Rounds": 289
    }],
    "103": [{
        "Event": "VCT 2024 Americas League",
        "Tier": "Tier 1", "Region": "Americas", "Year": 2024,
        "Team": "LOUD", "TeamID": "203",
        "Rating": 1.18, "ACS": 231.6, "KD": 1.28,
        "KAST": 71.4, "ADR": 153.4, "KPR": 0.77, "APR": 0.17,
        "FKPR": 0.10, "FDPR": 0.08, "HS%": "20.8%", "Rounds": 301
    }],
}

TEAM_STATS = [
    {
        "TeamID": "201", "Name": "NAVI", "Region": "EMEA",
        "SeriesWins": 12, "SeriesLosses": 4, "MapsWon": 28, "MapsLost": 14,
        "Maps": {
            "Ascent":  {"Played": 8, "Wins": 5, "Losses": 3, "Picks": 4, "Bans": 1, "Deciders": 3},
            "Haven":   {"Played": 6, "Wins": 4, "Losses": 2, "Picks": 3, "Bans": 2, "Deciders": 1},
        }
    },
    {
        "TeamID": "202", "Name": "NRG", "Region": "Americas",
        "SeriesWins": 10, "SeriesLosses": 6, "MapsWon": 24, "MapsLost": 18,
        "Maps": {
            "Ascent": {"Played": 7, "Wins": 4, "Losses": 3, "Picks": 3, "Bans": 2, "Deciders": 2},
            "Split":  {"Played": 5, "Wins": 3, "Losses": 2, "Picks": 2, "Bans": 3, "Deciders": 0},
        }
    },
    {
        "TeamID": "203", "Name": "LOUD", "Region": "Americas",
        "SeriesWins": 9, "SeriesLosses": 7, "MapsWon": 22, "MapsLost": 20,
        "Maps": {
            "Split":    {"Played": 6, "Wins": 4, "Losses": 2, "Picks": 3, "Bans": 1, "Deciders": 2},
            "Fracture": {"Played": 4, "Wins": 2, "Losses": 2, "Picks": 2, "Bans": 2, "Deciders": 0},
        }
    },
]

_MAP_LIST = [
    {"map": "Ascent", "score": "13-8", "side_first": "atk",
     "picks": [{"team": "201", "agent": "Jett"}, {"team": "202", "agent": "Chamber"}],
     "players_us": [{"name": "s1mple", "agent": "Jett", "rating": "1.45", "acs": "265",
                     "k": "22", "d": "14", "a": "6", "kast": "78%", "adr": "175", "hs": "25%"}],
     "players_them": [{"name": "TenZ", "agent": "Chamber", "rating": "1.12", "acs": "230",
                       "k": "18", "d": "20", "a": "5", "kast": "68%", "adr": "155", "hs": "22%"}]},
    {"map": "Haven",  "score": "9-13", "side_first": "def", "picks": [], "players_us": [], "players_them": []},
    {"map": "Split",  "score": "13-10","side_first": "atk", "picks": [], "players_us": [], "players_them": []},
]

TEAM_MATCHES = {
    "201": [{
        "match_id": 1001, "url": "https://vlr.gg/1001/navi-vs-nrg",
        "event": "VCT 2024 Masters Madrid", "opponent": "NRG", "opponent_id": "202",
        "score": "2-1", "result": "W", "date": "2024-03-15", "maps": _MAP_LIST,
    }],
    "202": [{
        "match_id": 1001, "url": "https://vlr.gg/1001/navi-vs-nrg",
        "event": "VCT 2024 Masters Madrid", "opponent": "NAVI", "opponent_id": "201",
        "score": "1-2", "result": "L", "date": "2024-03-15", "maps": _MAP_LIST,
    }],
    "203": [{
        "match_id": 1002, "url": "https://vlr.gg/1002/loud-vs-nrg",
        "event": "VCT 2024 Americas League", "opponent": "NRG", "opponent_id": "202",
        "score": "2-0", "result": "W", "date": "2024-04-10", "maps": [],
    }],
}

SCHEDULE = [{
    "match_id": 9001, "team1": "NAVI", "team2": "NRG",
    "event": "VCT 2025 Masters Shanghai",
    "time": "2025-06-01T14:00:00Z", "url": "https://vlr.gg/9001"
}]

RAW_MATCHES = {
    "1001": {
        "match_id": 1001, "url": "https://vlr.gg/1001/navi-vs-nrg",
        "event": "VCT 2024 Masters Madrid", "maps": []
    }
}


# ---------------------------------------------------------------------------
# Fixture: temporary data directory
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    """Create a minimal data/ directory with valid fixture files."""
    d = tmp_path_factory.mktemp("data")
    (d / "players_2024.csv").write_text(PLAYERS_CSV)
    (d / "player_events_2024.json").write_text(json.dumps(PLAYER_EVENTS))
    (d / "team_stats_2024.json").write_text(json.dumps(TEAM_STATS))
    (d / "team_matches_2024.json").write_text(json.dumps(TEAM_MATCHES))
    (d / "raw_matches_2024.json").write_text(json.dumps(RAW_MATCHES))
    (d / "schedule.json").write_text(json.dumps(SCHEDULE))
    return d


@pytest.fixture
def client(data_dir, monkeypatch):
    """
    Flask test client wired to the fixture data directory.

    Monkeypatches the working directory so app.py's relative data/ paths
    resolve to the temp data directory, and resets all module-level caches
    so each test starts from a clean state.
    """
    import app as flask_module

    monkeypatch.chdir(data_dir.parent)
    link = data_dir.parent / "data"
    if link.exists() or link.is_symlink():
        if link.is_symlink():
            link.unlink()
        else:
            shutil.rmtree(str(link))
    link.symlink_to(data_dir)

    # Reset all module-level caches
    flask_module._df_cache = None
    flask_module._df_merged_cache = None
    flask_module._events_cache = None
    flask_module._team_stats_cache = None
    flask_module._team_matches_cache = None
    flask_module._agents_cache = {}
    flask_module._maps_cache = {}
    flask_module._true_tier_cache = {}
    flask_module._t1_team_ids_cache = None
    flask_module._team_regions_cache = None
    flask_module._team_id_merges = {}
    if hasattr(flask_module, "_player_team_stats_cache"):
        flask_module._player_team_stats_cache = None
    if hasattr(flask_module, "_player_team_fullname_cache"):
        flask_module._player_team_fullname_cache = None

    flask_module.app.config["TESTING"] = True
    with flask_module.app.test_client() as c:
        yield c
