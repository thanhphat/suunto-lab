"""Read model shared by the HTTP API and the MCP server."""
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import db

STREAM_FIELDS = {
    "distance_m": "avg(distance_m)", "altitude_m": "avg(altitude_m)", "hr": "avg(hr)",
    "speed_ms": "avg(speed_ms)", "cadence": "avg(cadence)", "power": "avg(power)",
    "temperature_c": "avg(temperature_c)", "grade": "avg(grade)",
    "lat": "avg(lat)", "lon": "avg(lon)",
}


def clean(v):
    """Make DB values JSON friendly."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if isinstance(v, float):
        return round(v, 4)
    return v


def get_settings(athlete_id: int) -> dict:
    with db.conn() as c:
        return clean(c.execute("SELECT hr_max, hr_rest, zone_floors, timezone, updated_at FROM athlete_settings "
                               "WHERE athlete_id=%s", (athlete_id,)).fetchone())


def update_settings(athlete_id: int, hr_max: int | None = None, hr_rest: int | None = None,
                    zone_floors: list[float] | None = None) -> dict:
    from . import processing
    with db.conn() as c, c.transaction():
        c.execute("""UPDATE athlete_settings SET hr_max = coalesce(%s, hr_max), hr_rest = coalesce(%s, hr_rest),
                     zone_floors = coalesce(%s, zone_floors), updated_at = now() WHERE athlete_id=%s""",
                  (hr_max, hr_rest, zone_floors, athlete_id))
        processing.recompute_zones(c, athlete_id)
    return get_settings(athlete_id)


def list_activities(athlete_id: int, start: date | None = None, end: date | None = None,
                    sport: str | None = None, min_km: float | None = None,
                    limit: int = 50, offset: int = 0) -> list[dict]:
    q = ["SELECT id, start_time, local_date, sport, sub_sport, name, distance_km, moving_s, elapsed_s, ascent_m,"
         " descent_m, hr_avg, hr_max, avg_pace_s_km, gap_pace_s_km, vam_m_h, trimp, decoupling_pct, has_gps"
         " FROM v_activities WHERE athlete_id = %s"]
    args: list = [athlete_id]
    if start:
        q.append("AND local_date >= %s"); args.append(start)
    if end:
        q.append("AND local_date <= %s"); args.append(end)
    if sport:
        q.append("AND (sport = %s OR sub_sport = %s)"); args += [sport, sport]
    if min_km:
        q.append("AND distance_km >= %s"); args.append(min_km)
    q.append("ORDER BY start_time DESC LIMIT %s OFFSET %s"); args += [min(max(limit, 1), 500), max(offset, 0)]
    with db.conn() as c:
        return [clean(r) for r in c.execute(" ".join(q), args).fetchall()]


def get_activity(athlete_id: int, activity_id: int) -> dict | None:
    with db.conn() as c:
        a = c.execute("SELECT * FROM v_activities WHERE id=%s AND athlete_id=%s", (activity_id, athlete_id)).fetchone()
        if not a:
            return None
        laps = c.execute("SELECT lap_index, elapsed_s, timer_s, distance_m, ascent_m, descent_m, hr_avg, hr_max "
                         "FROM laps WHERE activity_id=%s ORDER BY lap_index", (activity_id,)).fetchall()
        splits = c.execute("SELECT km, distance_m, moving_s, pace_s_km, gap_s_km, hr_avg, ascent_m, descent_m "
                           "FROM splits WHERE activity_id=%s ORDER BY km", (activity_id,)).fetchall()
        rr = c.execute("SELECT cardinality(rr_ms) AS n FROM rr_intervals WHERE activity_id=%s", (activity_id,)).fetchone()
    return clean({**a, "laps": laps, "splits": splits, "rr_count": rr["n"] if rr else 0})


def get_streams(athlete_id: int, activity_id: int, fields: list[str] | None = None, points: int = 600) -> dict | None:
    fields = [f for f in (fields or ["distance_m", "altitude_m", "hr", "speed_ms", "lat", "lon"]) if f in STREAM_FIELDS]
    points = min(max(points, 10), 5000)
    cols = ", ".join(f"{STREAM_FIELDS[f]} AS {f}" for f in fields)
    with db.conn() as c:
        own = c.execute("SELECT 1 FROM activities WHERE id=%s AND athlete_id=%s", (activity_id, athlete_id)).fetchone()
        if not own:
            return None
        rows = c.execute(f"""
            WITH s AS (SELECT *, ntile(%s) OVER (ORDER BY t) AS b FROM samples WHERE activity_id=%s)
            SELECT b, min(elapsed_s) AS elapsed_s, {cols},
                   sum(CASE WHEN moving THEN 1 ELSE 0 END)::float / count(*) AS moving_frac
            FROM s GROUP BY b ORDER BY b""", (points, activity_id)).fetchall()
    out = {"elapsed_s": [r["elapsed_s"] for r in rows]}
    for f in fields:
        nd = 5 if f in ("lat", "lon") else 2
        out[f] = [None if r[f] is None else round(float(r[f]), nd) for r in rows]
    out["moving_frac"] = [round(r["moving_frac"], 2) for r in rows]
    return out


def training_load(athlete_id: int, start: date | None = None, end: date | None = None) -> list[dict]:
    end = end or date.today()
    start = start or end - timedelta(days=90)
    with db.conn() as c:
        return [clean(r) for r in c.execute(
            "SELECT day, activities, distance_m, moving_s, ascent_m, trimp, ctl, atl, tsb FROM daily_load "
            "WHERE athlete_id=%s AND day BETWEEN %s AND %s ORDER BY day", (athlete_id, start, end)).fetchall()]


def weekly(athlete_id: int, weeks: int = 12) -> list[dict]:
    with db.conn() as c:
        tz = c.execute("SELECT timezone FROM athlete_settings WHERE athlete_id=%s", (athlete_id,)).fetchone()["timezone"]
        today = datetime.now(ZoneInfo(tz)).date()
        monday = today - timedelta(days=today.weekday())
        first = monday - timedelta(weeks=weeks - 1)
        rows = {r["week_start"]: r for r in c.execute(
            "SELECT * FROM v_weekly WHERE athlete_id=%s AND week_start >= %s", (athlete_id, first)).fetchall()}
    out = []
    for i in range(weeks):
        wk = first + timedelta(weeks=i)
        r = rows.get(wk)
        out.append(clean({"week_start": wk, "activities": r["activities"] if r else 0,
                          "distance_km": r["distance_km"] if r else 0, "moving_h": r["moving_h"] if r else 0,
                          "ascent_m": r["ascent_m"] if r else 0, "trimp": r["trimp"] if r else 0,
                          "ctl_end": r["ctl_end"] if r else None, "tsb_end": r["tsb_end"] if r else None}))
    return out


def period_summary(athlete_id: int, start: date, end: date) -> dict:
    with db.conn() as c:
        r = c.execute("""
            SELECT count(*) AS activities, round(coalesce(sum(distance_km),0),1) AS distance_km,
                   round(coalesce(sum(moving_s),0)/3600.0, 1) AS moving_h, round(coalesce(sum(ascent_m),0)) AS ascent_m,
                   round(coalesce(sum(trimp),0)) AS trimp,
                   round((sum(moving_s) / nullif(sum(distance_km),0))::numeric) AS avg_pace_s_km,
                   round(avg(hr_avg)) AS hr_avg, round(avg(vam_m_h)) AS vam_m_h_avg,
                   round(avg(decoupling_pct)::numeric, 1) AS decoupling_avg_pct,
                   max(distance_km) AS longest_km
            FROM v_activities WHERE athlete_id=%s AND local_date BETWEEN %s AND %s""",
                      (athlete_id, start, end)).fetchone()
        z = c.execute("""SELECT array_agg(s ORDER BY i) AS zone_s FROM (
                           SELECT i, sum(zone_s[i]) AS s FROM v_activities, generate_series(1,5) i
                           WHERE athlete_id=%s AND local_date BETWEEN %s AND %s AND zone_s IS NOT NULL GROUP BY i) x""",
                      (athlete_id, start, end)).fetchone()
    return clean({"start": start, "end": end, **r, "zone_s": z["zone_s"]})


def overview(athlete_id: int) -> dict:
    with db.conn() as c:
        tz = c.execute("SELECT timezone FROM athlete_settings WHERE athlete_id=%s", (athlete_id,)).fetchone()["timezone"]
        today = datetime.now(ZoneInfo(tz)).date()
        last = c.execute("SELECT day, ctl, atl, tsb FROM daily_load WHERE athlete_id=%s ORDER BY day DESC LIMIT 1",
                         (athlete_id,)).fetchone()
        totals = c.execute("SELECT count(*) AS activities, min(local_date) AS first_day, max(local_date) AS last_day "
                           "FROM activities WHERE athlete_id=%s", (athlete_id,)).fetchone()
        queue = c.execute("SELECT status, count(*) AS n FROM fit_files WHERE athlete_id=%s GROUP BY status",
                          (athlete_id,)).fetchall()
    return clean({
        "today": today, "load": last, "totals": totals,
        "last_7d": period_summary(athlete_id, today - timedelta(days=6), today),
        "prev_7d": period_summary(athlete_id, today - timedelta(days=13), today - timedelta(days=7)),
        "last_28d": period_summary(athlete_id, today - timedelta(days=27), today),
        "files": {r["status"]: r["n"] for r in queue},
    })


def file_status(athlete_id: int, limit: int = 30) -> list[dict]:
    with db.conn() as c:
        return [clean(r) for r in c.execute(
            """SELECT f.id, f.original_name, f.source, f.status, f.error, f.uploaded_at, f.processed_at, a.id AS activity_id
               FROM fit_files f LEFT JOIN activities a ON a.fit_file_id = f.id
               WHERE f.athlete_id=%s ORDER BY f.uploaded_at DESC LIMIT %s""", (athlete_id, limit)).fetchall()]


def delete_activity(athlete_id: int, activity_id: int) -> bool:
    with db.conn() as c, c.transaction():
        a = c.execute("SELECT fit_file_id FROM activities WHERE id=%s AND athlete_id=%s",
                      (activity_id, athlete_id)).fetchone()
        if not a:
            return False
        c.execute("DELETE FROM samples WHERE activity_id=%s", (activity_id,))
        c.execute("DELETE FROM activities WHERE id=%s", (activity_id,))
        # keep the raw file row so a re-upload is recognised; mark it so the worker leaves it alone
        if a["fit_file_id"]:
            c.execute("UPDATE fit_files SET status='done', error='Đã xoá hoạt động' WHERE id=%s", (a["fit_file_id"],))
        from . import processing
        processing.recompute_daily_load(c, athlete_id)
    return True
