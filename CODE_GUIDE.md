# VLR Scraper — Complete Code Guide

This guide walks through every part of the codebase so you can understand what each line
does and why. It assumes you know variables, if/else, loops, and functions (def).
Everything beyond that is explained when it first appears.

---

## The Big Picture

The project has three layers:

```
scraper.py       →   data/ folder   →   app.py   →   browser
(collects data)      (stores data)      (serves it)   (shows it)
```

1. **scraper.py** visits vlr.gg, reads the HTML, pulls out numbers, saves them to files.
2. **app.py** is a web server. It reads those files and answers questions from the browser
   ("give me the top players", "give me this team's stats").
3. **templates/index.html** is the page the browser loads. It talks to app.py to get data
   and draws the tables and charts.

---

## New Python concepts you'll see

Before diving in, here are the ideas used in the code that go beyond basic `def`:

### Imports
```python
import requests
```
An **import** loads a library — code someone else wrote that you can use.
- `requests` — lets you download web pages
- `json` — reads/writes JSON files
- `os` — talks to the file system (check if file exists, make folders)
- `re` — regular expressions: pattern matching in text
- `time` — pause execution (`time.sleep(0.4)`)
- `threading` — run two functions at the same time

### Dictionaries
You know lists (`[1, 2, 3]`). A **dict** maps keys to values:
```python
person = {"name": "neon", "team": "LEV", "rating": 1.35}
person["name"]   # → "neon"
person["rating"] # → 1.35
```

### List comprehensions
A shortcut for building a list with a loop:
```python
# Normal way:
result = []
for x in [1, 2, 3]:
    result.append(x * 2)

# Comprehension:
result = [x * 2 for x in [1, 2, 3]]   # → [2, 4, 6]

# With a filter:
result = [x for x in [1, 2, 3] if x > 1]   # → [2, 3]
```

### try / except
When something might fail (network error, missing key), wrap it:
```python
try:
    r = requests.get(url)   # might fail if internet is down
except Exception:
    return []               # if it fails, return empty list instead of crashing
```

### with
Opens a file and automatically closes it when done:
```python
with open("data/players.csv", "w") as f:
    f.write("something")
# file is closed here, even if an error happened
```

### global
A variable declared outside a function is a **module-level** variable.
To *change* it inside a function, you must declare it global:
```python
_cache = None

def load():
    global _cache       # "I want to write to the outer _cache"
    _cache = {}
```

### defaultdict
A dict that automatically creates a default value for missing keys:
```python
from collections import defaultdict

normal = {}
normal["a"] += 1    # KeyError — "a" doesn't exist yet

auto = defaultdict(int)   # default is 0 for ints
auto["a"] += 1            # works fine, starts at 0
```

### Decorators (@)
A decorator wraps a function with extra behaviour.
You'll see `@app.route("/api/players")` — this tells Flask:
"when someone visits /api/players in the browser, run this function."
You don't need to write decorators yourself; just know that `@something` above
a `def` modifies that function.

### Lambda
A tiny one-line function without a name:
```python
double = lambda x: x * 2
double(5)   # → 10

# Same as:
def double(x):
    return x * 2
```
Used a lot in sorting: `sorted(players, key=lambda p: p["rating"])`.

### f-strings
Put variables directly inside strings:
```python
year = 2026
print(f"Scraping {year}...")   # → "Scraping 2026..."
```

---

## scraper.py — The Data Collector

### Constants (lines 22–51)

```python
HEADERS = {"User-Agent": "Mozilla/5.0 ..."}
BASE    = "https://www.vlr.gg"
```

**Why `HEADERS`?** Websites can block requests from scripts. By setting a `User-Agent`
that looks like a real browser, the site thinks a human is visiting.

```python
REGION_PATTERNS = [
    ("International", ["valorant masters", "valorant champions", ...]),
    ("South America", ["south america"]),
    ...
]
```

A list of tuples. Each tuple is `(region_name, [keywords])`. Used to figure out which
region an event belongs to by checking if any keyword appears in the event title.

```python
FIELDNAMES = ["PlayerID", "Player", "Team", ..., "Rating", "ACS", ...]
```

The column names for the CSV file. `csv.DictWriter` uses this list to know which
columns to write and in what order.

---

### Helper functions (lines 56–89)

```python
def extract_region(title):
    padded = " " + title.lower() + " "
    for region, keywords in REGION_PATTERNS:
        if any(kw in padded for kw in keywords):
            return region
    return "Other"
```

Loops through `REGION_PATTERNS`. For each region, checks if ANY of its keywords
appear in the title. `any(...)` returns True if at least one item in the list is True.
The padding trick (`" " + title + " "`) prevents partial word matches —
`" sea "` only matches " sea " surrounded by spaces, not "southeast".

```python
def classify_tier(title):
    t = title.lower()
    if "game changers" in t: return "Game Changers"
    if "challengers"   in t: return "Tier 2"
    if t.startswith("valorant masters"):   return "Tier 1"
    if t.startswith("valorant champions"): return "Tier 1"
    if t.startswith("vct "):               return "Tier 1"
    return None
```

Returns a tier string or `None`. Order matters — "game changers" is checked before
"challengers" because Game Changers events also contain the word "challengers".

```python
def _safe_float(s, default=0.0):
    try:
        return float(str(s).replace("%", "").strip())
    except (ValueError, TypeError):
        return default
```

Converts strings like `"1.35"` or `"72.3%"` to a float. If it fails (empty string,
`None`, weird text), returns the default. The `replace("%", "")` strips the percent
sign before converting.

---

### discover_events (lines 95–200)

```python
def discover_events(year="2026", max_pages=50):
```

The default value `year="2026"` means if you call `discover_events()` without arguments,
it uses 2026. You can override with `discover_events("2023")`.

```python
def _scrape_items(items, force_tier=None, require_year=True):
    for a in items:
        ...
```

This is a function *defined inside* another function. That's legal in Python.
`_scrape_items` can see all the variables from `discover_events` (like `events`,
`seen_ids`, `year`). Defining it inside keeps it private to `discover_events`.

```python
def _paginate(base_url, stop_before_year=True):
    for page in range(1, max_pages + 1):
        url = base_url if page == 1 else f"{base_url}&page={page}"
```

`range(1, max_pages + 1)` counts from 1 to 50. The conditional expression
`a if condition else b` is Python's one-line if/else.

```python
page_years = [
    int(m.group(0))
    for a in items
    for t in [a.select_one(".event-item-title")]
    if t
    for m in [re.search(r"20\d\d", t.get_text(strip=True))]
    if m
]
```

A nested list comprehension. Read it as: for each event `a`, find its title `t`,
search for a year pattern in the title, collect matching years. `\d` in regex means
"any digit", so `20\d\d` matches 2023, 2024, 2025, etc.

---

### get_event_player_stats and helpers (lines 194–329)

```python
def _parse_stats_table(soup, event):
    table = soup.find("table")
    if not table:
        return []
    for row in table.find_all("tr")[1:]:
```

`soup` is a BeautifulSoup object — it represents an HTML page as a tree you can navigate.
`soup.find("table")` finds the first `<table>` tag. `find_all("tr")[1:]` gets all
table rows, skipping index 0 (the header row). This is called **HTML parsing**.

**How web scraping works:**
A webpage is text that looks like this:
```html
<table>
  <tr><th>Player</th><th>Rating</th></tr>
  <tr><td>neon</td><td>1.35</td></tr>
</table>
```
BeautifulSoup reads this text and lets you find elements by tag name, class, etc.

```python
name_tag = cols[0].find("div", style=lambda s: s and "font-weight: 700" in s)
```

`lambda s: s and "font-weight: 700" in s` is a filter function passed to `.find()`.
BeautifulSoup will call this function for every `<div>` it finds and only return
the ones where it returns True. This finds bold divs (player names are bold on VLR).

---

### aggregate_players (lines 332–406)

This is the most complex function in scraper.py. Its job: take raw rows from every
event and merge them into one row per player.

```python
grouped = defaultdict(list)
for r in rows:
    pid = extract_player_id(r.get("ProfileURL", ""))
    key = pid if pid else f"{r['Player']}::{r['Team']}"
    grouped[key].append(r)
```

Groups all rows by player. If we have a player ID from their VLR profile URL, use that
as the key (most reliable). Otherwise use `"name::team"` as a fallback key.
`defaultdict(list)` automatically creates an empty list for any new key.

```python
for s in weighted_stats:
    weighted[s] += _safe_float(e.get(s)) * rnd
```

**Weighted average**: if a player played 200 rounds in Event A with rating 1.4,
and 100 rounds in Event B with rating 1.1, their combined rating should be
`(1.4×200 + 1.1×100) / 300 = 1.3`, not `(1.4+1.1)/2 = 1.25`.
Multiplying stat × rounds and then dividing by total rounds gives the weighted average.

---

### Team match pipeline (lines 410–605)

```python
def get_event_match_list(event):
```

Visits `/event/matches/{id}/{slug}?series_id=all` and returns a list of match dicts,
each with `url`, `t1_name`, `t2_name`, `t1_score`, `t2_score`, `winner`, `event`.

```python
def parse_match_detail(match_url):
```

Visits a single match page like `vlr.gg/123456/team1-vs-team2-event`. Extracts:
- Which two teams played (and their VLR IDs)
- Per-map results (who won each map, player stats on each map)
- Veto sequence (which maps were picked/banned)
- Team names and event name directly from the page

```python
t1_score = sum(1 for m in maps if m.get("winner") == 1)
t2_score = sum(1 for m in maps if m.get("winner") == 2)
winner   = 1 if t1_score > t2_score else (2 if t2_score > t1_score else 0)
```

`sum(1 for m in maps if ...)` counts how many maps match a condition.
The series score is derived by counting which team won more maps.

---

### aggregate_teams (lines 608–829)

Takes the flat list of all matches and builds three things:

**`team_stats`** — overall record for each team:
```python
{TeamID, Name, Region, SeriesWins, SeriesLosses, MapsWon, MapsLost, Maps: {...}}
```

**`team_history`** — each team's list of individual match results:
```python
{"opponent": "NRG", "result": "W", "score": "2-1", "maps": [...], ...}
```

**`player_team_stats`** — for every player, their stats on each team they played for:
```python
{"29243": {"1234": {"team_name": "LEV", "wins": 12, "losses": 4, "maps": 16, "rating": 1.31}}}
```

The outer key `"29243"` is the player's VLR ID. The inner key `"1234"` is the team's ID.

```python
player_team = defaultdict(lambda: defaultdict(lambda: {
    "team_name": "", "wins": 0, ...
}))
```

A dict of dicts where both levels auto-create their defaults. The `lambda:` syntax
means: "the default value is produced by calling this function". So any new
`player_team[pid][tid]` automatically starts as `{"team_name": "", "wins": 0, ...}`.

---

### scrape_all (lines 834–911)

The main entry point. Ties everything together:

```python
def scrape_all(year="2026", existing_matches=None):
```

**Step 1:** Call `discover_events(year)` to get a list of all events.

**Step 2:** Use a `ThreadPoolExecutor` to fetch player stats and match lists for all
events *at the same time* instead of one by one.

```python
with ThreadPoolExecutor(max_workers=4) as pool:
    futures = {pool.submit(_process_event, ev): ev for ev in events}
    for fut in as_completed(futures):
        ev, stats, match_list = fut.result()
```

`ThreadPoolExecutor` runs multiple functions in parallel (threads).
`pool.submit(fn, arg)` starts `fn(arg)` in the background and returns a "future" —
a placeholder for the result that isn't ready yet.
`as_completed(futures)` yields each future as it finishes, so we process results
as they come in rather than waiting for all of them.
`max_workers=4` means at most 4 are running at once.

**Step 3:** For each match stub collected, check if we already have it cached in
`existing_by_url`. Only fetch detail pages for truly new matches.

**Step 4:** Use another `ThreadPoolExecutor` (8 workers this time) to fetch all
new match detail pages in parallel.

**Step 5:** Call `aggregate_players` and `aggregate_teams` to compute final stats.

**Returns:** a 6-tuple: `(aggregated, event_index, team_stats, team_history, player_team_stats, all_matches)`

---

### scrape_upcoming (lines 916–1003)

Visits `vlr.gg/matches` and returns upcoming/live matches. Used by the schedule
feature in the frontend. Returns dicts with `url`, `team1`, `team2`, `event`,
`status` ("LIVE" or "Upcoming"), `eta`.

---

### main (lines 1006–1051)

The CLI entry point. Only runs when you execute `python scraper.py` directly.

```python
if __name__ == "__main__":
    main()
```

`__name__` is a special variable. When you run a file directly, Python sets it to
`"__main__"`. When another file imports it (`import scraper`), it's set to
`"scraper"`. So this block only runs for direct execution.

```python
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--year", default="2026")
args = parser.parse_args()
```

`argparse` handles command-line arguments. After this, `args.year` contains whatever
you passed with `--year`, or `"2026"` if you didn't pass anything.

---

## app.py — The Web Server

Flask is a library that turns a Python file into a web server. When the browser
visits a URL, Flask finds the matching function and calls it.

### Setup (lines 1–31)

```python
app = Flask(__name__)
```

Creates the Flask application. `__name__` tells Flask where the project files are.

```python
_df_cache           = None
_team_stats_cache   = None
_team_matches_cache = None
```

**Module-level variables** — they exist as long as the server is running.
These are caches: the first time you load player data, it reads from disk (slow).
The result is stored in `_df_cache`. Every request after that uses the in-memory
copy (fast). When a scrape finishes, these are set back to `None` so the next
request re-reads the fresh files.

### load_df, load_team_stats, load_team_matches, load_player_events, load_player_team_stats

These five functions follow the same pattern:
```python
def load_team_stats():
    global _team_stats_cache
    if _team_stats_cache is not None:   # already loaded? return immediately
        return _team_stats_cache
    # otherwise read from disk...
    with open("data/team_stats_2026.json") as f:
        data = json.load(f)
    _team_stats_cache = data
    return _team_stats_cache
```

`json.load(f)` reads a JSON file and converts it to Python dicts/lists.
The opposite is `json.dump(data, f)` — converts Python to JSON and writes it.

`load_df()` is slightly different — it uses **pandas**:
```python
import pandas as pd
df = pd.read_csv("data/players_2026.csv")
```
Pandas loads CSV files into a `DataFrame` — essentially a spreadsheet in memory.
You can filter, sort, and aggregate it with simple code.

### API routes

```python
@app.route("/api/players")
def api_players():
    df = load_df()
    ...
    return jsonify(result)
```

`@app.route("/api/players")` registers this function as the handler for that URL.
`jsonify(result)` converts a Python dict/list to a JSON HTTP response that the
browser can read.

`request.args.get("tier", "")` reads URL parameters.
If the browser visits `/api/players?tier=Tier+1&region=Americas`,
then `request.args.get("tier")` returns `"Tier 1"`.

### api_player_profile (line ~1238)

The most complex API. Given a player name (and optionally their VLR ID), it:

1. Loops through all team matches (`load_team_matches()`)
2. For each map in each match, looks for the player by name
3. Accumulates stats across matches — rating, ACS, K/D, etc.

```python
pid_event_filter: set | None = None
if pid:
    evs = load_player_events()
    pid_event_filter = {ev["Event"] for ev in evs.get(pid, [])}
```

`{ev["Event"] for ev in ...}` is a **set comprehension** — like a list comprehension
but produces a set (no duplicates, very fast lookup with `in`).

This solves the name collision problem: if two players share the same IGN "neon",
and we know the VLR ID of the one we want, we only count events that specific
player participated in. Without this, stats from both players would be merged.

### _update_finished_matches (line ~1797)

Called when a live match ends. Has two code paths:

**Path 1 — `raw_matches` file exists** (after first full scrape with new code):
Load all raw matches, add the new one, re-aggregate everything from scratch.
This is perfectly accurate.

**Path 2 — `raw_matches` file doesn't exist** (first time, using old scraped data):
Don't re-aggregate from scratch — that would wipe existing data. Instead, run
`aggregate_teams` on JUST the new match to get a "delta" (what changed), then
merge those changes into the existing files.
- Append new match to `team_matches_{year}.json`
- Add wins/losses to the right team in `team_stats_{year}.json`
- Update player-team stats in `player_team_stats_{year}.json`

### _schedule_watcher (line ~1850)

A background thread that runs forever:

```python
import threading
threading.Thread(target=_schedule_watcher, daemon=True).start()
```

`threading.Thread(target=fn)` creates a new thread that runs `fn` in parallel
with the main server. `daemon=True` means it stops automatically when the main
program exits.

Inside the watcher:
```python
while True:
    matches = _st.scrape_upcoming()
    live_now = {m["url"]: m for m in matches if m.get("status") == "LIVE"}
    just_finished = [prev_live[u] for u in prev_live if u not in live_now]
```

`{m["url"]: m for m in matches if ...}` is a **dict comprehension** — like a list
comprehension but builds a dict. Keys are URLs, values are match dicts.

`just_finished` is all the matches that were LIVE in the previous cycle but aren't
anymore — meaning they just ended.

```python
    if live_now:
        sleep_secs = 60
    elif upcoming:
        sleep_secs = 600
    else:
        sleep_secs = 1800
```

Adaptive interval: check every minute while matches are live, every 10 minutes
when there are upcoming matches, every 30 minutes when nothing is scheduled.

### _run_scrape (line ~2110)

The full scrape triggered manually from the UI. Calls `scraper_mod.scrape_all()`
and saves all output files. Supports `incremental=True` which passes existing
raw matches to `scrape_all` so only new match pages are fetched.

### Cache clearing

After any scrape or update, all caches are set to `None`:
```python
_team_stats_cache = _team_matches_cache = _player_team_stats_cache = None
```

Python allows chaining assignments like this — all three variables get `None`.
On the next API request, `load_team_stats()` will see `None` and re-read from disk.

---

## templates/index.html — The Frontend

This is a single HTML file with embedded JavaScript. The browser loads it once and
then talks to app.py via **fetch** calls (AJAX — gets data without reloading the page).

### JavaScript basics for context

- `fetch(url)` — downloads data from a URL, returns a Promise
- `.then(r => r.json())` — when the response arrives, parse it as JSON
- `Promise.all([req1, req2])` — wait for multiple fetches to finish, then get all results
- `document.getElementById("x")` — finds an HTML element by its id attribute
- Template literals: `` `Hello ${name}` `` — same as Python's f-strings

### openPlayerModal

```javascript
function openPlayerModal(name, pid) {
    const pidParam = pid ? "&pid=" + encodeURIComponent(pid) : "";
    const profileReq = fetch("/api/player/profile?name=" + name + pidParam)
        .then(r => r.json());
    const vlrHistoryReq = pid
        ? fetch("/api/player/" + pid + "/team-history").then(r => r.json())
        : Promise.resolve(null);

    Promise.all([profileReq, eventsReq, vlrHistoryReq])
        .then(([data, events, vlrHistory]) => {
            document.getElementById("player-modal-body").innerHTML =
                buildProfileHTML(data, events, pid, vlrHistory);
        });
}
```

Makes three API calls at once. When all three are done, builds the HTML for the
player's profile modal and injects it into the page.

### buildTeamHistoryHTML

Takes the team history from two sources and merges them:

1. **`derivedTeams`** — computed by app.py from match data (exact stats, limited teams)
2. **`vlrHistory`** — scraped from vlr.gg/player/{id} (authoritative team list, but
   may lack stats for older teams)

Priority for stats: `e.stats` (exact, from `player_team_stats`) → `st` (from match
data lookup) → `pe` (approximate, from player_events rounds data).

The `~` indicator is shown next to stats that are approximated from rounds data
rather than actual match data.

---

## How data flows end to end

Here is the complete journey for one piece of data — say, Aspas's rating on LOUD:

1. **scraper.py** visits `vlr.gg/event/stats/...` for each event LOUD played in.
   Parses the stats table, finds Aspas, records his Rating per event.

2. **scraper.py** visits each LOUD match page, finds Aspas in the player tables,
   records his per-map rating. `aggregate_teams` averages these into one record:
   `player_team_stats["12345"]["678"] = {rating: 1.41, maps: 94, wins: 68, losses: 26}`.

3. **scraper.py** saves `player_team_stats_2026.json`, `team_matches_2026.json`, etc.

4. **app.py** starts. `load_player_team_stats()` reads the JSON into memory.

5. Browser opens player profile for Aspas. JavaScript calls:
   - `GET /api/player/12345/team-history` → app.py scrapes vlr.gg/player/12345,
     gets the authoritative list of his teams. For each team, looks up
     `player_team_stats["12345"][team_id]` and attaches exact stats.

6. **buildTeamHistoryHTML** in JavaScript renders the table row:
   LOUD | Nov 2021 – present | 94 maps | 68–26 | 72.3% | 1.41 | 285 | 1.38

---

## File structure summary

```
vlr-scraper/
├── scraper.py          collect data from vlr.gg, save to data/
├── app.py              Flask web server, reads data/, answers API requests
├── templates/
│   └── index.html      the webpage: HTML + all JavaScript
└── data/
    ├── players_2026.csv              player leaderboard
    ├── player_events_2026.json       which events each player played in
    ├── team_stats_2026.json          team win rates and map stats
    ├── team_matches_2026.json        every match with player stats per map
    ├── player_team_stats_2026.json   per-player per-team exact stats
    ├── raw_matches_2026.json         flat list of all match data (incremental cache)
    └── schedule.json                 upcoming / live matches
```

---

## Running the project

**Full initial scrape** (do once per year, takes 20–40 minutes):
```
python scraper.py --year 2026
```

**Start the web server:**
```
python app.py
```
Then open `http://localhost:8080` in your browser.

**After startup**, the schedule watcher thread starts automatically. When matches
finish, it calls `_update_finished_matches` which scrapes just that one match page
(~1 second) and updates all data files. No manual scraping needed after the first run.
