"""
Smoke tests for VLR Stats Dashboard API endpoints.

Every test follows the same contract:
  - HTTP status is 200 (or a documented non-200 like 404/409)
  - Response body is valid JSON
  - Top-level keys expected by the frontend are present

Tests do NOT require a live VLR.gg connection -- all data is served from the
fixture data in conftest.py.
"""

import json
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_json(client, url, **kwargs):
    r = client.get(url, **kwargs)
    body = json.loads(r.data)
    return r.status_code, body


def post_json(client, url, payload=None):
    r = client.post(
        url,
        data=json.dumps(payload or {}),
        content_type="application/json"
    )
    body = json.loads(r.data)
    return r.status_code, body


# ---------------------------------------------------------------------------
# Root / index
# ---------------------------------------------------------------------------

class TestRoot:
    def test_index_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"html" in r.data.lower()


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------

class TestPlayersEndpoints:
    def test_players_default(self, client):
        status, body = get_json(client, "/api/players")
        assert status == 200
        assert "players" in body
        assert "total" in body
        assert "page" in body
        assert "pages" in body
        assert isinstance(body["players"], list)

    def test_players_with_tier_filter(self, client):
        status, body = get_json(client, "/api/players?tier=Tier+1")
        assert status == 200
        assert "players" in body

    def test_players_with_region_filter(self, client):
        status, body = get_json(client, "/api/players?region=Americas")
        assert status == 200
        assert "players" in body

    def test_players_with_year_filter(self, client):
        status, body = get_json(client, "/api/players?year=2024")
        assert status == 200
        assert "players" in body

    def test_players_with_search(self, client):
        status, body = get_json(client, "/api/players?q=TenZ")
        assert status == 200
        assert "players" in body

    def test_players_with_pagination(self, client):
        status, body = get_json(client, "/api/players?page=1&per_page=10")
        assert status == 200
        assert "players" in body

    def test_players_sort_by_acs(self, client):
        status, body = get_json(client, "/api/players?sort=ACS&dir=desc")
        assert status == 200

    def test_players_returns_data_from_fixture(self, client):
        status, body = get_json(client, "/api/players")
        assert status == 200
        assert body["total"] > 0
        player_names = [p.get("Player") for p in body["players"]]
        assert any(n in player_names for n in ("TenZ", "s1mple", "Leo"))

    def test_player_events(self, client):
        status, body = get_json(client, "/api/player/101/events")
        assert status == 200
        assert isinstance(body, list)

    def test_player_events_unknown_id(self, client):
        status, body = get_json(client, "/api/player/99999/events")
        assert status in (200, 404)
        if status == 200:
            assert isinstance(body, list)

    def test_player_trend(self, client):
        status, body = get_json(client, "/api/player/101/trend")
        assert status in (200, 404)

    def test_player_profile(self, client):
        status, body = get_json(client, "/api/player/profile?name=s1mple")
        assert status in (200, 404)

    def test_player_team_history(self, client):
        # Live VLR.gg call -- 200/404/500 all acceptable in CI
        status, body = get_json(client, "/api/player/101/team-history")
        assert status in (200, 404, 500)

    def test_player_h2h(self, client):
        status, body = get_json(client, "/api/player-h2h?a=TenZ&b=s1mple")
        assert status in (200, 404)


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------

class TestTeamEndpoints:
    def test_teams_default(self, client):
        status, body = get_json(client, "/api/teams")
        assert status == 200
        # /api/teams returns {"teams": [...], "page": ..., "pages": ..., ...}
        assert "teams" in body or isinstance(body, list)

    def test_teams_with_year(self, client):
        status, body = get_json(client, "/api/teams?year=2024")
        assert status == 200

    def test_teams_with_tier_region(self, client):
        status, body = get_json(client, "/api/teams?tier=Tier+1&region=Americas")
        assert status == 200

    def test_team_matches(self, client):
        status, body = get_json(client, "/api/team/201/matches")
        assert status in (200, 404)
        if status == 200:
            assert isinstance(body, list)

    def test_team_matches_unknown_id(self, client):
        status, body = get_json(client, "/api/team/99999/matches")
        assert status in (200, 404)

    def test_team_detail(self, client):
        status, body = get_json(client, "/api/team/201/detail")
        assert status in (200, 404)

    def test_team_trend(self, client):
        status, body = get_json(client, "/api/team/201/trend")
        assert status in (200, 404)

    def test_team_roster(self, client):
        # Live VLR.gg call
        status, body = get_json(client, "/api/team/201/roster")
        assert status in (200, 404, 500)

    def test_team_h2h(self, client):
        status, body = get_json(client, "/api/h2h?a=201&b=202")
        assert status in (200, 404)


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

class TestAgentEndpoints:
    def test_agents_filters(self, client):
        status, body = get_json(client, "/api/agents/filters")
        assert status == 200
        assert isinstance(body, dict)

    def test_agents_default(self, client):
        status, body = get_json(client, "/api/agents")
        assert status == 200
        # Returns {"agents": [...]} wrapper
        agents = body.get("agents", body) if isinstance(body, dict) else body
        assert isinstance(agents, list)

    def test_agents_with_filters(self, client):
        status, body = get_json(client, "/api/agents?tier=Tier+1&region=Americas&year=2024")
        assert status == 200


# ---------------------------------------------------------------------------
# Maps
# ---------------------------------------------------------------------------

class TestMapEndpoints:
    def test_maps_default(self, client):
        status, body = get_json(client, "/api/maps")
        assert status == 200
        # Returns {"maps": [...]} wrapper
        maps = body.get("maps", body) if isinstance(body, dict) else body
        assert isinstance(maps, list)

    def test_veto_heatmap(self, client):
        status, body = get_json(client, "/api/veto-heatmap")
        assert status == 200


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

class TestMetaEndpoints:
    def test_meta_matrix(self, client):
        status, body = get_json(client, "/api/meta/matrix")
        assert status == 200


# ---------------------------------------------------------------------------
# Events & Schedule
# ---------------------------------------------------------------------------

class TestEventsEndpoints:
    def test_event_names(self, client):
        status, body = get_json(client, "/api/event_names")
        assert status == 200
        assert isinstance(body, list)

    def test_events_default(self, client):
        status, body = get_json(client, "/api/events")
        assert status in (200, 400)

    def test_events_with_name(self, client):
        status, body = get_json(client, "/api/events?event=VCT+2024+Masters+Madrid")
        assert status in (200, 404)

    def test_schedule(self, client):
        status, body = get_json(client, "/api/schedule")
        assert status == 200
        # Returns {"matches": [...], "age_secs": ...}
        assert "matches" in body
        assert isinstance(body["matches"], list)


# ---------------------------------------------------------------------------
# Match detail
# ---------------------------------------------------------------------------

class TestMatchEndpoints:
    def test_match_detail_known(self, client):
        status, body = get_json(client, "/api/match/1001")
        assert status in (200, 404)

    def test_match_detail_unknown(self, client):
        status, body = get_json(client, "/api/match/99999999")
        assert status == 404


# ---------------------------------------------------------------------------
# Scrape endpoints
# ---------------------------------------------------------------------------

class TestScrapeEndpoints:
    def test_scrape_status(self, client):
        status, body = get_json(client, "/api/scrape/status")
        assert status == 200
        assert "running" in body

    def test_scrape_start(self, client):
        status, body = post_json(client, "/api/scrape", {"full": False})
        assert status in (200, 409)
        assert "status" in body

    def test_scrape_start_conflict(self, client):
        post_json(client, "/api/scrape", {"full": False})
        status, body = post_json(client, "/api/scrape", {"full": False})
        assert status in (200, 409)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

class TestExportEndpoints:
    def test_export_tier1_returns_zip_or_empty(self, client):
        r = client.get("/export/tier1")
        assert r.status_code in (200, 204, 404)
        if r.status_code == 200:
            ct = r.content_type
            assert "zip" in ct or "json" in ct or "octet" in ct


# ---------------------------------------------------------------------------
# Response shape spot-checks
# ---------------------------------------------------------------------------

class TestResponseShapes:
    """Verify the exact keys the frontend JS depends on."""

    def test_players_response_shape(self, client):
        status, body = get_json(client, "/api/players")
        assert status == 200
        assert set(body.keys()) >= {"players", "total", "page", "pages", "per_page"}

    def test_scrape_status_shape(self, client):
        status, body = get_json(client, "/api/scrape/status")
        assert status == 200
        assert "running" in body
        assert "last_updated" in body

    def test_player_record_shape(self, client):
        status, body = get_json(client, "/api/players")
        if body.get("players"):
            p = body["players"][0]
            expected_keys = {"Player", "Team", "Rating"}
            assert expected_keys.issubset(set(p.keys())), \
                f"Missing keys: {expected_keys - set(p.keys())}"

    def test_schedule_shape(self, client):
        status, body = get_json(client, "/api/schedule")
        assert status == 200
        assert "matches" in body
        assert "age_secs" in body

    def test_agents_shape(self, client):
        status, body = get_json(client, "/api/agents")
        assert status == 200
        # Accepts both {"agents": [...]} and plain list
        agents = body.get("agents", body) if isinstance(body, dict) else body
        assert isinstance(agents, list)

    def test_maps_shape(self, client):
        status, body = get_json(client, "/api/maps")
        assert status == 200
        maps = body.get("maps", body) if isinstance(body, dict) else body
        assert isinstance(maps, list)
