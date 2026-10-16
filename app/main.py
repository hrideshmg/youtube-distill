import asyncio
import re
import sqlite3
from pathlib import Path

import markdown
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from yt_dlp.extractor.youtube import YoutubeIE

from app import pipeline

JOBS_DIR = Path("data/jobs")
JOBS_DIR.mkdir(parents=True, exist_ok=True)

# NOTE: one shared connection, only touched from the event loop thread
# (pipeline step callbacks run there too). Writes are tiny, so blocking
# the loop briefly is fine for a local tool.
db = sqlite3.connect("data/distill.db", check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        video_id   TEXT PRIMARY KEY,
        status     TEXT NOT NULL,  -- running | done | error
        step       TEXT,
        error      TEXT,
        markdown   TEXT,
        title      TEXT,  -- original YouTube title
        thumbnail  TEXT,  -- thumbnail URL
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
""")
# Databases created before title/thumbnail existed. Their rows get backfilled lazily in get_job.
_cols = {r["name"] for r in db.execute("PRAGMA table_info(jobs)")}
for _col in ("title", "thumbnail"):
    if _col not in _cols:
        db.execute(f"ALTER TABLE jobs ADD COLUMN {_col} TEXT")
# Anything still "running" was killed by a restart; mark it so it can be retried.
db.execute("UPDATE jobs SET status = 'error', error = 'Interrupted by server restart' WHERE status = 'running'")

app = FastAPI()
_tasks: set[asyncio.Task] = set()  # keep references so tasks aren't GC'd mid-run


class JobRequest(BaseModel):
    url: str
    regenerate: bool = False


def video_id_from_url(url: str) -> str | None:
    # NOTE: _match_valid_url is a semi-private yt-dlp API. It matches watch,
    # youtu.be, shorts, and watch?v=..&list=.. URLs offline (no network call).
    m = YoutubeIE._match_valid_url(url)
    return m and m.group("id")


def render_markdown(md: str) -> str:
    # arithmatex shields \( \) / \[ \] / \begin{} maths from Markdown (which would
    # otherwise eat backslashes and underscores) and leaves the LaTeX for KaTeX in
    # the browser. $ delimiters are off so prices like "$5" stay plain text.
    return markdown.markdown(
        md,
        extensions=["fenced_code", "tables", "pymdownx.arithmatex"],
        extension_configs={
            "pymdownx.arithmatex": {"generic": True, "inline_syntax": ["round"], "block_syntax": ["square", "begin"]}
        },
    )


def update_job(video_id: str, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    db.execute(
        f"UPDATE jobs SET {cols}, updated_at = CURRENT_TIMESTAMP WHERE video_id = ?",
        (*fields.values(), video_id),
    )


@app.post("/api/jobs")
async def create_job(req: JobRequest):
    video_id = video_id_from_url(req.url)
    if not video_id:
        raise HTTPException(400, "Not a YouTube video URL")

    # No await between this check and the insert, so concurrent requests
    # for the same video can't both start a run.
    row = db.execute("SELECT status FROM jobs WHERE video_id = ?", (video_id,)).fetchone()
    if row and (row["status"] == "running" or (row["status"] == "done" and not req.regenerate)):
        return {"id": video_id}

    # Upsert rather than replace, so the previous article survives until the
    # new run succeeds (a failed regenerate keeps the old markdown).
    db.execute(
        """INSERT INTO jobs (video_id, status, step) VALUES (?, 'running', 'Starting')
           ON CONFLICT (video_id) DO UPDATE SET status = 'running', step = 'Starting', error = NULL""",
        (video_id,),
    )
    task = asyncio.create_task(_run(video_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"id": video_id}


@app.get("/api/jobs")
async def list_jobs():
    rows = db.execute("SELECT video_id, status, title, markdown, updated_at FROM jobs ORDER BY updated_at DESC").fetchall()
    return [
        {
            "id": r["video_id"],
            "status": r["status"],
            # Original YouTube title; rows not yet backfilled fall back to the generated heading.
            "title": r["title"] or (m.group(1) if (m := re.match(r"#\s+(.+)", r["markdown"] or "")) else None),
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


@app.get("/api/jobs/{video_id}")
async def get_job(video_id: str):
    row = db.execute("SELECT * FROM jobs WHERE video_id = ?", (video_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Unknown job")
    md = row["markdown"]
    title, thumbnail = row["title"], row["thumbnail"]
    if row["status"] == "done" and not title:
        # Article generated before we stored video metadata: fetch it once.
        # Best-effort, so a network failure doesn't block reading the article.
        try:
            info = await asyncio.to_thread(pipeline.fetch_info, f"https://www.youtube.com/watch?v={video_id}")
            title, thumbnail = info.get("title"), info.get("thumbnail")
            db.execute(
                "UPDATE jobs SET title = ?, thumbnail = ? WHERE video_id = ?", (title, thumbnail, video_id)
            )
        except Exception:
            pass
    return {
        "status": row["status"],
        "step": row["step"],
        "error": row["error"],
        "title": title,
        "thumbnail": thumbnail,
        # NOTE: rendered HTML is inserted as-is by the page. It's our own LLM
        # output, but raw HTML in it isn't sanitised — fine for local use.
        "article_html": render_markdown(md) if md else None,
    }


async def _run(video_id: str):
    # Canonical URL so yt-dlp never treats a &list= URL as a playlist.
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        md, info = await pipeline.run(
            url, JOBS_DIR / video_id, f"/files/{video_id}", lambda s: update_job(video_id, step=s)
        )
        update_job(
            video_id, status="done", markdown=md, title=info.get("title"), thumbnail=info.get("thumbnail")
        )
    except Exception as e:
        update_job(video_id, status="error", error=str(e) or type(e).__name__)


app.mount("/files", StaticFiles(directory=JOBS_DIR), name="files")
app.mount("/", StaticFiles(directory="static", html=True), name="static")
