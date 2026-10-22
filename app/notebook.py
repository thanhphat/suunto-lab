"""Training notebook: exercise library, plans (one per goal or race) and the daily journal.

Shared by the HTTP API (web notebook) and the MCP server (agent reads and writes).

Plan format, used by save_plan and `python -m app.notebook load-plan`:
  {"id": "race-2027", "name": "...", "race": "...", "start": "2027-01-04", "end": "2027-03-14",
   "test_note": "...", "test_targets": {"calf": [10, 12, ...]},            # one target per plan week
   "weeks": [{"n": 1, "start": "...", "end": "...", "name": "...", "focus": "...",
              "goals": [["test" | "volume" | "habit", "text"], ...]}],
   "days": {"2027-01-04": [["S" | "T" | "C" | "N", "label", "#BLOCK" | "@exercise" | ""], ...]}}
An item's label may be empty when ref points at a block or exercise; the UI then shows its name.

Library and plans are personal content and are not shipped in the repo. Load them from JSON on stdin:
  python -m app.notebook load-library < library.json   # {"cats": {id: name}, "exercises": [...], "blocks": {id: {...}}, "tests": [...]}
  python -m app.notebook load-plan < plan.json
  python -m app.notebook import-legacy < export.json   # old claude.ai notebook export, optional "coach" key
"""
import json
import re
from datetime import date, timedelta

import psycopg

from . import db
from .queries import clean

SLOTS = ("S", "T", "C", "N")
GOAL_CATS = ("test", "volume", "habit")


class NotebookError(ValueError):
    pass


def _day(v) -> date:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise NotebookError(f"Ngày không hợp lệ: {v!r} (cần YYYY-MM-DD)")


# ---------------- library ----------------

def library() -> dict:
    with db.conn() as c:
        cats = c.execute("SELECT id, name FROM nb_categories ORDER BY sort, id").fetchall()
        ex = c.execute("SELECT data FROM nb_exercises ORDER BY sort, id").fetchall()
        blocks = c.execute("SELECT id, data FROM nb_blocks ORDER BY sort, id").fetchall()
        tests = c.execute("SELECT data FROM nb_tests ORDER BY sort, id").fetchall()
    return {"cats": {r["id"]: r["name"] for r in cats}, "exercises": [r["data"] for r in ex],
            "blocks": {r["id"]: r["data"] for r in blocks}, "tests": [r["data"] for r in tests]}


def search_library(query: str | None = None, kind: str | None = None) -> list[dict]:
    """Compact listing (id, name, kind, category/minutes) for building plans."""
    q = f"%{(query or '').strip().lower()}%"
    out = []
    with db.conn() as c:
        if kind in (None, "exercise"):
            out += [{"kind": "exercise", "ref": "@" + r["id"], **r} for r in c.execute(
                """SELECT id, name, cat, data->>'en' AS en, data->>'muscles' AS muscles FROM nb_exercises
                   WHERE lower(name || ' ' || coalesce(data->>'en','') || ' ' || coalesce(data->>'muscles','')
                               || ' ' || coalesce(data->>'purpose','')) LIKE %s ORDER BY sort, id""", (q,)).fetchall()]
        if kind in (None, "block"):
            out += [{"kind": "block", "ref": "#" + r["id"], **r} for r in c.execute(
                """SELECT id, name, (data->>'min')::int AS minutes, jsonb_array_length(data->'items') AS exercises
                   FROM nb_blocks WHERE lower(name || ' ' || id) LIKE %s ORDER BY sort, id""", (q,)).fetchall()]
        if kind in (None, "test"):
            out += [{"kind": "test", **r} for r in c.execute(
                """SELECT id, name, data->>'unit' AS unit, data->>'group' AS "group" FROM nb_tests
                   WHERE lower(name || ' ' || id) LIKE %s ORDER BY sort, id""", (q,)).fetchall()]
    return out


def get_library_item(kind: str, item_id: str) -> dict | None:
    table = {"exercise": "nb_exercises", "block": "nb_blocks", "test": "nb_tests"}.get(kind)
    if not table:
        raise NotebookError("kind phải là exercise, block hoặc test")
    with db.conn() as c:
        r = c.execute(f"SELECT data FROM {table} WHERE id=%s", (item_id,)).fetchone()
    return r["data"] if r else None


def save_library_item(kind: str, item: dict) -> dict:
    """Insert or replace one exercise, block or test. Blocks may only use exercises that exist."""
    iid = str(item.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", iid):
        raise NotebookError("id chỉ gồm chữ, số, '-' và '_' (tối đa 60 ký tự)")
    if not item.get("name"):
        raise NotebookError("Thiếu name")
    with db.conn() as c, c.transaction():
        if kind == "exercise":
            if not c.execute("SELECT 1 FROM nb_categories WHERE id=%s", (item.get("cat"),)).fetchone():
                cats = [r["id"] for r in c.execute("SELECT id FROM nb_categories ORDER BY sort").fetchall()]
                raise NotebookError(f"cat phải là một trong {cats}")
            c.execute("""INSERT INTO nb_exercises (id, sort, cat, name, data)
                         VALUES (%s, (SELECT coalesce(max(sort), 0) + 1 FROM nb_exercises), %s, %s, %s)
                         ON CONFLICT (id) DO UPDATE SET cat=excluded.cat, name=excluded.name, data=excluded.data,
                                                        updated_at=now()""",
                      (iid, item["cat"], item["name"], db.Jsonb(item)))
        elif kind == "block":
            items = item.get("items") or []
            ids = [x[0] for x in items if isinstance(x, (list, tuple)) and len(x) == 2]
            if len(ids) != len(items) or not ids:
                raise NotebookError("items phải là danh sách [exercise_id, liều]")
            known = {r["id"] for r in c.execute("SELECT id FROM nb_exercises WHERE id = ANY(%s)", (ids,)).fetchall()}
            if missing := sorted(set(ids) - known):
                raise NotebookError(f"Bài tập chưa có trong thư viện: {missing}")
            c.execute("""INSERT INTO nb_blocks (id, sort, name, data)
                         VALUES (%s, (SELECT coalesce(max(sort), 0) + 1 FROM nb_blocks), %s, %s)
                         ON CONFLICT (id) DO UPDATE SET name=excluded.name, data=excluded.data, updated_at=now()""",
                      (iid, item["name"], db.Jsonb(item)))
        elif kind == "test":
            if item.get("better") not in ("up", "down", "none") or not item.get("unit"):
                raise NotebookError("test cần unit và better = up | down | none")
            c.execute("""INSERT INTO nb_tests (id, sort, name, data)
                         VALUES (%s, (SELECT coalesce(max(sort), 0) + 1 FROM nb_tests), %s, %s)
                         ON CONFLICT (id) DO UPDATE SET name=excluded.name, data=excluded.data""",
                      (iid, item["name"], db.Jsonb(item)))
        else:
            raise NotebookError("kind phải là exercise, block hoặc test")
    return {"kind": kind, "id": iid, "saved": True}


# ---------------- plans ----------------

def list_plans(athlete_id: int) -> list[dict]:
    with db.conn() as c:
        return [clean(r) for r in c.execute(
            """SELECT p.id, p.name, p.race, p.start_date, p.end_date,
                      (SELECT count(*) FROM plan_weeks w WHERE w.plan_id = p.id) AS weeks,
                      (SELECT count(*) FILTER (WHERE done_at IS NOT NULL) || '/' || count(*)
                       FROM plan_items i WHERE i.plan_id = p.id AND i.day <= current_date) AS items_done_to_date
               FROM plans p WHERE athlete_id=%s ORDER BY start_date DESC""", (athlete_id,)).fetchall()]


def active_plan_id(athlete_id: int, on: date | None = None) -> str | None:
    """The plan covering `on` (default today), else the next one to start, else the latest."""
    on = on or date.today()
    with db.conn() as c:
        r = c.execute("""SELECT id FROM plans WHERE athlete_id=%s
                         ORDER BY (%s BETWEEN start_date AND end_date) DESC,
                                  (start_date > %s) DESC, abs(start_date - %s), start_date DESC LIMIT 1""",
                      (athlete_id, on, on, on)).fetchone()
    return r["id"] if r else None


def get_plan(athlete_id: int, plan_id: str, start: date | None = None, end: date | None = None) -> dict | None:
    with db.conn() as c:
        p = c.execute("SELECT * FROM plans WHERE id=%s AND athlete_id=%s", (plan_id, athlete_id)).fetchone()
        if not p:
            return None
        weeks = c.execute("SELECT n, start_date, end_date, name, focus FROM plan_weeks WHERE plan_id=%s ORDER BY n",
                          (plan_id,)).fetchall()
        goals = c.execute("SELECT id, week_n, cat, text, done_at IS NOT NULL AS done FROM plan_goals "
                          "WHERE plan_id=%s ORDER BY week_n, idx", (plan_id,)).fetchall()
        items = c.execute("""SELECT id, day, slot, label, ref, done_at IS NOT NULL AS done FROM plan_items
                             WHERE plan_id=%s AND day BETWEEN %s AND %s ORDER BY day, idx""",
                          (plan_id, start or p["start_date"], end or p["end_date"])).fetchall()
    by_week: dict[int, list] = {}
    for g in goals:
        by_week.setdefault(g["week_n"], []).append({k: g[k] for k in ("id", "cat", "text", "done")})
    days: dict[str, list] = {}
    for i in items:
        days.setdefault(i["day"].isoformat(), []).append({k: i[k] for k in ("id", "slot", "label", "ref", "done")})
    return clean({
        "id": p["id"], "name": p["name"], "race": p["race"], "start": p["start_date"], "end": p["end_date"],
        "test_note": p["test_note"], "test_targets": p["test_targets"],
        "weeks": [{"n": w["n"], "start": w["start_date"], "end": w["end_date"], "name": w["name"],
                   "focus": w["focus"], "goals": by_week.get(w["n"], [])} for w in weeks],
        "days": days,
    })


def _norm_item(it) -> tuple[str, str, str]:
    if isinstance(it, dict):
        slot, label, ref = it.get("slot"), it.get("label") or "", it.get("ref") or ""
    elif isinstance(it, (list, tuple)) and len(it) == 3:
        slot, label, ref = it
    else:
        raise NotebookError(f"Mục không hợp lệ: {it!r} (cần [slot, label, ref])")
    if slot not in SLOTS:
        raise NotebookError(f"slot phải là S, T, C hoặc N: {it!r}")
    if not label and not ref:
        raise NotebookError(f"Mục cần label hoặc ref: {it!r}")
    return slot, str(label or ""), str(ref or "")


def _check_refs(c, refs: set[str]) -> None:
    blocks = {r["id"] for r in c.execute("SELECT id FROM nb_blocks").fetchall()}
    exercises = {r["id"] for r in c.execute("SELECT id FROM nb_exercises").fetchall()}
    bad = sorted(r for r in refs if r and not ((r[0] == "#" and r[1:] in blocks) or (r[0] == "@" and r[1:] in exercises)))
    if bad:
        raise NotebookError(f"ref không có trong thư viện: {bad}. Dùng search_library hoặc thêm bài trước.")


def _write_days(c, plan_id: str, days: dict) -> int:
    """Replace the items of each given day. Ticks carry over to items with the same slot, label and ref."""
    n = 0
    for d, items in days.items():
        day = _day(d)
        norm = [_norm_item(it) for it in items]
        old: dict[tuple, list] = {}
        for r in c.execute("SELECT slot, label, ref, done_at FROM plan_items WHERE plan_id=%s AND day=%s",
                           (plan_id, day)).fetchall():
            old.setdefault((r["slot"], r["label"], r["ref"]), []).append(r["done_at"])
        c.execute("DELETE FROM plan_items WHERE plan_id=%s AND day=%s", (plan_id, day))
        for idx, key in enumerate(norm):
            done = old[key].pop(0) if old.get(key) else None
            c.execute("INSERT INTO plan_items (plan_id, day, idx, slot, label, ref, done_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (plan_id, day, idx, *key, done))
            n += 1
    return n


def save_plan(athlete_id: int, plan: dict) -> dict:
    """Create or replace a whole plan. Ticks on unchanged items and goals are kept."""
    pid = str(plan.get("id") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,60}", pid):
        raise NotebookError("id dạng chữ thường, số và '-' (vd. 'race-2027')")
    start, end = _day(plan.get("start")), _day(plan.get("end"))
    if end < start or not plan.get("name"):
        raise NotebookError("Cần name, và end >= start")
    weeks = plan.get("weeks") or []
    days = plan.get("days") or {}
    if any(not (start <= _day(d) <= end) for d in days):
        raise NotebookError("Có ngày nằm ngoài khoảng start–end của giáo án")
    with db.conn() as c, c.transaction():
        own = c.execute("SELECT athlete_id FROM plans WHERE id=%s", (pid,)).fetchone()
        if own and own["athlete_id"] != athlete_id:
            raise NotebookError("id này đã được dùng")
        _check_refs(c, {_norm_item(it)[2] for its in days.values() for it in its})
        tests = {r["id"] for r in c.execute("SELECT id FROM nb_tests").fetchall()}
        targets = plan.get("test_targets") or {}
        if bad := sorted(set(targets) - tests):
            raise NotebookError(f"test_targets có bài test không tồn tại: {bad}")
        c.execute("""INSERT INTO plans (id, athlete_id, name, race, start_date, end_date, test_note, test_targets)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT (id) DO UPDATE SET name=excluded.name, race=excluded.race,
                       start_date=excluded.start_date, end_date=excluded.end_date, test_note=excluded.test_note,
                       test_targets=excluded.test_targets, updated_at=now()""",
                  (pid, athlete_id, plan["name"], plan.get("race"), start, end, plan.get("test_note"),
                   db.Jsonb(targets)))
        done_goals = {(r["week_n"], r["cat"], r["text"]): r["done_at"] for r in c.execute(
            "SELECT week_n, cat, text, done_at FROM plan_goals WHERE plan_id=%s AND done_at IS NOT NULL", (pid,)).fetchall()}
        c.execute("DELETE FROM plan_weeks WHERE plan_id=%s", (pid,))
        for w in weeks:
            n = int(w["n"])
            c.execute("INSERT INTO plan_weeks (plan_id, n, start_date, end_date, name, focus) VALUES (%s,%s,%s,%s,%s,%s)",
                      (pid, n, _day(w["start"]), _day(w["end"]), w.get("name") or f"Tuần {n}", w.get("focus")))
            for idx, g in enumerate(w.get("goals") or []):
                cat, text = (g["cat"], g["text"]) if isinstance(g, dict) else g
                if cat not in GOAL_CATS:
                    raise NotebookError(f"Nhóm mục tiêu phải là {GOAL_CATS}: {g!r}")
                c.execute("INSERT INTO plan_goals (plan_id, week_n, idx, cat, text, done_at) VALUES (%s,%s,%s,%s,%s,%s)",
                          (pid, n, idx, cat, text, done_goals.get((n, cat, text))))
        c.execute("DELETE FROM plan_items WHERE plan_id=%s AND day NOT BETWEEN %s AND %s", (pid, start, end))
        items = _write_days(c, pid, days)
    return {"id": pid, "weeks": len(weeks), "days": len(days), "items": items}


def update_plan_days(athlete_id: int, plan_id: str, days: dict) -> dict:
    """Replace the items of some days only (e.g. adjust tomorrow after a bad night)."""
    with db.conn() as c, c.transaction():
        p = c.execute("SELECT start_date, end_date FROM plans WHERE id=%s AND athlete_id=%s",
                      (plan_id, athlete_id)).fetchone()
        if not p:
            raise NotebookError(f"Không có giáo án {plan_id!r}")
        if any(not (p["start_date"] <= _day(d) <= p["end_date"]) for d in days):
            raise NotebookError("Có ngày nằm ngoài khoảng của giáo án")
        _check_refs(c, {_norm_item(it)[2] for its in days.values() for it in its})
        n = _write_days(c, plan_id, days)
        c.execute("UPDATE plans SET updated_at=now() WHERE id=%s", (plan_id,))
    return {"id": plan_id, "days": sorted(days), "items": n}


def set_item_done(athlete_id: int, item_id: int, done: bool) -> bool:
    with db.conn() as c:
        return c.execute("""UPDATE plan_items i SET done_at = CASE WHEN %s THEN coalesce(i.done_at, now()) END
                            FROM plans p WHERE i.id=%s AND p.id=i.plan_id AND p.athlete_id=%s""",
                         (done, item_id, athlete_id)).rowcount > 0


def set_goal_done(athlete_id: int, goal_id: int, done: bool) -> bool:
    with db.conn() as c:
        return c.execute("""UPDATE plan_goals g SET done_at = CASE WHEN %s THEN coalesce(g.done_at, now()) END
                            FROM plans p WHERE g.id=%s AND p.id=g.plan_id AND p.athlete_id=%s""",
                         (done, goal_id, athlete_id)).rowcount > 0


# ---------------- journal ----------------

def journal(athlete_id: int, start: date, end: date) -> dict:
    with db.conn() as c:
        notes = c.execute("SELECT day, note FROM daily_notes WHERE athlete_id=%s AND day BETWEEN %s AND %s ORDER BY day",
                          (athlete_id, start, end)).fetchall()
        tests = c.execute("""SELECT test_id, day, side, value FROM test_results
                             WHERE athlete_id=%s AND day BETWEEN %s AND %s ORDER BY day, test_id, side""",
                          (athlete_id, start, end)).fetchall()
        coach = c.execute("""SELECT kind, day, body, updated_at FROM coach_notes
                             WHERE athlete_id=%s AND day BETWEEN %s AND %s ORDER BY day""",
                          (athlete_id, start, end)).fetchall()
    return clean({
        "notes": {r["day"].isoformat(): r["note"] for r in notes},
        "tests": tests,
        "coach": {"days": {r["day"].isoformat(): r["body"] for r in coach if r["kind"] == "day"},
                  "weeks": {r["day"].isoformat(): r["body"] for r in coach if r["kind"] == "week"}},
    })


def set_note(athlete_id: int, day: date, note: str) -> None:
    with db.conn() as c:
        if note.strip():
            c.execute("""INSERT INTO daily_notes (athlete_id, day, note) VALUES (%s,%s,%s)
                         ON CONFLICT (athlete_id, day) DO UPDATE SET note=excluded.note, updated_at=now()""",
                      (athlete_id, day, note))
        else:
            c.execute("DELETE FROM daily_notes WHERE athlete_id=%s AND day=%s", (athlete_id, day))


def set_test_result(athlete_id: int, test_id: str, day: date, side: str, value: float | None) -> None:
    if side not in ("L", "R", "V"):
        raise NotebookError("side phải là L, R hoặc V")
    with db.conn() as c:
        t = c.execute("SELECT data FROM nb_tests WHERE id=%s", (test_id,)).fetchone()
        if not t:
            raise NotebookError(f"Không có bài test {test_id!r}")
        if (side == "V") == bool(t["data"].get("side")):
            raise NotebookError("Bài test hai bên dùng side L/R, bài một chỉ số dùng V")
        if value is None:
            c.execute("DELETE FROM test_results WHERE athlete_id=%s AND test_id=%s AND day=%s AND side=%s",
                      (athlete_id, test_id, day, side))
        else:
            c.execute("""INSERT INTO test_results (athlete_id, test_id, day, side, value) VALUES (%s,%s,%s,%s,%s)
                         ON CONFLICT (athlete_id, test_id, day, side) DO UPDATE SET value=excluded.value, updated_at=now()""",
                      (athlete_id, test_id, day, side, float(value)))


def set_coach_note(athlete_id: int, kind: str, day: date, body: str, author: str = "claude") -> None:
    if kind not in ("day", "week"):
        raise NotebookError("kind phải là day hoặc week")
    with db.conn() as c:
        if body.strip():
            c.execute("""INSERT INTO coach_notes (athlete_id, kind, day, body, author) VALUES (%s,%s,%s,%s,%s)
                         ON CONFLICT (athlete_id, kind, day) DO UPDATE SET body=excluded.body, author=excluded.author,
                                                                         updated_at=now()""",
                      (athlete_id, kind, day, body, author))
        else:
            c.execute("DELETE FROM coach_notes WHERE athlete_id=%s AND kind=%s AND day=%s", (athlete_id, kind, day))


def day_report(athlete_id: int, start: date, end: date) -> list[dict]:
    """One entry per day: plan items with ticks, the athlete's note, coach note, test results and the
    activities recorded by the watch. This is what an agent reads to review a day or a week."""
    plan_id = active_plan_id(athlete_id, start)
    plan = get_plan(athlete_id, plan_id, start, end) if plan_id else None
    j = journal(athlete_id, start, end)
    races = list_races(athlete_id, start, end)
    with db.conn() as c:
        acts = c.execute("""SELECT id, local_date, name, sport, distance_km, moving_s, ascent_m, hr_avg, hr_max,
                                   gap_pace_s_km, trimp, decoupling_pct FROM v_activities
                            WHERE athlete_id=%s AND local_date BETWEEN %s AND %s ORDER BY start_time""",
                         (athlete_id, start, end)).fetchall()
        refs = {"#" + r["id"]: r["name"] for r in c.execute("SELECT id, name FROM nb_blocks").fetchall()}
        refs |= {"@" + r["id"]: r["name"] for r in c.execute("SELECT id, name FROM nb_exercises").fetchall()}
    out = []
    d = start
    while d <= end:
        k = d.isoformat()
        items = (plan or {}).get("days", {}).get(k, [])
        out.append(clean({
            "day": k,
            "plan": [{"slot": i["slot"], "title": i["label"] or refs.get(i["ref"], i["ref"]), "ref": i["ref"],
                      "done": i["done"]} for i in items],
            "done": f"{sum(i['done'] for i in items)}/{len(items)}",
            "note": j["notes"].get(k),
            "coach_note": j["coach"]["days"].get(k),
            "tests": [t for t in j["tests"] if t["day"] == k],
            "activities": [a for a in acts if a["local_date"] == d],
            "races": [r for r in races if r["race_date"] == k],
        }))
        d += timedelta(days=1)
    return out


# ---------------- race calendar ----------------

RACE_FIELDS = ("name", "race_date", "location", "distance_km", "ascent_m", "descent_m", "cutoff_h", "start_time",
               "priority", "status", "register_by", "change_by", "url", "notes", "plan_id", "result_time_s",
               "result_note", "checkpoints", "analysis")
RACE_STATUS = ("planned", "registered", "done", "dnf", "dns")
LEVELS = ("", "Nhẹ", "Vừa", "Khó", "Rất khó", "Cực khó")
# Finish time grows faster than effort distance on long trail races: t2 = t1 * (e2 / e1) ** RIEGEL_K.
RIEGEL_K = 1.10
REF_MIN_S = 3 * 3600        # efforts used as reference: at least 3 h elapsed ...
REF_WINDOW_DAYS = 180       # ... within this many days before the race (or before today for upcoming races)
RECOVERY_DAYS = 21          # a big race (>= 60 eKm) closer than this before another one costs recovery


def _ekm(km, ascent) -> float | None:
    """Effort km: distance plus 1 km per 100 m of climbing."""
    return None if km is None else float(km) + float(ascent or 0) / 100


def _hm(hours: float) -> str:
    h, m = divmod(round(hours * 60), 60)
    return f"{h}h{m:02d}"


def _thousands(x: float) -> str:
    return f"{x:,.0f}".replace(",", ".")


def _difficulty(race: dict, refs: list[dict], prev: dict | None) -> dict:
    """Rate a race 1-5 for this athlete. The level comes from the projected finish against the cutoff
    (Riegel-style from the best recent long effort): >= 30 % spare -> 2, >= 15 % -> 3, >= 0 -> 4, over -> 5.
    Short courses are capped (< 25 eKm at 2, < 45 eKm at 3); without a projection the course size decides.
    A big race (>= 60 eKm) less than RECOVERY_DAYS before adds one level."""
    ekm = race["ekm"]
    if not ekm:
        return {"level": None, "label": "Chưa đủ dữ liệu", "reasons": ["Cần cự ly (và D+) để đánh giá."]}
    km, asc = float(race["distance_km"]), float(race["ascent_m"] or 0)
    course = 1 + sum(ekm >= t for t in (25, 45, 75, 115))
    reasons = [f"Cung đường {ekm:.0f} eKm: " + f"{km:g}".replace(".", ",") + f" km, +{_thousands(asc)} m"
               + (f", trung bình {asc / km:.0f} m leo mỗi km" if km and asc / km >= 50 else "")]
    out: dict = {}
    best = None
    for r in refs:
        e = _ekm(r["distance_km"], r["ascent_m"])
        t = r["elapsed_s"] / 3600 * (ekm / e) ** RIEGEL_K
        if best is None or t < best[0]:
            best = (t, r, e)
    level = course
    if best:
        t, r, e = best
        out |= {"est_hours": round(t, 2), "est_from": {"date": r["local_date"], "ekm": round(e, 1),
                                                        "hours": round(r["elapsed_s"] / 3600, 1)}}
        basis = f"ước tính từ buổi {r['local_date']:%d/%m}: {e:.0f} eKm trong {_hm(r['elapsed_s'] / 3600)}"
        if race["cutoff_h"]:
            cut = float(race["cutoff_h"])
            margin = (cut - t) / cut
            out["margin_pct"] = round(margin * 100)
            level = 2 if margin >= 0.3 else 3 if margin >= 0.15 else 4 if margin >= 0 else 5
            if margin < 0:
                reasons.append(f"Dự kiến ~{_hm(t)}, quá cutoff {_hm(cut)} khoảng {_hm(t - cut)} ({basis})")
            else:
                word = "Sát cutoff" if margin < 0.05 else "Vừa đủ" if margin < 0.15 else "Dư thời gian"
                reasons.append(f"{word}: dự kiến ~{_hm(t)}, cutoff {_hm(cut)}, dư {round(margin * 100)}% ({basis})")
            level = min(level, 2 if ekm < 25 else 3 if ekm < 45 else 5)
        else:
            reasons.append(f"Dự kiến ~{_hm(t)}, chưa có cutoff để so ({basis})")
    else:
        reasons.append("Chưa có buổi dài từ 3 giờ trong 6 tháng để ước tính thời gian")
    if prev:
        gap = (race["_date"] - prev["race_date"]).days
        if gap < RECOVERY_DAYS and (_ekm(prev["distance_km"], prev["ascent_m"]) or 0) >= 60:
            level += 1
            reasons.append(f"Chỉ {gap} ngày sau {prev['name']}: chưa kịp hồi phục hoàn toàn")
    level = max(1, min(5, level))
    label = "Vượt sức hiện tại" if out.get("margin_pct", 0) < 0 else LEVELS[level]
    return {"level": level, "label": label, "reasons": reasons, **out}


def list_races(athlete_id: int, start: date | None = None, end: date | None = None) -> list[dict]:
    """Races in a date range (default all), oldest first. Each race carries the eKm/h needed to make the
    cutoff, a difficulty rating for this athlete (level 1-5, reasons, projected finish time), and once
    run, the watch activity of that day with the eKm/h actually held."""
    today = date.today()
    with db.conn() as c:
        all_rows = c.execute("""SELECT r.*, p.name AS plan_name FROM races r LEFT JOIN plans p ON p.id = r.plan_id
                                WHERE r.athlete_id=%s ORDER BY r.race_date, r.id""", (athlete_id,)).fetchall()
        efforts = c.execute("""SELECT id, local_date, name, distance_km, elapsed_s, moving_s, ascent_m, hr_avg
                               FROM v_activities WHERE athlete_id=%s AND distance_km > 0 ORDER BY local_date""",
                            (athlete_id,)).fetchall()
    longest_on: dict = {}
    for a in efforts:
        if a["local_date"] not in longest_on or a["distance_km"] > longest_on[a["local_date"]]["distance_km"]:
            longest_on[a["local_date"]] = a
    lo, hi = start or date(1900, 1, 1), end or date(9999, 12, 31)
    out = []
    for n, r in enumerate(all_rows):
        if not (lo <= r["race_date"] <= hi):
            continue
        race = {k: r[k] for k in ("id", *RACE_FIELDS)} | {"plan_name": r["plan_name"]}
        race["start_time"] = r["start_time"].strftime("%H:%M") if r["start_time"] else None
        need = _ekm(r["distance_km"], r["ascent_m"])
        race["ekm"] = round(need, 1) if need else None
        race["ekm_h_needed"] = round(need / r["cutoff_h"], 2) if need and r["cutoff_h"] else None
        race["days_to"] = (r["race_date"] - today).days
        a = longest_on.get(r["race_date"]) if r["race_date"] <= today else None
        if a:
            secs = r["result_time_s"] or a["elapsed_s"]
            got = _ekm(a["distance_km"], a["ascent_m"])
            race["activity"] = {k: a[k] for k in ("id", "name", "distance_km", "elapsed_s", "moving_s", "ascent_m", "hr_avg")}
            race["ekm_h"] = round(got / (secs / 3600), 2) if got and secs else None
        ref_end = min(r["race_date"], today)
        refs = [e for e in efforts if e["elapsed_s"] >= REF_MIN_S and e["local_date"] < ref_end
                and (ref_end - e["local_date"]).days <= REF_WINDOW_DAYS]
        prev = next((x for x in reversed(all_rows[:n]) if x["race_date"] < r["race_date"]), None)
        race["difficulty"] = _difficulty(race | {"_date": r["race_date"]}, refs, prev)
        out.append(clean(race))
    return out


def save_race(athlete_id: int, data: dict, race_id: int | None = None) -> dict:
    """Create a race, or update the given fields of an existing one."""
    fields = {k: data[k] for k in RACE_FIELDS if k in data}
    if "checkpoints" in fields:
        cps = fields["checkpoints"] or []
        if not isinstance(cps, list) or not all(isinstance(x, dict) and x.get("name") for x in cps):
            raise NotebookError("checkpoints là danh sách {name, km, cutoff, note}")
        fields["checkpoints"] = db.Jsonb(cps)
    for k in ("name", "location", "url", "notes", "result_note", "plan_id", "start_time", "analysis"):
        if isinstance(fields.get(k), str):
            fields[k] = fields[k].strip() or None
    for k in ("race_date", "register_by", "change_by"):
        if fields.get(k):
            fields[k] = _day(fields[k])
    if fields.get("priority", "B") not in ("A", "B", "C"):
        raise NotebookError("priority phải là A, B hoặc C")
    if fields.get("status", "planned") not in RACE_STATUS:
        raise NotebookError(f"status phải là một trong {RACE_STATUS}")
    if race_id is None and (not fields.get("name") or not fields.get("race_date")):
        raise NotebookError("Cần tên giải và ngày (race_date)")
    with db.conn() as c, c.transaction():
        if fields.get("plan_id") and not c.execute("SELECT 1 FROM plans WHERE id=%s AND athlete_id=%s",
                                                   (fields["plan_id"], athlete_id)).fetchone():
            raise NotebookError(f"Không có giáo án {fields['plan_id']!r}")
        try:
            if race_id is None:
                cols = ", ".join(fields)
                r = c.execute(f"INSERT INTO races (athlete_id, {cols}) VALUES (%s, {', '.join(['%s'] * len(fields))}) "
                              "RETURNING id", (athlete_id, *fields.values())).fetchone()
            else:
                if not fields:
                    raise NotebookError("Không có trường nào để cập nhật")
                sets = ", ".join(f"{k}=%s" for k in fields)
                r = c.execute(f"UPDATE races SET {sets}, updated_at=now() WHERE id=%s AND athlete_id=%s RETURNING id",
                              (*fields.values(), race_id, athlete_id)).fetchone()
                if not r:
                    raise NotebookError(f"Không có giải #{race_id}")
        except psycopg.errors.UniqueViolation:
            raise NotebookError("Đã có giải cùng tên và ngày")
        except psycopg.errors.CheckViolation as e:
            raise NotebookError(f"Giá trị không hợp lệ: {e.diag.constraint_name}")
    return {"id": r["id"], "saved": True}


def delete_race(athlete_id: int, race_id: int) -> bool:
    with db.conn() as c:
        return c.execute("DELETE FROM races WHERE id=%s AND athlete_id=%s", (race_id, athlete_id)).rowcount > 0


# ---------------- loading and legacy import ----------------

def load_library(lib: dict) -> dict:
    """Insert or replace the whole library (categories, exercises, blocks, tests), keeping the given order."""
    with db.conn() as c, c.transaction():
        for i, (cid, name) in enumerate((lib.get("cats") or {}).items()):
            c.execute("""INSERT INTO nb_categories (id, name, sort) VALUES (%s,%s,%s)
                         ON CONFLICT (id) DO UPDATE SET name=excluded.name, sort=excluded.sort""", (cid, name, i))
        for i, e in enumerate(lib.get("exercises") or []):
            c.execute("""INSERT INTO nb_exercises (id, sort, cat, name, data) VALUES (%s,%s,%s,%s,%s)
                         ON CONFLICT (id) DO UPDATE SET sort=excluded.sort, cat=excluded.cat, name=excluded.name,
                                                        data=excluded.data, updated_at=now()""",
                      (e["id"], i, e["cat"], e["name"], db.Jsonb(e)))
        for i, (bid, b) in enumerate((lib.get("blocks") or {}).items()):
            c.execute("""INSERT INTO nb_blocks (id, sort, name, data) VALUES (%s,%s,%s,%s)
                         ON CONFLICT (id) DO UPDATE SET sort=excluded.sort, name=excluded.name, data=excluded.data,
                                                        updated_at=now()""",
                      (bid, i, b["name"], db.Jsonb({"id": bid, **b})))
        for i, t in enumerate(lib.get("tests") or []):
            c.execute("""INSERT INTO nb_tests (id, sort, name, data) VALUES (%s,%s,%s,%s)
                         ON CONFLICT (id) DO UPDATE SET sort=excluded.sort, name=excluded.name, data=excluded.data""",
                      (t["id"], i, t["name"], db.Jsonb(t)))
    return {k: len(lib.get(k) or []) for k in ("cats", "exercises", "blocks", "tests")}


def import_legacy(athlete_id: int, data: dict) -> dict:
    """Import the old claude.ai notebook: its export file {"app": "so-tap-trail", "plan", "weeks": {key: doc}},
    optionally with "coach": {"days": {date: text}, "weeks": {n: text}}. Existing values are overwritten."""
    plan_id = data.get("plan")
    weeks_in = data.get("weeks") or {}
    coach = data.get("coach") or {}
    counts = {"items": 0, "goals": 0, "notes": 0, "tests": 0, "coach": 0}
    with db.conn() as c, c.transaction():
        weeks = {w["n"]: w for w in c.execute("SELECT n, start_date, end_date FROM plan_weeks WHERE plan_id=%s",
                                              (plan_id,)).fetchall()}
        if not weeks:
            raise NotebookError(f"Chưa có giáo án {plan_id!r} để nhập vào")
        tests = {r["id"]: r["data"] for r in c.execute("SELECT id, data FROM nb_tests").fetchall()}
        for key, doc in weeks_in.items():
            m = re.search(r"-w(\d+)$", key)
            if not m or int(m.group(1)) not in weeks:
                continue
            w = weeks[int(m.group(1))]
            for k, on in (doc.get("items") or {}).items():
                d, idx = k.split("|")
                counts["items"] += c.execute(
                    "UPDATE plan_items SET done_at = CASE WHEN %s THEN coalesce(done_at, now()) END "
                    "WHERE plan_id=%s AND day=%s AND idx=%s", (bool(on), plan_id, d, int(idx))).rowcount
            for k, on in (doc.get("goals") or {}).items():
                counts["goals"] += c.execute(
                    "UPDATE plan_goals SET done_at = CASE WHEN %s THEN coalesce(done_at, now()) END "
                    "WHERE plan_id=%s AND week_n=%s AND idx=%s", (bool(on), plan_id, w["n"], int(k[1:]))).rowcount
            for d, note in (doc.get("notes") or {}).items():
                if note.strip():
                    c.execute("""INSERT INTO daily_notes (athlete_id, day, note) VALUES (%s,%s,%s)
                                 ON CONFLICT (athlete_id, day) DO UPDATE SET note=excluded.note, updated_at=now()""",
                              (athlete_id, d, note))
                    counts["notes"] += 1
            for tid, rec in (doc.get("tests") or {}).items():
                if tid not in tests:
                    continue
                for side, val in rec.items():
                    if val is None:
                        continue
                    c.execute("""INSERT INTO test_results (athlete_id, test_id, day, side, value) VALUES (%s,%s,%s,%s,%s)
                                 ON CONFLICT (athlete_id, test_id, day, side) DO UPDATE SET value=excluded.value""",
                              (athlete_id, tid, w["end_date"], "V" if side == "v" else side, float(val)))
                    counts["tests"] += 1
        for kind, entries in (("day", coach.get("days") or {}), ("week", coach.get("weeks") or {})):
            for k, body in entries.items():
                if kind == "week":
                    if int(k) not in weeks:
                        continue
                    k = weeks[int(k)]["start_date"]
                c.execute("""INSERT INTO coach_notes (athlete_id, kind, day, body) VALUES (%s,%s,%s,%s)
                             ON CONFLICT (athlete_id, kind, day) DO UPDATE SET body=excluded.body, updated_at=now()""",
                          (athlete_id, kind, k, body))
                counts["coach"] += 1
    return counts


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("load-library", "load-plan", "import-legacy"):
        sys.exit(__doc__)
    payload = json.load(sys.stdin)
    try:
        athlete = db.default_athlete_id()
        if cmd == "load-library":
            print(load_library(payload))
        elif cmd == "load-plan":
            print(save_plan(athlete, payload))
        else:
            print(import_legacy(athlete, payload))
    except NotebookError as e:
        sys.exit(f"Lỗi: {e}")
    finally:
        db.pool().close()
