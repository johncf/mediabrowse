# CLAUDE.md

Local media browser: `mediabrowse.py` (FastAPI + uvicorn backend) and `index.html` (the
whole frontend: HTML, CSS and vanilla JS in one file). User docs are in `README.md`.

## Layout of mediabrowse.py

Sections are separated by `# ----` banner comments, in this order:

1. **Media types:** `INDEX_HTML` (path to `index.html`), extension sets, `kind_of()`,
   `clean_path()` (rejects `..` and NUL in client-supplied paths).
2. **Sources:** `Source` interface with `DirSource` and `ZipSource`. Paths everywhere are
   relative and `/`-separated, with `''` as the root. `DirSource._resolve` also blocks
   symlinks that escape the root. `ZipSource` indexes the zip once at startup and keeps
   one `ZipFile` per thread.
3. **Thumbnail cache:** `ThumbCache` (SQLite, WAL, one connection per thread) and
   `KeyedLocks` (one build per key).
4. **Favorites:** `Favorites` re-reads the text file on every call and writes atomically
   (tmp file + `os.replace`).
5. **Thumbnail generation:** Pillow for images, `ffmpeg` subprocess for video.
6. **Web app:** `create_app()`. Endpoints: `/` (reads `index.html` on every request, so
   frontend edits only need a browser reload, not a restart), `/api/list`,
   `/api/favorites` (GET and POST), `/api/favorites/entries`, `/thumb`, `/file` (supports
   Range requests for video seeking).
7. **CLI:** `main()`.

## Conventions

- Keep it to these two files, with no build step and no frontend dependencies.
- Endpoints that block (disk, Pillow, ffmpeg, sqlite) are plain `def` so they run in
  FastAPI's threadpool. Don't make them `async def`.
- Run every client-supplied path through `clean_path()` and then `source.stat()` or
  `source.list()`.
- `FileNotFoundError` → 404, `PermissionError` → 403, `ValueError` → 400 through app-level
  exception handlers, so raise these rather than building error responses.
- State-changing endpoints take a JSON body (pydantic model), not query params. The
  required JSON content type prevents a plain cross-site form POST.
- Frontend JS: `$` = `querySelector`, `el(tag, attrs, ...kids)` builds DOM, `enc` =
  `encodeURIComponent`. Routing is by `location.hash`: `#<encoded path>` for folders and
  `#:favorites` for the favorites view (`:` is always percent-encoded in folder hashes,
  so this can't collide with a folder).
- Comments are sparse and explain *why*, not *what*.

## Users and testing

- The main user browses from an iPhone. Check touch behaviour (swipe, scroll lock, iOS
  video quirks) as well as desktop.
- There are no tests. To check a change, start the server on a scratch folder and call it
  with curl:
  ```sh
  python3 mediabrowse.py /path/to/scratch --port 8765 --cache /path/to/scratch/t.db
  curl -s -H 'Content-Type: application/json' -d '{"path":"a.jpg"}' localhost:8765/api/favorites
  ```
  When scripting curl in zsh, don't keep a header in an unquoted variable: zsh doesn't
  word-split it, so the header won't be sent.
- Node isn't installed, so frontend JS can't be syntax-checked locally. Say so when
  reporting frontend changes.
