"""MCP server: lets an AI agent (Claude Desktop, claude.ai, Claude Code...) read and analyse training data.

Activity data is read-only. The training notebook (plans, coach notes, test results, exercise library)
can also be written by the agent. Run:
  python -m app.mcp_server --stdio          # local, e.g. Claude Desktop
  python -m app.mcp_server --http 8001      # remote (streamable HTTP at /mcp)
"""
import argparse
import logging
import re
import secrets
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import config, db, metrics, notebook, queries

log = logging.getLogger("mcp")
RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
RW = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
REPLACE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

INSTRUCTIONS = f"""
You have access to one athlete's training data. Two sources:
1. Activities recorded by the watch (FIT files): read-only.
2. The training notebook: plans (one per goal or race) with items per day and goals per week, the athlete's
   daily notes (pain scores, HRV, sleep, how it felt), weekly self-test results, and coach notes.
   You can write coach notes, record test results, create or adjust plans, and extend the exercise library. Units: distance metres (or *_km), time seconds, pace seconds per km,
altitude metres, HR bpm, speed m/s, grade as a fraction. Dates are in the athlete's local timezone.

Start with get_overview, then narrow down with list_activities / get_activity / get_activity_streams.
Use get_training_load for fitness (CTL), fatigue (ATL) and form (TSB) over time, and compare_periods
to compare blocks. Use query_sql only when no other tool answers the question; call describe_schema first.

To review a day or a week ("đánh giá ngày/tuần"): call get_day_report for the range (plan items and ticks,
the athlete's note, tests, and the watch's activities side by side), then save your feedback with
write_coach_note. Write coach notes in the athlete's language (Vietnamese), plain paragraphs, lines starting
with "-" render as bullets. To build a plan for a new race: search_library for blocks/exercises to reference,
add missing ones with save_library_item, then save_plan. Adjust single days with update_plan_days.

Metric definitions:
{metrics.__doc__}
Load model: daily load = sum of Edwards TRIMP. CTL = 42-day exponential average, ATL = 7-day, TSB = CTL - ATL.
Edwards TRIMP runs higher than TrainingPeaks TSS; compare against this athlete's own history, not TSS norms.
"""

mcp = MCPServer("suunto-lab", title="Suunto Lab", instructions=INSTRUCTIONS)
_athlete: int | None = None


def athlete() -> int:
    global _athlete
    if _athlete is None:
        _athlete = db.default_athlete_id()
    return _athlete


def _d(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ToolError(f"Bad date {s!r}, expected YYYY-MM-DD")


@mcp.tool(annotations=RO)
def get_overview() -> dict:
    """Snapshot of the athlete: HR settings, current CTL/ATL/TSB, totals for the last 7 and 28 days
    versus the previous 7, and the date range of available data."""
    return {"settings": queries.get_settings(athlete()), **queries.overview(athlete())}


@mcp.tool(annotations=RO)
def list_activities(start_date: str | None = None, end_date: str | None = None, sport: str | None = None,
                    min_distance_km: float | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    """List activities newest first. Dates are ISO (YYYY-MM-DD), inclusive. `sport` matches the FIT
    sport or sub-sport (e.g. running, trail, hiking). Returns summary metrics per activity."""
    return queries.list_activities(athlete(), _d(start_date), _d(end_date), sport, min_distance_km, limit, offset)


@mcp.tool(annotations=RO)
def get_activity(activity_id: int) -> dict:
    """Full detail for one activity: summary, metrics (GAP, VAM, zones, TRIMP, decoupling),
    per-km splits and device laps."""
    a = queries.get_activity(athlete(), activity_id)
    if a is None:
        raise ToolError(f"Activity {activity_id} not found")
    return a


@mcp.tool(annotations=RO)
def get_activity_streams(activity_id: int, fields: list[str] | None = None, points: int = 300) -> dict:
    """Time series for one activity, averaged into `points` equal-time buckets (10-2000).
    fields: any of distance_m, altitude_m, hr, speed_ms, cadence, power, temperature_c, grade, lat, lon.
    Use this to look at pacing, HR drift, climbs or where something happened."""
    s = queries.get_streams(athlete(), activity_id, fields, min(points, 2000))
    if s is None:
        raise ToolError(f"Activity {activity_id} not found")
    return s


@mcp.tool(annotations=RO)
def get_training_load(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    """Daily load series (rest days included): trimp, distance, moving time, ascent, CTL, ATL, TSB.
    Defaults to the last 90 days."""
    return queries.training_load(athlete(), _d(start_date), _d(end_date))


@mcp.tool(annotations=RO)
def get_weekly_summary(weeks: int = 12) -> list[dict]:
    """Per-week totals (Monday start) for the last N weeks: distance, time, ascent, TRIMP, CTL and TSB at week end."""
    return queries.weekly(athlete(), min(max(weeks, 1), 104))


@mcp.tool(annotations=RO)
def compare_periods(a_start: str, a_end: str, b_start: str, b_end: str) -> dict:
    """Compare two date ranges (ISO dates, inclusive): volume, ascent, load, average pace and HR,
    VAM, decoupling and time in each HR zone."""
    return {"a": queries.period_summary(athlete(), _d(a_start), _d(a_end)),
            "b": queries.period_summary(athlete(), _d(b_start), _d(b_end))}


# ---------------- training notebook ----------------

def _plan_id(plan_id: str | None) -> str:
    pid = plan_id or notebook.active_plan_id(athlete())
    if not pid:
        raise ToolError("No plan yet. Create one with save_plan.")
    return pid


def _nb(fn, *args):
    try:
        return fn(*args)
    except notebook.NotebookError as e:
        raise ToolError(str(e))


@mcp.tool(annotations=RO)
def list_plans() -> dict:
    """All training plans (one per goal or race) and which one is active today."""
    return {"plans": notebook.list_plans(athlete()), "active": notebook.active_plan_id(athlete())}


@mcp.tool(annotations=RO)
def get_plan(plan_id: str | None = None, start_date: str | None = None, end_date: str | None = None) -> dict:
    """A plan with its weeks (name, focus, goals with done flags), test targets per week, and the items
    for each day in the range (slot S/T/C/N = morning/noon/evening/daily, label, ref '#BLOCK' or
    '@exercise', done). Defaults: active plan, whole date range."""
    p = notebook.get_plan(athlete(), _plan_id(plan_id), _d(start_date), _d(end_date))
    if p is None:
        raise ToolError(f"Plan {plan_id!r} not found")
    return p


@mcp.tool(annotations=RO)
def get_day_report(start_date: str, end_date: str | None = None) -> list[dict]:
    """Everything about each day in the range (max 31 days): planned items and which were ticked,
    the athlete's own note, coach note, self-test results, and activities recorded by the watch.
    Use this first when asked to review a day or a week."""
    start = _d(start_date)
    end = _d(end_date) or start
    if (end - start).days > 31 or end < start:
        raise ToolError("Range must be 0-31 days")
    return notebook.day_report(athlete(), start, end)


@mcp.tool(annotations=RO)
def get_journal(start_date: str, end_date: str) -> dict:
    """Athlete notes, self-test results and coach notes in a date range (any length)."""
    return notebook.journal(athlete(), _d(start_date), _d(end_date))


@mcp.tool(annotations=RW)
def write_coach_note(date: str, text: str, kind: str = "day") -> dict:
    """Save coach feedback shown in the notebook. kind 'day' attaches it to that date; kind 'week'
    attaches it to the plan week whose start date is `date`. Replaces any previous note there;
    empty text deletes it."""
    _nb(notebook.set_coach_note, athlete(), kind, _d(date), text)
    return {"saved": True, "kind": kind, "date": date}


@mcp.tool(annotations=RW)
def record_test_result(test_id: str, date: str, value: float | None, side: str = "V") -> dict:
    """Record one self-test result (see search_library kind='test' for ids and units). side L or R for
    two-sided tests, V otherwise. Pace tests take minutes per km as a decimal (8:30 -> 8.5).
    The notebook shows a plan week's results stored on that week's end date. value null deletes."""
    _nb(notebook.set_test_result, athlete(), test_id, _d(date), side, value)
    return {"saved": True}


@mcp.tool(annotations=RO)
def search_library(query: str | None = None, kind: str | None = None) -> list[dict]:
    """Find exercises, session blocks and self-tests to reference in plans. kind: exercise, block, test
    or omitted for all. Returns ids and the ref to use in plan items ('@id' or '#ID')."""
    return notebook.search_library(query, kind)


@mcp.tool(annotations=RO)
def get_library_item(kind: str, item_id: str) -> dict:
    """Full content of one exercise (instructions, cues, figure poses), block (exercises with doses) or test."""
    item = _nb(notebook.get_library_item, kind, item_id)
    if item is None:
        raise ToolError(f"{kind} {item_id!r} not found")
    return item


@mcp.tool(annotations=RW)
def save_library_item(kind: str, item: dict) -> dict:
    """Add or replace an exercise, block or test. Copy the shape of an existing item from get_library_item.
    exercise: id, name, en, cat, purpose, muscles, dose, tempo, setup[], steps[], cues[], mistakes[],
    easier, harder, stop, optional poses (stick figure, copy and adapt from a similar exercise).
    block: id, name, min, note, items [[exercise_id, dose text]]. test: id, name, group, unit, side (bool),
    better ('up' | 'down' | 'none'), ex (exercise id with instructions), optional fmt 'pace'."""
    return _nb(notebook.save_library_item, kind, item)


@mcp.tool(annotations=REPLACE)
def save_plan(plan: dict) -> dict:
    """Create or fully replace a plan. Shape:
    {"id": "race-2027", "name", "race", "start": "YYYY-MM-DD", "end", "test_note",
     "test_targets": {test_id: [target for week 1, week 2, ...]},
     "weeks": [{"n": 1, "start", "end", "name", "focus", "goals": [["test"|"volume"|"habit", text], ...]}],
     "days": {"YYYY-MM-DD": [[slot, label, ref], ...]}}
    slot: S morning, T noon, C evening, N every day. ref '#BLOCK' or '@exercise' or ''; label may be ''
    when ref is set. List every day explicitly, daily items included. Ticks on unchanged items are kept.
    Read the current plan with get_plan before replacing it."""
    return _nb(notebook.save_plan, athlete(), plan)


@mcp.tool(annotations=REPLACE)
def update_plan_days(days: dict, plan_id: str | None = None) -> dict:
    """Replace the items of the given days only: {"YYYY-MM-DD": [[slot, label, ref], ...]}.
    Use it to adjust upcoming days after a review. Ticks on unchanged items are kept."""
    return _nb(notebook.update_plan_days, athlete(), _plan_id(plan_id), days)


ALLOWED_TABLES = ["athletes", "athlete_settings", "activities", "laps", "samples", "rr_intervals",
                  "activity_metrics", "splits", "daily_load", "v_activities", "v_weekly",
                  "plans", "plan_weeks", "plan_goals", "plan_items", "v_plan_days", "daily_notes",
                  "test_results", "coach_notes", "nb_tests", "nb_blocks", "nb_exercises", "nb_categories"]


@mcp.tool(annotations=RO)
def describe_schema() -> dict:
    """Tables and columns available to query_sql, with notes on how they relate."""
    with db.conn() as c:
        rows = c.execute("""SELECT table_name, column_name, data_type FROM information_schema.columns
                            WHERE table_schema='public' AND table_name = ANY(%s)
                            ORDER BY table_name, ordinal_position""", (ALLOWED_TABLES,)).fetchall()
    tables: dict[str, list[str]] = {}
    for r in rows:
        tables.setdefault(r["table_name"], []).append(f"{r['column_name']} {r['data_type']}")
    return {
        "tables": tables,
        "notes": [
            "activities.id joins laps, samples, splits, activity_metrics, rr_intervals on activity_id.",
            "samples is ~1 row per second per activity (large): always filter by activity_id and aggregate.",
            "v_activities = activities + activity_metrics, the best starting point.",
            "daily_load has one row per day including rest days; v_weekly aggregates it by ISO week.",
            "zone_s is an int array, Z1..Z5 seconds (1-based in SQL: zone_s[1] is Z1).",
            "local_date is the start date in the athlete's timezone; start_time is UTC timestamptz.",
            "Notebook: v_plan_days lists each planned item with its title and done flag; join daily_notes,"
            " test_results and coach_notes on day (athlete-local date) and v_activities on local_date.",
            "nb_* tables hold the exercise library as jsonb `data`; test_results.side is L/R/V.",
        ],
    }


_SQL_START = re.compile(r"^\s*(with|select)\b", re.I)


@mcp.tool(annotations=RO)
def query_sql(sql: str, max_rows: int = 200) -> dict:
    """Run one read-only SQL query (SELECT or WITH ... SELECT) on PostgreSQL. Runs as a read-only
    role with an 8 s timeout; returns at most max_rows (<= 1000) rows. Call describe_schema first."""
    body = sql.strip().rstrip(";").strip()
    if not _SQL_START.match(body) or ";" in body:
        raise ToolError("Only a single SELECT / WITH query is allowed.")
    max_rows = min(max(max_rows, 1), 1000)
    try:
        with psycopg.connect(config.agent_database_url(), row_factory=dict_row, connect_timeout=5) as c:
            c.execute("SET TRANSACTION READ ONLY")
            c.execute("SET LOCAL statement_timeout = '8s'")
            cur = c.execute(body)
            rows = cur.fetchmany(max_rows + 1)
            cols = [d.name for d in cur.description or []]
            c.rollback()
    except psycopg.errors.QueryCanceled:
        raise ToolError("Query exceeded the 8 s limit. Filter samples by activity_id or aggregate more.")
    except psycopg.Error as e:
        msg = (e.diag.message_primary if getattr(e, "diag", None) else None) or str(e).splitlines()[0]
        raise ToolError(f"SQL error: {msg}")
    truncated = len(rows) > max_rows
    return {"columns": cols, "rows": [queries.clean(dict(r)) for r in rows[:max_rows]],
            "row_count": min(len(rows), max_rows), "truncated": truncated}


# ---------------- transports ----------------

class TokenGate:
    """ASGI wrapper: accept `Authorization: Bearer <MCP_TOKEN>` on /mcp, or the secret path /mcp/<MCP_TOKEN>."""

    def __init__(self, app, token: str | None):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.token:
            return await self.app(scope, receive, send)
        path: str = scope.get("path", "")
        if path == "/healthz":
            return await self._json(send, 200, b'{"ok":true}')
        prefix = f"/mcp/{self.token}"
        if path == prefix or path.startswith(prefix + "/"):
            new = "/mcp" + path[len(prefix):]
            scope = dict(scope, path=new, raw_path=new.encode())
            return await self.app(scope, receive, send)
        auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        if path.startswith("/mcp") and auth.lower().startswith("bearer ") and \
                secrets.compare_digest(auth[7:].encode(), self.token.encode()):
            return await self.app(scope, receive, send)
        return await self._json(send, 401, b'{"error":"unauthorized"}')

    @staticmethod
    async def _json(send, status, body):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})


def http_app():
    sec = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(config.MCP_ALLOWED_HOSTS),
        allowed_hosts=config.MCP_ALLOWED_HOSTS,
        allowed_origins=[f"https://{h}" for h in config.MCP_ALLOWED_HOSTS] + ["https://claude.ai"],
    )
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                  transport_security=sec, host="0.0.0.0")
    if not config.MCP_TOKEN:
        log.warning("MCP_TOKEN is not set: the MCP endpoint is open to anyone who can reach it")
    return TokenGate(app, config.MCP_TOKEN)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stdio", action="store_true")
    ap.add_argument("--http", type=int, metavar="PORT")
    a = ap.parse_args()
    if a.http:
        import uvicorn
        logging.basicConfig(level=logging.INFO)
        uvicorn.run(http_app(), host="0.0.0.0", port=a.http, log_level="info")
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
