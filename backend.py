import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask
from yt_dlp.utils import download_range_func

BASE = Path(__file__).parent


def load_env():
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


load_env()

FFMPEG_PATH = None
if not shutil.which("ffmpeg"):
    try:
        import imageio_ffmpeg
        FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

app = FastAPI(title="ZapStream")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

FORMATS = {"1080", "720", "480", "360", "mp3", "best"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")
JOBS = {}
OLLAMA = "http://127.0.0.1:11434"


class Req(BaseModel):
    url: str
    format: str = "720"
    start: float = 0
    end: float = 0


def clean(e):
    return ANSI.sub("", str(e)).replace("ERROR: ", "")[:300]


def hms(sec):
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def base_opts(**extra):
    o = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if FFMPEG_PATH:
        o["ffmpeg_location"] = FFMPEG_PATH
    o.update(extra)
    return o


def new_job(kind):
    for k in [k for k, v in JOBS.items() if time.time() - v["ts"] > 7200]:
        shutil.rmtree(JOBS[k].get("dir") or "", ignore_errors=True)
        JOBS.pop(k)
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"kind": kind, "status": "queued", "progress": 0, "message": "Starting...",
                 "result": None, "error": None, "file": None, "dir": None, "ts": time.time()}
    return jid


def upd(jid, **kw):
    JOBS[jid].update(kw)


# ---------- AI helpers ----------
def ollama_model():
    wanted = os.environ.get("OLLAMA_MODEL")
    if wanted:
        return wanted
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=2) as r:
            names = [m["name"] for m in json.loads(r.read()).get("models", [])]
        return names[0] if names else "llama3.2"
    except Exception:
        return "llama3.2"


def ai_provider():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude"
    try:
        urllib.request.urlopen(OLLAMA + "/api/tags", timeout=1).close()
        return "ollama"
    except Exception:
        return None


def ask_llm(prompt, max_tokens=3500):
    key = os.environ.get("ANTHROPIC_API_KEY")
    try:
        if key:
            body = json.dumps({"model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
                               "max_tokens": max_tokens,
                               "messages": [{"role": "user", "content": prompt}]}).encode()
            req = urllib.request.Request("https://api.anthropic.com/v1/messages", body,
                                         {"content-type": "application/json", "x-api-key": key,
                                          "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read())["content"][0]["text"]
        body = json.dumps({"model": ollama_model(), "prompt": prompt, "stream": False,
                           "format": "json", "options": {"num_ctx": 8192}}).encode()
        req = urllib.request.Request(OLLAMA + "/api/generate", body, {"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())["response"]
    except Exception as e:
        print("LLM ERROR:", e)
        return None


def parse_json(txt):
    if not txt:
        return None
    try:
        return json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
    except Exception as e:
        print("JSON PARSE ERROR:", e)
        return None


# ---------- basic endpoints ----------
@app.get("/")
def home():
    return FileResponse(BASE / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True,
            "ffmpeg": bool(shutil.which("ffmpeg") or FFMPEG_PATH),
            "ai": ai_provider()}


@app.get("/api/info")
def info(url: str = Query(..., min_length=5)):
    try:
        with yt_dlp.YoutubeDL(base_opts(skip_download=True)) as y:
            d = y.extract_info(url, download=False)
    except Exception as e:
        print("INFO ERROR:", e)
        raise HTTPException(400, "Could not read this link")
    heights = {f["height"] for f in d.get("formats") or []
               if f.get("height") and f.get("vcodec") not in (None, "none")}
    top = max(heights, default=0)
    formats = [{"id": str(h), "label": f"MP4 {h}p"} for h in (1080, 720, 480, 360) if top >= h]
    if not formats:
        formats = [{"id": "best", "label": "Best quality"}]
    formats.append({"id": "mp3", "label": "MP3 audio"})
    return {
        "title": d.get("title"),
        "channel": d.get("channel") or d.get("uploader"),
        "duration": hms(d["duration"]) if d.get("duration") else "N/A",
        "seconds": d.get("duration") or 0,
        "views": d.get("view_count"),
        "likes": d.get("like_count"),
        "thumbnail": d.get("thumbnail"),
        "site": d.get("extractor_key"),
        "captions": bool(d.get("subtitles") or d.get("automatic_captions")),
        "formats": formats,
    }


# ---------- video / clip download ----------
def run_download(jid, url, fmt, start, end):
    tmp = tempfile.mkdtemp(prefix="zapstream_")
    upd(jid, dir=tmp, status="running", message="Starting...")

    def hook(h):
        if h["status"] == "downloading":
            total = h.get("total_bytes") or h.get("total_bytes_estimate") or 0
            if total:
                upd(jid, progress=min(94, int(h["downloaded_bytes"] * 100 / total)), message="Downloading...")
        elif h["status"] == "finished":
            upd(jid, progress=95, message="Finishing with ffmpeg...")

    opts = base_opts(outtmpl=os.path.join(tmp, "%(title).80s.%(ext)s"), progress_hooks=[hook])
    if fmt == "mp3":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    elif fmt == "best":
        opts.update(format="best", merge_output_format="mp4")
    else:
        opts.update(format=f"bestvideo[height<={fmt}]+bestaudio/best[height<={fmt}]", merge_output_format="mp4")
    if start or end:
        opts["download_ranges"] = download_range_func(None, [(start, end or float("inf"))])
        opts["force_keyframes_at_cuts"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([url])
        files = [os.path.join(tmp, f) for f in os.listdir(tmp)]
        if not files:
            raise RuntimeError("No file was created")
        upd(jid, status="done", progress=100, message="Ready", file=max(files, key=os.path.getsize))
    except Exception as e:
        print("DOWNLOAD ERROR:", e)
        upd(jid, status="error", error=clean(e))


# ---------- study notes ----------
STOP = set("""the a an and or but if of to in on at for with as is are was were be been it this that these those i you he
she we they my your our their me him her us them so then than too very can will just not no yes do does did have has had
from by about into over up down out what which who when where why how there here also like okay ok um uh yeah really going
gonna want need get got one now let lets because would could should know see look right well""".split())


def to_sec(s):
    p = [float(x) for x in s.replace(",", ".").split(":")]
    return sum(v * 60 ** i for i, v in enumerate(reversed(p)))


def parse_vtt(txt):
    cues, last = [], []
    for block in re.split(r"\n\s*\n", txt):
        lines = block.strip().splitlines()
        idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if idx is None:
            continue
        start = to_sec(lines[idx].split("-->")[0].strip())
        for l in lines[idx + 1:]:
            l = re.sub(r"<[^>]+>", "", l)
            l = l.replace("&amp;", "&").replace("&gt;", ">").replace("&lt;", "<").replace("&#39;", "'").replace("&quot;", '"').strip()
            if l and not re.fullmatch(r"\[.*\]", l) and l not in last:
                cues.append((start, l))
                last = (last + [l])[-4:]
    return cues


def pick_sub(d):
    subs, auto = d.get("subtitles") or {}, d.get("automatic_captions") or {}
    lang = (d.get("language") or "en").split("-")[0]
    tries = ((subs, ["en", "en-US", "en-GB", "en-IN", lang, "hi"]),
             (auto, [lang + "-orig", "en-orig", lang, "en", "hi"]))
    for pool, wanted in tries:
        for k in wanted:
            for f in pool.get(k, []):
                if f.get("ext") == "vtt":
                    return f["url"]
    for k, fl in subs.items():
        for f in fl:
            if f.get("ext") == "vtt":
                return f["url"]
    return None


def whisper_transcribe(url):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError("This video has no captions. To transcribe its audio, run once: pip install faster-whisper")
    tmp = tempfile.mkdtemp(prefix="zapstream_aud_")
    try:
        with yt_dlp.YoutubeDL(base_opts(format="bestaudio/best", outtmpl=os.path.join(tmp, "a.%(ext)s"))) as y:
            y.download([url])
        f = os.path.join(tmp, os.listdir(tmp)[0])
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segs, _ = model.transcribe(f)
        return [(s.start, s.text.strip()) for s in segs if s.text.strip()]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def make_sections(cues, d, start, end):
    dur = d.get("duration") or (cues[-1][0] if cues else 0)
    lo, hi = start, (end or dur)
    cues = [c for c in cues if lo <= c[0] <= hi]
    chapters = [c for c in (d.get("chapters") or []) if c["end_time"] > lo and c["start_time"] < hi]
    if chapters:
        bounds = [(c["start_time"], c["end_time"], c.get("title")) for c in chapters]
    else:
        n = max(1, min(12, math.ceil((hi - lo) / 300)))
        step = (hi - lo) / n
        bounds = [(lo + i * step, lo + (i + 1) * step, None) for i in range(n)]
    out = []
    for s, e, t in bounds:
        text = " ".join(l for ts, l in cues if s <= ts < e)
        if len(text.split()) > 15:
            out.append({"start": s, "end": e, "title": t, "text": text})
    return out


def summarize(text, k=5):
    parts = re.split(r"(?<=[.!?])\s+", text)
    if len(parts) < 4:  # auto captions often have no punctuation
        w = text.split()
        parts = [" ".join(w[i:i + 22]) for i in range(0, len(w), 22)]
    freq = Counter(w for w in re.findall(r"\w{3,}", text.lower()) if w not in STOP)
    scored = []
    for i, s in enumerate(parts):
        ws = [w for w in re.findall(r"\w{3,}", s.lower()) if w not in STOP]
        if len(ws) >= 3:
            scored.append((sum(freq[w] for w in ws) / len(ws) ** 0.5, i, s))
    top = sorted(sorted(scored, reverse=True)[:k], key=lambda x: x[1])
    points = [s.strip()[:250] for _, _, s in top] or [p[:250] for p in parts[:3]]
    return points, [w for w, _ in freq.most_common(8)]


SECTION_PROMPT = """You are an expert teacher writing study material for a student who has NOT watched the video.
Video title: {title}
Part: {part}

From the transcript below, write clear and accurate notes. Rules:
- Explain every concept properly: what it is, why it matters and how it works. Do not copy sentences from the transcript.
- Use only ideas that appear in the transcript, but fix speech mistakes and fill in obvious wording gaps.
- Ignore greetings, filler, sponsor and subscribe talk.
- Keep formulas, commands and code exactly as shown in the "code" list.
- Write in the same language as the transcript (simple English if it is mixed).

Reply with ONLY this JSON:
{{"heading": "short topic title",
 "summary": "2-3 sentence overview of this part",
 "concepts": [{{"name": "concept name", "explanation": "3-5 sentence clear explanation", "example": "example from the video, or empty string"}}],
 "steps": ["ordered steps if the video shows a procedure, otherwise an empty list"],
 "code": ["formulas, commands or code mentioned, otherwise an empty list"],
 "takeaways": ["3-5 key points to remember"]}}

Transcript:
{text}"""

FINAL_PROMPT = """Below are the notes for each part of the video "{title}". Create the final revision pages.
Reply with ONLY this JSON:
{{"overview": "4-6 sentence summary of the whole video and what a student will learn",
 "prerequisites": ["things a student should know first, at most 4, may be empty"],
 "glossary": [{{"term": "term", "meaning": "one-line definition"}}],
 "quiz": [{{"q": "question that tests understanding", "a": "short correct answer"}}],
 "cheatsheet": ["8-12 one-line facts for quick revision"]}}
Give 6-10 glossary terms and 6 quiz questions.

Notes:
{notes}"""


def run_notes(jid, url, start, end):
    try:
        upd(jid, status="running", progress=8, message="Reading video details...")
        with yt_dlp.YoutubeDL(base_opts(skip_download=True)) as y:
            d = y.extract_info(url, download=False)
            sub = pick_sub(d)
            raw = y.urlopen(sub).read().decode("utf-8", "ignore") if sub else ""
        cues = parse_vtt(raw) if raw else []
        if not cues:
            upd(jid, progress=15, message="No captions found. Transcribing audio (slow)...")
            cues = whisper_transcribe(url)
        if not cues:
            raise RuntimeError("No captions or speech found in this video.")
        sections = make_sections(cues, d, start, end)
        if not sections:
            raise RuntimeError("Not enough spoken content in the selected part.")

        title = d.get("title") or ""
        provider = ai_provider()
        print("NOTES: AI provider =", provider)
        res, used_ai = [], False
        for i, s in enumerate(sections):
            upd(jid, progress=25 + int(65 * i / len(sections)),
                message=f"Writing notes {i + 1} of {len(sections)}...")
            part = f"{hms(s['start'])} - {hms(s['end'])}"
            n = None
            if provider:
                n = parse_json(ask_llm(SECTION_PROMPT.format(title=title, part=part, text=s["text"][:16000])))
            if n and (n.get("concepts") or n.get("takeaways")):
                used_ai = True
                n["title"] = s["title"] or n.get("heading") or f"Part {i + 1}"
            else:
                pts, _ = summarize(s["text"])
                n = {"title": s["title"] or f"Part {i + 1}", "summary": "", "concepts": [],
                     "steps": [], "code": [], "takeaways": pts}
            n["time"] = part
            res.append(n)

        extra = {}
        if used_ai:
            upd(jid, progress=94, message="Making glossary, quiz and revision sheet...")
            digest = "\n\n".join(
                f"## {r['title']}\n{r.get('summary', '')}\n" +
                "\n".join(f"- {c.get('name', '')}: {c.get('explanation', '')}" if isinstance(c, dict) else f"- {c}"
                          for c in (r.get("concepts") or []))
                for r in res)
            extra = parse_json(ask_llm(FINAL_PROMPT.format(title=title, notes=digest[:24000]))) or {}

        upd(jid, status="done", progress=100, message="Notes ready", result={
            "title": title, "channel": d.get("channel") or d.get("uploader"),
            "duration": hms(d["duration"]) if d.get("duration") else "",
            "range": f"{hms(start)} - {hms(end)}" if end else "",
            "ai": used_ai, "sections": res,
            "overview": extra.get("overview", ""),
            "prerequisites": extra.get("prerequisites", []),
            "glossary": extra.get("glossary", []),
            "quiz": extra.get("quiz", []),
            "cheatsheet": extra.get("cheatsheet", [])})
    except Exception as e:
        print("NOTES ERROR:", e)
        upd(jid, status="error", error=clean(e))


# ---------- job endpoints ----------
@app.post("/api/download")
def start_download(r: Req):
    if r.format not in FORMATS:
        raise HTTPException(400, "Unknown format")
    jid = new_job("video")
    threading.Thread(target=run_download, args=(jid, r.url, r.format, r.start, r.end), daemon=True).start()
    return {"id": jid}


@app.post("/api/notes")
def start_notes(r: Req):
    jid = new_job("notes")
    threading.Thread(target=run_notes, args=(jid, r.url, r.start, r.end), daemon=True).start()
    return {"id": jid}


@app.get("/api/job/{jid}")
def job(jid: str):
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "Unknown job")
    return {k: j[k] for k in ("status", "progress", "message", "result", "error")}


@app.get("/api/file/{jid}")
def get_file(jid: str):
    j = JOBS.get(jid)
    if not j or not j.get("file"):
        raise HTTPException(404, "File not ready")
    path = j["file"]
    return FileResponse(path, filename=os.path.basename(path),
                        background=BackgroundTask(shutil.rmtree, j["dir"], ignore_errors=True))