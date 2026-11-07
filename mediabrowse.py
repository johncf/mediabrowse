#!/usr/bin/env python3
"""mediabrowse - browse the media in a folder or zip file through a local web UI.

    pip install -r requirements.txt
    brew install ffmpeg            # optional, for video thumbnails
    python mediabrowse.py ~/Pictures
    python mediabrowse.py ~/Downloads/holiday.zip --port 9000 --open

Thumbnails are generated lazily and kept in one SQLite file
(default: ~/Library/Caches/mediabrowse/thumbs.db on macOS).
"""
from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from PIL import Image, ImageOps
from pydantic import BaseModel

try:  # optional HEIC support
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:
    pass

# --------------------------------------------------------------------------
# Media types
# --------------------------------------------------------------------------

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif"}
VIDEO_EXT = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}

mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/heif", ".heif")
mimetypes.add_type("image/avif", ".avif")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/opus", ".opus")

MAX_ZIP_VIDEO_THUMB = 1 << 30  # don't extract videos bigger than 1 GiB just for a thumbnail


def kind_of(name: str) -> Optional[str]:
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return None


def natural_key(name: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name.lower())]


def clean_path(path: str) -> str:
    """Normalise a user-supplied relative path; reject anything sneaky."""
    if "\x00" in path:
        raise ValueError("bad path")
    parts = []
    for seg in path.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError("bad path")
        parts.append(seg)
    return "/".join(parts)


# --------------------------------------------------------------------------
# Sources: one interface, two implementations
# --------------------------------------------------------------------------


@dataclass
class Entry:
    name: str
    path: str  # relative, '/'-separated, '' = root
    is_dir: bool
    size: int
    mtime: float
    kind: Optional[str]  # image | video | audio | None for dirs


def _sorted(entries: list[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda e: (not e.is_dir, natural_key(e.name)))


class Source:
    label: str

    def list(self, path: str) -> list[Entry]: ...
    def stat(self, path: str) -> Entry: ...
    def signature(self, path: str) -> str: ...
    def open(self, path: str) -> BinaryIO: ...
    def local_path(self, path: str) -> Optional[Path]:
        return None


class DirSource(Source):
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.label = self.root.name or str(self.root)

    def _resolve(self, path: str) -> Path:
        p = self.root.joinpath(*path.split("/")) if path else self.root
        p = p.resolve()
        # blocks '..' tricks and symlinks that lead outside the root
        if p != self.root and not p.is_relative_to(self.root):
            raise PermissionError(path)
        return p

    def list(self, path: str) -> list[Entry]:
        d = self._resolve(path)
        if not d.is_dir():
            raise FileNotFoundError(path)
        out: list[Entry] = []
        with os.scandir(d) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    st = e.stat()
                    rel = f"{path}/{e.name}" if path else e.name
                    if e.is_dir():
                        out.append(Entry(e.name, rel, True, 0, st.st_mtime, None))
                    else:
                        k = kind_of(e.name)
                        if k:
                            out.append(Entry(e.name, rel, False, st.st_size, st.st_mtime, k))
                except OSError:
                    continue
        return _sorted(out)

    def stat(self, path: str) -> Entry:
        p = self._resolve(path)
        if not p.is_file():
            raise FileNotFoundError(path)
        k = kind_of(p.name)
        if not k:
            raise FileNotFoundError(path)
        st = p.stat()
        return Entry(p.name, path, False, st.st_size, st.st_mtime, k)

    def signature(self, path: str) -> str:
        p = self._resolve(path)
        st = p.stat()
        return f"dir|{p}|{st.st_mtime_ns}|{st.st_size}"

    def open(self, path: str) -> BinaryIO:
        return open(self._resolve(path), "rb")

    def local_path(self, path: str) -> Optional[Path]:
        return self._resolve(path)


class ZipSource(Source):
    def __init__(self, zip_path: Path):
        self.zip_path = Path(zip_path).expanduser().resolve()
        st = self.zip_path.stat()
        self._zsig = f"{st.st_mtime_ns}|{st.st_size}"
        self.label = self.zip_path.name
        self._tl = threading.local()
        self.files: dict[str, zipfile.ZipInfo] = {}
        self.children: dict[str, dict[str, Entry]] = {"": {}}
        self._index()

    def _index(self) -> None:
        with zipfile.ZipFile(self.zip_path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                parts = [p for p in info.filename.split("/") if p]
                if not parts or parts[0] == "__MACOSX" or any(p.startswith(".") for p in parts):
                    continue
                kind = kind_of(parts[-1])
                if not kind:
                    continue
                try:
                    mtime = time.mktime(info.date_time + (0, 0, -1))
                except (ValueError, OverflowError):
                    mtime = 0.0
                # register parent folders (only folders that contain media appear)
                for i in range(len(parts) - 1):
                    parent = "/".join(parts[:i])
                    full = "/".join(parts[: i + 1])
                    self.children.setdefault(parent, {}).setdefault(
                        parts[i], Entry(parts[i], full, True, 0, 0.0, None)
                    )
                    self.children.setdefault(full, {})
                full = "/".join(parts)
                self.files[full] = info
                self.children.setdefault("/".join(parts[:-1]), {})[parts[-1]] = Entry(
                    parts[-1], full, False, info.file_size, mtime, kind
                )

    def list(self, path: str) -> list[Entry]:
        kids = self.children.get(path)
        if kids is None:
            raise FileNotFoundError(path)
        return _sorted(list(kids.values()))

    def stat(self, path: str) -> Entry:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.children["/".join(path.split("/")[:-1])][path.split("/")[-1]]

    def signature(self, path: str) -> str:
        info = self.files.get(path)
        if info is None:
            raise FileNotFoundError(path)
        return f"zip|{self.zip_path}|{self._zsig}|{path}|{info.CRC}|{info.file_size}"

    def _zf(self) -> zipfile.ZipFile:
        # ZipFile objects aren't safe to share naively; keep one per worker thread
        zf = getattr(self._tl, "zf", None)
        if zf is None:
            zf = self._tl.zf = zipfile.ZipFile(self.zip_path)
        return zf

    def open(self, path: str) -> BinaryIO:
        info = self.files.get(path)
        if info is None:
            raise FileNotFoundError(path)
        return self._zf().open(info)


# --------------------------------------------------------------------------
# Thumbnail cache (one SQLite file)
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS thumbs (
    key      TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    data     BLOB NOT NULL,
    created  REAL NOT NULL,
    last_hit REAL NOT NULL
) WITHOUT ROWID
"""


class ThumbCache:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._tl = threading.local()
        c = self._conn()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute(SCHEMA)
        c.execute("DELETE FROM thumbs WHERE last_hit < ?", (time.time() - 90 * 86400,))
        c.commit()

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._tl, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.execute("PRAGMA synchronous=NORMAL")
            self._tl.c = c
        return c

    def get(self, key: str) -> Optional[bytes]:
        c = self._conn()
        row = c.execute("SELECT data, last_hit FROM thumbs WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        if time.time() - row[1] > 86400:  # touch at most once a day
            with c:
                c.execute("UPDATE thumbs SET last_hit=? WHERE key=?", (time.time(), key))
        return row[0]

    def put(self, key: str, size: int, data: bytes) -> None:
        now = time.time()
        c = self._conn()
        with c:
            c.execute(
                "INSERT OR REPLACE INTO thumbs (key,size,data,created,last_hit) VALUES (?,?,?,?,?)",
                (key, size, data, now, now),
            )


class KeyedLocks:
    """Per-key mutex so 50 requests for the same missing thumb do the work once."""

    def __init__(self) -> None:
        self._g = threading.Lock()
        self._l: dict[str, list] = {}

    @contextmanager
    def hold(self, key: str):
        with self._g:
            slot = self._l.setdefault(key, [threading.Lock(), 0])
            slot[1] += 1
        try:
            with slot[0]:
                yield
        finally:
            with self._g:
                slot[1] -= 1
                if slot[1] == 0:
                    del self._l[key]


# --------------------------------------------------------------------------
# Favorites (plain text file, one relative path per line)
# --------------------------------------------------------------------------


class Favorites:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()

    def _read(self) -> list[str]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return list(dict.fromkeys(l.strip() for l in lines if l.strip()))

    def all(self) -> list[str]:
        with self._lock:
            return self._read()

    def set(self, rel: str, on: bool) -> list[str]:
        with self._lock:
            # re-read every time so hand edits to the file aren't clobbered
            favs = self._read()
            if on and rel not in favs:
                favs.append(rel)
            elif not on and rel in favs:
                favs.remove(rel)
            else:
                return favs
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text("".join(f + "\n" for f in favs), encoding="utf-8")
            os.replace(tmp, self.path)  # atomic: never leave a half-written file
            return favs


# --------------------------------------------------------------------------
# Thumbnail generation
# --------------------------------------------------------------------------


def image_thumb(src, size: int) -> bytes:
    with Image.open(src) as im:
        im.draft("RGB", (size * 2, size * 2))  # fast JPEG downscale
        im = ImageOps.exif_transpose(im)
        im.thumbnail((size, size))
        if im.mode not in ("RGB", "RGBA"):
            has_alpha = "A" in im.getbands() or "transparency" in im.info
            im = im.convert("RGBA" if has_alpha else "RGB")
        out = BytesIO()
        im.save(out, "WEBP", quality=80)
        return out.getvalue()


def video_thumb(source: Source, entry: Entry, size: int) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not installed (brew install ffmpeg)")
    local = source.local_path(entry.path)
    tmp = None
    try:
        if local is None:  # inside a zip: ffmpeg needs a real file
            if entry.size > MAX_ZIP_VIDEO_THUMB:
                raise RuntimeError("video too large to extract for a thumbnail")
            tmp = tempfile.NamedTemporaryFile(suffix=Path(entry.name).suffix, delete=False)
            with source.open(entry.path) as f:
                shutil.copyfileobj(f, tmp, 1 << 20)
            tmp.close()
            local = Path(tmp.name)
        frame = b""
        for ts in ("1", "0"):  # try 1s in, fall back to first frame (short clips)
            r = subprocess.run(
                [ffmpeg, "-v", "error", "-ss", ts, "-i", str(local), "-frames:v", "1",
                 "-vf", f"scale='min({size * 2},iw)':-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                capture_output=True, timeout=60,
            )
            if r.returncode == 0 and r.stdout:
                frame = r.stdout
                break
        if not frame:
            raise RuntimeError("ffmpeg produced no frame")
        return image_thumb(BytesIO(frame), size)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


def generate_thumb(source: Source, entry: Entry, size: int) -> bytes:
    if entry.kind == "image":
        local = source.local_path(entry.path)
        if local is not None:
            return image_thumb(local, size)
        with source.open(entry.path) as f:  # zip: read whole entry so Pillow can seek freely
            return image_thumb(BytesIO(f.read()), size)
    if entry.kind == "video":
        return video_thumb(source, entry, size)
    raise RuntimeError("no thumbnail for this type")


# --------------------------------------------------------------------------
# Web app
# --------------------------------------------------------------------------


class FavoriteIn(BaseModel):
    path: str
    on: bool = True


def create_app(source: Source, cache: ThumbCache, favorites: Favorites) -> FastAPI:
    app = FastAPI(title="mediabrowse", docs_url=None, redoc_url=None)
    locks = KeyedLocks()
    slots = threading.Semaphore(max(2, os.cpu_count() or 2))  # cap concurrent thumb jobs
    failed: set[str] = set()  # don't retry broken files on every page load

    @app.exception_handler(FileNotFoundError)
    async def _nf(_, exc):
        return JSONResponse({"error": "not found"}, status_code=404)

    @app.exception_handler(PermissionError)
    async def _pe(_, exc):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    @app.exception_handler(ValueError)
    async def _ve(_, exc):
        return JSONResponse({"error": "bad request"}, status_code=400)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return INDEX_HTML

    # NOTE: plain `def` endpoints run in FastAPI's threadpool, so blocking
    # I/O and CPU work (Pillow, ffmpeg, sqlite) never stalls the event loop.

    @app.get("/api/list")
    def api_list(path: str = ""):
        path = clean_path(path)
        entries = source.list(path)
        return {"path": path, "label": source.label, "entries": [asdict(e) for e in entries]}

    @app.get("/api/favorites")
    def api_favorites():
        return {"paths": favorites.all()}

    @app.get("/api/favorites/entries")
    def api_favorite_entries():
        entries = []
        for path in reversed(favorites.all()):  # most recently favorited first
            try:
                entries.append(asdict(source.stat(clean_path(path))))
            except (OSError, ValueError):
                continue  # moved/deleted since it was favorited; leave the line alone
        return {"path": "", "label": source.label, "entries": entries}

    # JSON body (not query params) so a random web page can't toggle favorites
    # with a simple cross-site form POST
    @app.post("/api/favorites")
    def api_set_favorite(body: FavoriteIn):
        path = clean_path(body.path)
        source.stat(path)  # only real media files can be favorited
        return {"paths": favorites.set(path, body.on)}

    @app.get("/thumb")
    def thumb(request: Request, path: str, size: int = 256):
        path = clean_path(path)
        size = 512 if size > 256 else 256  # snap to two fixed sizes
        entry = source.stat(path)
        key = hashlib.sha1(f"{source.signature(path)}|{size}".encode()).hexdigest()
        headers = {"ETag": f'"{key}"', "Cache-Control": "private, max-age=86400"}
        if request.headers.get("if-none-match") == headers["ETag"]:
            return Response(status_code=304, headers=headers)
        data = cache.get(key)
        if data is None:
            if key in failed:
                return JSONResponse({"error": "no thumbnail"}, status_code=404)
            with locks.hold(key):
                data = cache.get(key)  # someone else may have just built it
                if data is None:
                    try:
                        with slots:
                            data = generate_thumb(source, entry, size)
                    except Exception as exc:
                        failed.add(key)
                        print(f"thumb failed for {path}: {exc}", file=sys.stderr)
                        return JSONResponse({"error": "no thumbnail"}, status_code=404)
                    cache.put(key, size, data)
        return Response(data, media_type="image/webp", headers=headers)

    @app.get("/file")
    def file(request: Request, path: str):
        path = clean_path(path)
        entry = source.stat(path)
        size = entry.size
        ctype = mimetypes.guess_type(entry.name)[0] or "application/octet-stream"
        base = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
        if size == 0:
            return Response(b"", media_type=ctype, headers=base)

        start, end, status = 0, size - 1, 200
        rng = request.headers.get("range")
        if rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if not m or (m.group(1) == "" and m.group(2) == ""):
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            a, b = m.groups()
            if a == "":  # suffix range: last N bytes
                start = max(size - int(b), 0)
            else:
                start = int(a)
                if b:
                    end = min(int(b), size - 1)
            if start >= size or start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            status = 206
            base["Content-Range"] = f"bytes {start}-{end}/{size}"
        base["Content-Length"] = str(end - start + 1)

        def stream():
            with source.open(path) as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(stream(), status_code=status, media_type=ctype, headers=base)

    return app


# --------------------------------------------------------------------------
# Frontend (no build step)
# --------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>mediabrowse</title>
<style>
:root{color-scheme:dark;--bg:#111;--panel:#1b1b1d;--fg:#eee;--dim:#999;--accent:#6aa9ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.4 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}
header{position:sticky;top:0;z-index:5;display:flex;gap:12px;align-items:center;padding:10px 16px;
  background:rgba(17,17,17,.9);backdrop-filter:blur(10px);border-bottom:1px solid #2a2a2c}
#crumbs{flex:1;display:flex;flex-wrap:wrap;gap:6px;align-items:center;min-width:0}
#crumbs a{color:var(--accent);text-decoration:none}
#crumbs a:hover{text-decoration:underline}
#crumbs .sep{color:var(--dim)}
.hbtn{display:flex;align-items:center;justify-content:center;width:32px;height:32px;padding:0;border:0;border-radius:8px;
  background:none;color:var(--dim);font-size:19px;line-height:1;text-decoration:none;cursor:pointer}
.hbtn:hover{background:var(--panel);color:var(--fg)}
#favlink.on{color:#ffcc33}
#filter{display:none;background:var(--panel);border:1px solid #333;color:var(--fg);padding:6px 10px;border-radius:8px;width:180px}
#search.open #filter{display:block}
#search.open #searchbtn{display:none}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;padding:16px}
.tile{position:relative;display:block;aspect-ratio:1;background:var(--panel);border-radius:10px;overflow:hidden;
  cursor:pointer;color:inherit;text-decoration:none}
.tile:hover{outline:2px solid var(--accent)}
.tile.recent{outline:2px solid var(--accent);outline-offset:3px}
.tile img{width:100%;height:100%;object-fit:cover;display:block}
.tile .ico{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:48px}
.tile .name{position:absolute;left:0;right:0;bottom:0;padding:18px 8px 6px;font-size:12px;
  background:linear-gradient(transparent,rgba(0,0,0,.75));white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile .badge{position:absolute;top:8px;right:8px;background:rgba(0,0,0,.6);border-radius:999px;padding:2px 8px;font-size:12px}
.tile .fav{position:absolute;top:6px;left:6px;width:30px;height:30px;border:0;border-radius:50%;padding:0;
  background:rgba(0,0,0,.55);color:#fff;font-size:17px;line-height:30px;cursor:pointer;opacity:0}
.tile:hover .fav,.tile .fav.on{opacity:1}
.fav.on{color:#ffcc33}
#empty{padding:40px;text-align:center;color:var(--dim)}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.94);display:none;z-index:10;align-items:center;justify-content:center}
#lb.open{display:flex}
#stage{display:flex;align-items:center;justify-content:center}
#stage img,#stage video{max-width:96vw;max-height:88vh}
#cap{position:absolute;bottom:10px;left:0;right:0;text-align:center;color:var(--dim);font-size:13px;pointer-events:none}
#lb button{position:absolute;background:rgba(255,255,255,.1);border:0;color:#fff;font-size:26px;width:48px;height:48px;
  border-radius:50%;cursor:pointer}
#lb button:hover{background:rgba(255,255,255,.25)}
#prev{left:16px;top:50%;margin-top:-24px}
#next{right:16px;top:50%;margin-top:-24px}
#close{right:16px;top:16px}
#lbfav{right:72px;top:16px;font-size:22px}
</style></head>
<body>
<header><div id="crumbs"></div><a id="favlink" class="hbtn" href="#:favorites" title="Favorites">&#9733;</a>
<div id="search"><button id="searchbtn" class="hbtn" title="Filter (/)"><svg width="17" height="17" viewBox="0 0 24 24" fill="none"
  stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/></svg></button>
<input id="filter" type="search" placeholder="Filter..."></div></header>
<div id="grid"></div>
<div id="empty" hidden></div>
<div id="lb"><div id="stage"></div><div id="cap"></div>
<button id="prev" title="Previous">&#8249;</button><button id="next" title="Next">&#8250;</button><button id="close" title="Close">&#10005;</button><button id="lbfav" class="fav" title="Favorite (F)">&#9734;</button></div>
<script>
const $ = s => document.querySelector(s);
const enc = encodeURIComponent;
const ICON = {dir: "\uD83D\uDCC1", image: "\uD83D\uDDBC\uFE0F", video: "\uD83C\uDF9E\uFE0F", audio: "\uD83C\uDFB5"};
let state = {path: "", label: "", entries: []}, shown = [], lbIndex = -1;
let favs = new Set();
// ':' is always percent-encoded in folder hashes, so this can't collide with a real path
const FAV_HASH = "#:favorites";

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const kid of kids) e.append(kid);
  return e;
}
function fmt(n) {
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}

async function load() {
  if (lbIndex >= 0) closeLb();  // e.g. the phone's back gesture while viewing
  const favView = location.hash === FAV_HASH;
  const path = favView ? "" : decodeURIComponent(location.hash.slice(1));
  const r = await fetch(favView ? "/api/favorites/entries" : "/api/list?path=" + enc(path));
  if (!r.ok) {
    renderCrumbs(path, state.label);
    $("#grid").replaceChildren();
    $("#empty").hidden = false;
    $("#empty").textContent = "Cannot open this folder (" + r.status + ")";
    return;
  }
  const d = await r.json();
  const prev = state;
  state = {path: d.path, label: d.label, entries: d.entries, favView};
  $("#filter").value = "";
  $("#search").classList.remove("open");
  $("#favlink").classList.toggle("on", favView);
  document.title = d.label + (favView ? " / Favorites" : d.path ? " / " + d.path : "");
  renderCrumbs(d.path, d.label, favView);
  render();
  const t = !favView && !prev.favView && cameFrom(prev.path, d.path);
  if (t) {
    t.classList.add("recent");
    t.scrollIntoView({block: "center"});
  } else window.scrollTo(0, 0);
}

// going up from a/b/c to a: return the tile for a/b
function cameFrom(prevPath, path) {
  if (prevPath === path || (path && !prevPath.startsWith(path + "/"))) return null;
  const rest = prevPath.slice(path ? path.length + 1 : 0);
  const child = (path ? path + "/" : "") + rest.split("/")[0];
  const i = shown.findIndex(e => e.path === child);
  return i < 0 ? null : $("#grid").children[i];
}

async function loadFavs() {
  const r = await fetch("/api/favorites");
  if (r.ok) favs = new Set((await r.json()).paths);
}
async function toggleFav(path) {
  const r = await fetch("/api/favorites", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path, on: !favs.has(path)}),
  });
  if (!r.ok) return;
  favs = new Set((await r.json()).paths);
  document.querySelectorAll(".fav").forEach(syncFav);
}
function syncFav(b) {
  const on = favs.has(b.dataset.path);
  b.classList.toggle("on", on);
  b.textContent = on ? "\u2605" : "\u2606";
  b.title = on ? "Unfavorite" : "Favorite";
}
function favButton(path, attrs = {}) {
  const b = el("button", {class: "fav", ...attrs});
  b.dataset.path = path;
  b.onclick = ev => { ev.stopPropagation(); toggleFav(path); };
  syncFav(b);
  return b;
}

function renderCrumbs(path, label, favView = false) {
  const c = $("#crumbs"); c.replaceChildren();
  if (favView) {
    c.append(el("a", {href: "#"}, label), el("span", {class: "sep"}, "/"), el("b", {}, "\u2605 Favorites"));
    return;
  }
  const parts = path ? path.split("/") : [];
  c.append(parts.length ? el("a", {href: "#"}, label) : el("b", {}, label));
  let acc = "";
  parts.forEach((p, i) => {
    acc += (acc ? "/" : "") + p;
    c.append(el("span", {class: "sep"}, "/"));
    c.append(i === parts.length - 1 ? el("b", {}, p) : el("a", {href: "#" + enc(acc)}, p));
  });
}

function render() {
  const q = $("#filter").value.trim().toLowerCase();
  // in the favorites view, match on the folder too since files come from all over
  shown = state.entries.filter(e => !q || (state.favView ? e.path : e.name).toLowerCase().includes(q));
  const grid = $("#grid"); grid.replaceChildren();
  shown.forEach((e, i) => grid.append(tile(e, i)));
  $("#empty").hidden = shown.length > 0;
  $("#empty").textContent = q ? "No matches." : state.favView ? "No favorites yet." : "No media files here.";
}

function tile(e, i) {
  const t = e.is_dir
    ? el("a", {class: "tile", href: "#" + enc(e.path)})
    : el("div", {class: "tile", onclick: () => openLb(i)});
  const ico = el("div", {class: "ico"}, ICON[e.is_dir ? "dir" : e.kind]);
  t.append(ico);
  if (e.kind === "image" || e.kind === "video") {
    const img = el("img", {loading: "lazy", alt: "", src: "/thumb?path=" + enc(e.path) + "&size=256"});
    img.onload = () => ico.remove();
    img.onerror = () => img.remove();
    t.append(img);
    if (e.kind === "video") t.append(el("div", {class: "badge"}, "\u25B6"));
  }
  t.append(el("div", {class: "name", title: e.path}, state.favView ? e.path : e.name));
  if (!e.is_dir) t.append(favButton(e.path));
  return t;
}

function clearStage() {
  const st = $("#stage");
  st.querySelectorAll("video,audio").forEach(m => { m.pause(); m.removeAttribute("src"); m.load(); });
  st.replaceChildren();
}
// overflow:hidden doesn't stop iOS from scrolling the page behind the viewer, so pin the body
let savedY = 0;
function lockScroll() {
  savedY = window.scrollY;
  Object.assign(document.body.style, {position: "fixed", top: -savedY + "px", left: "0", right: "0"});
}
function unlockScroll() {
  Object.assign(document.body.style, {position: "", top: "", left: "", right: ""});
  window.scrollTo(0, savedY);
}

function openLb(i) {
  clearStage();
  if (lbIndex < 0) lockScroll();
  lbIndex = i;
  const e = shown[i], src = "/file?path=" + enc(e.path);
  let m;
  if (e.kind === "image") m = el("img", {src, alt: e.name});
  // no autoplay; the thumbnail poster stands in for the first frame, which iOS won't load until play
  else if (e.kind === "video") m = el("video", {src, controls: "", playsinline: "", preload: "metadata",
                                                poster: "/thumb?path=" + enc(e.path) + "&size=512"});
  else m = el("audio", {src, controls: "", autoplay: ""});
  $("#stage").append(m);
  $("#cap").textContent = e.name + "  (" + fmt(e.size) + ")";
  const fb = $("#lbfav");
  fb.dataset.path = e.path;
  syncFav(fb);
  $("#prev").hidden = neighbor(-1) < 0;
  $("#next").hidden = neighbor(1) < 0;
  $("#lb").classList.add("open");
}
function closeLb() { clearStage(); $("#lb").classList.remove("open"); lbIndex = -1; unlockScroll(); }
// index of the next file (skipping folders) in direction d, or -1 at either end
function neighbor(d) {
  let i = lbIndex + d;
  while (i >= 0 && i < shown.length && shown[i].is_dir) i += d;
  return i >= 0 && i < shown.length ? i : -1;
}
function step(d) {
  if (lbIndex < 0) return;
  const i = neighbor(d);
  if (i >= 0) openLb(i);
}

// swipe left/right in the viewer; a playing video/audio keeps its swipes
let touch = null;
$("#lb").addEventListener("touchstart", ev => {
  touch = null;
  if (ev.touches.length !== 1 || (window.visualViewport?.scale ?? 1) > 1.01) return;  // pinch / zoomed in
  const m = $("#stage").querySelector("video,audio");
  if (m && !m.paused && !m.ended) return;
  const t = ev.touches[0];
  // the bottom strip of a video is its scrubber; don't turn a seek into a page turn
  if (m && m.contains(ev.target) && t.clientY > m.getBoundingClientRect().bottom - 60) return;
  touch = {x: t.clientX, y: t.clientY, at: Date.now()};
}, {passive: true});
$("#lb").addEventListener("touchmove", ev => { if (ev.touches.length > 1) touch = null; }, {passive: true});
$("#lb").addEventListener("touchend", ev => {
  if (!touch) return;
  const t = ev.changedTouches[0], dx = t.clientX - touch.x, dy = t.clientY - touch.y;
  const quick = Date.now() - touch.at < 800;
  touch = null;
  if (quick && Math.abs(dx) > 50 && Math.abs(dx) > 1.5 * Math.abs(dy)) step(dx < 0 ? 1 : -1);
});

function openSearch() { $("#search").classList.add("open"); $("#filter").focus(); }
$("#searchbtn").onclick = openSearch;
// collapse back to the icon when the box is left empty
$("#filter").addEventListener("blur", () => { if (!$("#filter").value) $("#search").classList.remove("open"); });
$("#filter").addEventListener("keydown", ev => {
  if (ev.key !== "Escape") return;
  $("#filter").value = "";
  render();
  $("#filter").blur();
});

$("#close").onclick = closeLb;
$("#lbfav").onclick = () => { if (lbIndex >= 0) toggleFav(shown[lbIndex].path); };
$("#prev").onclick = () => step(-1);
$("#next").onclick = () => step(1);
$("#lb").addEventListener("click", ev => { if (ev.target.id === "lb" || ev.target.id === "stage") closeLb(); });
document.addEventListener("keydown", ev => {
  if (lbIndex < 0) {
    if (ev.key === "/" && document.activeElement !== $("#filter")) { ev.preventDefault(); openSearch(); }
    return;
  }
  if (ev.key === "Escape") closeLb();
  else if (ev.key === "ArrowLeft") step(-1);
  else if (ev.key === "ArrowRight") step(1);
  else if (ev.key === "f" || ev.key === "F") toggleFav(shown[lbIndex].path);
});
$("#filter").addEventListener("input", render);
window.addEventListener("hashchange", load);
loadFavs().then(load);
</script></body></html>
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def default_cache_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "mediabrowse" / "thumbs.db"


def main() -> None:
    ap = argparse.ArgumentParser(description="Browse media in a folder or zip file in your web browser.")
    ap.add_argument("source", help="directory or .zip file")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1, local only)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--cache", type=Path, default=default_cache_path(), help="thumbnail cache database")
    ap.add_argument("--open", action="store_true", help="open the browser on start")
    ap.add_argument("--favorites", type=Path,
                    help="favorites list (default: favorites.txt in the folder, or <name>.favorites.txt next to a zip)")
    args = ap.parse_args()

    p = Path(args.source).expanduser()
    if p.is_dir():
        source: Source = DirSource(p)
    elif p.is_file() and zipfile.is_zipfile(p):
        source = ZipSource(p)
    else:
        ap.error(f"{args.source!r} is not a directory or a zip file")
    if args.favorites is None:
        p = p.resolve()
        args.favorites = p / "favorites.txt" if p.is_dir() else p.with_name(p.stem + ".favorites.txt")

    if not shutil.which("ffmpeg"):
        print("note: ffmpeg not found - video thumbnails disabled (brew install ffmpeg)", file=sys.stderr)

    import uvicorn

    app = create_app(source, ThumbCache(args.cache), Favorites(args.favorites))
    url = f"http://{args.host}:{args.port}/"
    print(f"Serving {source.label} at {url}  (thumb cache: {args.cache}, favorites: {args.favorites})")
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
