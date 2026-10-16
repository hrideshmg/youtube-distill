"""YouTube URL -> captions + video -> screenshot selection from frame boards -> article that places the screenshots."""

import asyncio
import base64
import html
import os
import re
import shutil
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import yt_dlp
from openai import AsyncOpenAI
from pydantic import BaseModel

MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna")

# Screenshot boards: one frame every BOARD_INTERVAL seconds, tiled BOARD_GRID per
# image with its timestamp burned in, so the model can compare frames side by side.
BOARD_INTERVAL = 12
BOARD_GRID = "3x3"
BOARD_TILE_WIDTH = 640

FIGURE_RE = re.compile(r"^\[\[FIGURE F(\d+) \| (.+?)\]\][ \t]*$", re.MULTILINE)
CUE_TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})\.\d{3} -->")

YDL_BASE = {"quiet": True, "no_warnings": True, "noprogress": True}
# NOTE: optional, e.g. YT_COOKIES_BROWSER=chromium. Uses that browser's logged-in
# YouTube cookies, which avoids 429s on subtitle downloads.
if browser := os.environ.get("YT_COOKIES_BROWSER"):
    YDL_BASE["cookiesfrombrowser"] = (browser,)
# NOTE: for headless servers with no browser, e.g. YT_COOKIES_FILE=/path/to/cookies.txt
# (Netscape format). yt-dlp may rewrite this file, so it must be writable.
if cookie_file := os.environ.get("YT_COOKIES_FILE"):
    YDL_BASE["cookiefile"] = cookie_file
# NOTE: optional path to a bgutil-ytdlp-pot-provider checkout's server/ dir. The plugin
# generates PO tokens with deno/node; without them, YouTube withholds web-client
# captions on flagged IPs. Only needed when the checkout isn't in ~ of the user running the app.
if pot_home := os.environ.get("BGUTIL_SERVER_HOME"):
    YDL_BASE["extractor_args"] = {"youtubepot-bgutilscript": {"server_home": [pot_home]}}

client = AsyncOpenAI()


class PipelineError(Exception):
    pass


# ---------- captions ----------


def fetch_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(YDL_BASE) as ydl:
        return ydl.extract_info(url, download=False)


def fetch_info_and_captions(url: str, job_dir: Path) -> tuple[dict, list[tuple[float, str]]]:
    """Return yt-dlp info dict and caption lines as (start_seconds, text).

    Prefers uploader-provided English subtitles over auto-generated ones.
    """
    # NOTE: English-only for now. Non-English videos fall back to YouTube's
    # auto-translated track if one exists.
    info = fetch_info(url)

    manual = _pick_lang(info.get("subtitles") or {})
    auto = None if manual else _pick_lang(info.get("automatic_captions") or {})
    if not (manual or auto):
        others = sorted({*(info.get("subtitles") or {}), *(info.get("automatic_captions") or {})})
        if others:
            raise PipelineError(f"This video has no English captions (available: {', '.join(others[:5])}).")
        # NOTE: YouTube also returns zero tracks when it throttles the server's IP
        # (403s / missing PO token), so an empty list isn't proof the video has none.
        raise PipelineError(
            "YouTube returned no caption tracks. The video may have none, or YouTube may be blocking this server."
        )

    # Let yt-dlp download the track itself: auto-captions are served as an HLS
    # playlist of VTT segments, not a single file.
    opts = {
        **YDL_BASE,
        "skip_download": True,
        "writesubtitles": bool(manual),
        "writeautomaticsub": bool(auto),
        "subtitleslangs": [manual or auto],
        "subtitlesformat": "vtt",
        "outtmpl": str(job_dir / "captions"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.process_ie_result(info, download=True)

    vtt_file = next(job_dir.glob("captions*.vtt"), None)
    if not vtt_file:
        raise PipelineError("Failed to download captions.")
    return info, parse_vtt(vtt_file.read_text())


def _pick_lang(tracks: dict) -> str | None:
    for lang in ("en", "en-orig", "en-US", "en-GB", *tracks.keys()):
        if lang.startswith("en") and lang in tracks:
            return lang
    return None


def parse_vtt(vtt: str) -> list[tuple[float, str]]:
    """Parse VTT into deduplicated lines.

    YouTube auto-captions use "rolling" cues where each cue repeats the
    previous line, so we drop any line identical to the last one emitted.
    """
    lines: list[tuple[float, str]] = []
    last = None
    for block in re.split(r"\n\s*\n", vtt):
        m = CUE_TIME_RE.search(block)
        if not m:
            continue
        h, mnt, s = map(int, m.groups())
        start = h * 3600 + mnt * 60 + s
        for raw in block.splitlines()[1:]:
            if "-->" in raw:
                continue
            text = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
            if text and text != last:
                lines.append((start, text))
                last = text
    return lines


def format_transcript(lines: list[tuple[float, str]], group_seconds: int = 15) -> str:
    """Group caption lines into ~15s chunks, each prefixed with [MM:SS]."""
    out, group, group_start = [], [], None
    for start, text in lines:
        if group_start is None:
            group_start = start
        elif start - group_start >= group_seconds:
            out.append(f"[{fmt_ts(group_start)}] {' '.join(group)}")
            group, group_start = [], start
        group.append(text)
    if group:
        out.append(f"[{fmt_ts(group_start)}] {' '.join(group)}")
    return "\n".join(out)


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_ts(ts: str) -> float:
    """Parse [H:]MM:SS[.mmm] into seconds."""
    total = 0.0
    for part in ts.split(":"):
        total = total * 60 + float(part)
    return total


# ---------- pass 1: article ----------

ARTICLE_INSTRUCTIONS = """\
You turn transcripts of educational videos into high-quality educational articles.

The article must stand on its own: the reader has never seen the video and should be able to learn the material from the article alone.

Content (what to teach):
- Teach everything of substance the video covers: explanations, examples, analogies, numbers, caveats, and the reasoning behind them. Do not compress or water it down; length should follow the material.
- Everything you teach must come from the video. Where the video assumes context the reader may lack (a term from a previous episode, an unstated prerequisite), add a brief, accurate explanation so the article is self-contained, but do not expand beyond the video's scope.
- Never refer to the video or the speaker ("in this video", "as shown here", "the speaker says"). Anything that relied on what was on screen must be made explicit in words, or shown with a figure.
- Drop filler, sponsor segments, channel/series housekeeping and requests to like/subscribe. Fix transcription errors using context, especially technical terms and names.

Teaching (how to present it) — you may reorganise the video's material freely to achieve this:
- Open with the problem or question that motivates the topic, not with a definition. Then say plainly what the reader will understand by the end.
- Give the big picture before the details: show the overall shape of the system or idea first, then zoom into each part.
- Build up step by step from the simplest case. Where the video's material allows, let each new idea arrive as the answer to a limitation of the previous one, so the reader sees why it exists.
- Anchor the explanation in one concrete running example from the video and come back to it at each step. Use the actual numbers the video gives.
- Intuition before formalism: walk through a concrete instance first, then give the general rule, formula or notation. Introduce notation and terms just before they are needed, and define each at first use.
- Anticipate the reader's questions and confusions; state the question explicitly ("Why not just…?", "What happens when…?") and answer it. Call out gotchas and common misconceptions the video mentions.
- Use the video's analogies to resolve a specific confusion, then connect them back to the precise meaning.
- Signpost transitions so the reader always knows where they are and why the next part follows ("Now that we know X, we can look at Y").
- Be honest about limits, tradeoffs and simplifications the video mentions.

Style and format (Markdown):
- Write in a clear, direct, friendly voice; "we" for working through ideas together is fine.
- Start with `# Title` describing the topic (not the video's title verbatim).
- Descriptive `##` / `###` headings that name the idea or question a section answers.
- Short paragraphs. **Bold** the single key sentence of an important paragraph, sparingly.
- Lists only for genuinely list-like content; fenced code blocks for code; tables where they clarify a comparison.
- Write maths in LaTeX: `\\( ... \\)` for inline and `\\[ ... \\]` on their own lines for display equations. Never use `$` as a maths delimiter.
- End with a conclusion that ties the pieces back into one coherent mental model, rather than a list of bullet points.

Figures:
The transcript is captions only; you cannot see the screen. Instead you get a list of available figures: screenshots of what was on screen, each with an ID, the time it was taken and a short description of what it shows. To use one, put it on its own line:

[[FIGURE F3 | caption]]

- Use only IDs from the list, each at most once. Place a figure where the transcript discusses it; its timestamp tells you where that is.
- Good explanations lean on visuals: use a figure wherever it makes the explanation clearer. You don't have to use them all.
- The caption is one short sentence stating the point the figure makes (e.g. "Each layer's activations are computed from the previous layer's."). It must not claim anything beyond what the figure's description says it shows.
- In the text around each figure, tell the reader what to notice in it.
- You only know a figure through its description. When it shows details the transcript doesn't give (exact code, numbers, formulas, labels), never invent or approximate them: refer the reader to the figure instead.
- If the list is empty, use no figures.
"""


async def write_article(info: dict, transcript: str, figures: list[tuple[float, str]]) -> str:
    chapters = info.get("chapters") or []
    chapter_text = "\n".join(f"[{fmt_ts(c['start_time'])}] {c['title']}" for c in chapters)
    figure_text = "\n".join(f"[F{i + 1}] {fmt_ts(t)} {shows}" for i, (t, shows) in enumerate(figures)) or "(none)"
    prompt = (
        f"Video title: {info.get('title')}\n"
        f"Channel: {info.get('channel')}\n"
        + (f"\nChapters (author-provided, use as structural hints):\n{chapter_text}\n" if chapters else "")
        + f"\nAvailable figures:\n{figure_text}\n"
        + f"\nTranscript:\n{transcript}"
    )
    resp = await client.responses.create(model=MODEL, instructions=ARTICLE_INSTRUCTIONS, input=prompt)
    return resp.output_text


# ---------- screenshots ----------


def download_video(url: str, job_dir: Path) -> Path:
    # NOTE: downloads the whole video (≤720p, video-only). Boards only decode its
    # keyframes, but yt-dlp can't fetch just those.
    opts = {
        **YDL_BASE,
        "format": "bv*[height<=720]/b[height<=720]/bv*/b",
        "outtmpl": str(job_dir / "video.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))


async def make_boards(video: Path, out_dir: Path) -> list[Path]:
    """Tile one frame every BOARD_INTERVAL seconds into boards, each frame stamped with its timestamp."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tile_h = BOARD_TILE_WIDTH * 9 // 16
    # Keep a frame once BOARD_INTERVAL has passed since the last kept one, so the
    # stamped time is the frame's real time (fps= would print a nominal one).
    vf = (
        f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{BOARD_INTERVAL})',"
        f"scale={BOARD_TILE_WIDTH}:{tile_h},"
        f"drawtext=text='%{{pts\\:hms}}':x=6:y=6:fontsize={BOARD_TILE_WIDTH // 20}"
        ":fontcolor=white:box=1:boxcolor=black@0.75:boxborderw=5,"
        f"tile={BOARD_GRID}:padding=8:margin=8:color=white"
    )
    # NOTE: -skip_frame nokey decodes only keyframes (YouTube puts one every ~5-6 s),
    # ~9x cheaper than a full decode on a small box. Spacing can stretch to ~15 s.
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-y", "-skip_frame", "nokey", "-i", str(video),
        "-vf", vf, "-fps_mode", "vfr", "-q:v", "3", str(out_dir / "board_%03d.jpg"),
    )  # fmt: skip
    if await proc.wait() != 0:
        return []
    return sorted(out_dir.glob("board_*.jpg"))


class Pick(BaseModel):
    timestamp: str
    shows: str


class Selection(BaseModel):
    picks: list[Pick]


SELECT_INSTRUCTIONS = """\
You choose screenshots for an educational article written from a video.
You get contact sheets ("boards") of frames sampled every 10-15 seconds, in time order: left to right, top to bottom, board after board. Each frame's timestamp is printed in its top-left corner.

Pick one frame for each distinct visual state: a slide, a code listing, a diagram, a chart, what is written on a board, a step of a demo or animation.
- When the same content stays on screen across several frames (even as annotations are added, bullets build up, or a board fills in), pick only the frame where it is most complete, normally the last one before it changes.
- A slide counts as new content when its text changes, even if its title and layout stay the same.
- Prefer a frame where the content is unobstructed over a later one where a person blocks part of it.
- Skip frames with nothing to read or see beyond a person talking: talking heads, logos, title cards without content, credits, transitions.

For each pick, return the timestamp exactly as printed, and in `shows` describe what the frame shows precisely enough for a writer who can't see it to decide where it belongs (topic, kind of visual, key labels or values).
"""


async def select_frames(boards: list[Path]) -> list[tuple[float, str]]:
    """Return (seconds, description) for each frame worth using as a figure, in time order."""
    content: list[dict] = []
    for i, board in enumerate(boards):
        b64 = base64.b64encode(board.read_bytes()).decode()
        content.append({"type": "input_text", "text": f"Board {i + 1}:"})
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}", "detail": "high"})
    # NOTE: one call sees every board, so it can dedupe across board boundaries.
    # Very long videos might do better split into overlapping chunks.
    resp = await client.responses.parse(
        model=MODEL,
        instructions=SELECT_INSTRUCTIONS,
        input=[{"role": "user", "content": content}],
        text_format=Selection,
    )
    picks = []
    for pick in resp.output_parsed.picks if resp.output_parsed else []:
        try:
            picks.append((parse_ts(pick.timestamp), pick.shows))
        except ValueError:
            continue  # mistyped timestamp
    return sorted(picks)


async def extract_frame(video: Path, at: float, out: Path) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-y", "-ss", f"{at:.3f}", "-i", str(video),
        "-frames:v", "1", "-q:v", "3", str(out),
    )  # fmt: skip
    return await proc.wait() == 0 and out.exists()


async def insert_figures(
    article: str, figures: list[tuple[float, str]], video: Path, job_dir: Path, url_prefix: str
) -> str:
    matches = list(FIGURE_RE.finditer(article))

    async def resolve(n: int, m: re.Match) -> str:
        idx = int(m.group(1)) - 1
        if not 0 <= idx < len(figures):
            return ""  # ID not in the list
        name = f"fig_{n}.jpg"
        if not await extract_frame(video, figures[idx][0], job_dir / name):
            return ""
        caption = m.group(2)
        return f"![{caption}]({url_prefix}/{name})\n\n*{caption}*\n"

    replacements = await asyncio.gather(*(resolve(n, m) for n, m in enumerate(matches)))

    # Splice replacements back in from the end so earlier offsets stay valid.
    for m, rep in zip(reversed(matches), reversed(replacements)):
        article = article[: m.start()] + rep + article[m.end() :]
    return article


# ---------- entry point ----------


async def run(url: str, job_dir: Path, url_prefix: str, step: Callable[[str], None] = print) -> tuple[str, dict]:
    """Run the full pipeline and return the article as Markdown plus the yt-dlp info dict."""
    job_dir.mkdir(parents=True, exist_ok=True)

    step("Fetching captions")
    info, lines = await asyncio.to_thread(fetch_info_and_captions, url, job_dir)

    # NOTE: download runs after captions, not in parallel, so a video without
    # captions fails fast and we don't double the YouTube requests from a flagged IP.
    step("Downloading video for screenshots")
    video = await asyncio.to_thread(download_video, url, job_dir)
    try:
        step("Selecting screenshots")
        boards = await make_boards(video, job_dir / "boards")
        figures = await select_frames(boards) if boards else []

        step("Writing article")
        article = await write_article(info, format_transcript(lines), figures)
        article = await insert_figures(article, figures, video, job_dir, url_prefix)
    finally:
        video.unlink(missing_ok=True)
        shutil.rmtree(job_dir / "boards", ignore_errors=True)

    (job_dir / "article.md").write_text(article)
    return article, info


if __name__ == "__main__":
    # Usage: uv run python -m app.pipeline <youtube-url>
    job_dir = Path("data/jobs") / f"cli-{uuid.uuid4().hex[:8]}"
    asyncio.run(run(sys.argv[1], job_dir, url_prefix="."))
    print(f"Wrote {job_dir / 'article.md'}")
