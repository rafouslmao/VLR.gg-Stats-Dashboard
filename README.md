# VLR Stats Dashboard

A self-hosted Valorant esports stats dashboard powered by data scraped from [VLR.gg](https://www.vlr.gg). Aggregates player, team, agent, and map performance across Tier 1 (VCT), Tier 2 (Challengers), and Game Changers competition into a fast, filterable web UI.

[![Python](https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white)](requirements.txt)
[![Flask](https://img.shields.io/badge/flask-2.x-000000?logo=flask&logoColor=white)](requirements.txt)
[![License](https://img.shields.io/badge/license-MIT-green)](https://opensource.org/licenses/MIT)
![Status](https://img.shields.io/badge/status-active-brightgreen)

---

## Features

- **Players** — leaderboard with rounds-weighted aggregate stats (Rating, ACS, K/D, KAST, ADR, KPR, APR, FKPR, FDPR, HS%) across an entire season, filterable by tier, region, team, and minimum rounds. Sub/stand-in detection flags T1 players who aren't on a current roster.
- **Teams** — series and map win rates, per-map win/loss/pick/ban breakdowns, and full match history.
- **Maps** — pick/ban rates, attacker/defender side stats, top agents and team comps per map.
- **Agents** — pick rate, win rate, top players, and per-map performance for every agent.
- **Meta** — agent-vs-agent and comp-vs-comp matrices.
- **Events** — per-event leaderboards.
- **H2H** — head-to-head record between any two players or teams.
- **Live re-scrape** — trigger a refresh from the UI without restarting the server.

## Stack

- **Backend** — Python 3.10+, Flask, BeautifulSoup, Requests, Pandas
- **Frontend** — vanilla HTML/CSS/JS (single `index.html`, no build step)
- **Data** — flat files (`.csv` and `.json`), cached in memory on first read
- **Concurrency** — `ThreadPoolExecutor` for parallel event/match/roster fetches

## Project Structure

```
vlr-scraper/
├── app.py              # Flask server + all API endpoints
├── scraper.py          # Player stats scraper (events → players_YYYY.csv)
├── scrape_teams.py     # Match scraper (matches → team_stats_YYYY / team_matches_YYYY)
├── templates/
│   └── index.html      # Single-page dashboard UI
├── players_YYYY.csv         # Aggregated player stats (output of scraper.py)
├── player_events_YYYY.json  # Per-player event history
├── team_stats_YYYY.json     # Aggregated team stats (2023+ only)
└── team_matches_YYYY.json   # Per-team match history with map details (2023+ only)
```

## Quickstart

### 1. Install dependencies

```bash
pip install flask pandas requests beautifulsoup4
```

### 2. Scrape data

Run the scrapers once to generate the data files. Use `--year` to target a specific season (2023–2026 supports full stats; pre-2023 supports players only).

```bash
python scraper.py --year 2026        # → players_2026.csv, player_events_2026.json
python scrape_teams.py --year 2026   # → team_stats_2026.json, team_matches_2026.json
```

The full pipeline takes a few minutes depending on how many events are live; progress is printed per event.

### 3. Run the server

```bash
python app.py
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080) and the dashboard loads.

## How It Works

### Player aggregation (`scraper.py`)

1. **Discover events** — scrapes `/events` and `/vct` for the target year, filters to `completed` or `ongoing`, classifies each event as Tier 1 / Tier 2 / Game Changers, and tags it with a region (Americas, EMEA, Pacific, China, etc.).
2. **Skip junk stages** — qualifiers, promotion, and relegation stages are detected via the page's series filter and dropped. If an event has both clean and junk stages, only the clean stages are fetched and merged.
3. **Per-event stats** — pulls every player's row from each event's stats table (Rating, ACS, K/D, KAST, ADR, KPR, APR, FKPR, FDPR, HS%, rounds).
4. **Group by player ID** — uses the VLR profile ID (not name) so player aliases and team transfers don't fragment a player's record.
5. **Best-tier aggregation** — for each player, finds their highest tier of competition and aggregates only those entries with rounds-weighted averages. Lower-tier entries are kept in the event index for the history view but don't pollute the headline stats.
6. **Sub detection** — fetches the current roster of every T1 team. Any T1 player whose ID isn't on a current roster is flagged `IsSub=1` and gets a relaxed minimum-rounds threshold (50 instead of 100).

### Team aggregation (`scrape_teams.py`)

1. Pulls the match list from each T1 event.
2. Fetches every match page in parallel (8 workers) for map scores, vetoes, picks/bans, and per-map player stats.
3. Aggregates into per-team series record, map win rates, and per-map pick/ban/decider counts.
4. Stores full match history in `team_matches_YYYY.json` for the team detail view.

### API endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/players` | Filtered/paginated player leaderboard |
| `GET /api/player/<id>/events` | Player's per-event history |
| `GET /api/player/profile?name=...` | Aggregated player profile with agent/map breakdown |
| `GET /api/teams` | Team leaderboard (supports `year`, `tier`, `region` filters) |
| `GET /api/team/<id>/matches` | Team match history |
| `GET /api/team/<id>/detail` | Team profile with maps and roster |
| `GET /api/maps` | Per-map stats with agents and comps |
| `GET /api/agents` | Per-agent stats |
| `GET /api/agents/filters` | Available regions and years for the agents view |
| `GET /api/meta/matrix` | Agent and comp matchup matrices |
| `GET /api/events` | Per-event leaderboards |
| `GET /api/h2h` | Head-to-head between two players or teams |
| `GET /export/tier1` | Download Tier 1 CSV export (ZIP) |
| `POST /api/scrape` | Trigger a full re-scrape in the background |
| `GET /api/scrape/status` | Poll scrape progress |

## Configuration

Edit constants near the top of `scraper.py`:

```python
MIN_ROUNDS     = 100   # minimum rounds for regular players to appear
MIN_ROUNDS_SUB = 50    # lower threshold for subs/stand-ins
```

Edit `app.py` to change the port or add team merges (when the same team gets a new VLR ID after a rebrand):

```python
TEAM_MERGES = {"ULF Esports": "Eternal Fire"}  # source_name → target_name
app.run(debug=True, port=8080)
```

## Refreshing the data

You have two options:

- **From the UI** — click the re-scrape button, select the year(s) and scrape type, then start. It runs in a background thread and the caches reload automatically when it finishes.
- **From the terminal** — re-run `python scraper.py --year YYYY` and `python scrape_teams.py --year YYYY`, then restart the server (or hit the `/api/scrape` endpoint).

## Notes

- Data files are named `players_YYYY.csv`, `team_stats_YYYY.json`, etc. The app auto-discovers all year files on startup and merges them in memory.
- Full team stats (Teams, Maps, Agents) are only available for 2023 and later. Pre-2023 scrapes produce player data only.
- The aggregator picks the league region (not International) when a player has both Masters/Champions and a regional league on their record, so a Pacific player who attended Masters Toronto stays tagged as Pacific.
- Computed views are cached by `(year, tier, region)` so filter switches in the UI are instant after the first load.

## License

MIT
