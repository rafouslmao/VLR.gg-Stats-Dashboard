from flask import Flask, render_template, send_file, jsonify, request
import pandas as pd
import json
import os
import math
import re
import threading
import zipfile
import io
import csv as csv_mod
import glob
from collections import defaultdict

app = Flask(__name__)
DEFAULT_PAGE_SIZE = 100
PCT_COLS = {"KAST", "HS%"}
MIN_YEAR_FULL = 2021

_df_cache           = None
_df_merged_cache    = None   # cross-year merged version of _df_cache
_events_cache       = None
_team_stats_cache   = None
_team_matches_cache = None
_agents_cache       = {}   # (tier, region) → list
_maps_cache         = {}   # (tier, region) → list
_true_tier_cache    = {}    # year_or_"all" → {pid(str) → 'Tier 1' | 'Tier 2' | 'Game Changers'}
_t1_team_ids_cache  = None  # set of team IDs that have played ≥1 T1 event
_team_regions_cache = None  # team_id → home region (derived from match history)

TEAM_MERGES     = {"ULF Esports": "Eternal Fire"}  # source_name → target_name
_team_id_merges = {}  # source_id → target_id (populated by load_team_stats)

def _is_intl_event(name):
    """True if the event is an international/Tier-1-level event by the user's definition:
    Champions, Masters (with a city), LOCK//IN, VCT regional leagues (Americas/EMEA/China/Pacific).
    Challengers circuits, regional Masters, off-season, etc. → False (Tier 2).
    """
    n = name.lower()
    if re.search(r"off.{0,2}season", n):
        return False
    if re.search(r"show.?match", n):
        return False
    if "game changers" in n:
        return False
    if "valorant champions" in n and "tour" not in n:
        return True
    if "masters" in n:
        return not bool(re.search(r"masters\s*$", n))  # city follows → intl; ends with Masters → regional
    if "lock" in n:
        return True
    # Sub-circuits under the VCT umbrella are always T2 regardless of region/year
    if re.search(r"challengers|ascension|last.{0,5}chance", n):
        return False
    # VCT regional leagues only became international franchises from 2023 onward.
    # Pre-2023 events with these keywords are regional circuits → Tier 2.
    yr = re.search(r'20(\d{2})', n)
    yr_int = int(yr.group(1)) if yr else 0
    if "americas" in n:
        return yr_int >= 23
    if "emea" in n:
        return yr_int >= 23
    if "china" in n:
        return yr_int >= 23 and n.startswith("vct ")
    if "pacific" in n and "asia-pacific" not in n:
        return yr_int >= 23
    return False

def _true_tier_map(year=None):
    """Map pid(str) → corrected tier for the given year (or all-time if year=None)."""
    global _true_tier_cache
    cache_key = year or "all"
    if cache_key in _true_tier_cache:
        return _true_tier_cache[cache_key]
    evs = load_player_events()
    result = {}
    for pid, ev_list in evs.items():
        filtered = [e for e in ev_list if e.get("Year") == year] if year else ev_list
        if not filtered:
            continue
        has_intl = any(_is_intl_event(ev.get("Event", "")) for ev in filtered)
        all_gc   = all(ev.get("Tier") == "Game Changers" for ev in filtered if ev.get("Tier"))
        if has_intl:
            result[pid] = "Tier 1"
        elif all_gc:
            result[pid] = "Game Changers"
        else:
            result[pid] = "Tier 2"
    _true_tier_cache[cache_key] = result
    return result

def _is_showmatch(m):
    """True if a match record is a showmatch (non-competitive exhibition)."""
    if re.search(r"show.?match", m.get("url", ""), re.I):
        return True
    if re.search(r"show.?match", m.get("event", ""), re.I):
        return True
    # Series scores are at most 3-0 / 2-0 etc. — if either side > 5 it's
    # a round score from a single-map exhibition, not a real series result.
    score = m.get("score", "")
    parts = re.findall(r"\d+", score)
    if len(parts) == 2 and max(int(parts[0]), int(parts[1])) > 5:
        return True
    return False

def _get_t1_team_ids():
    """Return the set of TeamIDs that have played at least 2 real T1 series.
    Requiring 2+ guards against one-off showmatch participants whose URL slug
    doesn't contain 'showmatch' (e.g. celebrity teams at LOCK//IN).
    """
    global _t1_team_ids_cache
    if _t1_team_ids_cache is not None:
        return _t1_team_ids_cache
    load_team_stats()  # ensure _team_id_merges is populated
    idx = load_team_matches()
    counts = {}
    for raw_id, matches in idx.items():
        canonical = _team_id_merges.get(raw_id, raw_id)
        n = sum(1 for m in matches
                if not _is_showmatch(m) and _is_intl_event(m.get("event", "")))
        counts[canonical] = counts.get(canonical, 0) + n
    _t1_team_ids_cache = {tid for tid, n in counts.items() if n >= 2}
    return _t1_team_ids_cache

def _get_all_team_regions():
    """Return dict{team_id → home_region} derived from match event history.
    Uses the most frequent non-international region from the team's most recent
    matches (recency-biased so orgs that changed regions — e.g. Gen.G NA→Pacific —
    show their current region). Respects team ID merges (e.g. ULF → Eternal Fire).
    Cached after first call.
    """
    global _team_regions_cache
    if _team_regions_cache is not None:
        return _team_regions_cache
    from collections import Counter, defaultdict
    load_team_stats()  # ensure _team_id_merges is populated
    idx = load_team_matches()
    team_stats = _team_stats_cache or []
    fallback = {t["TeamID"]: t.get("Region", "") for t in team_stats}

    # Collect all matches per canonical team ID (merging source → target)
    all_matches_by_team = defaultdict(list)
    for raw_id, matches in idx.items():
        canonical = _team_id_merges.get(raw_id, raw_id)
        all_matches_by_team[canonical].extend(matches)

    result = {}
    for team_id, matches in all_matches_by_team.items():
        # Group by year — prefer recent data so region changes are reflected
        by_year = defaultdict(list)
        for m in matches:
            y = _extract_year(m.get("event", "")) or "0000"
            by_year[y].append(m)
        recent = []
        for y in sorted(by_year.keys(), reverse=True):
            bucket = [m for m in by_year[y]
                      if _event_region(m.get("event", "")) not in ("International", "Other", "")]
            if bucket:
                recent.extend(bucket)
                if len(recent) >= 10:   # enough signal — stop going further back
                    break
        if not recent:
            recent = matches
        counts = Counter()
        for m in recent:
            r = _event_region(m.get("event", ""))
            if r not in ("International", "Other", ""):
                counts[r] += 1
        result[team_id] = counts.most_common(1)[0][0] if counts else fallback.get(team_id, "")

    _team_regions_cache = result
    return _team_regions_cache

def _year_files(pattern, legacy):
    """Return year-suffixed files if any exist, otherwise fall back to legacy path."""
    paths = sorted(glob.glob(pattern))
    if paths:
        return paths
    return [legacy] if os.path.exists(legacy) else []

def load_df():
    global _df_cache
    if _df_cache is not None:
        return _df_cache
    paths = _year_files("data/players_*.csv", "data/players.csv")
    if not paths:
        return pd.DataFrame()
    dfs = []
    for path in paths:
        df = pd.read_csv(path)
        m = re.search(r"_(\d{4})\.csv$", path)
        df["Year"] = m.group(1) if m else "unknown"
        dfs.append(df)
    merged = pd.concat(dfs, ignore_index=True)
    for col in PCT_COLS:
        if col in merged.columns:
            merged[f"_sort_{col}"] = pd.to_numeric(
                merged[col].astype(str).str.replace("%", "").str.strip(),
                errors="coerce",
            )
    # Override Tier per-year so a player's current-year context is used,
    # not their all-time career (avoids ex-T1 players staying T1 after moving to T2 teams).
    if "PlayerID" in merged.columns and "Year" in merged.columns:
        for yr in merged["Year"].dropna().unique():
            mask = merged["Year"] == yr
            tm   = _true_tier_map(year=str(yr))
            mapped = merged.loc[mask, "PlayerID"].astype(str).map(tm)
            merged.loc[mask, "Tier"] = mapped.where(mapped.notna(), merged.loc[mask, "Tier"])
    _df_cache = merged
    return _df_cache

def load_player_events():
    global _events_cache
    if _events_cache is not None:
        return _events_cache
    paths = _year_files("data/player_events_*.json", "data/player_events.json")
    merged = {}
    for path in paths:
        m = re.search(r"_(\d{4})\.json$", path)
        year = m.group(1) if m else "unknown"
        with open(path, encoding="utf-8") as f:
            for pid, evs in json.load(f).items():
                merged.setdefault(pid, [])
                for ev in evs:
                    ev["Year"] = year
                merged[pid] += evs
    _events_cache = merged
    return _events_cache

def _apply_team_merges(stats):
    by_name    = {t["Name"]: t for t in stats}
    merged_ids = {}
    to_remove  = set()
    for src_name, tgt_name in TEAM_MERGES.items():
        src = by_name.get(src_name)
        tgt = by_name.get(tgt_name)
        if not src or not tgt:
            continue
        for k in ("SeriesWins", "SeriesLosses", "MapsWon", "MapsLost"):
            tgt[k] = tgt.get(k, 0) + src.get(k, 0)
        sp = tgt["SeriesWins"] + tgt["SeriesLosses"]
        mp = tgt["MapsWon"] + tgt["MapsLost"]
        tgt["SeriesPlayed"] = sp
        tgt["SeriesWinPct"] = round(tgt["SeriesWins"] / sp * 100, 1) if sp else 0
        tgt["MapsPlayed"]   = mp
        tgt["MapWinPct"]    = round(tgt["MapsWon"] / mp * 100, 1) if mp else 0
        for mname, mdata in src.get("Maps", {}).items():
            tm = tgt.setdefault("Maps", {})
            if mname not in tm:
                tm[mname] = dict(mdata)
            else:
                for k in ("Played", "Wins", "Losses", "Picks", "Bans", "Deciders"):
                    tm[mname][k] = tm[mname].get(k, 0) + mdata.get(k, 0)
                played = tm[mname].get("Played", 0)
                tm[mname]["WinPct"] = round(tm[mname]["Wins"] / played * 100, 1) if played else 0
        merged_ids[src["TeamID"]] = tgt["TeamID"]
        to_remove.add(src["TeamID"])
    stats = [t for t in stats if t["TeamID"] not in to_remove]
    return stats, merged_ids

def load_team_stats():
    global _team_stats_cache, _team_id_merges
    if _team_stats_cache is not None:
        return _team_stats_cache
    paths = _year_files("data/team_stats_*.json", "data/team_stats.json")
    if not paths:
        _team_stats_cache, _team_id_merges = [], {}
        return _team_stats_cache

    # Merge across all year files: sum series/map records per team
    merged = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for team in json.load(f):
                tid = team["TeamID"]
                if tid not in merged:
                    merged[tid] = {
                        "TeamID": tid, "Name": team["Name"], "Region": team["Region"],
                        "SeriesWins": 0, "SeriesLosses": 0, "MapsWon": 0, "MapsLost": 0,
                        "Maps": {},
                    }
                d = merged[tid]
                for k in ("SeriesWins", "SeriesLosses", "MapsWon", "MapsLost"):
                    d[k] += team.get(k, 0)
                for mname, md in team.get("Maps", {}).items():
                    if mname not in d["Maps"]:
                        d["Maps"][mname] = {k: md.get(k, 0)
                                            for k in ("Played","Wins","Losses","Picks","Bans","Deciders")}
                    else:
                        for k in ("Played","Wins","Losses","Picks","Bans","Deciders"):
                            d["Maps"][mname][k] = d["Maps"][mname].get(k, 0) + md.get(k, 0)

    raw = []
    for tid, d in merged.items():
        sw, sl = d["SeriesWins"], d["SeriesLosses"]
        mw, ml = d["MapsWon"], d["MapsLost"]
        sp, mp = sw + sl, mw + ml
        maps_out = {}
        for mname, md in d["Maps"].items():
            played = md["Played"]
            if not played:
                continue
            maps_out[mname] = {
                "Played": played, "Wins": md["Wins"], "Losses": md["Losses"],
                "WinPct": round(md["Wins"] / played * 100, 1),
                "Picks": md["Picks"], "Bans": md["Bans"], "Deciders": md["Deciders"],
                "PickPct": round(md["Picks"] / sp * 100, 1) if sp else 0,
                "BanPct":  round(md["Bans"]  / sp * 100, 1) if sp else 0,
            }
        raw.append({
            "TeamID": tid, "Name": d["Name"], "Region": d["Region"],
            "SeriesWins": sw, "SeriesLosses": sl, "SeriesPlayed": sp,
            "SeriesWinPct": round(sw / sp * 100, 1) if sp else 0,
            "MapsWon": mw, "MapsLost": ml, "MapsPlayed": mp,
            "MapWinPct": round(mw / mp * 100, 1) if mp else 0,
            "Maps": maps_out,
        })

    raw.sort(key=lambda t: t["SeriesWinPct"], reverse=True)
    _team_stats_cache, _team_id_merges = _apply_team_merges(raw)
    return _team_stats_cache

def load_team_matches():
    global _team_matches_cache
    if _team_matches_cache is not None:
        return _team_matches_cache
    paths = _year_files("data/team_matches_*.json", "data/team_matches.json")
    merged   = defaultdict(list)
    seen_keys = set()   # (team_id, url) — dedup across year files
    for path in paths:
        ym = re.search(r'(\d{4})\.json$', path)
        file_year = ym.group(1) if ym else ""
        with open(path, encoding="utf-8") as f:
            for team_id, matches in json.load(f).items():
                for match in matches:
                    key = (team_id, match.get("url", ""))
                    if key not in seen_keys:
                        seen_keys.add(key)
                        # Tag with file year when event name lacks a year (e.g. 2021 Masters)
                        if file_year and not re.search(r'20[2-9]\d', match.get("event", "")):
                            match = dict(match, _year=file_year)
                        merged[team_id].append(match)
    _team_matches_cache = dict(merged)
    return _team_matches_cache

_player_team_stats_cache: dict | None = None

def load_player_team_stats():
    global _player_team_stats_cache
    if _player_team_stats_cache is not None:
        return _player_team_stats_cache
    merged = {}
    paths = sorted(glob.glob("data/player_team_stats_*.json"))
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            for pid, teams in data.items():
                if pid not in merged:
                    merged[pid] = {}
                for tid, rec in teams.items():
                    if tid not in merged[pid]:
                        merged[pid][tid] = rec
                    else:
                        # Merge: accumulate maps/wins/losses, re-average stats
                        existing = merged[pid][tid]
                        new_maps = existing["maps"] + rec["maps"]
                        if new_maps == 0:
                            continue
                        sm_e = existing.get("stat_maps", existing["maps"])
                        sm_n = rec.get("stat_maps", rec["maps"])
                        sm   = sm_e + sm_n
                        def _merge_avg(fe, fn):
                            if fe is None and fn is None: return None
                            fe = fe or 0; fn = fn or 0
                            return round((fe * sm_e + fn * sm_n) / sm, 2) if sm else None
                        merged[pid][tid] = {
                            "team_name": existing["team_name"] or rec["team_name"],
                            "wins":   existing["wins"]   + rec["wins"],
                            "losses": existing["losses"] + rec["losses"],
                            "maps":   new_maps,
                            "rating": _merge_avg(existing.get("rating"), rec.get("rating")),
                            "acs":    round(_merge_avg(existing.get("acs"), rec.get("acs")) or 0) if _merge_avg(existing.get("acs"), rec.get("acs")) is not None else None,
                            "kd":     _merge_avg(existing.get("kd"), rec.get("kd")),
                        }
        except Exception:
            pass
    _player_team_stats_cache = merged
    return _player_team_stats_cache


def _extract_year(s):
    m = re.search(r'20[2-9]\d', s or "")
    return m.group(0) if m else ""

def _match_passes_filters(match, this_team_region, tier_filter, region_filter, year_filter, event_filter=""):
    ev = match.get("event", "")
    if event_filter and ev != event_filter:
        return False
    if tier_filter and _event_tier(ev) != tier_filter:
        return False
    if region_filter:
        r = _event_region(ev)
        if r == "International":
            if this_team_region != region_filter:
                return False
        elif r != region_filter:
            return False
    if year_filter and not event_filter:
        ev_year = _extract_year(ev) or match.get("_year", "")
        if ev_year != year_filter:
            return False
    return True

@app.route("/")
def index():
    df = load_df()
    tiers   = sorted(df["Tier"].dropna().unique().tolist()) if not df.empty else []
    regions = sorted(df["Region"].dropna().unique().tolist()) if not df.empty and "Region" in df.columns else []
    return render_template("index.html",
                           has_data=not load_df().empty,
                           tiers=tiers, regions=regions)


_player_team_fullname_cache = None

def _player_team_fullname_map():
    """Build player_name_lower → full_team_name from team_matches + team_stats."""
    global _player_team_fullname_cache
    if _player_team_fullname_cache is not None:
        return _player_team_fullname_cache
    idx        = load_team_matches()
    team_stats = load_team_stats()
    team_names = {t["TeamID"]: t["Name"] for t in team_stats}
    result     = {}
    for team_id, matches in idx.items():
        tname = team_names.get(team_id, "")
        if not tname:
            continue
        for match in matches:
            for mp in match.get("maps", []):
                for p in mp.get("players_us", []):
                    name = (p.get("name") or "").strip()
                    if name:
                        result[name.lower()] = tname
    _player_team_fullname_cache = result
    return result

def _get_merged_df():
    """Return the cross-year merged player dataframe, computed once and cached.

    Stats are computed from per-event JSON data (player_events_YYYY.json), filtering
    each event through _is_intl_event() so only tier-appropriate events count.
    This prevents T2 rounds from inflating T1 players' stats (e.g. Leo 2022 had
    Masters Copenhagen + EMEA Challengers aggregated in the same CSV row).
    """
    global _df_merged_cache
    if _df_merged_cache is not None:
        return _df_merged_cache

    df = load_df()
    if df.empty or "PlayerID" not in df.columns:
        _df_merged_cache = df
        return _df_merged_cache

    WEIGHTED = ["Rating", "ACS", "KD", "ADR", "KPR", "APR", "FKPR", "FDPR"]
    PCT      = ["KAST", "HS%"]

    player_evs = load_player_events()
    tm_career  = _true_tier_map(year=None)

    year_col = "Year" if "Year" in df.columns else None
    merged_rows = []
    for pid, grp in df.groupby("PlayerID", sort=False):
        if year_col:
            grp = grp.sort_values(year_col, ascending=False)
        base = grp.iloc[0].to_dict()

        career_tier = tm_career.get(str(pid), base.get("Tier", ""))

        # Filter per-event JSON to only the events matching this player's career tier.
        # Per-year CSV rows aggregate ALL events (T1 + T2) in a year; using the JSON
        # per-event gives accurate tier-only round counts and weighted averages.
        ev_list = player_evs.get(str(pid), [])
        if career_tier == "Tier 1":
            tier_evs = [e for e in ev_list if _is_intl_event(e.get("Event", ""))]
        elif career_tier == "Game Changers":
            tier_evs = [e for e in ev_list if "game changers" in e.get("Event", "").lower()]
        else:
            tier_evs = [e for e in ev_list
                        if not _is_intl_event(e.get("Event", ""))
                        and "game changers" not in e.get("Event", "").lower()]

        if tier_evs:
            rounds_list = []
            for e in tier_evs:
                try:
                    rounds_list.append(int(e.get("Rounds", 0)))
                except (ValueError, TypeError):
                    rounds_list.append(0)
            total_rounds = sum(rounds_list)
            base["Rounds"] = total_rounds
            base["Events"] = len(tier_evs)

            if total_rounds > 0:
                for stat in WEIGHTED:
                    vals, ws = [], []
                    for e, r in zip(tier_evs, rounds_list):
                        if r == 0:
                            continue
                        v = e.get(stat)
                        if v is not None:
                            try:
                                vals.append(float(v))
                                ws.append(r)
                            except (ValueError, TypeError):
                                pass
                    if vals:
                        base[stat] = round(sum(v * w for v, w in zip(vals, ws)) / sum(ws), 2)

                for stat in PCT:
                    vals, ws = [], []
                    for e, r in zip(tier_evs, rounds_list):
                        if r == 0:
                            continue
                        v = str(e.get(stat, "")).replace("%", "").strip()
                        if v:
                            try:
                                vals.append(float(v))
                                ws.append(r)
                            except (ValueError, TypeError):
                                pass
                    if vals:
                        avg = round(sum(v * w for v, w in zip(vals, ws)) / sum(ws), 1)
                        base[stat] = f"{avg}%"
        else:
            # Fallback to CSV-based calculation when no per-event data exists
            if career_tier and "Tier" in grp.columns:
                tier_grp = grp[grp["Tier"] == career_tier]
                if not tier_grp.empty:
                    grp = tier_grp
            rounds_num = pd.to_numeric(grp["Rounds"], errors="coerce").fillna(0)
            events_num = pd.to_numeric(grp["Events"], errors="coerce").fillna(0)
            base["Events"] = int(events_num.sum())
            base["Rounds"] = int(rounds_num.sum())
            total_w = rounds_num.sum()
            if total_w > 0:
                for stat in WEIGHTED:
                    if stat not in grp.columns:
                        continue
                    vals = pd.to_numeric(grp[stat], errors="coerce")
                    ok = ~vals.isna()
                    if ok.any():
                        base[stat] = round((vals[ok] * rounds_num[ok]).sum() / rounds_num[ok].sum(), 2)
                for stat in PCT:
                    if stat not in grp.columns:
                        continue
                    vals = pd.to_numeric(
                        grp[stat].astype(str).str.replace("%", "").str.strip(), errors="coerce"
                    )
                    ok = ~vals.isna()
                    if ok.any():
                        avg = round((vals[ok] * rounds_num[ok]).sum() / rounds_num[ok].sum(), 1)
                        base[stat] = f"{avg}%"

        merged_rows.append(base)

    result = pd.DataFrame(merged_rows)
    for col in PCT_COLS:
        if col in result.columns:
            result[f"_sort_{col}"] = pd.to_numeric(
                result[col].astype(str).str.replace("%", "").str.strip(), errors="coerce"
            )
    if "PlayerID" in result.columns:
        tm = _true_tier_map(year=None)
        result["Tier"] = result["PlayerID"].astype(str).map(tm).fillna(result["Tier"])
    _df_merged_cache = result
    return _df_merged_cache


def _build_event_df(event_name):
    """Return a DataFrame of players who played in event_name, using per-event JSON stats."""
    evs     = load_player_events()
    df_base = load_df()

    # Build pid → most-recent CSV row dict for player metadata (name, team, region, etc.)
    meta_dict = {}
    if not df_base.empty and "PlayerID" in df_base.columns:
        year_col = "Year" if "Year" in df_base.columns else None
        grp_src  = df_base.sort_values(year_col, ascending=False) if year_col else df_base
        for pid, grp in grp_src.groupby("PlayerID", sort=False):
            meta_dict[str(pid)] = grp.iloc[0].to_dict()

    rows = []
    for pid, ev_list in evs.items():
        ev = next((e for e in ev_list if e.get("Event") == event_name), None)
        if not ev:
            continue
        base = dict(meta_dict.get(pid, {}))
        base["PlayerID"] = pid
        base["Events"]   = 1
        for col in ("Rating", "ACS", "KD", "ADR", "KPR", "APR", "FKPR", "FDPR"):
            v = ev.get(col)
            if v is not None:
                try:
                    base[col] = float(v)
                except (ValueError, TypeError):
                    pass
        for col in ("KAST", "HS%"):
            if ev.get(col) is not None:
                base[col] = ev[col]
        try:
            base["Rounds"] = int(ev.get("Rounds", 0))
        except (ValueError, TypeError):
            base["Rounds"] = 0
        if ev.get("Team"):
            base["Team"]     = ev["Team"]
            base["TeamFull"] = ev["Team"]
        if ev.get("Region"):
            base["Region"] = ev["Region"]
        rows.append(base)

    if not rows:
        return pd.DataFrame()

    result = pd.DataFrame(rows)
    for col in PCT_COLS:
        if col in result.columns:
            result[f"_sort_{col}"] = pd.to_numeric(
                result[col].astype(str).str.replace("%", "").str.strip(), errors="coerce"
            )
    if "PlayerID" in result.columns:
        tm = _true_tier_map(year=None)
        result["Tier"] = result["PlayerID"].astype(str).map(tm).fillna(result.get("Tier", ""))
    return result


@app.route("/api/players")
def api_players():
    try:
        q        = request.args.get("q", "").strip().lower()
        tier     = request.args.get("tier", "").strip()
        region   = request.args.get("region", "").strip()
        year     = request.args.get("year", "").strip()
        event    = request.args.get("event", "").strip()
        sort     = request.args.get("sort", "Rating").strip()
        asc      = request.args.get("dir", "desc") == "asc"
        try:    page     = max(1, int(request.args.get("page", 1)))
        except ValueError: page = 1
        try:    per_page = min(200, max(10, int(request.args.get("per_page", DEFAULT_PAGE_SIZE))))
        except ValueError: per_page = DEFAULT_PAGE_SIZE

        # Event filter: build DataFrame from per-event JSON (only players in that event)
        # Year filter: use per-year CSV rows
        # Neither: use cross-year merged view
        if event:
            df = _build_event_df(event)
        elif year:
            df = load_df()
        else:
            df = _get_merged_df()
        if df.empty:
            return jsonify({"players": [], "total": 0, "page": 1, "pages": 0, "per_page": DEFAULT_PAGE_SIZE})

        if tier and "Tier" in df.columns:
            df = df[df["Tier"] == tier]
        if region and "Region" in df.columns:
            df = df[df["Region"] == region]
        if year and not event and "Year" in df.columns:
            df = df[df["Year"] == year]
        if q:
            mask = pd.Series(False, index=df.index)
            for col in ("Player", "Team", "TeamFull"):
                if col in df.columns:
                    mask |= df[col].astype(str).str.lower().str.contains(q, na=False, regex=False)
            try:
                ptmap = _player_team_fullname_map()
                matched = {n for n, t in ptmap.items() if q in t.lower()}
                if matched and "Player" in df.columns:
                    mask |= df["Player"].astype(str).str.lower().isin(matched)
            except Exception:
                pass
            df = df[mask]

        sort_col = f"_sort_{sort}" if sort in PCT_COLS and f"_sort_{sort}" in df.columns else sort
        if sort_col in df.columns:
            df = df.sort_values(sort_col, ascending=asc, na_position="last")

        total = len(df)
        pages = math.ceil(total / per_page) if total > 0 else 0
        page  = min(page, max(pages, 1))
        start = (page - 1) * per_page
        df_page = df.iloc[start : start + per_page].copy()

        drop = [c for c in df_page.columns if c.startswith("_sort_")]
        df_page.drop(columns=drop, errors="ignore", inplace=True)

        full_df = load_df()
        years   = sorted(full_df["Year"].dropna().unique().tolist(), reverse=True) if "Year" in full_df.columns else []
        players = json.loads(df_page.to_json(orient="records"))
        return jsonify({"players": players, "total": total, "page": page,
                        "pages": pages, "per_page": per_page, "years": years})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e), "players": [], "total": 0,
                        "page": 1, "pages": 0, "per_page": DEFAULT_PAGE_SIZE}), 500

@app.route("/api/player/<path:player_id>/events")
def player_events(player_id):
    try:
        idx = load_player_events()
        return jsonify(idx.get(player_id, []))
    except Exception as e:
        print(f"[api/player/events error] {e}")
        return jsonify([]), 500

_teams_dynamic_cache = {}   # (year, tier, region) → list

def _compute_team_stats_dynamic(tier_filter="", region_filter="", year_filter="", event_filter="", intl_only=True):
    """Aggregate team stats from team_matches.json — used when a year filter is active."""
    idx         = load_team_matches()
    team_stats  = load_team_stats()
    team_info   = {t["TeamID"]: {"Name": t["Name"], "Region": t["Region"]} for t in team_stats}
    true_regions = _get_all_team_regions()

    agg = {}
    for raw_team_id, matches in idx.items():
        # Remap merged source IDs (e.g. ULF → Eternal Fire) so stats are combined
        team_id = _team_id_merges.get(raw_team_id, raw_team_id)
        this_region = true_regions.get(team_id) or true_regions.get(raw_team_id) or ""
        for match in matches:
            if not _match_passes_filters(match, this_region, tier_filter, region_filter, year_filter, event_filter):
                continue
            if intl_only and not _is_intl_event(match.get("event", "")):
                continue
            if team_id not in agg:
                info = team_info.get(team_id, {})
                agg[team_id] = {
                    "TeamID": team_id,
                    "Name":   info.get("Name", team_id),
                    "Region": this_region or info.get("Region", ""),
                    "SeriesWins": 0, "SeriesLosses": 0,
                    "MapsWon": 0, "MapsLost": 0,
                    "_matches": [],
                }
            d  = agg[team_id]
            mr = match.get("result", "")
            if mr == "W":   d["SeriesWins"]   += 1
            elif mr == "L": d["SeriesLosses"] += 1
            for mp in match.get("maps", []):
                r = mp.get("result", "")
                if r == "W":   d["MapsWon"]  += 1
                elif r == "L": d["MapsLost"] += 1
            d["_matches"].append(match)

    result = []
    for tid, d in agg.items():
        sw, sl = d["SeriesWins"], d["SeriesLosses"]
        mw, ml = d["MapsWon"], d["MapsLost"]
        mp = mw + ml
        total = sw + sl
        def _mid(m):
            nm = re.search(r'/(\d+)/', m.get("url", ""))
            return int(nm.group(1)) if nm else 0
        recent = [m["result"] for m in sorted(d["_matches"], key=_mid, reverse=True)[:5]]
        result.append({
            "TeamID":       tid,
            "Name":         d["Name"],
            "Region":       d["Region"],
            "SeriesWins":   sw, "SeriesLosses": sl, "SeriesPlayed": total,
            "SeriesWinPct": round(sw / total * 100, 1) if total else 0,
            "MapsWon":      mw, "MapsLost": ml, "MapsPlayed": mp,
            "MapWinPct":    round(mw / mp * 100, 1) if mp else 0,
            "RecentForm":   recent,
        })
    result.sort(key=lambda t: t["SeriesWinPct"], reverse=True)
    return result

@app.route("/api/teams")
def api_teams():
    try:
        global _teams_dynamic_cache
        q      = request.args.get("q",      "").strip().lower()
        region = request.args.get("region", "").strip()
        year   = request.args.get("year",   "").strip()
        event  = request.args.get("event",  "").strip()
        sort   = request.args.get("sort", "SeriesWinPct").strip()
        asc    = request.args.get("dir", "desc") == "asc"
        try:    page     = max(1, int(request.args.get("page", 1)))
        except ValueError: page = 1
        try:    per_page = min(200, max(10, int(request.args.get("per_page", DEFAULT_PAGE_SIZE))))
        except ValueError: per_page = DEFAULT_PAGE_SIZE

        if event or year:
            cache_key = (year, "", region, event)
            if cache_key not in _teams_dynamic_cache:
                _teams_dynamic_cache[cache_key] = _compute_team_stats_dynamic(
                    tier_filter="", region_filter=region, year_filter=year, event_filter=event,
                    intl_only=not bool(event))
            teams = _teams_dynamic_cache[cache_key]
        else:
            teams = load_team_stats()

        if not teams:
            return jsonify({"teams": [], "total": 0, "page": 1, "pages": 0,
                            "per_page": DEFAULT_PAGE_SIZE, "regions": [], "years": []})

        filtered = teams
        # Only show teams that have appeared in at least one T1 event
        t1_ids = _get_t1_team_ids()
        filtered = [t for t in filtered if t.get("TeamID") in t1_ids]
        if region and not year:   # year path already filters by region
            _tr_early = _get_all_team_regions()
            filtered = [t for t in filtered
                        if (_tr_early.get(t.get("TeamID", "")) or t.get("Region", "")) == region]
        if q:
            filtered = [t for t in filtered if q in t.get("Name", "").lower()]

        numeric_sort = {"SeriesWinPct", "MapWinPct", "SeriesWins", "SeriesLosses",
                        "SeriesPlayed", "MapsWon", "MapsLost", "MapsPlayed"}
        if sort in numeric_sort:
            filtered = sorted(filtered, key=lambda t: t.get(sort) or 0, reverse=not asc)
        else:
            filtered = sorted(filtered, key=lambda t: t.get(sort, "") or "", reverse=not asc)

        all_teams_base = [t for t in load_team_stats() if t.get("TeamID") in t1_ids]
        _tr = _get_all_team_regions()
        regions = sorted({_tr.get(t.get("TeamID", ""), t.get("Region", "")) for t in all_teams_base
                          if _tr.get(t.get("TeamID", ""), t.get("Region", ""))})

        # Available years from match data
        idx   = load_team_matches()
        years_set = set()
        for matches in idx.values():
            for m in matches:
                y = _extract_year(m.get("event", ""))
                if y: years_set.add(y)
        years_list = sorted(years_set, reverse=True)

        total  = len(filtered)
        pages  = math.ceil(total / per_page) if total > 0 else 0
        page   = min(page, max(pages, 1))
        start  = (page - 1) * per_page
        page_teams = [dict(t) for t in filtered[start : start + per_page]]

        # Override stored region with home-region derived from match history
        true_regions = _get_all_team_regions()
        for t in page_teams:
            tr = true_regions.get(t.get("TeamID", ""))
            if tr:
                t["Region"] = tr

        if not year:
            for t in page_teams:
                matches = idx.get(t.get("TeamID", ""), [])
                def _mk(m):
                    nm = re.search(r'/(\d+)/', m.get("url", ""))
                    return int(nm.group(1)) if nm else 0
                t["RecentForm"] = [m["result"] for m in sorted(matches, key=_mk, reverse=True)[:5]]

        return jsonify({"teams": page_teams, "total": total, "page": page,
                        "pages": pages, "per_page": per_page,
                        "regions": regions, "years": years_list})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e), "teams": [], "total": 0,
                        "page": 1, "pages": 0, "per_page": DEFAULT_PAGE_SIZE,
                        "regions": []}), 500

@app.route("/api/team/<path:team_id>/matches")
def api_team_matches(team_id):
    try:
        load_team_stats()  # ensure _team_id_merges is populated
        idx     = load_team_matches()
        matches = list(idx.get(team_id, []))
        for src_id, tgt_id in _team_id_merges.items():
            if tgt_id == team_id:
                matches.extend(idx.get(src_id, []))
        return jsonify(matches)
    except Exception as e:
        print(f"[api/team/matches error] {e}")
        return jsonify([]), 500

def _blank_stat_bucket():
    return {
        "picks": 0, "wins": 0,
        "ratings": [], "acss": [], "adrs": [], "kds": [], "kasts": [],
        "hs_kills": 0.0, "total_kills": 0.0,
    }

def _fill_bucket(d, p, map_result):
    d["picks"] += 1
    if map_result == "W":
        d["wins"] += 1
    try: d["ratings"].append(float(p["rating"]))
    except: pass
    try: d["acss"].append(float(p["acs"]))
    except: pass
    try: d["adrs"].append(float(p.get("adr", "")))
    except: pass
    try: d["kds"].append(float(p["k"]) / max(float(p["d"]), 1))
    except: pass
    try: d["kasts"].append(float(str(p.get("kast", "")).replace("%", "")))
    except: pass
    try:
        kills  = float(p["k"])
        hs_pct = float(str(p.get("hs", "")).replace("%", ""))
        d["hs_kills"]    += hs_pct / 100.0 * kills
        d["total_kills"] += kills
    except: pass

def _bucket_to_stats(d):
    picks = d["picks"]
    wins  = d["wins"]
    hs = (round(d["hs_kills"] / d["total_kills"] * 100, 1)
          if d["total_kills"] > 0 else None)
    return {
        "picks":  picks,
        "wins":   wins,
        "losses": picks - wins,
        "winpct": round(wins / picks * 100, 1) if picks else 0,
        "rating": round(sum(d["ratings"]) / len(d["ratings"]), 2) if d["ratings"] else None,
        "acs":    round(sum(d["acss"]) / len(d["acss"])) if d["acss"] else None,
        "kd":     round(sum(d["kds"]) / len(d["kds"]), 2) if d["kds"] else None,
        "kast":   f"{round(sum(d['kasts']) / len(d['kasts']), 1)}%" if d["kasts"] else None,
        "hs":     f"{hs}%" if hs is not None else None,
        "adr":    round(sum(d["adrs"]) / len(d["adrs"])) if d["adrs"] else None,
    }

def _event_tier(title):
    t = title.lower()
    if t.startswith("vct ") or t.startswith("valorant masters") or t.startswith("valorant champions"):
        return "Tier 1"
    if t.startswith("challengers "):
        return "Tier 2"
    if t.startswith("game changers "):
        return "Game Changers"
    return "Other"

_REGION_PATTERNS = [
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

def _event_region(title):
    padded = " " + title.lower() + " "
    for region, keywords in _REGION_PATTERNS:
        if any(kw in padded for kw in keywords):
            return region
    return "Other"

def _compute_agents(tier_filter="", region_filter="", year_filter="", event_filter=""):
    idx         = load_team_matches()
    team_stats  = load_team_stats()
    team_region = {t["TeamID"]: t.get("Region", "") for t in team_stats}
    team_names  = {t["TeamID"]: t["Name"] for t in team_stats}

    agg         = defaultdict(_blank_stat_bucket)
    player_agg  = defaultdict(lambda: defaultdict(_blank_stat_bucket))
    player_team = {}   # name → most recently seen team name
    player_tier = {}   # name → highest tier seen across their events
    _tier_rank  = {"Tier 1": 3, "Game Changers": 2, "Tier 2": 1}

    for team_id, matches in idx.items():
        this_team_region = team_region.get(team_id, "")
        tname = team_names.get(team_id, "")
        for match in matches:
            if not _match_passes_filters(match, this_team_region, tier_filter, region_filter, year_filter, event_filter):
                continue
            ev_tier = _event_tier(match.get("event", ""))
            for mp in match.get("maps", []):
                map_result = mp.get("result", "")
                for p in mp.get("players_us", []):
                    if not isinstance(p, dict):
                        continue
                    agent = (p.get("agent") or "").strip().title()
                    name  = (p.get("name")  or "").strip()
                    if not agent or agent in ("-", "Undefined") or not name:
                        continue
                    _fill_bucket(agg[agent], p, map_result)
                    _fill_bucket(player_agg[agent][name], p, map_result)
                    if tname:
                        player_team[name] = tname
                    if ev_tier and _tier_rank.get(ev_tier, 0) > _tier_rank.get(player_tier.get(name, ""), 0):
                        player_tier[name] = ev_tier

    result = []
    for agent_name, d in agg.items():
        stats = _bucket_to_stats(d)
        players = []
        for pname, pd in player_agg[agent_name].items():
            ps = _bucket_to_stats(pd)
            ps["name"] = pname
            ps["team"] = player_team.get(pname, "")
            ps["tier"] = player_tier.get(pname, "")
            players.append(ps)
        players.sort(key=lambda p: p["picks"], reverse=True)
        result.append({"agent": agent_name, "players": players, **stats})

    result.sort(key=lambda a: a["picks"], reverse=True)
    return result

@app.route("/api/agents/filters")
def api_agents_filters():
    try:
        idx     = load_team_matches()
        regions = set()
        years   = set()
        for matches in idx.values():
            for match in matches:
                title = match.get("event", "")
                r = _event_region(title)
                y = _extract_year(title)
                if r and r not in ("Other", "International"): regions.add(r)
                if y: years.add(y)
        return jsonify({"regions": sorted(regions), "years": sorted(years, reverse=True)})
    except Exception as e:
        return jsonify({"regions": [], "years": []}), 500

@app.route("/api/agents")
def api_agents():
    global _agents_cache
    try:
        tier   = request.args.get("tier",   "").strip()
        region = request.args.get("region", "").strip()
        year   = request.args.get("year",   "").strip()
        event  = request.args.get("event",  "").strip()
        key    = (tier, region, year, event)
        if key not in _agents_cache:
            _agents_cache[key] = _compute_agents(tier, region, year, event)
        return jsonify({"agents": _agents_cache[key]})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e), "agents": []}), 500

def _blank_map_bucket():
    return {
        "played":   0,
        "picks":    0,
        "bans":     0,
        "deciders": 0,
        "agents":   defaultdict(_blank_stat_bucket),
        "players":  defaultdict(_blank_stat_bucket),
        "comps":    defaultdict(lambda: {"count": 0, "wins": 0}),
    }

def _compute_maps(tier_filter="", region_filter="", year_filter="", event_filter=""):
    idx         = load_team_matches()
    team_stats  = load_team_stats()
    team_region = {t["TeamID"]: t.get("Region", "") for t in team_stats}
    team_names  = {t["TeamID"]: t["Name"] for t in team_stats}

    map_agg         = {}
    seen_plays      = set()   # (url, map_name) — dedup physical map plays
    seen_series     = set()   # url — dedup veto/series counting
    total_series    = 0
    map_team_stats  = defaultdict(lambda: defaultdict(lambda: {"wins": 0, "played": 0, "name": ""}))
    seen_team_plays = set()   # (url, map_name, team_id) — dedup per-team map count
    player_team     = {}      # name → most recently seen team name

    for team_id, matches in idx.items():
        this_team_region = team_region.get(team_id, "")
        tname = team_names.get(team_id, "")
        for match in matches:
            url = match.get("url", "")
            if not _match_passes_filters(match, this_team_region, tier_filter, region_filter, year_filter, event_filter):
                continue

            # Veto data — count once per series
            if url and url not in seen_series:
                seen_series.add(url)
                total_series += 1
                for v in match.get("veto", []):
                    mname  = (v.get("map") or "").strip()
                    action = v.get("action", "")
                    if not mname or mname == "-" or mname.lower() in ("undefined", "tbd"):
                        continue
                    if mname not in map_agg:
                        map_agg[mname] = _blank_map_bucket()
                    if action == "pick":
                        map_agg[mname]["picks"] += 1
                    elif action == "ban":
                        map_agg[mname]["bans"] += 1
                    elif action == "decider":
                        map_agg[mname]["deciders"] += 1

            # Map plays
            for mp in match.get("maps", []):
                mname = (mp.get("map") or "").strip()
                if not mname or mname == "-" or mname.lower() in ("undefined", "tbd"):
                    continue
                if mname not in map_agg:
                    map_agg[mname] = _blank_map_bucket()

                map_result = mp.get("result", "")
                play_key   = (url, mname)
                if play_key not in seen_plays:
                    seen_plays.add(play_key)
                    map_agg[mname]["played"] += 1

                # Per-team map tracking for best_teams
                if team_id:
                    team_play_key = (url, mname, team_id)
                    if team_play_key not in seen_team_plays:
                        seen_team_plays.add(team_play_key)
                        td = map_team_stats[mname][team_id]
                        td["played"] += 1
                        td["name"]    = tname
                        if map_result == "W":
                            td["wins"] += 1

                # Stats from our team's players only — no double-counting
                players_us = mp.get("players_us", [])
                for p in players_us:
                    if not isinstance(p, dict):
                        continue
                    agent = (p.get("agent") or "").strip().title()
                    name  = (p.get("name")  or "").strip()
                    if not agent or agent in ("-", "Undefined"):
                        continue
                    _fill_bucket(map_agg[mname]["agents"][agent], p, map_result)
                    if name:
                        _fill_bucket(map_agg[mname]["players"][name], p, map_result)
                        if tname:
                            player_team[name] = tname

                # 5-agent composition key
                agents_in_map = [
                    (p.get("agent") or "").strip().title()
                    for p in players_us
                    if isinstance(p, dict) and (p.get("agent") or "").strip()
                    and p.get("agent") not in ("-", "undefined", "Undefined")
                ]
                if len(agents_in_map) == 5:
                    comp = tuple(sorted(agents_in_map))
                    map_agg[mname]["comps"][comp]["count"] += 1
                    if map_result == "W":
                        map_agg[mname]["comps"][comp]["wins"] += 1

    result = []
    for mname, d in map_agg.items():
        agents_list = []
        for aname, abucket in d["agents"].items():
            s = _bucket_to_stats(abucket)
            s["agent"] = aname
            agents_list.append(s)
        agents_list.sort(key=lambda a: a["picks"], reverse=True)

        players_list = []
        for pname, pbucket in d["players"].items():
            s = _bucket_to_stats(pbucket)
            s["name"] = pname
            s["team"] = player_team.get(pname, "")
            players_list.append(s)
        players_list.sort(key=lambda p: p["picks"], reverse=True)
        players_list = players_list[:30]

        comps_list = sorted(
            [
                {
                    "agents": list(comp),
                    "count":  cd["count"],
                    "wins":   cd["wins"],
                    "losses": cd["count"] - cd["wins"],
                    "winpct": round(cd["wins"] / cd["count"] * 100, 1) if cd["count"] else 0,
                }
                for comp, cd in d["comps"].items()
            ],
            key=lambda c: c["count"], reverse=True
        )[:10]

        # Best teams on this map (min 3 plays, top 10 by win%)
        best_teams = []
        for tid, td in map_team_stats.get(mname, {}).items():
            played = td["played"]
            if played < 3:
                continue
            wins = td["wins"]
            best_teams.append({
                "name":   td["name"],
                "played": played,
                "wins":   wins,
                "losses": played - wins,
                "winpct": round(wins / played * 100, 1),
            })
        best_teams.sort(key=lambda t: (-t["winpct"], -t["played"]))
        best_teams = best_teams[:10]

        result.append({
            "map":        mname,
            "played":     d["played"],
            "picks":      d["picks"],
            "bans":       d["bans"],
            "deciders":   d["deciders"],
            "pickpct":    round(d["picks"] / total_series * 100, 1) if total_series else None,
            "banpct":     round(d["bans"]  / total_series * 100, 1) if total_series else None,
            "agents":     agents_list,
            "players":    players_list,
            "comps":      comps_list,
            "best_teams": best_teams,
        })

    result.sort(key=lambda m: m["played"], reverse=True)
    return result

@app.route("/api/maps")
def api_maps():
    global _maps_cache
    try:
        tier   = request.args.get("tier",   "").strip()
        region = request.args.get("region", "").strip()
        year   = request.args.get("year",   "").strip()
        event  = request.args.get("event",  "").strip()
        key    = (tier, region, year, event)
        if key not in _maps_cache:
            _maps_cache[key] = _compute_maps(tier, region, year, event)
        return jsonify({"maps": _maps_cache[key]})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e), "maps": []}), 500

def _aggregate_players_from_matches(team_matches):
    """Aggregate per-player season stats from a team's full match history."""
    from collections import defaultdict
    pmap = defaultdict(lambda: {
        "maps": 0, "ratings": [], "acss": [], "ks": 0, "ds": 0, "avs": 0,
        "hss": [], "adrs": [], "agents": defaultdict(int),
    })
    for match in team_matches:
        for mp in match.get("maps", []):
            for p in mp.get("players_us", []):
                if not isinstance(p, dict):
                    continue
                name = (p.get("name") or "").strip()
                if not name or name == "-":
                    continue
                d = pmap[name]
                d["maps"] += 1
                if p.get("agent"):
                    d["agents"][p["agent"]] += 1
                try: d["ratings"].append(float(p["rating"]))
                except: pass
                try: d["acss"].append(float(p["acs"]))
                except: pass
                try: d["ks"] += int(p["k"])
                except: pass
                try: d["ds"] += int(p["d"])
                except: pass
                try: d["avs"] += int(p["a"])
                except: pass
                try: d["hss"].append(float(str(p.get("hs", "")).replace("%", "")))
                except: pass
                try: d["adrs"].append(float(p.get("adr", "")))
                except: pass

    result = []
    for name, d in pmap.items():
        r   = sum(d["ratings"]) / len(d["ratings"]) if d["ratings"] else None
        acs = sum(d["acss"])    / len(d["acss"])    if d["acss"]    else None
        hs  = sum(d["hss"])     / len(d["hss"])     if d["hss"]     else None
        adr = sum(d["adrs"])    / len(d["adrs"])    if d["adrs"]    else None
        top_agents = sorted(d["agents"].items(), key=lambda x: -x[1])
        result.append({
            "name":   name,
            "agents": " / ".join(a for a, _ in top_agents[:3]),
            "maps":   d["maps"],
            "rating": round(r,   2) if r   is not None else None,
            "acs":    round(acs)    if acs is not None else None,
            "kda":    f"{d['ks']}/{d['ds']}/{d['avs']}",
            "hs":     f"{round(hs, 1)}%" if hs is not None else None,
            "adr":    round(adr)    if adr is not None else None,
        })
    return sorted(result, key=lambda p: (p["rating"] or -999), reverse=True)


@app.route("/api/player/profile")
def api_player_profile():
    try:
        name          = request.args.get("name",   "").strip()
        pid           = request.args.get("pid",    "").strip()
        tier_filter   = request.args.get("tier",   "").strip()
        region_filter = request.args.get("region", "").strip()
        if not name:
            return jsonify({"error": "no name"}), 400

        idx         = load_team_matches()
        team_stats  = load_team_stats()
        team_region = {t["TeamID"]: t.get("Region", "") for t in team_stats}
        team_names  = {t["TeamID"]: t["Name"] for t in team_stats}

        # If a player ID is known, restrict matches to events that specific player
        # participated in — prevents name collisions between players sharing an IGN.
        pid_event_filter: set | None = None
        if pid:
            evs = load_player_events()
            pid_event_filter = {ev["Event"] for ev in evs.get(pid, [])}

        overall  = _blank_stat_bucket()
        by_agent = defaultdict(_blank_stat_bucket)
        by_map   = defaultdict(_blank_stat_bucket)
        # team_id → {name, bucket, years}  — built WITHOUT pid_event_filter so
        # past T2 teams are included (team_id itself disambiguates name collisions)
        by_team  = {}

        for team_id, matches in idx.items():
            canonical_id     = _team_id_merges.get(team_id, team_id) if _team_id_merges else team_id
            this_team_region = team_region.get(team_id, "")
            tname            = team_names.get(team_id, "") or team_names.get(canonical_id, "")
            for match in matches:
                event_title = match.get("event", "")
                ev_tier     = _event_tier(event_title)
                ev_region   = _event_region(event_title)
                ev_year     = _extract_year(event_title) or "0000"

                if tier_filter and ev_tier != tier_filter:
                    continue
                if region_filter:
                    if ev_region == "International":
                        if this_team_region != region_filter:
                            continue
                    else:
                        if ev_region != region_filter:
                            continue

                # pid_event_filter applied only to overall/agent/map — not by_team
                passes_pid = pid_event_filter is None or event_title in pid_event_filter

                for mp in match.get("maps", []):
                    map_result = mp.get("result", "")
                    mname      = (mp.get("map") or "").strip()
                    for p in mp.get("players_us", []):
                        if not isinstance(p, dict):
                            continue
                        pname = (p.get("name") or "").strip()
                        if pname.lower() != name.lower():
                            continue
                        agent = (p.get("agent") or "").strip().title()
                        if passes_pid:
                            _fill_bucket(overall, p, map_result)
                            if agent and agent != "-":
                                _fill_bucket(by_agent[agent], p, map_result)
                            if mname and mname != "-":
                                _fill_bucket(by_map[mname], p, map_result)
                        if tname:
                            if canonical_id not in by_team:
                                by_team[canonical_id] = {
                                    "name":   tname,
                                    "bucket": _blank_stat_bucket(),
                                    "years":  set(),
                                }
                            _fill_bucket(by_team[canonical_id]["bucket"], p, map_result)
                            by_team[canonical_id]["years"].add(ev_year)

        agents_list = []
        for aname, b in by_agent.items():
            s = _bucket_to_stats(b); s["agent"] = aname; agents_list.append(s)
        agents_list.sort(key=lambda a: a["picks"], reverse=True)

        maps_list = []
        for mname, b in by_map.items():
            s = _bucket_to_stats(b); s["map"] = mname; maps_list.append(s)
        maps_list.sort(key=lambda m: m["picks"], reverse=True)

        team_history = []
        for tid, td in by_team.items():
            s = _bucket_to_stats(td["bucket"])
            years = sorted(td["years"])
            s["team_id"]   = tid
            s["team_name"] = td["name"]
            s["year_from"] = years[0]  if years else ""
            s["year_to"]   = years[-1] if years else ""
            team_history.append(s)
        team_history.sort(key=lambda t: t["year_to"], reverse=True)

        return jsonify({
            "name":         name,
            "overall":      _bucket_to_stats(overall),
            "agents":       agents_list,
            "maps":         maps_list,
            "team_history": team_history,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/h2h")
def api_h2h():
    try:
        t1   = request.args.get("t1",   "").strip()
        t2   = request.args.get("t2",   "").strip()
        year = request.args.get("year", "").strip()
        if not t1 or not t2:
            return jsonify({"matches": [], "t1_name": "", "t2_name": "",
                            "series_wins": 0, "series_losses": 0,
                            "maps_won": 0, "maps_lost": 0,
                            "t1_maps": {}, "t2_maps": {},
                            "t1_players": [], "t2_players": []})

        idx   = load_team_matches()
        teams = load_team_stats()
        team_lookup = {t["TeamID"]: t for t in teams}

        t1_data = team_lookup.get(t1, {})
        t2_data = team_lookup.get(t2, {})

        def _match_year(m):
            y = _extract_year(m.get("event", "")) or m.get("_year", "")
            return y

        all_t1 = idx.get(t1, [])
        all_t2 = idx.get(t2, [])
        if year:
            all_t1 = [m for m in all_t1 if _match_year(m) == year]
            all_t2 = [m for m in all_t2 if _match_year(m) == year]

        h2h = [m for m in all_t1 if m.get("opponent_id") == t2]

        series_wins   = sum(1 for m in h2h if m["result"] == "W")
        series_losses = sum(1 for m in h2h if m["result"] == "L")
        maps_won  = sum(sum(1 for mp in m.get("maps", []) if mp["result"] == "W") for m in h2h)
        maps_lost = sum(sum(1 for mp in m.get("maps", []) if mp["result"] == "L") for m in h2h)

        # Year-specific map pool from match history; fall back to full stats when no year
        def _map_pool_from_matches(matches):
            pool = {}
            for m in matches:
                sp = m.get("score", "")
                for mp in m.get("maps", []):
                    mname = (mp.get("map") or "").strip()
                    if not mname or mname == "-":
                        continue
                    if mname not in pool:
                        pool[mname] = {"Played": 0, "Wins": 0, "Losses": 0,
                                       "Picks": 0, "Bans": 0, "WinPct": 0}
                    pool[mname]["Played"] += 1
                    if mp.get("result") == "W":
                        pool[mname]["Wins"] += 1
                    elif mp.get("result") == "L":
                        pool[mname]["Losses"] += 1
                for v in m.get("veto", []):
                    mname = (v.get("map") or "").strip()
                    if not mname or mname == "-":
                        continue
                    if mname not in pool:
                        pool[mname] = {"Played": 0, "Wins": 0, "Losses": 0,
                                       "Picks": 0, "Bans": 0, "WinPct": 0}
                    if v.get("action") == "pick":
                        pool[mname]["Picks"] += 1
                    elif v.get("action") == "ban":
                        pool[mname]["Bans"] += 1
            for d in pool.values():
                p = d["Played"]
                d["WinPct"] = round(d["Wins"] / p * 100, 1) if p else 0
            return pool

        if year:
            t1_maps = _map_pool_from_matches(all_t1)
            t2_maps = _map_pool_from_matches(all_t2)
        else:
            t1_maps = t1_data.get("Maps", {})
            t2_maps = t2_data.get("Maps", {})

        def _top5(team_matches):
            all_players = _aggregate_players_from_matches(team_matches)
            starters = sorted(all_players, key=lambda p: p["maps"], reverse=True)[:5]
            starters.sort(key=lambda p: p["rating"] or -999, reverse=True)
            return starters

        t1_players = _top5(all_t1)
        t2_players = _top5(all_t2)

        # Available years for this matchup
        all_years = sorted({_match_year(m) for m in idx.get(t1, []) + idx.get(t2, []) if _match_year(m)}, reverse=True)

        return jsonify({
            "t1_name":       t1_data.get("Name", t1),
            "t2_name":       t2_data.get("Name", t2),
            "series_wins":   series_wins,
            "series_losses": series_losses,
            "maps_won":      maps_won,
            "maps_lost":     maps_lost,
            "matches":       h2h,
            "t1_maps":       t1_maps,
            "t2_maps":       t2_maps,
            "t1_players":    t1_players,
            "t2_players":    t2_players,
            "years":         all_years,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e), "matches": []}), 500


@app.route("/export/tier1")
def export_tier1():
    try:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:

            # Players — Tier 1 only, Player column first
            df = load_df()
            if not df.empty and "Tier" in df.columns:
                t1 = df[df["Tier"] == "Tier 1"].copy()
                drop = [c for c in t1.columns if c.startswith("_sort_")] + ["PlayerID"]
                t1.drop(columns=drop, errors="ignore", inplace=True)
                col_order = ["Player", "TeamFull", "Region", "Tier", "Events", "Rounds",
                             "Rating", "ACS", "KD", "KAST", "ADR", "KPR", "APR", "FKPR", "FDPR", "HS%"]
                ordered = [c for c in col_order if c in t1.columns] + \
                          [c for c in t1.columns if c not in col_order]
                t1 = t1[ordered].rename(columns={"TeamFull": "Team"})
                zf.writestr("tier1_players.csv", t1.to_csv(index=False))

            # Teams — Tier 1 aggregated
            tier1_teams = _compute_team_stats_dynamic(tier_filter="Tier 1")
            sio = io.StringIO()
            w = csv_mod.DictWriter(sio, fieldnames=[
                "Name", "Region", "Series W", "Series L", "Series Played",
                "Series Win%", "Maps Won", "Maps Lost", "Maps Played", "Map Win%"])
            w.writeheader()
            for t in tier1_teams:
                w.writerow({
                    "Name": t["Name"], "Region": t["Region"],
                    "Series W": t["SeriesWins"], "Series L": t["SeriesLosses"],
                    "Series Played": t["SeriesPlayed"], "Series Win%": t["SeriesWinPct"],
                    "Maps Won": t["MapsWon"], "Maps Lost": t["MapsLost"],
                    "Maps Played": t["MapsPlayed"], "Map Win%": t["MapWinPct"],
                })
            zf.writestr("tier1_teams.csv", sio.getvalue())

            # Agents — Tier 1
            agents = _compute_agents(tier_filter="Tier 1")
            sio = io.StringIO()
            w = csv_mod.DictWriter(sio, fieldnames=[
                "Agent", "Picks", "Wins", "Losses", "Win%", "Rating", "ACS", "K/D", "KAST", "HS%", "ADR"])
            w.writeheader()
            for a in agents:
                w.writerow({
                    "Agent": a["agent"], "Picks": a["picks"], "Wins": a["wins"],
                    "Losses": a["losses"], "Win%": a["winpct"], "Rating": a["rating"],
                    "ACS": a["acs"], "K/D": a["kd"], "KAST": a["kast"],
                    "HS%": a["hs"], "ADR": a["adr"],
                })
            zf.writestr("tier1_agents.csv", sio.getvalue())

            # Maps — Tier 1
            maps_data = _compute_maps(tier_filter="Tier 1")
            sio = io.StringIO()
            w = csv_mod.DictWriter(sio, fieldnames=[
                "Map", "Played", "Picks", "Bans", "Deciders", "Pick%", "Ban%"])
            w.writeheader()
            for m in maps_data:
                w.writerow({
                    "Map": m["map"], "Played": m["played"], "Picks": m["picks"],
                    "Bans": m["bans"], "Deciders": m["deciders"],
                    "Pick%": m["pickpct"], "Ban%": m["banpct"],
                })
            zf.writestr("tier1_maps.csv", sio.getvalue())

        buf.seek(0)
        return send_file(buf, as_attachment=True,
                         download_name="vlr_tier1_export.zip",
                         mimetype="application/zip")
    except Exception as e:
        import traceback; traceback.print_exc()
        return str(e), 500

# ── Team Detail ──────────────────────────────────────────
@app.route("/api/team/<path:team_id>/detail")
def api_team_detail(team_id):
    try:
        load_team_stats()
        idx        = load_team_matches()
        team_stats = load_team_stats()
        team_lookup = {t["TeamID"]: t for t in team_stats}

        year  = request.args.get("year",  "").strip()
        event = request.args.get("event", "").strip()

        all_matches = list(idx.get(team_id, []))
        for src_id, tgt_id in _team_id_merges.items():
            if tgt_id == team_id:
                all_matches.extend(idx.get(src_id, []))

        # Filter by year/event for roster + stats, keep all for match history display
        if event:
            matches = [m for m in all_matches if m.get("event") == event]
        elif year:
            matches = [m for m in all_matches if _extract_year(m.get("event", "")) == year]
        else:
            matches = all_matches

        info = team_lookup.get(team_id, {})

        region = (_get_all_team_regions().get(team_id)
                  or info.get("Region", ""))

        roster = _aggregate_players_from_matches(matches)

        map_pool = {}
        for match in matches:
            for mp in match.get("maps", []):
                mname = (mp.get("map") or "").strip()
                if not mname or mname == "-":
                    continue
                if mname not in map_pool:
                    map_pool[mname] = {"wins": 0, "losses": 0}
                if mp.get("result") == "W":
                    map_pool[mname]["wins"] += 1
                elif mp.get("result") == "L":
                    map_pool[mname]["losses"] += 1

        map_pool_list = []
        for mname, d in map_pool.items():
            played = d["wins"] + d["losses"]
            map_pool_list.append({
                "map": mname, "wins": d["wins"], "losses": d["losses"],
                "played": played,
                "winpct": round(d["wins"] / played * 100, 1) if played else 0,
            })
        map_pool_list.sort(key=lambda m: -m["played"])

        def _mid(m):
            nm = re.search(r'/(\d+)/', m.get("url", ""))
            return int(nm.group(1)) if nm else 0

        match_list = []
        for m in sorted(matches, key=_mid, reverse=True):
            match_list.append({
                "opponent":    m.get("opponent"),
                "opponent_id": m.get("opponent_id"),
                "event":       m.get("event"),
                "result":      m.get("result"),
                "score":       m.get("score"),
                "url":         m.get("url"),
                "maps": [
                    {"map": mp.get("map"), "result": mp.get("result"),
                     "score": mp.get("score"), "note": mp.get("note")}
                    for mp in m.get("maps", [])
                ],
            })

        return jsonify({
            "team_id": team_id,
            "name":    info.get("Name", team_id),
            "region":  info.get("Region", ""),
            "roster":  roster,
            "map_pool": map_pool_list,
            "matches": match_list,
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ── Agent × Map Matrix ───────────────────────────────────
@app.route("/api/meta/matrix")
def api_meta_matrix():
    try:
        region = request.args.get("region", "").strip()
        year   = request.args.get("year",   "").strip()
        event  = request.args.get("event",  "").strip()

        idx         = load_team_matches()
        team_stats  = load_team_stats()
        team_region = {t["TeamID"]: t.get("Region", "") for t in team_stats}

        data    = defaultdict(lambda: defaultdict(lambda: {"picks": 0, "wins": 0}))
        map_set = set()

        for team_id, matches in idx.items():
            this_region = team_region.get(team_id, "")
            for match in matches:
                if not _match_passes_filters(match, this_region, "Tier 1", region, year, event):
                    continue
                for mp in match.get("maps", []):
                    mname = (mp.get("map") or "").strip()
                    if not mname or mname.lower() in ("-", "undefined", "tbd"):
                        continue
                    mr = mp.get("result", "")
                    for p in mp.get("players_us", []):
                        if not isinstance(p, dict):
                            continue
                        agent = (p.get("agent") or "").strip().title()
                        if not agent or agent in ("-", "Undefined"):
                            continue
                        data[agent][mname]["picks"] += 1
                        if mr == "W":
                            data[agent][mname]["wins"] += 1
                        map_set.add(mname)

        maps   = sorted(map_set)
        agents = sorted(data.keys(),
                        key=lambda a: sum(data[a][m]["picks"] for m in maps), reverse=True)

        matrix = []
        for agent in agents:
            total = sum(data[agent][m]["picks"] for m in maps)
            cells = []
            for mname in maps:
                d     = data[agent][mname]
                picks = d["picks"]
                wins  = d["wins"]
                cells.append({
                    "picks":  picks,
                    "winpct": round(wins / picks * 100, 1) if picks else None,
                })
            matrix.append({"agent": agent, "total_picks": total, "cells": cells})

        return jsonify({"maps": maps, "matrix": matrix})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e), "maps": [], "matrix": []}), 500


@app.route("/api/veto-heatmap")
def api_veto_heatmap():
    """Global veto ban/pick frequency per map, filtered by year/event."""
    try:
        year  = request.args.get("year",  "").strip()
        event = request.args.get("event", "").strip()

        idx        = load_team_matches()
        true_regs  = _get_all_team_regions()

        map_stats = defaultdict(lambda: {"bans": 0, "picks": 0, "deciders": 0})

        for team_id, matches in idx.items():
            this_region = true_regs.get(team_id, "")
            for match in matches:
                if _is_showmatch(match):
                    continue
                if not _is_intl_event(match.get("event", "")):
                    continue
                if not _match_passes_filters(match, this_region, "Tier 1", "", year, event):
                    continue
                for v in match.get("veto", []):
                    mname  = (v.get("map") or "").strip()
                    action = (v.get("action") or "").strip().lower()
                    if not mname or mname.lower() in ("-", "undefined", "tbd"):
                        continue
                    if action == "ban":
                        map_stats[mname]["bans"] += 1
                    elif action == "pick":
                        map_stats[mname]["picks"] += 1
                    elif action == "decider":
                        map_stats[mname]["deciders"] += 1

        result = []
        for mname, d in map_stats.items():
            total = d["bans"] + d["picks"] + d["deciders"]
            if not total:
                continue
            result.append({
                "map":      mname,
                "bans":     d["bans"],
                "picks":    d["picks"],
                "deciders": d["deciders"],
                "ban_pct":  round(d["bans"]     / total * 100, 1),
                "pick_pct": round(d["picks"]    / total * 100, 1),
                "dec_pct":  round(d["deciders"] / total * 100, 1),
            })
        result.sort(key=lambda x: -x["bans"])
        return jsonify({"maps": result})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e), "maps": []}), 500


_schedule_cache      = None
_schedule_mtime      = 0.0
_schedule_last_fetch = 0.0   # epoch seconds of last successful watcher scrape

def _parse_eta_minutes(eta_str):
    """Parse VLR ETA string like '4h 46m' or '45m' into total minutes."""
    h = re.search(r'(\d+)h', eta_str or "")
    m = re.search(r'(\d+)m', eta_str or "")
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)

def _save_schedule(matches):
    global _schedule_cache, _schedule_mtime, _schedule_last_fetch
    import time as _time
    os.makedirs("data", exist_ok=True)
    path = "data/schedule.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(matches, f, ensure_ascii=False)
    _schedule_cache      = matches
    _schedule_mtime      = os.path.getmtime(path)
    _schedule_last_fetch = _time.time()

def _update_finished_matches(finished_schedule_entries):
    """
    Called when matches go from LIVE → finished.
    Scrapes only those match detail pages and merges into stored data.
    Never does a full re-scrape — existing data is always preserved.
    """
    global _team_stats_cache, _team_matches_cache, _player_team_stats_cache
    global _player_vlr_history_cache, _agents_cache, _maps_cache
    global _teams_dynamic_cache, _team_id_merges
    try:
        import scraper as _sc
        year      = _current_year()
        raw_path  = f"data/raw_matches_{year}.json"
        hist_path = f"data/team_matches_{year}.json"
        stats_path = f"data/team_stats_{year}.json"
        pts_path  = f"data/player_team_stats_{year}.json"

        has_raw = os.path.exists(raw_path)
        existing_raw = []
        if has_raw:
            with open(raw_path, encoding="utf-8") as f:
                existing_raw = json.load(f)
        known_urls = {m["url"] for m in existing_raw if m.get("url")}

        # Also check team_matches for already-known URLs (avoids duplicates when raw is absent)
        if not has_raw and os.path.exists(hist_path):
            with open(hist_path, encoding="utf-8") as f:
                hist = json.load(f)
            for matches in hist.values():
                for m in matches:
                    if m.get("url"):
                        known_urls.add(m["url"])

        # Build name→pid map from player leaderboard
        name_to_pid = {}
        df = load_df()
        if df is not None and not df.empty and "Player" in df.columns and "PlayerID" in df.columns:
            for _, row in df.iterrows():
                pid = str(row.get("PlayerID", ""))
                if pid and not pid.startswith("name:"):
                    name_to_pid[str(row["Player"]).lower()] = pid

        # Scrape each finished match
        new_matches = []
        for entry in finished_schedule_entries:
            url = entry.get("url", "")
            if not url or url in known_urls:
                continue
            print(f"[update] Scraping finished match: {url}", flush=True)
            detail = _sc.parse_match_detail(url)
            if not detail or not detail.get("maps"):
                continue
            match = {
                "url":      url,
                "t1_name":  detail.get("t1_name") or entry.get("team1", ""),
                "t2_name":  detail.get("t2_name") or entry.get("team2", ""),
                "t1_score": detail.get("t1_score", 0),
                "t2_score": detail.get("t2_score", 0),
                "winner":   detail.get("winner",   0),
                "event":    detail.get("event")   or entry.get("event", ""),
                "region":   detail.get("region")  or _sc.extract_region(entry.get("event", "")),
                "t1_id":    detail.get("t1_id",  ""),
                "t2_id":    detail.get("t2_id",  ""),
                "t1_acr":   detail.get("t1_acr", ""),
                "t2_acr":   detail.get("t2_acr", ""),
                "maps":     detail.get("maps",   []),
                "veto":     detail.get("veto",   []),
            }
            if match["winner"] == 0:
                continue
            new_matches.append(match)

        if not new_matches:
            print("[update] No new completed matches to add.", flush=True)
            return

        os.makedirs("data", exist_ok=True)

        if has_raw:
            # Full re-aggregation path: raw_matches is the source of truth
            all_matches = existing_raw + new_matches
            team_stats, team_history, player_team_stats = _sc.aggregate_teams(all_matches, name_to_pid)
            with open(raw_path,   "w", encoding="utf-8") as f: json.dump(all_matches,       f, ensure_ascii=False)
            with open(stats_path, "w", encoding="utf-8") as f: json.dump(team_stats,         f, ensure_ascii=False, indent=2)
            with open(hist_path,  "w", encoding="utf-8") as f: json.dump(team_history,       f, ensure_ascii=False)
            with open(pts_path,   "w", encoding="utf-8") as f: json.dump(player_team_stats,  f, ensure_ascii=False)
        else:
            # Delta merge path: no raw_matches baseline — merge new data into existing files
            delta_stats, delta_history, delta_pts = _sc.aggregate_teams(new_matches, name_to_pid)

            # Merge team_history (append new match entries per team)
            existing_hist = {}
            if os.path.exists(hist_path):
                with open(hist_path, encoding="utf-8") as f:
                    existing_hist = json.load(f)
            for tid, entries in delta_history.items():
                if tid not in existing_hist:
                    existing_hist[tid] = []
                existing_hist[tid].extend(entries)
            with open(hist_path, "w", encoding="utf-8") as f:
                json.dump(existing_hist, f, ensure_ascii=False)

            # Merge team_stats (add wins/losses/maps to existing team entries)
            existing_stats = []
            if os.path.exists(stats_path):
                with open(stats_path, encoding="utf-8") as f:
                    existing_stats = json.load(f)
            by_id = {t["TeamID"]: t for t in existing_stats}
            for dt in delta_stats:
                tid = dt["TeamID"]
                if tid not in by_id:
                    by_id[tid] = dt
                else:
                    ex = by_id[tid]
                    for k in ("SeriesWins", "SeriesLosses", "SeriesPlayed", "MapsWon", "MapsLost", "MapsPlayed"):
                        ex[k] = ex.get(k, 0) + dt.get(k, 0)
                    sp = ex["SeriesPlayed"]; mp = ex["MapsPlayed"]
                    ex["SeriesWinPct"] = round(ex["SeriesWins"] / sp * 100, 1) if sp else 0
                    ex["MapWinPct"]    = round(ex["MapsWon"]    / mp * 100, 1) if mp else 0
                    for mname, mdata in dt.get("Maps", {}).items():
                        if mname not in ex.get("Maps", {}):
                            ex.setdefault("Maps", {})[mname] = mdata
                        else:
                            for k in ("Played", "Wins", "Losses", "Picks", "Bans", "Deciders"):
                                ex["Maps"][mname][k] = ex["Maps"][mname].get(k, 0) + mdata.get(k, 0)
            with open(stats_path, "w", encoding="utf-8") as f:
                json.dump(sorted(by_id.values(), key=lambda t: t.get("SeriesWinPct", 0), reverse=True),
                          f, ensure_ascii=False, indent=2)

            # Merge player_team_stats
            existing_pts = {}
            if os.path.exists(pts_path):
                with open(pts_path, encoding="utf-8") as f:
                    existing_pts = json.load(f)
            for pid, teams in delta_pts.items():
                for tid, rec in teams.items():
                    ex = existing_pts.setdefault(pid, {}).get(tid)
                    if not ex:
                        existing_pts[pid][tid] = rec
                    else:
                        sm_e = ex["maps"]; sm_n = rec["maps"]; sm = sm_e + sm_n
                        def _avg(a, b):
                            if a is None and b is None: return None
                            return round(((a or 0) * sm_e + (b or 0) * sm_n) / sm, 2) if sm else None
                        existing_pts[pid][tid] = {
                            "team_name": ex["team_name"] or rec["team_name"],
                            "wins":   ex["wins"]   + rec["wins"],
                            "losses": ex["losses"] + rec["losses"],
                            "maps":   sm,
                            "rating": _avg(ex.get("rating"), rec.get("rating")),
                            "acs":    round(_avg(ex.get("acs"), rec.get("acs")) or 0) if _avg(ex.get("acs"), rec.get("acs")) is not None else None,
                            "kd":     _avg(ex.get("kd"), rec.get("kd")),
                        }
            with open(pts_path, "w", encoding="utf-8") as f:
                json.dump(existing_pts, f, ensure_ascii=False)

            # Seed raw_matches with just the new ones for future use
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(new_matches, f, ensure_ascii=False)

        # Clear team-related caches
        _team_stats_cache = _team_matches_cache = _player_team_stats_cache = None
        _agents_cache = {}; _maps_cache = {}; _teams_dynamic_cache = {}
        _team_id_merges = None
        _player_vlr_history_cache.clear()
        print(f"[update] Added {len(new_matches)} match(es).", flush=True)

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[update] Error: {e}", flush=True)


def _schedule_watcher():
    """Background thread: re-scrapes upcoming/live matches on a dynamic interval.
    Interval:  60 s  — while any match is LIVE
               3 min — when a match starts within 15 minutes
              10 min — when upcoming matches exist but none imminent
              30 min — no upcoming matches at all
    Trigger:   if a previously-LIVE match disappears (= finished), scrape just
               that match's detail page and merge it into stored data.
    """
    import time as _time
    import scraper as _st

    prev_live = {}  # url → match data for matches that were LIVE last cycle

    while True:
        sleep_secs = 600   # default 10 min
        try:
            matches  = _st.scrape_upcoming()
            _save_schedule(matches)

            live_now = {m["url"]: m for m in matches if m.get("status") == "LIVE"}
            upcoming = [m for m in matches if m.get("status") == "Upcoming"]
            eta_mins = [_parse_eta_minutes(m.get("eta", "")) for m in upcoming]

            # Detect matches that were LIVE and are now gone (completed)
            just_finished = [prev_live[u] for u in prev_live if u not in live_now]
            if just_finished and not _scrape_state.get("running"):
                print(f"[schedule] {len(just_finished)} match(es) just finished — updating data", flush=True)
                threading.Thread(
                    target=_update_finished_matches,
                    args=(just_finished,),
                    daemon=True,
                ).start()

            prev_live = live_now

            if live_now:
                sleep_secs = 60         # active match: refresh every minute
            elif eta_mins and min(eta_mins) <= 15:
                sleep_secs = 120        # match starting in ≤15 min: every 2 min
            elif upcoming:
                sleep_secs = 600        # matches later today: every 10 min
            else:
                sleep_secs = 1800       # nothing upcoming: every 30 min

        except Exception as e:
            print(f"[schedule_watcher] error: {e}", flush=True)
            sleep_secs = 300

        _time.sleep(sleep_secs)


@app.route("/api/schedule")
def api_schedule():
    """Return current schedule + metadata for the frontend."""
    global _schedule_cache, _schedule_mtime
    try:
        import time as _time, datetime as _dt
        path = "data/schedule.json"
        if os.path.exists(path):
            mtime = os.path.getmtime(path)
            if _schedule_cache is None or mtime != _schedule_mtime:
                with open(path, encoding="utf-8") as f:
                    _schedule_cache = json.load(f)
                _schedule_mtime = mtime
        matches = _schedule_cache or []
        age_secs = int(_time.time() - _schedule_last_fetch) if _schedule_last_fetch else None
        return jsonify({"matches": matches, "age_secs": age_secs})
    except Exception as e:
        return jsonify({"error": str(e), "matches": [], "age_secs": None}), 500


# ── Events / Tournaments ─────────────────────────────────

_ROUND_ORDER = {
    "seeding": 0,
    "ur1": 1,  "ur2": 2,  "ur3": 3,  "ur4": 4,
    "w1":  1,  "w2":  2,  "w3":  3,  "w4":  4,
    "r1":  1,  "r2":  2,  "r3":  3,  "r4":  4,
    "mr1": 5,  "mr2": 6,  "mr3": 7,  "mr4": 8, "mbf": 9,
    "ubqf": 10, "ubsf": 11, "ubf": 12,
    "lr1": 13, "lr2": 14, "lr3": 15, "lr4": 16, "lr5": 17,
    "lbf": 18,
    "gf":  20,
}
_ROUND_LABELS = {
    "seeding": "Seeding",
    "ur1": "Upper Rd 1", "ur2": "Upper Rd 2", "ur3": "Upper Rd 3", "ur4": "Upper Rd 4",
    "w1":  "Week 1",     "w2":  "Week 2",     "w3":  "Week 3",     "w4":  "Week 4",
    "r1":  "Round 1",    "r2":  "Round 2",    "r3":  "Round 3",    "r4":  "Round 4",
    "mr1": "Mid Rd 1",  "mr2": "Mid Rd 2",  "mr3": "Mid Rd 3",  "mr4": "Mid Rd 4",
    "mbf": "Mid BF",
    "ubqf": "UB Quarters", "ubsf": "UB Semis", "ubf": "UB Final",
    "lr1": "LB Rd 1", "lr2": "LB Rd 2", "lr3": "LB Rd 3",
    "lr4": "LB Rd 4", "lr5": "LB Rd 5",
    "lbf": "LB Final",
    "gf":  "Grand Final",
}
_PHASE_OF = {
    "seeding": "Group Stage",
    "ur1": "Group Stage", "ur2": "Group Stage", "ur3": "Group Stage", "ur4": "Group Stage",
    "w1":  "Group Stage", "w2":  "Group Stage", "w3":  "Group Stage", "w4":  "Group Stage",
    "r1":  "Group Stage", "r2":  "Group Stage", "r3":  "Group Stage", "r4":  "Group Stage",
    "mr1": "Group Stage", "mr2": "Group Stage", "mr3": "Group Stage", "mr4": "Group Stage",
    "mbf": "Group Stage",
    "ubqf": "Upper Bracket", "ubsf": "Upper Bracket", "ubf": "Upper Bracket",
    "lr1": "Lower Bracket",  "lr2": "Lower Bracket",  "lr3": "Lower Bracket",
    "lr4": "Lower Bracket",  "lr5": "Lower Bracket",  "lbf": "Lower Bracket",
    "gf":  "Grand Final",
}

def _extract_round_key(url):
    slug = url.rstrip("/").split("/")[-1]
    for key in sorted(_ROUND_ORDER, key=len, reverse=True):
        if re.search(r"-" + re.escape(key) + r"(-\d+)*$", slug):
            return key
    return None

def _match_id(url):
    m = re.search(r"/(\d+)/", url or "")
    return int(m.group(1)) if m else 0

@app.route("/api/event_names")
def api_event_names():
    """Return sorted list of unique Tier 1 event names from per-event JSON, optionally filtered by year."""
    try:
        year = request.args.get("year", "").strip()
        evs = load_player_events()
        seen = set()
        for ev_list in evs.values():
            for e in ev_list:
                if year and e.get("Year") != year:
                    continue
                name = e.get("Event", "")
                if name and _is_intl_event(name):
                    seen.add(name)
        return jsonify(sorted(seen))
    except Exception:
        return jsonify([])

@app.route("/api/events")
def api_events():
    try:
        year_filter = request.args.get("year", "").strip()
        idx        = load_team_matches()
        team_stats = load_team_stats()
        team_names = {t["TeamID"]: t["Name"] for t in team_stats}

        # Collect all unique matches per event (dedup by url)
        ev_matches  = defaultdict(dict)  # event → url → match_entry
        team_records = defaultdict(lambda: defaultdict(
            lambda: {"wins": 0, "losses": 0, "maps_won": 0, "maps_lost": 0, "name": ""}
        ))

        for team_id, matches in idx.items():
            tname = team_names.get(team_id, team_id)
            for match in matches:
                event  = match.get("event", "Unknown")
                url    = match.get("url", "")
                result = match.get("result", "")
                if year_filter:
                    ev_year = _extract_year(event) or match.get("_year", "")
                    if ev_year != year_filter:
                        continue

                # Per-team record
                d = team_records[event][team_id]
                d["name"] = tname
                if result == "W":   d["wins"]   += 1
                elif result == "L": d["losses"]  += 1
                for mp in match.get("maps", []):
                    if mp.get("result") == "W":   d["maps_won"]  += 1
                    elif mp.get("result") == "L": d["maps_lost"] += 1

                # Unique match entry — store from winner's perspective
                if url not in ev_matches[event] or result == "W":
                    round_key = _extract_round_key(url)
                    ev_matches[event][url] = {
                        "url":    url,
                        "team1":  tname  if result == "W" else match.get("opponent", "?"),
                        "team2":  match.get("opponent", "?") if result == "W" else tname,
                        "score":  match.get("score", ""),
                        "result": result,   # W = team1 won
                        "round_key":   round_key,
                        "round_label": _ROUND_LABELS.get(round_key, round_key or "?"),
                        "round_order": _ROUND_ORDER.get(round_key, 99),
                        "phase":       _PHASE_OF.get(round_key, "Other"),
                    }

        result_list = []
        for event_name, url_map in ev_matches.items():
            # Sort matches chronologically
            sorted_matches = sorted(url_map.values(), key=lambda m: (m["round_order"], _match_id(m["url"])))

            # Determine if tournament has concluded
            is_kickoff      = "kickoff" in event_name.lower()
            all_final_keys  = {"gf", "lbf", "ubf"}  # any of these = bracket deep enough to assume finished
            final_keys      = ("gf", "lbf") if is_kickoff else ("gf",)
            has_round_keys  = any(m.get("round_key") is not None for m in sorted_matches)
            has_final_match = any(m.get("round_key") in final_keys for m in sorted_matches)
            if has_round_keys:
                if has_final_match:
                    is_finished = True
                else:
                    # If highest round reached is bracket-finals level (ubf/lbf range),
                    # the data was likely scraped before the GF occurred — treat as unknown
                    # rather than ONGOING to avoid false "still running" badges.
                    max_order = max((m.get("round_order") or 0) for m in sorted_matches)
                    if max_order >= _ROUND_ORDER.get("ubf", 12):
                        is_finished = None
                    else:
                        is_finished = False   # genuinely early bracket — still ongoing
            else:
                is_finished = None  # can't tell from URL slugs alone

            # Find champion: only if tournament is finished
            champion = None
            if is_finished is not False:
                if is_kickoff:
                    # Prefer GF winner (full bracket format); fall back to UBF
                    gf_match = next((m for m in reversed(sorted_matches) if m["round_key"] == "gf"), None)
                    if gf_match:
                        champion = gf_match["team1"]
                    else:
                        ubf_match = next((m for m in sorted_matches if m["round_key"] == "ubf"), None)
                        if ubf_match:
                            champion = ubf_match["team1"]
                else:
                    for m in reversed(sorted_matches):
                        if m["round_key"] in ("gf", "ubf", "lbf"):
                            champion = m["team1"]
                            break
                if not champion and sorted_matches and not has_round_keys:
                    champion = sorted_matches[-1]["team1"]

            # Group by phase
            phases = defaultdict(list)
            for m in sorted_matches:
                phases[m["phase"]].append(m)
            phase_order = ["Group Stage", "Upper Bracket", "Lower Bracket", "Grand Final", "Other"]
            bracket = [
                {"phase": ph, "matches": phases[ph]}
                for ph in phase_order if ph in phases
            ]

            # Team standings
            teams_list = []
            for tid, td in team_records[event_name].items():
                played = td["wins"] + td["losses"]
                maps_p = td["maps_won"] + td["maps_lost"]
                teams_list.append({
                    "team_id":    tid,
                    "name":       td["name"],
                    "wins":       td["wins"],
                    "losses":     td["losses"],
                    "played":     played,
                    "maps_won":   td["maps_won"],
                    "maps_lost":  td["maps_lost"],
                    "map_winpct": round(td["maps_won"] / maps_p * 100, 1) if maps_p else 0,
                    "is_champion": td["name"] == champion,
                })
            # Champion first, then by wins
            teams_list.sort(key=lambda t: (0 if t["is_champion"] else 1, -t["wins"], t["losses"]))

            result_list.append({
                "event":       event_name,
                "champion":    champion,
                "is_finished": is_finished,
                "teams":       teams_list,
                "bracket":     bracket,
            })

        def _ev_priority(name):
            n = name.lower()
            # Off-season events always sort last
            if re.search(r"off.{0,2}season", n):
                return 3
            # Group 0: World Championship — "Valorant Champions YYYY" (not "Champions Tour")
            if "valorant champions" in n and "tour" not in n:
                return 0
            # Group 1: International Masters + LOCK//IN
            # Regional Masters end with "Masters" (no city after it) → sort with other
            if "masters" in n:
                return 1 if not re.search(r"masters\s*$", n) else 3
            if "lock" in n:
                return 1
            # Group 2: VCT regional leagues (Americas / EMEA / Pacific / China)
            if any(r in n for r in ("americas", "emea", "pacific", "china")):
                return 2
            # Group 3: Everything else
            return 3

        def _ev_year(name):
            m = re.search(r"20[2-9]\d", name)
            return int(m.group(0)) if m else 0

        result_list.sort(key=lambda e: (
            0 if e.get("is_finished") is not False else 1,    # ongoing events last
            0 if _ev_priority(e["event"]) < 3 else 1,         # T1 before T2
            _ev_priority(e["event"]),                          # Champions > Masters > VCT leagues
            -_ev_year(e["event"]),                             # newer first within group
            e["event"],
        ))
        return jsonify({"events": result_list})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e), "events": []}), 500

# ── Scrape trigger ───────────────────────────────────────
_scrape_state = {"running": False, "last_run": None, "error": None, "progress": ""}

def _current_year():
    import datetime
    return str(datetime.datetime.now().year)

def _run_scrape(years=None, full=True, incremental=False):
    global _df_cache, _df_merged_cache, _events_cache
    global _team_stats_cache, _team_matches_cache, _agents_cache, _maps_cache
    global _teams_dynamic_cache, _team_id_merges, _player_team_fullname_cache
    global _t1_team_ids_cache, _team_regions_cache, _player_team_stats_cache
    if years is None:
        years = [_current_year()]
    try:
        _scrape_state["running"]  = True
        _scrape_state["error"]    = None
        import scraper as scraper_mod

        for year in years:
            _scrape_state["progress"] = f"Scraping {year}…"

            # Incremental mode: reuse already-fetched match pages, only fetch new ones
            existing_matches = None
            if incremental:
                raw_path = f"data/raw_matches_{year}.json"
                if os.path.exists(raw_path):
                    with open(raw_path, encoding="utf-8") as f:
                        existing_matches = json.load(f)

            aggregated, event_index, team_stats, team_history, player_team_stats, all_matches = \
                scraper_mod.scrape_all(year=year, existing_matches=existing_matches)
            os.makedirs("data", exist_ok=True)
            if aggregated:
                with open(f"data/players_{year}.csv", "w", newline="", encoding="utf-8") as f:
                    w = csv_mod.DictWriter(f, fieldnames=scraper_mod.FIELDNAMES)
                    w.writeheader(); w.writerows(aggregated)
                with open(f"data/player_events_{year}.json", "w", encoding="utf-8") as f:
                    json.dump(event_index, f, ensure_ascii=False)

            if full and int(year) >= MIN_YEAR_FULL and team_stats:
                with open(f"data/team_stats_{year}.json", "w", encoding="utf-8") as f:
                    json.dump(team_stats, f, ensure_ascii=False, indent=2)
                with open(f"data/team_matches_{year}.json", "w", encoding="utf-8") as f:
                    json.dump(team_history, f, ensure_ascii=False)
                if player_team_stats:
                    with open(f"data/player_team_stats_{year}.json", "w", encoding="utf-8") as f:
                        json.dump(player_team_stats, f, ensure_ascii=False)
                if all_matches:
                    with open(f"data/raw_matches_{year}.json", "w", encoding="utf-8") as f:
                        json.dump(all_matches, f, ensure_ascii=False)

        # Also refresh the upcoming-matches schedule
        _scrape_state["progress"] = "Schedule…"
        try:
            upcoming = scraper_mod.scrape_upcoming()
            os.makedirs("data", exist_ok=True)
            with open("data/schedule.json", "w", encoding="utf-8") as f:
                json.dump(upcoming, f, ensure_ascii=False)
        except Exception:
            pass

        # Clear all caches so next request reloads fresh data
        _df_cache = _df_merged_cache = _events_cache = _team_stats_cache = _team_matches_cache = None
        _agents_cache = {}
        _maps_cache = {}
        _teams_dynamic_cache = {}
        _team_id_merges = _player_team_fullname_cache = _player_team_stats_cache = None
        _true_tier_cache = {}
        _t1_team_ids_cache = None
        _team_regions_cache = None
        _player_vlr_history_cache.clear()
        _scrape_state["last_run"] = "success"
    except Exception as e:
        import traceback; traceback.print_exc()
        _scrape_state["error"]    = str(e)
        _scrape_state["last_run"] = "error"
    finally:
        _scrape_state["running"]  = False
        _scrape_state["progress"] = ""

@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    if _scrape_state["running"]:
        return jsonify({"status": "already_running"}), 409
    body  = request.get_json(silent=True) or {}
    full  = body.get("full", True)
    threading.Thread(target=_run_scrape, args=([_current_year()], full), daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/api/scrape/status")
def api_scrape_status():
    import datetime
    state = dict(_scrape_state)
    files = glob.glob("data/*.json") + glob.glob("data/*.csv")
    if files:
        newest = max(files, key=os.path.getmtime)
        ts = os.path.getmtime(newest)
        state["last_updated"] = datetime.datetime.fromtimestamp(ts).strftime("%b %d %Y, %H:%M")
    else:
        state["last_updated"] = None
    return jsonify(state)


# ── Match drill-down ──────────────────────────────────────────────────────────

@app.route("/api/match/<int:match_id>")
def api_match_detail(match_id):
    """Find a match by its VLR numeric ID and return full detail."""
    try:
        idx = load_team_matches()
        needle = f"/{match_id}/"
        for team_id, matches in idx.items():
            for m in matches:
                if needle in m.get("url", ""):
                    return jsonify(m)
        return jsonify({"error": "not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Team roster per event ─────────────────────────────────────────────────────

@app.route("/api/team/<path:team_id>/roster")
def api_team_roster(team_id):
    """Return { event: [player_names] } for a team across all scraped matches."""
    try:
        load_team_stats()
        idx     = load_team_matches()
        matches = list(idx.get(team_id, []))
        for src_id, tgt_id in _team_id_merges.items():
            if tgt_id == team_id:
                matches.extend(idx.get(src_id, []))

        events = {}  # event_name → {player_name → count}
        for m in matches:
            ev = m.get("event", "Unknown")
            if ev not in events:
                events[ev] = {}
            for mp in m.get("maps", []):
                for p in mp.get("players_us", []):
                    name = (p.get("name") or "").strip()
                    if name:
                        events[ev][name] = events[ev].get(name, 0) + 1

        # Sort events by year then name; return top-5 players per event by map count
        result = []
        for ev, players in events.items():
            sorted_players = sorted(players.items(), key=lambda x: -x[1])
            result.append({
                "event":   ev,
                "players": [p for p, _ in sorted_players[:10]],
            })
        result.sort(key=lambda e: e["event"], reverse=True)
        return jsonify(result)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ── Team win-rate trend ───────────────────────────────────────────────────────

@app.route("/api/team/<path:team_id>/trend")
def api_team_trend(team_id):
    """Return per-event win% for a team, sorted chronologically."""
    try:
        load_team_stats()
        idx     = load_team_matches()
        matches = list(idx.get(team_id, []))
        for src_id, tgt_id in _team_id_merges.items():
            if tgt_id == team_id:
                matches.extend(idx.get(src_id, []))

        ev_stats = {}  # event → {w, l}
        for m in matches:
            ev = m.get("event", "Unknown")
            if ev not in ev_stats:
                ev_stats[ev] = {"w": 0, "l": 0}
            if m.get("result") == "W":
                ev_stats[ev]["w"] += 1
            else:
                ev_stats[ev]["l"] += 1

        result = []
        for ev, s in ev_stats.items():
            total = s["w"] + s["l"]
            yr = re.search(r"20\d\d", ev)
            result.append({
                "event":   ev,
                "year":    yr.group(0) if yr else "0",
                "wins":    s["w"],
                "losses":  s["l"],
                "played":  total,
                "winpct":  round(s["w"] / total * 100, 1) if total else 0,
            })
        result.sort(key=lambda e: (e["year"], e["event"]))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Player VLR team history (scraped from player page) ───────────────────────

_player_vlr_history_cache: dict = {}

@app.route("/api/player/<path:player_id>/team-history")
def api_player_team_history(player_id):
    """Scrape and return team history from vlr.gg/player/{id}."""
    if player_id in _player_vlr_history_cache:
        return jsonify(_player_vlr_history_cache[player_id])
    try:
        import urllib.request as _ur
        from bs4 import BeautifulSoup as _BS
        url  = f"https://www.vlr.gg/player/{player_id}"
        req  = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = _ur.urlopen(req, timeout=8).read()
        soup = _BS(html, "html.parser")

        entries = []
        col1    = soup.find("div", class_="col mod-1")
        if col1:
            for a in col1.find_all("a", href=True):
                href = a.get("href", "")
                if "/team/" not in href:
                    continue
                parts = href.strip("/").split("/")
                tid   = parts[1] if len(parts) > 1 else ""
                # Name is in the bold div (font-weight:500), period in ge-text-light
                inner = a.find("div", style=lambda s: s and "flex: 1" in s)
                if not inner:
                    inner = a
                name_div   = inner.find("div", style=lambda s: s and "font-weight" in s)
                tname      = name_div.get_text(strip=True) if name_div else ""
                period_divs = inner.find_all("div", class_="ge-text-light")
                period      = ""
                for pd in period_divs:
                    t = pd.get_text(strip=True)
                    if t:
                        period = t
                        break
                if tname:
                    entries.append({
                        "team_id":   tid,
                        "team_name": tname,
                        "period":    period,
                    })

        # Only the first entry can be current (no end date = still there)
        for i, e in enumerate(entries):
            period = e["period"]
            e["is_current"] = (i == 0) and "–" not in period and "-" not in period

        # Prefer exact match stats from player_team_stats; fall back to pe_stats
        pts = load_player_team_stats().get(str(player_id), {})
        for entry in entries:
            tid = str(entry.get("team_id", ""))
            if tid and tid in pts:
                rec = pts[tid]
                entry["stats"] = {
                    "rating": rec.get("rating"),
                    "acs":    rec.get("acs"),
                    "kd":     rec.get("kd"),
                    "maps":   rec.get("maps"),
                    "wins":   rec.get("wins"),
                    "losses": rec.get("losses"),
                    "exact":  True,
                }

        # Enrich remaining entries with player_events approximation
        _enrich_team_history_from_events(entries, player_id)

        result = {"teams": entries}
        _player_vlr_history_cache[player_id] = result
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "teams": []}), 500


def _parse_period_years(period):
    """Return (from_year, to_year) ints from a period string. to_year=None means ongoing."""
    years = [int(y) for y in re.findall(r"20\d\d", period or "")]
    if not years:
        return None, None
    return years[0], years[-1] if len(years) > 1 else None


def _enrich_team_history_from_events(entries, player_id):
    """Add 'pe_stats' to each entry using player_events data matched by year."""
    evs = load_player_events().get(player_id, [])
    if not evs:
        return

    # Group events by year
    by_year: dict = defaultdict(list)
    for ev in evs:
        y = ev.get("Year") or _extract_year(ev.get("Event", ""))
        if y:
            by_year[y].append(ev)

    for entry in entries:
        from_y, to_y = _parse_period_years(entry.get("period", ""))
        matched = []
        for y, year_evs in by_year.items():
            try:
                yi = int(y)
            except (ValueError, TypeError):
                continue
            if from_y is None:
                matched.extend(year_evs)
            elif to_y is None:
                if yi >= from_y:
                    matched.extend(year_evs)
            else:
                if from_y <= yi <= to_y:
                    matched.extend(year_evs)

        if not matched:
            continue

        total_rounds = sum(int(e.get("Rounds") or 0) for e in matched)
        if total_rounds == 0:
            continue

        def _wavg(field):
            total = sum(int(e.get("Rounds") or 0) for e in matched if e.get(field))
            if not total:
                return None
            try:
                return round(sum(float(e[field]) * int(e.get("Rounds") or 0)
                                 for e in matched if e.get(field)) / total, 2)
            except (ValueError, TypeError):
                return None

        entry["pe_stats"] = {
            "rating": _wavg("Rating"),
            "acs":    round(_wavg("ACS") or 0),
            "kd":     _wavg("KD"),
            "maps":   round(total_rounds / 25),  # ~25 rounds/map average
        }


# ── Player win-rate / rating trend ───────────────────────────────────────────

@app.route("/api/player/<path:player_id>/trend")
def api_player_trend(player_id):
    """Return per-event rating/ACS for a player from their event history."""
    try:
        evs = load_player_events()
        entries = evs.get(str(player_id), [])
        result = []
        for e in entries:
            rating = e.get("Rating")
            acs    = e.get("ACS")
            try:    rating = round(float(rating), 2) if rating else None
            except: rating = None
            try:    acs    = int(float(acs)) if acs else None
            except: acs    = None
            if rating is None and acs is None:
                continue
            result.append({
                "event":  e.get("Event", ""),
                "year":   e.get("Year",  ""),
                "tier":   e.get("Tier",  ""),
                "rating": rating,
                "acs":    acs,
                "maps":   e.get("Maps",  0),
            })
        result.sort(key=lambda e: (e["year"], e["event"]))
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Player vs Player (H2H confrontations) ────────────────────────────────────

@app.route("/api/player-h2h")
def api_player_h2h():
    """Find maps where p1 and p2 were on opposing teams and return their stats."""
    try:
        p1_name = request.args.get("p1", "").strip().lower()
        p2_name = request.args.get("p2", "").strip().lower()
        if not p1_name or not p2_name:
            return jsonify({"confrontations": [], "p1_stats": {}, "p2_stats": {}})

        idx = load_team_matches()
        p1_stats = {"maps": 0, "wins": 0, "rating": [], "acs": [], "kd": [], "adr": [], "hs_kills": 0.0, "total_kills": 0.0}
        p2_stats = {"maps": 0, "wins": 0, "rating": [], "acs": [], "kd": [], "adr": [], "hs_kills": 0.0, "total_kills": 0.0}
        confrontations = []
        seen = set()

        for team_id, matches in idx.items():
            for m in matches:
                url = m.get("url", "")
                if url in seen:
                    continue
                for mp in m.get("maps", []):
                    us_names   = [p.get("name","").strip().lower() for p in mp.get("players_us", [])]
                    opp_names  = [p.get("name","").strip().lower() for p in mp.get("players_opp", [])]
                    p1_us  = p1_name in us_names
                    p1_opp = p1_name in opp_names
                    p2_us  = p2_name in us_names
                    p2_opp = p2_name in opp_names
                    if not ((p1_us and p2_opp) or (p1_opp and p2_us)):
                        continue
                    seen.add(url)

                    p1_list = mp.get("players_us", []) if p1_us else mp.get("players_opp", [])
                    p2_list = mp.get("players_us", []) if p2_us else mp.get("players_opp", [])
                    map_res = mp.get("result", "")
                    p1_won  = (p1_us and map_res == "W") or (p1_opp and map_res == "L")

                    def _add(stats, plist, name, won):
                        row = next((p for p in plist if (p.get("name","")).strip().lower() == name), None)
                        if not row:
                            return
                        stats["maps"] += 1
                        if won:
                            stats["wins"] += 1
                        try: stats["rating"].append(float(row.get("rating", 0) or 0))
                        except: pass
                        try: stats["acs"].append(float(row.get("acs", 0) or 0))
                        except: pass
                        try:
                            k, d = int(row.get("k", 0) or 0), int(row.get("d", 1) or 1)
                            stats["kd"].append(round(k / max(d, 1), 2))
                        except: pass
                        try: stats["adr"].append(float(row.get("adr", 0) or 0))
                        except: pass
                        try:
                            hsp = row.get("hs", "0%")
                            hspf = float(str(hsp).replace("%","")) / 100
                            kills = int(row.get("k", 0) or 0)
                            stats["hs_kills"]    += hspf * kills
                            stats["total_kills"] += kills
                        except: pass

                    _add(p1_stats, p1_list, p1_name, p1_won)
                    _add(p2_stats, p2_list, p2_name, not p1_won)

                    confrontations.append({
                        "event":  m.get("event", ""),
                        "map":    mp.get("map", ""),
                        "score":  mp.get("score", ""),
                        "p1_won": p1_won,
                        "url":    url,
                    })
                    break  # one confrontation per series url

        def _agg(s):
            maps = s["maps"]
            if not maps:
                return {}
            return {
                "maps":    maps,
                "wins":    s["wins"],
                "winpct":  round(s["wins"] / maps * 100, 1),
                "rating":  round(sum(s["rating"]) / len(s["rating"]), 2) if s["rating"] else None,
                "acs":     round(sum(s["acs"]) / len(s["acs"])) if s["acs"] else None,
                "kd":      round(sum(s["kd"]) / len(s["kd"]), 2) if s["kd"] else None,
                "adr":     round(sum(s["adr"]) / len(s["adr"])) if s["adr"] else None,
                "hs":      round(s["hs_kills"] / s["total_kills"] * 100, 1) if s["total_kills"] else None,
            }

        confrontations.sort(key=lambda c: c["event"])
        return jsonify({
            "confrontations": confrontations,
            "p1_stats":       _agg(p1_stats),
            "p2_stats":       _agg(p2_stats),
        })
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _prewarm_caches():
    """Load all heavy data files into memory before serving any requests."""
    import time
    t0 = time.time()
    print("Pre-warming caches...", flush=True)
    load_player_events()
    print(f"  player events loaded ({time.time()-t0:.1f}s)", flush=True)
    # Warm per-year tier maps (load_df will call these, but pre-warm all here)
    _true_tier_map(year=None)
    for yr in ["2021","2022","2023","2024","2025","2026"]:
        _true_tier_map(year=yr)
    print(f"  true-tier maps ready ({time.time()-t0:.1f}s)", flush=True)
    load_df()
    print(f"  players loaded ({time.time()-t0:.1f}s)", flush=True)
    _get_merged_df()
    print(f"  merged df ready ({time.time()-t0:.1f}s)", flush=True)
    load_team_stats()
    print(f"  team stats loaded ({time.time()-t0:.1f}s)", flush=True)
    load_team_matches()
    print(f"  team matches loaded ({time.time()-t0:.1f}s)", flush=True)
    _player_team_fullname_map()
    print(f"  player-team map ready ({time.time()-t0:.1f}s)", flush=True)
    print(f"All caches warm — ready in {time.time()-t0:.1f}s", flush=True)

if __name__ == "__main__":
    import datetime
    _df_cache = _df_merged_cache = _events_cache = _team_stats_cache = _team_matches_cache = None
    _player_team_fullname_cache = None
    _agents_cache = {}
    _maps_cache = {}
    _teams_dynamic_cache = {}
    _team_id_merges = {}
    _prewarm_caches()

    # Auto-scrape current year if data files are older than today
    _data_files = glob.glob("data/*.json") + glob.glob("data/*.csv")
    if _data_files:
        _newest_mtime = max(os.path.getmtime(f) for f in _data_files)
        _newest_date  = datetime.datetime.fromtimestamp(_newest_mtime).date()
        if _newest_date < datetime.datetime.now().date():
            print(f"[startup] Data last updated {_newest_date} — auto-scraping {_current_year()}…", flush=True)
            threading.Thread(target=_run_scrape, args=([_current_year()], True), daemon=True).start()

    # Always start the schedule watcher — it keeps /api/schedule fresh
    # and auto-triggers a data scrape when a live match finishes
    print("[startup] Starting schedule watcher…", flush=True)
    threading.Thread(target=_schedule_watcher, daemon=True).start()

    app.run(debug=True, port=8080)
