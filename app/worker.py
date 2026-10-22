"""Queue worker: claims pending fit_files with SKIP LOCKED, so several workers can run safely."""
import logging
import select
import time

import psycopg

from . import config, db, processing

log = logging.getLogger("worker")


def claim_one() -> int | None:
    with db.conn() as c:
        row = c.execute("""UPDATE fit_files SET status='processing', attempts=attempts+1
                           WHERE id = (SELECT id FROM fit_files WHERE status='pending'
                                       ORDER BY uploaded_at FOR UPDATE SKIP LOCKED LIMIT 1)
                           RETURNING id""").fetchone()
        return row["id"] if row else None


def requeue_stuck() -> None:
    """Files left in 'processing' by a crashed worker go back to the queue (max 3 attempts)."""
    with db.conn() as c:
        c.execute("""UPDATE fit_files SET status = CASE WHEN attempts >= 3 THEN 'failed' ELSE 'pending' END,
                            error = CASE WHEN attempts >= 3 THEN 'Xử lý thất bại sau 3 lần' ELSE error END
                     WHERE status='processing' AND uploaded_at < now() - interval '10 minutes'""")


def drain() -> int:
    n = 0
    while (fid := claim_one()) is not None:
        try:
            processing.process_fit_file(fid)
        except Exception:
            log.exception("fit_file %s crashed", fid)
            with db.conn() as c:
                c.execute("UPDATE fit_files SET status='pending' WHERE id=%s AND attempts < 3", (fid,))
                c.execute("UPDATE fit_files SET status='failed', error='Lỗi không xác định' WHERE id=%s AND status='processing'", (fid,))
        n += 1
    return n


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    db.migrate()
    requeue_stuck()
    listen = psycopg.connect(config.DATABASE_URL, autocommit=True)
    listen.execute("LISTEN fit_files_pending")
    log.info("worker ready")
    last_sweep = time.monotonic()
    while True:
        drain()
        # wake on NOTIFY, or poll as a fallback
        if select.select([listen.fileno()], [], [], config.WORKER_POLL_SECONDS) != ([], [], []):
            for _ in listen.notifies(timeout=0):
                pass
        if time.monotonic() - last_sweep > 300:
            requeue_stuck()
            last_sweep = time.monotonic()


if __name__ == "__main__":
    main()
