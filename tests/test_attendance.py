"""Attendance calculations (no database needed)."""
from datetime import datetime, timedelta, timezone

from backend.attendance import build_rows, build_timeline, merge_intervals, summarize, total_minutes

T0 = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def test_merge_and_total_minutes():
    merged = merge_intervals([(at(0), at(10)), (at(5), at(20)), (at(30), at(40))])
    assert merged == [(at(0), at(20)), (at(30), at(40))]
    assert total_minutes(merged, at(0), at(60)) == 30
    assert total_minutes(merged, at(10), at(35)) == 15          # only the overlap counts


def test_statuses_rate_and_late():
    people = {i: {"name": n, "email": f"{n}@x.com", "role": "student"} for i, n in [(1, "Host"), (2, "Anna"), (3, "Ben"), (4, "Cara"), (5, "Dan"), (6, "Eve")]}
    roster = {2, 3, 4, 5}
    intervals = {
        1: [(at(0), at(60))],
        2: [(at(2), at(30))],            # left after 28 of 60 min = 47 %  -> left_early
        3: [(at(15), at(60))],           # joined late, still here          -> present_now + late
        6: [(at(1), at(60))],            # not on the roster                 -> guest
    }
    rows = build_rows(people, roster, 1, intervals, connected={1, 3, 6}, marks={5: "excused"}, rc_total=2,
                      rc_answered={3: 2}, session_start=at(0), end=at(60), grace_min=10, min_pct=50)
    by = {r["name"]: r for r in rows}
    assert "Host" not in by
    assert by["Anna"]["status"] == "left_early" and by["Anna"]["minutes"] == 28
    assert by["Ben"]["status"] == "present_now" and by["Ben"]["late"] is True and by["Ben"]["rollcalls_answered"] == 2
    assert by["Cara"]["status"] == "absent" and by["Dan"]["status"] == "excused"
    assert by["Eve"]["on_roster"] is False
    s = summarize(rows, roster)
    assert (s["present_now"], s["left_early"], s["absent"], s["excused"], s["guests"]) == (1, 1, 1, 1, 1)
    assert s["rate"] == 33                                       # 1 attended of (4 - 1 excused)


def test_manual_mark_overrides_automatic_result():
    people = {2: {"name": "Anna", "email": "", "role": "student"}}
    rows = build_rows(people, {2}, 1, {}, connected=set(), marks={2: "present"}, rc_total=0, rc_answered={},
                      session_start=at(0), end=at(30), grace_min=10, min_pct=50)
    assert rows[0]["status"] == "left"                           # teacher says present although never connected


def test_timeline_counts_people_over_time():
    merged = {1: [(at(0), at(10))], 2: [(at(2), at(6))], 3: [(at(4), at(10))]}
    points = build_timeline(at(0), at(10), merged, roster={2}, host_id=1)
    by_minute = {p["minute"]: p for p in points}
    assert by_minute[0]["people"] == 0 and by_minute[5]["people"] == 2 and by_minute[5]["roster"] == 1
