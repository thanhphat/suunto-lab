"""Turn a stored FIT file into canonical + derived rows, and keep daily load current."""
import logging
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from . import db, fit_parser, metrics

log = logging.getLogger("processing")

SPORT_VI = {
    ("running", "trail"): "Chạy trail", ("running", "treadmill"): "Chạy máy", ("running", None): "Chạy bộ",
    ("hiking", None): "Leo núi", ("walking", None): "Đi bộ", ("cycling", None): "Đạp xe",
    ("cycling", "mountain"): "Đạp MTB", ("swimming", None): "Bơi", ("training", None): "Tập luyện",
}


def sport_label(sport: str, sub: str | None) -> str:
    return SPORT_VI.get((sport, sub)) or SPORT_VI.get((sport, None)) or sport.replace("_", " ").capitalize()


def day_part(h: int) -> str:
    return "sáng sớm" if h < 6 else "buổi sáng" if h < 11 else "buổi trưa" if h < 14 else "buổi chiều" if h < 18 else "buổi tối"


def _n(v):
    """numpy/float -> JSON/SQL-safe python value."""
    if v is None:
        return None
    if isinstance(v, (np.floating, float)):
        return None if math.isnan(v) else float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    return v


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def settings(c, athlete_id: int) -> dict:
    return c.execute("SELECT * FROM athlete_settings WHERE athlete_id = %s", (athlete_id,)).fetchone()


def process_fit_file(fit_file_id: int) -> str:
    """Parse one fit_files row. Returns final status."""
    with db.conn() as c:
        ff = c.execute("SELECT * FROM fit_files WHERE id = %s", (fit_file_id,)).fetchone()
        if ff is None:
            return "missing"
        athlete_id = ff["athlete_id"]
        try:
            data = Path(ff["storage_path"]).read_bytes()
            p = fit_parser.decode(data)
            m = metrics.compute(p)
        except Exception as e:  # parse errors are permanent for this file
            log.warning("fit_file %s failed: %s", fit_file_id, e)
            c.execute("UPDATE fit_files SET status='failed', error=%s, processed_at=now() WHERE id=%s",
                      (str(e)[:500], fit_file_id))
            return "failed"

        st = settings(c, athlete_id)
        tz = ZoneInfo(st["timezone"])
        start_local = p.start_time.astimezone(tz)

        with c.transaction():
            dup = c.execute(
                "SELECT id, fit_file_id FROM activities WHERE athlete_id=%s AND start_time=%s AND sport=%s",
                (athlete_id, p.start_time, p.sport)).fetchone()
            if dup and dup["fit_file_id"] != fit_file_id:
                c.execute("UPDATE fit_files SET status='duplicate', error=%s, processed_at=now() WHERE id=%s",
                          (f"Trùng với hoạt động #{dup['id']}", fit_file_id))
                return "duplicate"
            reuse_id = None
            if dup:  # reprocessing the same file: rebuild rows but keep the activity id stable
                reuse_id = dup["id"]
                c.execute("DELETE FROM samples WHERE activity_id=%s", (reuse_id,))
                c.execute("DELETE FROM activities WHERE id=%s", (reuse_id,))

            s, fid = p.session, p.file_id
            gps = ~np.isnan(p.lat)
            first_gps = int(np.argmax(gps)) if gps.any() else None
            ascent = _num(s.get("total_ascent"))
            descent = _num(s.get("total_descent"))
            alt = m["alt_smooth"]
            product = fid.get("garmin_product") or fid.get("product")
            act = c.execute("""
                INSERT INTO activities (id, athlete_id, fit_file_id, start_time, local_date, sport, sub_sport, name,
                  device_manufacturer, device_product, device_serial, distance_m, elapsed_s, timer_s, moving_s,
                  ascent_m, descent_m, alt_min_m, alt_max_m, hr_avg, hr_max, cadence_avg, power_avg, temp_avg_c,
                  calories, start_lat, start_lon, has_gps)
                VALUES (coalesce(%s, nextval('activities_id_seq')),
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id""", (
                reuse_id, athlete_id, fit_file_id, p.start_time, start_local.date(), p.sport, p.sub_sport,
                f"{sport_label(p.sport, p.sub_sport)} {day_part(start_local.hour)}",
                _n(fid.get("manufacturer")), None if product in (None, 0) else str(product),
                None if fid.get("serial_number") is None else str(fid.get("serial_number")),
                _num(s.get("total_distance")) or m["distance_m"],
                round(_num(s.get("total_elapsed_time")) or (p.t[-1] - p.t[0])),
                round(_num(s.get("total_timer_time")) or (p.t[-1] - p.t[0])),
                round(m["moving_s"]),
                ascent if ascent is not None else round(m["ascent_calc"], 1),
                descent if descent is not None else round(m["descent_calc"], 1),
                _n(np.nanmin(alt)) if (~np.isnan(alt)).any() else None,
                _n(np.nanmax(alt)) if (~np.isnan(alt)).any() else None,
                _num(s.get("avg_heart_rate")) or (round(_n(np.nanmean(p.hr))) if (~np.isnan(p.hr)).any() else None),
                _num(s.get("max_heart_rate")) or (int(np.nanmax(p.hr)) if (~np.isnan(p.hr)).any() else None),
                _num(s.get("avg_cadence")) or (round(_n(np.nanmean(p.cadence))) if (~np.isnan(p.cadence)).any() else None),
                _num(s.get("avg_power")) or (round(_n(np.nanmean(p.power))) if (~np.isnan(p.power)).any() else None),
                _n(np.nanmean(p.temperature)) if (~np.isnan(p.temperature)).any() else None,
                _num(s.get("total_calories")),
                _n(p.lat[first_gps]) if first_gps is not None else None,
                _n(p.lon[first_gps]) if first_gps is not None else None,
                bool(gps.any()),
            )).fetchone()
            aid = act["id"]

            for i, lap in enumerate(p.laps):
                c.execute("""INSERT INTO laps (activity_id, lap_index, start_time, elapsed_s, timer_s, distance_m,
                             ascent_m, descent_m, hr_avg, hr_max, cadence_avg) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                          (aid, i, lap.get("start_time") if isinstance(lap.get("start_time"), datetime) else None,
                           _num(lap.get("total_elapsed_time")), _num(lap.get("total_timer_time")),
                           _num(lap.get("total_distance")), _num(lap.get("total_ascent")),
                           _num(lap.get("total_descent")), _num(lap.get("avg_heart_rate")),
                           _num(lap.get("max_heart_rate")), _num(lap.get("avg_cadence"))))

            t0 = p.t[0]
            with c.cursor().copy(
                "COPY samples (activity_id, t, elapsed_s, distance_m, lat, lon, altitude_m, hr, speed_ms, "
                "cadence, power, temperature_c, grade, moving) FROM STDIN") as cp:
                for i in range(len(p.t)):
                    cp.write_row((
                        aid, datetime.fromtimestamp(p.t[i], tz=p.start_time.tzinfo), int(round(p.t[i] - t0)),
                        _n(p.distance[i]), _n(p.lat[i]), _n(p.lon[i]),
                        None if np.isnan(alt[i]) else round(float(alt[i]), 2),
                        None if np.isnan(p.hr[i]) else int(p.hr[i]),
                        _n(p.speed[i]),
                        None if np.isnan(p.cadence[i]) else int(p.cadence[i]),
                        None if np.isnan(p.power[i]) else int(p.power[i]),
                        _n(p.temperature[i]),
                        round(float(m["grade"][i]), 4), bool(m["moving_mask"][i]),
                    ))

            if p.rr_ms:
                c.execute("INSERT INTO rr_intervals (activity_id, rr_ms) VALUES (%s, %s)", (aid, p.rr_ms))

            zone_s = metrics.zone_seconds(m["hr_hist"], st["hr_max"], st["zone_floors"])
            c.execute("""INSERT INTO activity_metrics (activity_id, avg_pace_s_km, gap_distance_m, gap_pace_s_km,
                         climb_s, climb_m, vam_m_h, hr_hist, zone_s, trimp, decoupling_pct)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (
                aid, _n(m["avg_pace_s_km"]), _n(m["gap_distance_m"]), _n(m["gap_pace_s_km"]),
                round(m["climb_s"]), round(m["climb_m"], 1), _n(m["vam_m_h"]),
                db.Jsonb(m["hr_hist"]), zone_s, metrics.edwards_trimp(zone_s) if m["hr_hist"] else None,
                m["decoupling_pct"]))

            for sp in metrics.splits_per_km(p, m):
                c.execute("""INSERT INTO splits (activity_id, km, distance_m, moving_s, pace_s_km, gap_s_km, hr_avg,
                             ascent_m, descent_m) VALUES (%(aid)s,%(km)s,%(distance_m)s,%(moving_s)s,%(pace_s_km)s,
                             %(gap_s_km)s,%(hr_avg)s,%(ascent_m)s,%(descent_m)s)""", {"aid": aid, **sp})

            c.execute("UPDATE fit_files SET status='done', error=NULL, processed_at=now() WHERE id=%s", (fit_file_id,))
            recompute_daily_load(c, athlete_id)
        log.info("fit_file %s -> activity %s", fit_file_id, aid)
        return "done"


def recompute_zones(c, athlete_id: int) -> int:
    """Re-derive zone_s and TRIMP for every activity after HR settings change."""
    st = settings(c, athlete_id)
    rows = c.execute("""SELECT m.activity_id, m.hr_hist FROM activity_metrics m
                        JOIN activities a ON a.id = m.activity_id WHERE a.athlete_id = %s""", (athlete_id,)).fetchall()
    for r in rows:
        if not r["hr_hist"]:
            continue
        z = metrics.zone_seconds(r["hr_hist"], st["hr_max"], st["zone_floors"])
        c.execute("UPDATE activity_metrics SET zone_s=%s, trimp=%s, computed_at=now() WHERE activity_id=%s",
                  (z, metrics.edwards_trimp(z), r["activity_id"]))
    recompute_daily_load(c, athlete_id)
    return len(rows)


def recompute_daily_load(c, athlete_id: int) -> None:
    """Rebuild daily_load from the first activity day to today (athlete timezone), rest days included."""
    st = settings(c, athlete_id)
    today = datetime.now(ZoneInfo(st["timezone"])).date()
    rows = c.execute("""SELECT a.local_date AS day, count(*) AS n, sum(a.distance_m) AS dist,
                               sum(a.moving_s) AS mov, sum(a.ascent_m) AS asc_m, sum(coalesce(m.trimp,0)) AS trimp
                        FROM activities a LEFT JOIN activity_metrics m ON m.activity_id = a.id
                        WHERE a.athlete_id = %s GROUP BY a.local_date ORDER BY a.local_date""",
                     (athlete_id,)).fetchall()
    c.execute("DELETE FROM daily_load WHERE athlete_id = %s", (athlete_id,))
    if not rows:
        return
    by_day = {r["day"]: r for r in rows}
    day: date = rows[0]["day"]
    end = max(today, rows[-1]["day"])
    ctl = atl = 0.0
    k_ctl, k_atl = 1 - math.exp(-1 / 42), 1 - math.exp(-1 / 7)
    out = []
    while day <= end:
        r = by_day.get(day)
        load = float(r["trimp"]) if r else 0.0
        ctl += (load - ctl) * k_ctl
        atl += (load - atl) * k_atl
        out.append((athlete_id, day, r["n"] if r else 0, float(r["dist"] or 0) if r else 0.0,
                    int(r["mov"] or 0) if r else 0, float(r["asc_m"] or 0) if r else 0.0, load,
                    round(ctl, 2), round(atl, 2), round(ctl - atl, 2)))
        day += timedelta(days=1)
    with c.cursor().copy("COPY daily_load (athlete_id, day, activities, distance_m, moving_s, ascent_m, trimp, "
                         "ctl, atl, tsb) FROM STDIN") as cp:
        for row in out:
            cp.write_row(row)
