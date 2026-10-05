import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv
from faster_whisper import WhisperModel
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse

load_dotenv()

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# ---------------- Settings ----------------
DAILY_LIMIT = 5               # clips per visitor per day
IP_DAILY_LIMIT = 15           # clips per IP per day (stops cookie-clearing abuse)
MAX_CLIPS_PER_VIDEO = 3
MIN_CLIP_SEC = 15
MAX_CLIP_SEC = 60
MAX_UPLOAD_MB = 300
MAX_VIDEO_MIN = 30
KEEP_HOURS = 24               # clips auto-delete after this
TZ = ZoneInfo("Asia/Kolkata") # daily reset at midnight IST
CLAUDE_MODEL = "claude-sonnet-5-5"
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

DATA_DIR = os.getenv("DATA_DIR", "./data")
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
DB_PATH = os.path.join(DATA_DIR, "usage.db")
os.makedirs(JOBS_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("clipsite")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
executor = ThreadPoolExecutor(max_workers=1)  # one video at a time
jobs: dict = {}
jobs_lock = threading.Lock()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------- Usage / daily limit ----------------
def db_init():
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS usage (
                key TEXT, day TEXT, clips_used INTEGER DEFAULT 0,
                PRIMARY KEY (key, day))"""
        )


def today() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def used(key: str) -> int:
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            "SELECT clips_used FROM usage WHERE key=? AND day=?", (key, today())
        ).fetchone()
    return row[0] if row else 0


def add_usage(key: str, n: int):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            """INSERT INTO usage (key, day, clips_used) VALUES (?, ?, MAX(0, ?))
               ON CONFLICT(key, day) DO UPDATE SET clips_used = MAX(0, clips_used + ?)""",
            (key, today(), n, n),
        )


def remaining(cid: str, ip: str) -> int:
    return max(0, min(DAILY_LIMIT - used(f"c:{cid}"), IP_DAILY_LIMIT - used(f"ip:{ip}")))


def reserve(cid: str, ip: str, n: int):
    add_usage(f"c:{cid}", n)
    add_usage(f"ip:{ip}", n)


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def ensure_cid(request: Request, response: Response) -> str:
    cid = request.cookies.get("cid")
    if not cid or not re.fullmatch(r"[0-9a-f]{32}", cid):
        cid = uuid.uuid4().hex
        response.set_cookie("cid", cid, max_age=60 * 60 * 24 * 365,
                            httponly=True, samesite="lax")
    return cid


# ---------------- Video processing (blocking) ----------------
def video_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def transcribe(path: str):
    segments, _ = whisper.transcribe(path, word_timestamps=True, vad_filter=True)
    words, lines = [], []
    for seg in segments:
        lines.append(f"[{seg.start:.1f}] {seg.text.strip()}")
        for w in seg.words or []:
            words.append({"start": w.start, "end": w.end, "word": w.word.strip()})
    return words, "\n".join(lines)


def pick_moments(transcript: str, n: int, duration: float):
    prompt = f"""Below is a timestamped transcript (seconds) of a video that is {duration:.0f} seconds long.
Pick the {n} most engaging, self-contained moments for short vertical clips.
Rules:
- Each clip must be between {MIN_CLIP_SEC} and {MAX_CLIP_SEC} seconds long.
- Start at the beginning of a sentence and end at the end of a sentence.
- Clips must not overlap.
- Reply with ONLY a JSON array, no other text:
[{{"start": 12.0, "end": 55.0, "title": "short title"}}]

Transcript:
{transcript}"""
    msg = claude.messages.create(
        model=CLAUDE_MODEL, max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = msg.content[0].text
    data = json.loads(text[text.index("["): text.rindex("]") + 1])
    clips = []
    for c in data:
        start = max(0.0, float(c["start"]))
        end = min(duration, float(c["end"]))
        if end - start >= MIN_CLIP_SEC:
            clips.append({"start": start, "end": min(end, start + MAX_CLIP_SEC),
                          "title": str(c.get("title", "Clip"))})
    return clips[:n]


def srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(words, start, end, path, group=4):
    cw = [w for w in words if w["start"] >= start and w["end"] <= end]
    with open(path, "w", encoding="utf-8") as f:
        for idx, i in enumerate(range(0, len(cw), group), 1):
            chunk = cw[i: i + group]
            f.write(f"{idx}\n{srt_time(chunk[0]['start'] - start)} --> "
                    f"{srt_time(chunk[-1]['end'] - start)}\n")
            f.write(" ".join(w["word"] for w in chunk) + "\n\n")


def cut_clip(workdir, src, words, clip, out_name):
    write_srt(words, clip["start"], clip["end"], os.path.join(workdir, "cap.srt"))
    style = ("FontName=Arial,FontSize=14,Bold=1,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Alignment=2,MarginV=60")
    vf = ("crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=720:1280,"
          f"subtitles=cap.srt:force_style='{style}'")
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(clip["start"]), "-to", str(clip["end"]),
         "-i", src, "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "23", "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart", out_name],
        cwd=workdir, check=True, capture_output=True,
    )


def set_job(job_id, **kw):
    with jobs_lock:
        jobs[job_id].update(kw)


def run_job(job_id: str, n: int, cid: str, ip: str):
    jdir = os.path.join(JOBS_DIR, job_id)
    src = os.path.join(jdir, "input.mp4")
    produced = 0
    try:
        set_job(job_id, status="processing", message="Video check ho rahi hai...")
        duration = video_duration(src)
        if duration > MAX_VIDEO_MIN * 60:
            raise ValueError(f"Video {MAX_VIDEO_MIN} minute se lambi hai.")

        set_job(job_id, message="Awaaz ko text me badla ja raha hai...")
        words, transcript = transcribe(src)
        if not words:
            raise ValueError("Video me koi speech nahi mili.")

        set_job(job_id, message="Best moments dhundhe ja rahe hain...")
        moments = pick_moments(transcript, n, duration)
        if not moments:
            raise ValueError("Achhe clips nahi mile, dusri video try karo.")

        clips = []
        for i, m in enumerate(moments, 1):
            set_job(job_id, message=f"Clip {i}/{len(moments)} ban rahi hai...")
            name = f"clip{i}.mp4"
            cut_clip(jdir, src, words, m, name)
            clips.append({"title": m["title"], "name": name,
                          "url": f"/clips/{job_id}/{name}"})
            produced += 1
        set_job(job_id, status="done", message="Ho gaya!", clips=clips)
    except ValueError as e:
        set_job(job_id, status="error", error=str(e))
    except Exception:
        log.exception("job failed")
        set_job(job_id, status="error", error="Kuch gadbad ho gayi, thodi der baad try karo.")
    finally:
        # refund clips that were reserved but not produced
        if produced < n:
            reserve(cid, ip, produced - n)
        for f in ("input.mp4", "cap.srt"):
            try:
                os.remove(os.path.join(jdir, f))
            except OSError:
                pass


def cleanup_loop():
    while True:
        time.sleep(1800)
        cutoff = time.time() - KEEP_HOURS * 3600
        for name in os.listdir(JOBS_DIR):
            p = os.path.join(JOBS_DIR, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    shutil.rmtree(p, ignore_errors=True)
                    with jobs_lock:
                        jobs.pop(name, None)
            except OSError:
                pass


# ---------------- API ----------------
app = FastAPI(title="Clip Maker")


@app.on_event("startup")
def startup():
    db_init()
    threading.Thread(target=cleanup_loop, daemon=True).start()


@app.get("/")
def index(request: Request):
    resp = FileResponse(os.path.join(BASE_DIR, "static", "index.html"))
    ensure_cid(request, resp)
    return resp


@app.get("/api/usage")
def api_usage(request: Request, response: Response):
    cid = ensure_cid(request, response)
    rem = remaining(cid, client_ip(request))
    return {"limit": DAILY_LIMIT, "remaining": rem, "used": DAILY_LIMIT - rem,
            "max_upload_mb": MAX_UPLOAD_MB, "max_video_min": MAX_VIDEO_MIN}


@app.post("/api/upload")
async def api_upload(request: Request, response: Response, file: UploadFile = File(...)):
    cid = ensure_cid(request, response)
    ip = client_ip(request)

    rem = remaining(cid, ip)
    if rem <= 0:
        raise HTTPException(429, "Aaj ka limit khatam ho gaya. Kal phir aana!")

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, "Sirf video file upload karo (mp4, mov, mkv, webm).")

    job_id = uuid.uuid4().hex
    jdir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(jdir)
    src = os.path.join(jdir, "input.mp4")
    size, max_bytes = 0, MAX_UPLOAD_MB * 1024 * 1024
    try:
        with open(src, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, f"File {MAX_UPLOAD_MB} MB se badi hai.")
                f.write(chunk)
    except Exception:
        shutil.rmtree(jdir, ignore_errors=True)
        raise

    n = min(MAX_CLIPS_PER_VIDEO, rem)
    reserve(cid, ip, n)
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "message": "Line me hai, jaldi shuru hoga...",
                        "clips": [], "error": None, "cid": cid}
    executor.submit(run_job, job_id, n, cid, ip)
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
def api_job(job_id: str, request: Request):
    cid = request.cookies.get("cid")
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job["cid"] != cid:
            raise HTTPException(404, "Job nahi mila (shayad expire ho gaya).")
        return {k: v for k, v in job.items() if k != "cid"}


@app.get("/clips/{job_id}/{name}")
def get_clip(job_id: str, name: str, dl: int = 0):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id) or not re.fullmatch(r"clip\d+\.mp4", name):
        raise HTTPException(404)
    path = os.path.join(JOBS_DIR, job_id, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "Clip expire ho gayi.")
    return FileResponse(path, media_type="video/mp4",
                        filename=name if dl else None)
