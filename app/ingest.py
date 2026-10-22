"""Accept raw FIT bytes: store once (content-addressed), queue for the worker."""
import hashlib
from pathlib import Path

from . import config, db


def store_upload(athlete_id: int, data: bytes, original_name: str | None, source: str = "upload") -> dict:
    sha = hashlib.sha256(data).hexdigest()
    with db.conn() as c:
        existing = c.execute("SELECT id, status FROM fit_files WHERE athlete_id=%s AND sha256=%s",
                             (athlete_id, sha)).fetchone()
        if existing:
            return {"fit_file_id": existing["id"], "status": existing["status"], "already_uploaded": True}
        path = Path(config.FIT_STORAGE_DIR) / str(athlete_id) / sha[:2] / f"{sha}.fit"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        row = c.execute("""INSERT INTO fit_files (athlete_id, sha256, source, original_name, size_bytes, storage_path)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, status""",
                        (athlete_id, sha, source, original_name, len(data), str(path.resolve()))).fetchone()
        c.execute("NOTIFY fit_files_pending")
        return {"fit_file_id": row["id"], "status": row["status"], "already_uploaded": False}
