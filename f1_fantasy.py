#!/usr/bin/env python3
"""
F1 Fantasy Scorer
==================

Prototype for a Formula 1 fantasy game among friends.

Each friend picks ONE driver and ONE team (constructor) independently --
they don't have to match (e.g. you can pick Verstappen as your driver but
McLaren as your team). For every race in a season, a friend's score for
that race is:

    friend_points = (driver's championship points earned in that race)
                  + (team's championship points earned in that race)

Points "earned in that race" are computed as the delta between
`points_current` and `points_start` from OpenF1's beta championship
endpoints (`/v1/championship_drivers` and `/v1/championship_teams`).

Data source: https://openf1.org (free, no API key needed for historical data)

Usage
-----
    # First time: create/edit friends.json (see create_sample_config())
    python f1_fantasy.py init-config

    # Pull results for a season and update the local scoreboard
    python f1_fantasy.py update --year 2025

    # Include sprint races in scoring too
    python f1_fantasy.py update --year 2025 --include-sprint

    # Show the leaderboard
    python f1_fantasy.py leaderboard --year 2025

    # Show a single friend's race-by-race breakdown
    python f1_fantasy.py breakdown --year 2025 --friend "Alex"
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import requests

API_BASE = "https://api.openf1.org/v1"
DEFAULT_CONFIG = Path("friends.json")
DEFAULT_DB = Path("f1_fantasy.db")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def api_get(path: str, **params):
    """GET a path from the OpenF1 API and return parsed JSON (a list of dicts).

    OpenF1 sometimes returns HTTP 404 for queries with no matching rows
    (e.g. session_type=Sprint on a season/round that had no sprint) instead
    of a 200 with an empty list. We treat 404 as "no results" rather than
    an error.
    """
    url = f"{API_BASE}/{path}"
    resp = requests.get(url, params=params, timeout=30)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Config (friends -> driver/team picks)
# ---------------------------------------------------------------------------

def create_sample_config(path: Path):
    sample = {
        "friends": [
            {"name": "Alex", "driver_number": 1, "team_name": "McLaren"},
            {"name": "Jamie", "driver_number": 16, "team_name": "Ferrari"},
            {"name": "Sam", "driver_number": 4, "team_name": "Red Bull Racing"},
        ]
    }
    path.write_text(json.dumps(sample, indent=2))
    print(f"Wrote sample config to {path}. Edit it with your 10 friends' picks.")
    print("Tip: run 'python f1_fantasy.py list-drivers --year 2025' to look up "
          "driver numbers and team names.")


def load_friends(config_path: Path):
    if not config_path.exists():
        sys.exit(
            f"Config file '{config_path}' not found. Run 'init-config' first."
        )
    data = json.loads(config_path.read_text())
    friends = data.get("friends", [])
    if not friends:
        sys.exit(f"No friends found in '{config_path}'.")
    return friends


# ---------------------------------------------------------------------------
# OpenF1 lookups
# ---------------------------------------------------------------------------

def get_race_sessions(year: int, include_sprint: bool = False):
    """Return race (and optionally sprint) sessions for a season, in date order."""
    sessions = api_get("sessions", year=year, session_type="Race")
    if include_sprint:
        sessions += api_get("sessions", year=year, session_type="Sprint")
    sessions.sort(key=lambda s: s["date_start"])
    return sessions


def get_driver_points_delta(session_key: int):
    """Return {driver_number: points_earned_this_session}."""
    rows = api_get("championship_drivers", session_key=session_key)
    return {
        r["driver_number"]: (r.get("points_current") or 0) - (r.get("points_start") or 0)
        for r in rows
    }


def get_team_points_delta(session_key: int):
    """Return {team_name: points_earned_this_session}."""
    rows = api_get("championship_teams", session_key=session_key)
    return {
        r["team_name"]: (r.get("points_current") or 0) - (r.get("points_start") or 0)
        for r in rows
    }


def list_drivers(year: int):
    """Helper to look up current driver numbers / team names for config setup."""
    sessions = api_get("sessions", year=year, session_type="Race")
    if not sessions:
        print(f"No race sessions found for {year}.")
        return
    latest_session_key = sessions[-1]["session_key"]
    drivers = api_get("drivers", session_key=latest_session_key)
    seen = set()
    print(f"{'Number':<8}{'Name':<25}{'Team':<25}")
    for d in sorted(drivers, key=lambda x: x["driver_number"]):
        key = d["driver_number"]
        if key in seen:
            continue
        seen.add(key)
        print(f"{d['driver_number']:<8}{d.get('full_name',''):<25}{d.get('team_name',''):<25}")


# ---------------------------------------------------------------------------
# Storage (SQLite)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS race_scores (
    year INTEGER NOT NULL,
    session_key INTEGER NOT NULL,
    meeting_name TEXT,
    session_name TEXT,
    date_start TEXT,
    friend_name TEXT NOT NULL,
    driver_number INTEGER,
    team_name TEXT,
    driver_points REAL,
    team_points REAL,
    total_points REAL,
    PRIMARY KEY (session_key, friend_name)
);
"""


def get_db(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def upsert_race_scores(conn, rows):
    conn.executemany(
        """
        INSERT INTO race_scores
            (year, session_key, meeting_name, session_name, date_start,
             friend_name, driver_number, team_name, driver_points, team_points, total_points)
        VALUES (:year, :session_key, :meeting_name, :session_name, :date_start,
                :friend_name, :driver_number, :team_name, :driver_points, :team_points, :total_points)
        ON CONFLICT(session_key, friend_name) DO UPDATE SET
            driver_points=excluded.driver_points,
            team_points=excluded.team_points,
            total_points=excluded.total_points,
            meeting_name=excluded.meeting_name,
            session_name=excluded.session_name,
            date_start=excluded.date_start
        """,
        rows,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Core update logic
# ---------------------------------------------------------------------------

def update_scores(year: int, config_path: Path, db_path: Path, include_sprint: bool):
    friends = load_friends(config_path)
    sessions = get_race_sessions(year, include_sprint=include_sprint)

    if not sessions:
        print(f"No sessions found for {year}. Nothing to update.")
        return

    conn = get_db(db_path)
    total_rows = []

    for session in sessions:
        session_key = session["session_key"]
        meeting_name = session.get("meeting_name") or session.get("location", "")
        session_name = session.get("session_name", "")
        date_start = session.get("date_start", "")

        try:
            driver_deltas = get_driver_points_delta(session_key)
            team_deltas = get_team_points_delta(session_key)
        except requests.HTTPError as exc:
            print(f"  Skipping {meeting_name} ({session_name}): API error {exc}")
            continue

        if not driver_deltas and not team_deltas:
            # Championship beta endpoints may not have data yet for this session
            # (e.g. race hasn't happened, or too old for the beta feature).
            print(f"  No championship data yet for {meeting_name} ({session_name}), skipping.")
            continue

        rows = []
        for friend in friends:
            name = friend["name"]
            driver_number = friend["driver_number"]
            team_name = friend["team_name"]

            driver_pts = driver_deltas.get(driver_number, 0)
            team_pts = team_deltas.get(team_name, 0)

            rows.append({
                "year": year,
                "session_key": session_key,
                "meeting_name": meeting_name,
                "session_name": session_name,
                "date_start": date_start,
                "friend_name": name,
                "driver_number": driver_number,
                "team_name": team_name,
                "driver_points": driver_pts,
                "team_points": team_pts,
                "total_points": driver_pts + team_pts,
            })

        upsert_race_scores(conn, rows)
        total_rows.extend(rows)
        print(f"  Updated {meeting_name} ({session_name})")

    conn.close()
    print(f"\nDone. Processed {len(sessions)} session(s), "
          f"{len(total_rows)} friend-race score rows written to {db_path}.")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def show_leaderboard(year: int, db_path: Path):
    conn = get_db(db_path)
    rows = conn.execute(
        """
        SELECT friend_name, SUM(total_points) AS total
        FROM race_scores
        WHERE year = ?
        GROUP BY friend_name
        ORDER BY total DESC
        """,
        (year,),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No scores recorded yet for {year}. Run 'update --year {year}' first.")
        return

    print(f"\n=== {year} Fantasy Leaderboard ===")
    print(f"{'Rank':<6}{'Friend':<20}{'Total Points':<12}")
    for i, (name, total) in enumerate(rows, start=1):
        print(f"{i:<6}{name:<20}{total:<12.1f}")


def show_summary(year: int, db_path: Path):
    """One row per friend: their picks + season total points."""
    conn = get_db(db_path)
    rows = conn.execute(
        """
        SELECT friend_name,
               driver_number,
               team_name,
               SUM(total_points) AS total
        FROM race_scores
        WHERE year = ?
        GROUP BY friend_name, driver_number, team_name
        ORDER BY total DESC
        """,
        (year,),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No scores recorded yet for {year}. Run 'update --year {year}' first.")
        return

    print(f"\n=== {year} Fantasy Summary ===")
    print(f"{'Friend':<20}{'Driver #':<10}{'Team':<25}{'Total Points':<12}")
    for name, driver_number, team_name, total in rows:
        print(f"{name:<20}{driver_number:<10}{team_name:<25}{total:<12.1f}")


def show_breakdown(year: int, friend: str, db_path: Path):
    conn = get_db(db_path)
    rows = conn.execute(
        """
        SELECT meeting_name, session_name, driver_points, team_points, total_points
        FROM race_scores
        WHERE year = ? AND friend_name = ?
        ORDER BY date_start
        """,
        (year, friend),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No scores found for '{friend}' in {year}.")
        return

    print(f"\n=== {friend}'s {year} Race-by-Race Breakdown ===")
    print(f"{'Race':<25}{'Session':<12}{'Driver Pts':<12}{'Team Pts':<12}{'Total':<8}")
    running_total = 0.0
    for meeting_name, session_name, dp, tp, total in rows:
        running_total += total
        print(f"{meeting_name:<25}{session_name:<12}{dp:<12.1f}{tp:<12.1f}{total:<8.1f}")
    print(f"\nSeason total so far: {running_total:.1f}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="F1 Fantasy Scorer (OpenF1-powered)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                         help="Path to friends config JSON (default: friends.json)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                         help="Path to SQLite database (default: f1_fantasy.db)")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-config", help="Create a sample friends.json to edit")

    p_list = sub.add_parser("list-drivers", help="List current driver numbers & teams")
    p_list.add_argument("--year", type=int, required=True)

    p_update = sub.add_parser("update", help="Fetch results and update the scoreboard")
    p_update.add_argument("--year", type=int, required=True)
    p_update.add_argument("--include-sprint", action="store_true",
                           help="Also count sprint session points")

    p_lb = sub.add_parser("leaderboard", help="Show the season leaderboard")
    p_lb.add_argument("--year", type=int, required=True)

    p_sum = sub.add_parser("summary", help="Show friend + driver + team + total points in one table")
    p_sum.add_argument("--year", type=int, required=True)

    p_bd = sub.add_parser("breakdown", help="Show one friend's race-by-race scores")
    p_bd.add_argument("--year", type=int, required=True)
    p_bd.add_argument("--friend", type=str, required=True)

    args = parser.parse_args()

    if args.command == "init-config":
        create_sample_config(args.config)
    elif args.command == "list-drivers":
        list_drivers(args.year)
    elif args.command == "update":
        update_scores(args.year, args.config, args.db, args.include_sprint)
    elif args.command == "leaderboard":
        show_leaderboard(args.year, args.db)
    elif args.command == "summary":
        show_summary(args.year, args.db)
    elif args.command == "breakdown":
        show_breakdown(args.year, args.friend, args.db)


if __name__ == "__main__":
    main()