# mediabrowse

Browse the photos, videos and audio in a folder or a zip file from your web browser,
including your phone. It's one Python file (`mediabrowse.py`) plus its frontend
(`index.html`), with no build step. Keep the two files together.

## Setup

```sh
pip install -r requirements.txt
brew install ffmpeg        # optional: video thumbnails
pip install pillow-heif    # optional: HEIC/HEIF thumbnails
```

## Running

```sh
python mediabrowse.py ~/Pictures
python mediabrowse.py ~/Downloads/holiday.zip --port 9000 --open
```

| Option | Default | |
|---|---|---|
| `source` | — | A directory or `.zip` file |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | |
| `--open` | off | Open the browser on start |
| `--cache` | `~/.cache/mediabrowse/thumbs.db` | SQLite file that holds thumbnails (respects `$XDG_CACHE_HOME`) |
| `--favorites` | see below | Text file that holds favorites |

The startup line prints the URL, the thumbnail cache path and the favorites path.

### From a phone

By default the server only listens on this machine. To reach it from a phone on the same
network, bind to all interfaces and open `http://<computer's IP>:8000/` on the phone:

```sh
python mediabrowse.py ~/Pictures --host 0.0.0.0
```

There is no login, so anyone on that network can browse the folder and change favorites
while the server runs. Only do this on a network you trust.

## Using it

- **Grid:** folders first, then media, in natural order (`img2` before `img10`). Hidden
  files and non-media files are not shown. Inside a zip, only folders that contain media
  appear.
- **Going up a folder** highlights and scrolls to the folder you came from.
- **Filter:** tap the magnifying glass or press `/`. `Esc` clears it.
- **Viewer:** tap a file to open it.
  - `←` / `→`, the on-screen arrows, or swipe left/right to move between files. The arrows
    hide at the first and last file.
  - Swiping is ignored while a video or audio file is playing, so pause it first.
  - Videos don't autoplay. Audio does.
  - `Esc` or tapping the background closes the viewer.
- **Favorites:** tap the ☆ on a tile (shown on hover on desktop) or in the viewer, or press
  `F` in the viewer. The ★ button in the header shows all favorites from every folder,
  newest first.

## Favorites file

Favorites are stored as plain text, one path per line, relative to the root folder:

```
holiday/IMG_0042.jpg
clips/birthday.mp4
```

- Browsing a folder: `favorites.txt` inside that folder.
- Browsing a zip: `<name>.favorites.txt` next to the zip.
- `--favorites PATH` overrides both.

You can edit the file by hand, even while the server is running. Lines pointing to files
that no longer exist are hidden in the Favorites view but are never removed.

## Thumbnails

Thumbnails are generated the first time they are needed and cached as WebP in one SQLite
file. A cached thumbnail is rebuilt automatically when its source file changes, and
thumbnails unused for 90 days are deleted on startup. Videos need `ffmpeg`. Videos inside
a zip are extracted to a temp file to make a thumbnail, up to 1 GiB.
