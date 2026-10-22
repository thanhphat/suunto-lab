"""HTTP API + web dashboard."""
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import config, db, ingest, notebook, queries

log = logging.getLogger("api")
STATIC = Path(__file__).parent / "static"
ATHLETE_ID: int = 1


@asynccontextmanager
async def lifespan(app: FastAPI):
    global ATHLETE_ID
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ATHLETE_ID = db.migrate()
    if not config.API_TOKEN:
        log.warning("API_TOKEN is not set: the API is open to anyone who can reach it")
    yield


app = FastAPI(title="Suunto Lab", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")


def auth(request: Request) -> int:
    if config.API_TOKEN:
        h = request.headers.get("authorization", "")
        token = h[7:] if h.lower().startswith("bearer ") else request.headers.get("x-api-token", "")
        if not secrets.compare_digest(token.encode(), config.API_TOKEN.encode()):
            raise HTTPException(401, "Sai hoặc thiếu API token.")
    return ATHLETE_ID


def _check_fit(data: bytes, name: str | None):
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"{name or 'File'} vượt quá {config.MAX_UPLOAD_BYTES // 1024 // 1024} MB.")
    if len(data) < 14 or data[8:12] != b".FIT":
        raise HTTPException(415, f"{name or 'File'} không phải file FIT.")


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...), source: str = Query("upload"), athlete: int = Depends(auth)):
    """Multipart upload, one or many files (field name `files`)."""
    results = []
    for f in files:
        data = await f.read()
        try:
            _check_fit(data, f.filename)
            results.append({"name": f.filename, **ingest.store_upload(athlete, data, f.filename, source)})
        except HTTPException as e:
            results.append({"name": f.filename, "status": "rejected", "error": e.detail})
    return {"results": results}


@app.post("/api/upload/raw")
async def upload_raw(request: Request, name: str | None = None, source: str = Query("shortcut"),
                     athlete: int = Depends(auth)):
    """Raw body upload (Content-Type: application/octet-stream). Easiest for iOS Shortcuts."""
    data = await request.body()
    _check_fit(data, name)
    return ingest.store_upload(athlete, data, name, source)


@app.get("/api/overview")
def overview(athlete: int = Depends(auth)):
    return queries.overview(athlete)


@app.get("/api/weekly")
def weekly(weeks: int = 12, athlete: int = Depends(auth)):
    return queries.weekly(athlete, min(max(weeks, 1), 104))


@app.get("/api/activities")
def activities(start: date | None = None, end: date | None = None, sport: str | None = None,
               limit: int = 50, offset: int = 0, athlete: int = Depends(auth)):
    return queries.list_activities(athlete, start, end, sport, None, limit, offset)


@app.get("/api/activities/{activity_id}")
def activity(activity_id: int, athlete: int = Depends(auth)):
    a = queries.get_activity(athlete, activity_id)
    if not a:
        raise HTTPException(404, "Không tìm thấy hoạt động.")
    return a


@app.get("/api/activities/{activity_id}/streams")
def streams(activity_id: int, points: int = 800, fields: str | None = None, athlete: int = Depends(auth)):
    s = queries.get_streams(athlete, activity_id, fields.split(",") if fields else
                            ["distance_m", "altitude_m", "hr", "speed_ms", "grade", "cadence", "lat", "lon"], points)
    if s is None:
        raise HTTPException(404, "Không tìm thấy hoạt động.")
    return s


@app.delete("/api/activities/{activity_id}")
def delete(activity_id: int, athlete: int = Depends(auth)):
    if not queries.delete_activity(athlete, activity_id):
        raise HTTPException(404, "Không tìm thấy hoạt động.")
    return {"deleted": activity_id}


@app.get("/api/load")
def load(start: date | None = None, end: date | None = None, athlete: int = Depends(auth)):
    return queries.training_load(athlete, start, end)


class SettingsIn(BaseModel):
    hr_max: int | None = Field(None, ge=120, le=230)
    hr_rest: int | None = Field(None, ge=30, le=100)
    zone_floors: list[float] | None = Field(None, min_length=5, max_length=5)


@app.get("/api/settings")
def get_settings(athlete: int = Depends(auth)):
    return queries.get_settings(athlete)


@app.put("/api/settings")
def put_settings(body: SettingsIn, athlete: int = Depends(auth)):
    if body.zone_floors and body.zone_floors != sorted(body.zone_floors):
        raise HTTPException(422, "Ngưỡng vùng phải tăng dần.")
    return queries.update_settings(athlete, body.hr_max, body.hr_rest, body.zone_floors)


@app.get("/api/files")
def files(limit: int = 30, athlete: int = Depends(auth)):
    return queries.file_status(athlete, min(max(limit, 1), 200))


@app.post("/api/files/{file_id}/reprocess")
def reprocess(file_id: int, athlete: int = Depends(auth)):
    with db.conn() as c:
        r = c.execute("UPDATE fit_files SET status='pending', attempts=0, error=NULL WHERE id=%s AND athlete_id=%s "
                      "RETURNING id", (file_id, athlete)).fetchone()
        c.execute("NOTIFY fit_files_pending")
    if not r:
        raise HTTPException(404, "Không tìm thấy file.")
    return {"queued": file_id}


@app.post("/api/reprocess-all")
def reprocess_all(athlete: int = Depends(auth)):
    """Re-run parsing and metrics for every stored file (after an algorithm change)."""
    with db.conn() as c:
        n = c.execute("UPDATE fit_files SET status='pending', attempts=0, error=NULL "
                      "WHERE athlete_id=%s AND status IN ('done','failed') "
                      "AND coalesce(error, '') <> 'Đã xoá hoạt động'", (athlete,)).rowcount
        c.execute("NOTIFY fit_files_pending")
    return {"queued": n}


# ---------------- training notebook ----------------

@app.get("/api/nb/library")
def nb_library(athlete: int = Depends(auth)):
    return notebook.library()


@app.get("/api/nb/plans")
def nb_plans(athlete: int = Depends(auth)):
    return {"plans": notebook.list_plans(athlete), "active": notebook.active_plan_id(athlete)}


@app.get("/api/nb/plans/{plan_id}")
def nb_plan(plan_id: str, athlete: int = Depends(auth)):
    p = notebook.get_plan(athlete, plan_id)
    if p is None:
        raise HTTPException(404, "Không tìm thấy giáo án.")
    return p


@app.get("/api/nb/journal")
def nb_journal(start: date, end: date, athlete: int = Depends(auth)):
    return notebook.journal(athlete, start, end)


class DoneIn(BaseModel):
    done: bool


@app.put("/api/nb/items/{item_id}")
def nb_item(item_id: int, body: DoneIn, athlete: int = Depends(auth)):
    if not notebook.set_item_done(athlete, item_id, body.done):
        raise HTTPException(404, "Không tìm thấy mục.")
    return {"ok": True}


@app.put("/api/nb/goals/{goal_id}")
def nb_goal(goal_id: int, body: DoneIn, athlete: int = Depends(auth)):
    if not notebook.set_goal_done(athlete, goal_id, body.done):
        raise HTTPException(404, "Không tìm thấy mục tiêu.")
    return {"ok": True}


class NoteIn(BaseModel):
    note: str = Field("", max_length=20000)


@app.put("/api/nb/notes/{day}")
def nb_note(day: date, body: NoteIn, athlete: int = Depends(auth)):
    notebook.set_note(athlete, day, body.note)
    return {"ok": True}


class TestIn(BaseModel):
    side: str = Field(pattern="^[LRV]$")
    value: float | None = None


@app.put("/api/nb/tests/{day}/{test_id}")
def nb_test(day: date, test_id: str, body: TestIn, athlete: int = Depends(auth)):
    notebook.set_test_result(athlete, test_id, day, body.side, body.value)
    return {"ok": True}


@app.get("/api/nb/volume")
def nb_volume(start: date, end: date, athlete: int = Depends(auth)):
    return notebook.week_volume(athlete, start, end)


@app.get("/api/nb/races")
def nb_races(athlete: int = Depends(auth)):
    return notebook.list_races(athlete)


class RaceIn(BaseModel):
    name: str | None = Field(None, max_length=200)
    race_date: date | None = None
    location: str | None = Field(None, max_length=200)
    distance_km: float | None = Field(None, gt=0, le=1000)
    ascent_m: float | None = Field(None, ge=0, le=30000)
    descent_m: float | None = Field(None, ge=0, le=30000)
    cutoff_h: float | None = Field(None, gt=0, le=400)
    start_time: str | None = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    priority: str | None = Field(None, pattern="^[ABC]$")
    status: str | None = None
    register_by: date | None = None
    change_by: date | None = None
    url: str | None = Field(None, max_length=500)
    notes: str | None = Field(None, max_length=5000)
    plan_id: str | None = None
    result_time_s: int | None = Field(None, gt=0)
    result_note: str | None = Field(None, max_length=5000)
    checkpoints: list[dict] | None = None
    analysis: str | None = Field(None, max_length=20000)


@app.post("/api/nb/races")
def nb_race_create(body: RaceIn, athlete: int = Depends(auth)):
    return notebook.save_race(athlete, body.model_dump(exclude_unset=True))


@app.put("/api/nb/races/{race_id}")
def nb_race_update(race_id: int, body: RaceIn, athlete: int = Depends(auth)):
    return notebook.save_race(athlete, body.model_dump(exclude_unset=True), race_id)


@app.delete("/api/nb/races/{race_id}")
def nb_race_delete(race_id: int, athlete: int = Depends(auth)):
    if not notebook.delete_race(athlete, race_id):
        raise HTTPException(404, "Không tìm thấy giải.")
    return {"deleted": race_id}


@app.post("/api/nb/import")
async def nb_import(request: Request, athlete: int = Depends(auth)):
    """Import a backup file exported by the notebook (format "so-tap-trail")."""
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "File không phải JSON.")
    if not isinstance(data, dict) or not isinstance(data.get("weeks"), dict):
        raise HTTPException(400, "File không đúng định dạng sao lưu của sổ tập.")
    return notebook.import_legacy(athlete, data)


@app.exception_handler(notebook.NotebookError)
async def nb_error(request: Request, exc: notebook.NotebookError):
    return JSONResponse({"detail": str(exc)}, status_code=422)


@app.get("/notebook")
def notebook_page():
    return FileResponse(STATIC / "notebook" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/notebook/{name}")
def notebook_asset(name: str):
    if name not in ("app.js", "figures.js"):
        raise HTTPException(404)
    return FileResponse(STATIC / "notebook" / name, headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
def healthz():
    with db.conn() as c:
        c.execute("SELECT 1")
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "Lỗi máy chủ."}, status_code=500)
