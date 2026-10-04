"""YouTube URL -> captions -> article (pass 1) -> frames -> figure selection (pass 2)."""

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

# Frame window around a figure timestamp. Skewed forward because speakers
# usually say "look at this" before the visual is finished.
WINDOW_BEFORE = 10
WINDOW_AFTER = 20
MAX_CANDIDATES = 8

FIGURE_RE = re.compile(r"^\[\[FIGURE (\d{1,2}(?::\d{2}){1,2}) \| (.+?)\]\][ \t]*$", re.MULTILINE)
CUE_TIME_RE = re.compile(r"(\d+):(\d{2}):(\d{2})\.\d{3} -->")

YDL_BASE = {"quiet": True, "no_warnings": True, "noprogress": True}

client = AsyncOpenAI()


class PipelineError(Exception):
    pass


# ---------- captions ----------


def fetch_info_and_captions(url: str, job_dir: Path) -> tuple[dict, list[tuple[float, str]]]:
    """Return yt-dlp info dict and caption lines as (start_seconds, text).

    Prefers uploader-provided English subtitles over auto-generated ones.
    """
    # NOTE: English-only for now. Non-English videos fall back to YouTube's
    # auto-translated track if one exists.
    with yt_dlp.YoutubeDL(YDL_BASE) as ydl:
        info = ydl.extract_info(url, download=False)

    manual = _pick_lang(info.get("subtitles") or {})
    auto = None if manual else _pick_lang(info.get("automatic_captions") or {})
    if not (manual or auto):
        raise PipelineError("This video has no English captions.")

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


def parse_ts(ts: str) -> int:
    total = 0
    for part in ts.split(":"):
        total = total * 60 + int(part)
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
The transcript is captions only; you cannot see the screen. When the transcript indicates that something on screen helps understanding (a diagram, chart, slide, code listing, equation, animation or demo), insert a placeholder on its own line:

[[FIGURE MM:SS | short description of what should be visible]]

Use the timestamp where that visual is being discussed. Good explanations lean on visuals: add a figure wherever one would make the explanation clearer, but never for a person talking. In the text around each figure, tell the reader what to notice in it.
"""


async def write_article(info: dict, transcript: str) -> str:
    chapters = info.get("chapters") or []
    chapter_text = "\n".join(f"[{fmt_ts(c['start_time'])}] {c['title']}" for c in chapters)
    prompt = (
        f"Video title: {info.get('title')}\n"
        f"Channel: {info.get('channel')}\n"
        + (f"\nChapters (author-provided, use as structural hints):\n{chapter_text}\n" if chapters else "")
        + f"\nTranscript:\n{transcript}"
    )
    resp = await client.responses.create(model=MODEL, instructions=ARTICLE_INSTRUCTIONS, input=prompt)
    return resp.output_text


# ---------- frames ----------


def download_video(url: str, job_dir: Path) -> Path:
    # NOTE: downloads the whole video (≤720p, video-only) even though we only
    # need a few short windows. Simple and reliable; yt-dlp's download_ranges
    # could cut this down if disk/bandwidth becomes a problem.
    opts = {
        **YDL_BASE,
        "format": "bv*[height<=720]/b[height<=720]/bv*/b",
        "outtmpl": str(job_dir / "video.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))


async def extract_candidates(video: Path, at: int, out_dir: Path) -> list[Path]:
    """Sample 1 fps around `at`, dropping near-duplicate frames via mpdecimate."""
    out_dir.mkdir(parents=True, exist_ok=True)
    start = max(0, at - WINDOW_BEFORE)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-y",
        "-ss", str(start), "-i", str(video), "-t", str(WINDOW_BEFORE + WINDOW_AFTER),
        "-vf", "fps=1,mpdecimate", "-fps_mode", "vfr", "-q:v", "3",
        str(out_dir / "%02d.jpg"),
    )  # fmt: skip
    if await proc.wait() != 0:
        return []
    frames = sorted(out_dir.glob("*.jpg"))
    if len(frames) > MAX_CANDIDATES:
        # Evenly subsample when the scene keeps changing (e.g. speaker in frame).
        step = len(frames) / MAX_CANDIDATES
        frames = [frames[int(i * step)] for i in range(MAX_CANDIDATES)]
    return frames


# ---------- pass 2: figure selection ----------


class FigureChoice(BaseModel):
    keep: bool
    frame_index: int
    caption: str


FIGURE_INSTRUCTIONS = """\
You are choosing a screenshot for an educational article written from a video.
You will get a description of the figure the article needs, followed by numbered frames sampled in time order around that moment.

Pick the single frame that best shows the described content:
- Prefer the most complete version (e.g. a fully drawn diagram, a fully typed code listing).
- Prefer legible, sharp frames; avoid transitions, motion blur, or overlays covering the content.
- If no frame clearly shows it (e.g. only a person talking, or unrelated content), set keep=false.

The caption is one short sentence stating the point the figure makes (e.g. "Each layer's activations are computed from the previous layer's."), not just a description of what is visible.
"""


async def choose_frame(description: str, frames: list[Path]) -> FigureChoice | None:
    content: list[dict] = [{"type": "input_text", "text": f"Figure needed: {description}"}]
    for i, frame in enumerate(frames):
        b64 = base64.b64encode(frame.read_bytes()).decode()
        content.append({"type": "input_text", "text": f"Frame {i}:"})
        # NOTE: "low" detail keeps this cheap; bump to "high" if it starts
        # rejecting frames with small text it can't read.
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}", "detail": "low"})
    resp = await client.responses.parse(
        model=MODEL,
        instructions=FIGURE_INSTRUCTIONS,
        input=[{"role": "user", "content": content}],
        text_format=FigureChoice,
    )
    choice = resp.output_parsed
    if not choice or not choice.keep or not 0 <= choice.frame_index < len(frames):
        return None
    return choice


async def resolve_figures(article: str, url: str, job_dir: Path, url_prefix: str, step: Callable[[str], None]) -> str:
    figures = list(FIGURE_RE.finditer(article))
    if not figures:
        return article

    step("Downloading video for screenshots")
    video = await asyncio.to_thread(download_video, url, job_dir)

    step(f"Selecting screenshots for {len(figures)} figures")
    sem = asyncio.Semaphore(4)

    async def resolve(n: int, m: re.Match) -> str:
        async with sem:
            frames = await extract_candidates(video, parse_ts(m.group(1)), job_dir / "candidates" / str(n))
            choice = await choose_frame(m.group(2), frames) if frames else None
        if not choice:
            return ""
        name = f"fig_{n}.jpg"
        shutil.copy(frames[choice.frame_index], job_dir / name)
        return f"![{choice.caption}]({url_prefix}/{name})\n\n*{choice.caption}*\n"

    replacements = await asyncio.gather(*(resolve(n, m) for n, m in enumerate(figures)))

    # Splice replacements back in from the end so earlier offsets stay valid.
    for m, rep in zip(reversed(figures), reversed(replacements)):
        article = article[: m.start()] + rep + article[m.end() :]

    video.unlink(missing_ok=True)
    shutil.rmtree(job_dir / "candidates", ignore_errors=True)
    return article


# ---------- entry point ----------


async def run(url: str, job_dir: Path, url_prefix: str, step: Callable[[str], None] = print) -> str:
    """Run the full pipeline and return the article as Markdown."""
    job_dir.mkdir(parents=True, exist_ok=True)

    step("Fetching captions")
    info, lines = await asyncio.to_thread(fetch_info_and_captions, url, job_dir)

    step("Writing article")
    article = await write_article(info, format_transcript(lines))

    article = await resolve_figures(article, url, job_dir, url_prefix, step)
    (job_dir / "article.md").write_text(article)
    return article


if __name__ == "__main__":
    # Usage: uv run python -m app.pipeline <youtube-url>
    job_dir = Path("data/jobs") / f"cli-{uuid.uuid4().hex[:8]}"
    asyncio.run(run(sys.argv[1], job_dir, url_prefix="."))
    print(f"Wrote {job_dir / 'article.md'}")
