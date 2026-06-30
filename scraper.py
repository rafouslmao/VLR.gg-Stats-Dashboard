"""
scraper.py — unified VLR scraper

Produces per year:
  data/players_{year}.csv              leaderboard (T1/T2 aggregate)
  data/player_events_{year}.json       per-player event history
  data/team_stats_{year}.json          per-team aggregate stats
  data/team_matches_{year}.json        per-team match history with player data
  data/player_team_stats_{year}.json   per-player per-team stats (exact W-L/Maps/Rating)
"""

import requests
from bs4 import BeautifulSoup
import csv
import json
import os
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

HEADERS   = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
BASE      = "https://www.vlr.gg"
MIN_ROUNDS = 100   # minimum rounds for leaderboard entry

REGION_PATTERNS = [
    ("International", ["valorant masters", "valorant champions", "lock//in", "lock/in"]),
    ("South America", ["south america"]),
    ("North America", ["north america"]),
    ("Americas",      ["americas"]),
    ("EMEA",          ["emea"]),
    ("Pacific",       ["pacific"]),
    ("China",         ["china"]),
    ("Japan",         ["japan"]),
    ("Korea",         ["korea"]),
    ("SEA",           ["southeast asia", " sea "]),
    ("South Asia",    ["south asia"]),
    ("Oceania",       ["oceania", " oce "]),
    ("MENA",          ["mena"]),
    ("Turkey",        ["turkey"]),
    ("LATAM",         ["latam"]),
    ("Brazil",        ["brazil"]),
]

FIELDNAMES = [
    "PlayerID", "Player", "Team", "TeamFull", "ProfileURL",
    "Tier", "Region", "Events", "Rounds",
    "Rating", "ACS", "KD", "KAST", "ADR", "KPR", "APR", "FKPR", "FDPR", "HS%",
]

SKIP_STAGE_KEYWORDS = ("qualifier", "promotion", "relegation")


# ── helpers ────────────────────────────────────────────────────────────────────

def extract_region(title):
    padded = " " + title.lower() + " "
    for region, keywords in REGION_PATTERNS:
        if any(kw in padded for kw in keywords):
            return region
    return "Other"


def classify_tier(title):
    t = title.strip().lower()
    # T2 regardless of umbrella — must check before T1 regions
    if re.search(r"challengers|ascension|last.{0,5}chance", t):
        return "Tier 2"
    if "game changers" in t:
        return "Game Changers"
    if re.search(r"off.{0,3}season|show.?match", t):
        return None
    # Non-T1 events that would otherwise slip through — block early
    if re.search(r"national tournament|national competition|college|collegiate|qualifier$", t):
        return None
    # 2025+ franchise era: "VCT YYYY: Region Stage"
    if t.startswith("vct "):
        return "Tier 1"
    # 2023-2024 franchise era: "Champions Tour YYYY: Region Stage"
    if t.startswith("champions tour "):
        yr = re.search(r"20(\d{2})", t)
        if yr and int(yr.group(1)) >= 23:
            if any(r in t for r in ("americas", "emea", "pacific")):
                return "Tier 1"
            if "china" in t:
                return "Tier 1"
            if "lock" in t or "masters" in t:
                return "Tier 1"
    # Standalone international events — use startswith to avoid substring false positives
    # e.g. "College VALORANT Championship" contains "valorant champions" as substring
    if t.startswith("valorant champions") or t.startswith("valorant masters"):
        return "Tier 1"
    return None


def extract_player_id(profile_url):
    m = re.search(r"/player/(\d+)/", profile_url or "")
    return m.group(1) if m else None


def _team_id_from_href(href):
    m = re.match(r"/team/(\d+)/", href or "")
    return m.group(1) if m else ""


def _safe_float(s, default=0.0):
    try:
        return float(str(s).replace("%", "").strip())
    except (ValueError, TypeError):
        return default


# ── event discovery ────────────────────────────────────────────────────────────

def discover_events(year="2026", max_pages=50):
    """Return all Tier 1, Tier 2, and Game Changers events for the given year."""
    events   = []
    seen_ids = set()
    year_int = int(year)

    def _scrape_items(items, force_tier=None, require_year=True):
        for a in items:
            title_el  = a.select_one(".event-item-title")
            status_el = a.select_one(".event-item-desc-item-status")
            href      = a.get("href", "")
            if not (title_el and href):
                continue
            title  = title_el.get_text(strip=True)
            status = status_el.get_text(strip=True) if status_el else ""
            if require_year and year not in title:
                continue
            tier = force_tier or classify_tier(title)
            if not tier:
                continue
            if status not in ("completed", "ongoing"):
                continue
            if tier == "Tier 2":
                tl = title.lower()
                if any(kw in tl for kw in ("qualifier", "promotion", "relegation")):
                    continue
            m = re.match(r"^/event/(\d+)/(.+)$", href)
            if not m:
                continue
            ev_id, slug = m.group(1), m.group(2)
            if ev_id in seen_ids:
                continue
            seen_ids.add(ev_id)
            events.append({
                "id": ev_id, "slug": slug, "title": title,
                "tier": tier, "status": status, "region": extract_region(title),
            })

    def _paginate(base_url, stop_before_year=True):
        for page in range(1, max_pages + 1):
            url = base_url if page == 1 else f"{base_url}&page={page}"
            try:
                r = requests.get(url, headers=HEADERS, timeout=15)
            except Exception:
                break
            soup  = BeautifulSoup(r.text, "html.parser")
            items = soup.select("a.event-item")
            if not items:
                break
            if stop_before_year:
                page_years = [
                    int(m.group(0))
                    for a in items
                    for t in [a.select_one(".event-item-title")]
                    if t
                    for m in [re.search(r"20\d\d", t.get_text(strip=True))]
                    if m
                ]
                if page_years and max(page_years) < year_int:
                    break
            _scrape_items(items)
            time.sleep(0.4)

    # ── 1. General events page — catches T1 and any T2/GC not on named pages ──
    _paginate(f"{BASE}/events?")

    # ── 2. Tier-2-specific pagination — /events?tier=2 only lists T2 events ──
    #    Much faster to reach historical years (2023 is ~page 5 vs. ~page 50+ on /events)
    _paginate(f"{BASE}/events?tier=2")

    # ── 3. Named year pages — authoritative, no year-in-title requirement ────
    named_pages = [
        # For /vct-{year}: classify_tier is still used; events that return None
        # (e.g. "China National Tournament") are skipped rather than forced to T1.
        (f"/vct-{year}",  None,      True),
        ("/vct",          None,      False),
        (f"/vcl-{year}",  "Tier 2",  True),
        ("/vcl",          "Tier 2",  False),
    ]
    for path, force_tier, is_year_page in named_pages:
        try:
            r = requests.get(f"{BASE}{path}", headers=HEADERS, timeout=15)
            soup  = BeautifulSoup(r.text, "html.parser")
            items = soup.select("a.event-item")
            # Year-specific pages: trust all entries; current-season pages: year filter
            _scrape_items(items, force_tier=force_tier, require_year=not is_year_page)
        except Exception as e:
            print(f"  [{path} fetch error] {e}")

    return events


# ── player stats pipeline ──────────────────────────────────────────────────────

def _parse_stats_table(soup, event):
    table = soup.find("table")
    if not table:
        return []
    players = []
    for row in table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < 13:
            continue
        name_tag   = cols[0].find("div", style=lambda s: s and "font-weight: 700" in s)
        team_tag   = cols[0].find("div", class_="stats-player-country")
        team_url   = ""
        team_full  = ""
        team_link  = cols[0].find("a", href=lambda h: h and "/team/" in h)
        if team_link:
            team_href = team_link.get("href", "")
            team_url  = BASE + team_href
            m = re.match(r"/team/\d+/(.+)", team_href)
            if m:
                slug = m.group(1)
                team_full = " ".join(
                    w.upper() if len(w) <= 3 else w.capitalize()
                    for w in slug.split("-")
                )
        player_link = cols[0].find("a", href=lambda h: h and "/player/" in h)
        profile_url = BASE + player_link.get("href") if player_link else ""
        players.append({
            "Player":     name_tag.text.strip() if name_tag else "",
            "Team":       team_tag.text.strip()  if team_tag  else "",
            "TeamFull":   team_full,
            "TeamURL":    team_url,
            "ProfileURL": profile_url,
            "EventID":    event["id"],
            "EventSlug":  event["slug"],
            "Event":      event["title"],
            "Tier":       event["tier"],
            "Region":     event["region"],
            "Rounds":     cols[2].text.strip(),
            "Rating":     cols[3].text.strip(),
            "ACS":        cols[4].text.strip(),
            "KD":         cols[5].text.strip(),
            "KAST":       cols[6].text.strip(),
            "ADR":        cols[7].text.strip(),
            "KPR":        cols[8].text.strip(),
            "APR":        cols[9].text.strip(),
            "FKPR":       cols[10].text.strip(),
            "FDPR":       cols[11].text.strip(),
            "HS%":        cols[12].text.strip(),
        })
    return players


def _get_stage_groups(soup):
    groups = {}
    form   = soup.find("form", action="")
    if not form:
        return groups
    for btn in form.find_all("a", class_="group-tag-btn"):
        if btn.get("data-action") != "none":
            continue
        sid = btn.get("data-series-id", "")
        if not sid:
            continue
        parent = btn.find_parent()
        label  = parent.find("div", class_="wf-label") if parent else None
        groups[sid] = label.get_text(strip=True) if label else ""
    return groups


def _merge_stage_rows(rows):
    weighted_stats = ["Rating", "ACS", "KD", "ADR", "KPR", "APR", "FKPR", "FDPR"]
    pct_stats      = ["KAST", "HS%"]
    grouped        = defaultdict(list)
    for r in rows:
        key = r.get("ProfileURL") or f"{r['Player']}::{r['Team']}"
        grouped[key].append(r)

    merged = []
    for entries in grouped.values():
        total_rounds = stat_rounds = 0
        weighted = {s: 0.0 for s in weighted_stats + pct_stats}
        primary  = max(entries, key=lambda e: _safe_float(e.get("Rounds")))
        for e in entries:
            rnd = _safe_float(e.get("Rounds"))
            if rnd == 0:
                continue
            total_rounds += rnd
            if _safe_float(e.get("Rating")) == 0:
                continue
            stat_rounds += rnd
            for s in weighted_stats:
                weighted[s] += _safe_float(e.get(s)) * rnd
            for s in pct_stats:
                weighted[s] += _safe_float(e.get(s)) * rnd
        if total_rounds == 0 or stat_rounds == 0:
            continue
        row = dict(primary)
        row["Rounds"] = str(int(total_rounds))
        for s in weighted_stats:
            row[s] = str(round(weighted[s] / stat_rounds, 2))
        for s in pct_stats:
            row[s] = f"{round(weighted[s] / stat_rounds, 1)}%"
        merged.append(row)
    return merged


def get_event_player_stats(event):
    url = f"{BASE}/event/stats/{event['id']}/{event['slug']}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except Exception:
        return []
    soup         = BeautifulSoup(r.text, "html.parser")
    stage_groups = _get_stage_groups(soup)
    if not stage_groups:
        return _parse_stats_table(soup, event)

    bad_ids  = {sid for sid, name in stage_groups.items()
                if any(kw in name.lower() for kw in SKIP_STAGE_KEYWORDS)}
    good_ids = [sid for sid in stage_groups if sid not in bad_ids]
    if not bad_ids:
        return _parse_stats_table(soup, event)
    if not good_ids:
        return []

    print(f"             [stage filter: skipping {[stage_groups[s] for s in bad_ids]}]")
    per_stage = []
    for sid in good_ids:
        try:
            r2 = requests.get(f"{url}?series_id={sid}", headers=HEADERS, timeout=15)
            per_stage.extend(_parse_stats_table(BeautifulSoup(r2.text, "html.parser"), event))
            time.sleep(0.3)
        except Exception:
            pass
    return _merge_stage_rows(per_stage)


def aggregate_players(rows):
    weighted_stats = ["Rating", "ACS", "KD", "ADR", "KPR", "APR", "FKPR", "FDPR"]
    pct_stats      = ["KAST", "HS%"]
    tier_rank      = {"Tier 1": 3, "Tier 2": 2, "Game Changers": 1}

    grouped = defaultdict(list)
    for r in rows:
        pid = extract_player_id(r.get("ProfileURL", ""))
        key = pid if pid else f"{r['Player']}::{r['Team']}"
        grouped[key].append(r)

    aggregated  = []
    event_index = {}

    for key, entries in grouped.items():
        rounds_by_tier = defaultdict(float)
        for e in entries:
            rounds_by_tier[e["Tier"]] += _safe_float(e.get("Rounds"))
        if not rounds_by_tier:
            continue

        best_tier   = max(rounds_by_tier, key=lambda t: tier_rank.get(t, 0))
        top_entries = [e for e in entries if e["Tier"] == best_tier]

        total_rounds = stat_rounds = 0
        weighted = {s: 0.0 for s in weighted_stats + pct_stats}
        for e in top_entries:
            rnd = _safe_float(e.get("Rounds"))
            if rnd == 0:
                continue
            total_rounds += rnd
            if _safe_float(e.get("Rating")) == 0:
                continue
            stat_rounds += rnd
            for s in weighted_stats:
                weighted[s] += _safe_float(e.get(s)) * rnd
            for s in pct_stats:
                weighted[s] += _safe_float(e.get(s)) * rnd

        if total_rounds < MIN_ROUNDS or stat_rounds == 0:
            continue

        primary        = max(top_entries, key=lambda e: _safe_float(e.get("Rounds")))
        league_entries = [e for e in top_entries if e.get("Region") != "International"]
        region_source  = max(league_entries, key=lambda e: _safe_float(e.get("Rounds"))) \
                         if league_entries else primary

        agg = {
            "PlayerID":   key,
            "Player":     entries[0]["Player"],
            "Team":       primary["Team"],
            "TeamFull":   primary["TeamFull"] or primary["Team"],
            "ProfileURL": primary.get("ProfileURL", ""),
            "Tier":       best_tier,
            "Region":     region_source.get("Region", "Other"),
            "Events":     len(top_entries),
            "Rounds":     int(total_rounds),
        }
        for s in weighted_stats:
            agg[s] = round(weighted[s] / stat_rounds, 2)
        for s in pct_stats:
            agg[s] = f"{round(weighted[s] / stat_rounds, 1)}%"
        aggregated.append(agg)

        keep = ("Event", "Tier", "Region", "Team", "TeamFull",
                "Rounds", "Rating", "ACS", "KD", "KAST", "ADR",
                "KPR", "APR", "FKPR", "FDPR", "HS%")
        event_index[key] = sorted(
            [{f: e.get(f, "") for f in keep} for e in entries],
            key=lambda e: tier_rank.get(e["Tier"], 0),
            reverse=True,
        )

    aggregated.sort(key=lambda p: p["Rating"], reverse=True)
    return aggregated, event_index


# ── team match pipeline ────────────────────────────────────────────────────────

def get_event_match_list(event):
    url = f"{BASE}/event/matches/{event['id']}/{event['slug']}?series_id=all"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
    except Exception:
        return []
    soup    = BeautifulSoup(r.text, "html.parser")
    matches = []
    for a in soup.select("a.match-item"):
        href = a.get("href", "")
        if not re.match(r"^/\d+/", href):
            continue
        if re.search(r"show.?match", href, re.I):
            continue
        if "mod-showmatch" in (a.get("class") or []):
            continue
        series_el = a.select_one(".match-item-event-series")
        if series_el and re.search(r"show.?match", series_el.get_text(strip=True), re.I):
            continue

        teams  = a.select(".match-item-vs-team")
        if len(teams) < 2:
            continue
        names  = [t.select_one(".text-of") for t in teams]
        scores = [t.select_one(".match-item-vs-team-score") for t in teams]
        if not all(n and s for n, s in zip(names, scores)):
            continue

        t1_name = names[0].text.strip()
        t2_name = names[1].text.strip()
        try:
            s1 = int(scores[0].text.strip())
            s2 = int(scores[1].text.strip())
        except ValueError:
            continue
        if s1 == 0 and s2 == 0:
            continue

        cls1 = teams[0].get("class") or []
        cls2 = teams[1].get("class") or []
        if "mod-winner" in cls1:
            winner = 1
        elif "mod-winner" in cls2:
            winner = 2
        elif s1 > s2:
            winner = 1
        elif s2 > s1:
            winner = 2
        else:
            continue

        matches.append({
            "url":      BASE + href,
            "t1_name":  t1_name,
            "t2_name":  t2_name,
            "t1_score": s1,
            "t2_score": s2,
            "winner":   winner,
            "event":    event["title"],
            "region":   event["region"],
        })
    return matches


def _parse_veto(veto_text, t1_acr, t2_acr):
    result = []
    for part in veto_text.split(";"):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.+?)\s+remains$", part, re.I)
        if m:
            result.append({"map": m.group(1).strip(), "action": "decider", "team": 0})
            continue
        m = re.match(r"^(\S+)\s+(ban|pick)\s+(.+)$", part, re.I)
        if m:
            acr, action, map_name = m.group(1), m.group(2).lower(), m.group(3).strip()
            team = 1 if acr == t1_acr else (2 if acr == t2_acr else 0)
            result.append({"map": map_name, "action": action, "team": team})
    return result


def _parse_map_players(game_div):
    def get_both(td):
        s = td.select_one(".mod-both")
        return s.get_text(strip=True) if s else "-"

    result = []
    for tbl in game_div.select("table.wf-table-inset")[:2]:
        players = []
        for row in tbl.select("tbody tr"):
            cols = row.find_all("td")
            if len(cols) < 7:
                continue
            name_el  = cols[0].select_one('[style*="font-weight"]')
            name     = name_el.get_text(strip=True) if name_el else ""
            if not name:
                continue
            link_el  = cols[0].select_one('a[href*="/player/"]')
            pid      = extract_player_id(link_el.get("href", "")) if link_el else None
            img      = cols[1].find("img") if len(cols) > 1 else None
            agent    = (img.get("alt") or "").strip().title() if img else ""
            players.append({
                "id":     pid,
                "name":   name,
                "agent":  agent,
                "rating": get_both(cols[2]),
                "acs":    get_both(cols[3]),
                "k":      get_both(cols[4]),
                "d":      get_both(cols[5]),
                "a":      get_both(cols[6]),
                "kast":   get_both(cols[8])  if len(cols) > 8  else "-",
                "adr":    get_both(cols[9])  if len(cols) > 9  else "-",
                "hs":     get_both(cols[10]) if len(cols) > 10 else "-",
            })
        result.append(players)
    while len(result) < 2:
        result.append([])
    return result[0], result[1]


def parse_match_detail(match_url):
    try:
        r = requests.get(match_url, headers=HEADERS, timeout=15)
    except Exception:
        return None
    soup    = BeautifulSoup(r.text, "html.parser")
    t1_link = soup.select_one(".match-header-link.mod-1")
    t2_link = soup.select_one(".match-header-link.mod-2")
    if not (t1_link and t2_link):
        return None

    t1_id   = _team_id_from_href(t1_link.get("href", ""))
    t2_id   = _team_id_from_href(t2_link.get("href", ""))
    t1_name = t1_link.get_text(strip=True)
    t2_name = t2_link.get_text(strip=True)

    event_name = ""
    event_el = soup.select_one(".match-header-event")
    if event_el:
        series_el = event_el.select_one(".match-header-event-series")
        if series_el:
            series_el.decompose()
        event_name = event_el.get_text(strip=True)

    t1_acr = t2_acr = ""
    round_col = soup.select_one(".vlr-rounds-row-col")
    if round_col:
        labels = round_col.select(".team")
        if len(labels) >= 2:
            t1_acr = labels[0].text.strip()
            t2_acr = labels[1].text.strip()

    veto = []
    veto_el = soup.select_one(".match-header-note")
    if veto_el:
        veto_text = veto_el.get_text(strip=True)
        if ";" in veto_text:
            veto = _parse_veto(veto_text, t1_acr, t2_acr)

    maps = []
    for game_div in soup.select("div.vm-stats-game"):
        game_id = game_div.get("data-game-id", "")
        if not game_id or game_id == "all":
            continue
        hdr = game_div.select_one(".vm-stats-game-header")
        if not hdr:
            continue
        map_div = hdr.select_one(".map")
        if not map_div:
            continue
        map_name_el = map_div.find(style=lambda s: s and "font-weight" in (s or ""))
        if not map_name_el:
            continue
        map_name = re.sub(r"\bPICK\b", "", map_name_el.get_text(), flags=re.I).strip()
        if not map_name:
            continue

        t1_score_el = hdr.select_one(".team:not(.mod-right) .score")
        t2_score_el = hdr.select_one(".team.mod-right .score")
        try:
            s1 = int(t1_score_el.text.strip())
            s2 = int(t2_score_el.text.strip())
        except (AttributeError, ValueError):
            continue

        if t1_score_el and "mod-win" in (t1_score_el.get("class") or []):
            map_winner = 1
        elif t2_score_el and "mod-win" in (t2_score_el.get("class") or []):
            map_winner = 2
        else:
            map_winner = 1 if s1 > s2 else (2 if s2 > s1 else 0)

        picker = 0
        picked_span = map_div.select_one("span.picked")
        if picked_span:
            cls    = picked_span.get("class", [])
            picker = 1 if "mod-1" in cls else (2 if "mod-2" in cls else 0)

        t1_players, t2_players = _parse_map_players(game_div)
        maps.append({
            "name": map_name, "score1": s1, "score2": s2,
            "winner": map_winner, "picker": picker,
            "t1_players": t1_players, "t2_players": t2_players,
        })

    # Derive series score from map wins
    t1_score = sum(1 for m in maps if m.get("winner") == 1)
    t2_score = sum(1 for m in maps if m.get("winner") == 2)
    winner   = 1 if t1_score > t2_score else (2 if t2_score > t1_score else 0)

    return {
        "t1_id":   t1_id,   "t2_id":   t2_id,
        "t1_name": t1_name, "t2_name": t2_name,
        "t1_acr":  t1_acr,  "t2_acr":  t2_acr,
        "t1_score": t1_score, "t2_score": t2_score,
        "winner":  winner,
        "event":   event_name,
        "region":  extract_region(event_name),
        "maps":    maps,
        "veto":    veto,
    }


def aggregate_teams(all_matches, name_to_pid=None):
    """
    Build team stats + match history + per-player-per-team stats.

    name_to_pid: optional dict of player_name_lower → player_id,
                 built from the player leaderboard so team history
                 is keyed by player ID rather than name.
    """
    name_to_pid  = name_to_pid or {}
    team_data    = {}
    team_history = defaultdict(list)
    # player_id (or name_lower) → team_id → {wins, losses, maps, rating_sum, acs_sum, kd_sum}
    player_team  = defaultdict(lambda: defaultdict(lambda: {
        "team_name": "", "wins": 0, "losses": 0, "maps": 0,
        "rating_sum": 0.0, "acs_sum": 0.0, "kd_sum": 0.0, "stat_maps": 0,
    }))

    def _ensure(tid, name, region):
        if tid not in team_data:
            team_data[tid] = {
                "TeamID": tid, "Name": name, "Region": region,
                "SeriesWins": 0, "SeriesLosses": 0,
                "MapsWon": 0, "MapsLost": 0, "TotalSeries": 0,
                "Maps": defaultdict(lambda: {
                    "Played": 0, "Wins": 0, "Losses": 0,
                    "Picks": 0, "Bans": 0, "Deciders": 0,
                }),
            }

    for match in all_matches:
        t1_name = match["t1_name"]
        t2_name = match["t2_name"]
        t1_id   = match.get("t1_id") or f"name:{t1_name}"
        t2_id   = match.get("t2_id") or f"name:{t2_name}"
        t1_acr  = match.get("t1_acr") or t1_name
        t2_acr  = match.get("t2_acr") or t2_name
        region  = match["region"]
        winner  = match["winner"]
        maps    = match.get("maps", [])
        veto    = match.get("veto", [])
        event   = match["event"]

        veto_named = []
        for v in veto:
            if v["action"] == "decider":
                veto_named.append({"map": v["map"], "action": "decider", "actor": ""})
            else:
                actor = t1_acr if v["team"] == 1 else (t2_acr if v["team"] == 2 else "")
                veto_named.append({"map": v["map"], "action": v["action"], "actor": actor})

        _ensure(t1_id, t1_name, region)
        _ensure(t2_id, t2_name, region)

        if winner == 1:
            team_data[t1_id]["SeriesWins"]   += 1
            team_data[t2_id]["SeriesLosses"] += 1
        else:
            team_data[t1_id]["SeriesLosses"] += 1
            team_data[t2_id]["SeriesWins"]   += 1
        team_data[t1_id]["TotalSeries"] += 1
        team_data[t2_id]["TotalSeries"] += 1

        for v in veto:
            if v["action"] in ("pick", "ban"):
                tid_t = t1_id if v["team"] == 1 else (t2_id if v["team"] == 2 else None)
                if tid_t:
                    key = "Picks" if v["action"] == "pick" else "Bans"
                    team_data[tid_t]["Maps"][v["map"]][key] += 1
            elif v["action"] == "decider":
                team_data[t1_id]["Maps"][v["map"]]["Deciders"] += 1
                team_data[t2_id]["Maps"][v["map"]]["Deciders"] += 1

        t1_maps_won = t2_maps_won = 0
        summaries_t1 = []
        summaries_t2 = []

        for m in maps:
            mname   = m["name"]
            s1, s2  = m["score1"], m["score2"]
            mwinner = m["winner"]
            picker  = m["picker"]

            for tid, side in [(t1_id, 1), (t2_id, 2)]:
                md = team_data[tid]["Maps"][mname]
                md["Played"] += 1
                if mwinner == side:
                    md["Wins"] += 1
                elif mwinner != 0:
                    md["Losses"] += 1

            if mwinner == 1:
                t1_maps_won += 1
            elif mwinner == 2:
                t2_maps_won += 1

            note = lambda side: ("Our pick"  if picker == side else
                                 ("Opp pick" if picker in (1, 2) else "Decider"))

            summaries_t1.append({
                "map": mname, "result": "W" if mwinner == 1 else ("L" if mwinner == 2 else "D"),
                "score": f"{s1}-{s2}", "note": note(1),
                "players_us":  m.get("t1_players", []),
                "players_opp": m.get("t2_players", []),
            })
            summaries_t2.append({
                "map": mname, "result": "W" if mwinner == 2 else ("L" if mwinner == 1 else "D"),
                "score": f"{s2}-{s1}", "note": note(2),
                "players_us":  m.get("t2_players", []),
                "players_opp": m.get("t1_players", []),
            })

            # Build per-player-per-team stats
            for side_id, side_num, side_players in [
                (t1_id, 1, m.get("t1_players", [])),
                (t2_id, 2, m.get("t2_players", [])),
            ]:
                map_won = (mwinner == side_num)
                tname   = t1_name if side_num == 1 else t2_name
                for p in side_players:
                    if not isinstance(p, dict):
                        continue
                    pname = (p.get("name") or "").strip()
                    if not pname:
                        continue
                    pid_key = name_to_pid.get(pname.lower(), pname.lower())
                    rec = player_team[pid_key][side_id]
                    if not rec["team_name"]:
                        rec["team_name"] = tname
                    rec["maps"] += 1
                    if mwinner != 0:
                        if map_won:
                            rec["wins"] += 1
                        else:
                            rec["losses"] += 1
                    r_val   = _safe_float(p.get("rating"))
                    acs_val = _safe_float(p.get("acs"))
                    kd_val  = _safe_float(p.get("k")) / max(_safe_float(p.get("d")), 1)
                    if r_val > 0:
                        rec["rating_sum"] += r_val
                        rec["acs_sum"]    += acs_val
                        rec["kd_sum"]     += kd_val
                        rec["stat_maps"]  += 1

        team_data[t1_id]["MapsWon"]  += t1_maps_won
        team_data[t1_id]["MapsLost"] += t2_maps_won
        team_data[t2_id]["MapsWon"]  += t2_maps_won
        team_data[t2_id]["MapsLost"] += t1_maps_won

        for tid, opp_name, opp_id, my_score, opp_score, summaries, my_winner, my_acr in [
            (t1_id, t2_name, t2_id, match["t1_score"], match["t2_score"],
             summaries_t1, winner == 1, t1_acr),
            (t2_id, t1_name, t1_id, match["t2_score"], match["t1_score"],
             summaries_t2, winner == 2, t2_acr),
        ]:
            team_history[tid].append({
                "opponent":    opp_name,
                "opponent_id": opp_id,
                "event":       event,
                "result":      "W" if my_winner else "L",
                "score":       f"{my_score}-{opp_score}",
                "my_acr":      my_acr,
                "url":         match.get("url", ""),
                "maps":        summaries,
                "veto":        veto_named,
            })

    # Finalise team stats
    team_stats = []
    for tid, td in team_data.items():
        sw, sl       = td["SeriesWins"], td["SeriesLosses"]
        mw, ml       = td["MapsWon"], td["MapsLost"]
        mp           = mw + ml
        total_series = td["TotalSeries"]
        maps_out     = {}
        for mname, md in td["Maps"].items():
            played = md["Played"]
            if played == 0:
                continue
            maps_out[mname] = {
                "Played":   played,
                "Wins":     md["Wins"],
                "Losses":   md["Losses"],
                "WinPct":   round(md["Wins"] / played * 100, 1) if played else 0,
                "Picks":    md["Picks"],
                "Bans":     md["Bans"],
                "Deciders": md["Deciders"],
                "PickPct":  round(md["Picks"] / total_series * 100, 1) if total_series else 0,
                "BanPct":   round(md["Bans"]  / total_series * 100, 1) if total_series else 0,
            }
        team_stats.append({
            "TeamID":       tid,
            "Name":         td["Name"],
            "Region":       td["Region"],
            "SeriesWins":   sw,
            "SeriesLosses": sl,
            "SeriesPlayed": sw + sl,
            "SeriesWinPct": round(sw / (sw + sl) * 100, 1) if (sw + sl) else 0,
            "MapsWon":      mw,
            "MapsLost":     ml,
            "MapsPlayed":   mp,
            "MapWinPct":    round(mw / mp * 100, 1) if mp else 0,
            "Maps":         maps_out,
        })
    team_stats.sort(key=lambda t: t["SeriesWinPct"], reverse=True)

    # Finalise player-team stats
    player_team_stats = {}
    for pid_key, teams in player_team.items():
        player_team_stats[pid_key] = {}
        for tid, rec in teams.items():
            sm = rec["stat_maps"]
            player_team_stats[pid_key][tid] = {
                "team_name": rec["team_name"],
                "wins":      rec["wins"],
                "losses":    rec["losses"],
                "maps":      rec["maps"],
                "rating":    round(rec["rating_sum"] / sm, 2) if sm else None,
                "acs":       round(rec["acs_sum"] / sm) if sm else None,
                "kd":        round(rec["kd_sum"] / sm, 2) if sm else None,
            }

    return team_stats, dict(team_history), player_team_stats


# ── unified main scraper ───────────────────────────────────────────────────────

def scrape_all(year="2026", existing_matches=None):
    """
    Run both pipelines in a single event-discovery pass.
    Returns (aggregated_players, event_index, team_stats, team_history, player_team_stats).

    existing_matches: flat list of already-fetched raw match dicts (from a previous
                      scrape stored in raw_matches_{year}.json).  When provided only
                      NEW match URLs are fetched from the network — all others are
                      reused from this list.  Pass None for a full re-scrape.
    """
    existing_by_url = {m["url"]: m for m in (existing_matches or []) if m.get("url")}

    print(f"Discovering {year} events…")
    events = discover_events(year=year)
    print(f"  Found {len(events)} events "
          f"({sum(1 for e in events if e['tier']=='Tier 1')} T1, "
          f"{sum(1 for e in events if e['tier']=='Tier 2')} T2, "
          f"{sum(1 for e in events if e['tier']=='Game Changers')} GC)\n")

    all_player_rows = []
    all_matches     = []
    seen_match_urls = set()

    def _process_event(ev):
        stats   = get_event_player_stats(ev)
        matches = get_event_match_list(ev)
        return ev, stats, matches

    # Collect all match stubs keyed by future so we can retrieve them after completion
    fut_to_stubs = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_process_event, ev): ev for ev in events}
        done    = 0
        for fut in as_completed(futures):
            ev, stats, match_list = fut.result()
            done += 1
            unique_teams = len({p["Team"] for p in stats if p["Team"]})

            if unique_teams <= 25 or ev["tier"] == "Tier 1":
                all_player_rows.extend(stats)
                tag = f"{unique_teams} teams, {len(stats)} rows"
            else:
                tag = f"LEADERBOARD SKIP ({unique_teams} teams)"

            new = [m for m in match_list if m["url"] not in seen_match_urls]
            for m in new:
                seen_match_urls.add(m["url"])
            fut_to_stubs[id(fut)] = match_list
            print(f"[{done:3}/{len(events)}] {ev['tier']:14s} | {ev['region']:14s} | "
                  f"{ev['title'][:50]:50s} | {tag} | {len(new)} matches")

    # Deduplicate match stubs from all events
    seen = set()
    unique_stubs = []
    for stubs in fut_to_stubs.values():
        for m in stubs:
            if m["url"] not in seen:
                seen.add(m["url"])
                unique_stubs.append(m)

    # Split into already-cached vs truly new
    cached_matches = [existing_by_url[m["url"]] for m in unique_stubs if m["url"] in existing_by_url]
    new_stubs      = [m for m in unique_stubs if m["url"] not in existing_by_url]
    print(f"\nMatch pages: {len(cached_matches)} cached, {len(new_stubs)} new to fetch…")

    def _fetch_detail(m):
        detail = parse_match_detail(m["url"])
        if detail:
            m.update(detail)
        return m

    new_fetched = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(_fetch_detail, m) for m in new_stubs]
        done = 0
        for fut in as_completed(futs):
            new_fetched.append(fut.result())
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(new_stubs)} new match pages fetched…")

    all_matches = cached_matches + new_fetched

    print(f"\nAggregating players ({len(all_player_rows)} rows, min {MIN_ROUNDS} rounds)…")
    aggregated, event_index = aggregate_players(all_player_rows)
    print(f"  -> {len(aggregated)} unique players")

    # Build name→pid map from leaderboard for cross-referencing match data
    name_to_pid = {}
    for row in aggregated:
        pid = str(row.get("PlayerID", ""))
        if pid and not pid.startswith("name:"):
            name_to_pid[row["Player"].lower()] = pid

    print(f"Aggregating {len(all_matches)} matches…")
    team_stats, team_history, player_team_stats = aggregate_teams(all_matches, name_to_pid)
    print(f"  -> {len(team_stats)} teams, {len(player_team_stats)} player-team records")

    return aggregated, event_index, team_stats, team_history, player_team_stats, all_matches


# ── schedule scraper ───────────────────────────────────────────────────────────

def scrape_upcoming(max_pages=5):
    """Scrape upcoming and live matches from vlr.gg/matches."""
    matches = []
    seen    = set()
    for page in range(1, max_pages + 1):
        url = BASE + "/matches" if page == 1 else f"{BASE}/matches?page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
        except Exception:
            break
        soup  = BeautifulSoup(r.text, "html.parser")
        items = soup.select("a.match-item")
        if not items:
            break
        found_upcoming = False
        for item in items:
            href = item.get("href", "")
            if not href or href in seen:
                continue
            seen.add(href)
            status_el = item.select_one(".ml-status")
            eta_el    = item.select_one(".ml-eta")
            status    = status_el.get_text(strip=True) if status_el else ""
            eta       = eta_el.get_text(strip=True) if eta_el else ""
            if status not in ("Upcoming", "LIVE"):
                continue
            found_upcoming = True

            team_wraps = item.select(".match-item-vs-team-name .text-of")
            team_names = []
            for tw in team_wraps:
                for flag in tw.select(".flag"):
                    flag.decompose()
                team_names.append(tw.get_text(strip=True))
            if len(team_names) < 2:
                continue

            event_el    = item.select_one(".match-item-event")
            series_name = ""
            event_name  = ""
            if event_el:
                series_el = event_el.select_one(".match-item-event-series")
                if series_el:
                    series_name = series_el.get_text(strip=True)
                    series_el.decompose()
                event_name = event_el.get_text(strip=True)

            time_el    = item.select_one(".match-item-time")
            match_time = time_el.get_text(strip=True) if time_el else ""

            matches.append({
                "url":    BASE + href,
                "team1":  team_names[0],
                "team2":  team_names[1],
                "event":  event_name,
                "series": series_name,
                "status": status,
                "eta":    eta,
                "time":   match_time,
            })
        if not found_upcoming:
            break
    return matches


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Scrape VLR.gg — players + teams")
    parser.add_argument("--year", default="2026", help="Season year (e.g. 2025)")
    args = parser.parse_args()

    year = args.year
    os.makedirs("data", exist_ok=True)

    # Load previously cached raw matches for incremental scraping
    raw_path = f"data/raw_matches_{year}.json"
    existing_matches = None
    if os.path.exists(raw_path):
        with open(raw_path, encoding="utf-8") as f:
            existing_matches = json.load(f)
        print(f"Loaded {len(existing_matches)} cached matches from {raw_path}")

    aggregated, event_index, team_stats, team_history, player_team_stats, all_matches = \
        scrape_all(year=year, existing_matches=existing_matches)

    if not aggregated and not team_stats:
        print("No data scraped.")
        return

    if aggregated:
        with open(f"data/players_{year}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES)
            w.writeheader(); w.writerows(aggregated)
        with open(f"data/player_events_{year}.json", "w", encoding="utf-8") as f:
            json.dump(event_index, f, ensure_ascii=False)
        print(f"OK {len(aggregated)} players -> data/players_{year}.csv")
        print(f"OK event index -> data/player_events_{year}.json")

    if team_stats:
        with open(f"data/team_stats_{year}.json", "w", encoding="utf-8") as f:
            json.dump(team_stats, f, ensure_ascii=False, indent=2)
        with open(f"data/team_matches_{year}.json", "w", encoding="utf-8") as f:
            json.dump(team_history, f, ensure_ascii=False)
        print(f"OK {len(team_stats)} teams -> data/team_stats_{year}.json")
        print(f"OK match history -> data/team_matches_{year}.json")

    if player_team_stats:
        with open(f"data/player_team_stats_{year}.json", "w", encoding="utf-8") as f:
            json.dump(player_team_stats, f, ensure_ascii=False)
        print(f"OK player-team stats -> data/player_team_stats_{year}.json")

    if all_matches:
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(all_matches, f, ensure_ascii=False)
        print(f"OK {len(all_matches)} raw matches -> {raw_path}")


if __name__ == "__main__":
    main()
