# BRAIN_BRAKE_REVIEW — the review wall

**STEP 92. Frames appear on Baba's screen by themselves; KEEP and REDO send a verdict back with no
typing in chat.**

The chat session, in its sandbox, makes a frame and pushes it to
`BRAIN_BRAKE_ORIGINALS/review/` with, in the same commit, a rewritten `review/latest.json`. That
file is the doorbell. This Mac process polls it, downloads each new frame, and shows it full screen.

## What it is

- `review_wall.py` — a Flask app on `127.0.0.1:8777`. A background thread polls
  `review/latest.json` over the **raw** url every 3 s (never the GitHub API, which is rate limited),
  downloads any frame it does not already have into `~/BRAIN_BRAKE_REVIEW/frames/`, and serves a page
  that fills the screen with the newest frame, its name and the chat session's verdict under it, and a
  strip of the previous few. The page refreshes itself over SSE, so Baba never reloads and never
  clicks a link.
- KEEP and REDO under the picture write `~/BRAIN_BRAKE_REVIEW/verdicts.json` and push it to
  `review/verdicts.json`. REDO asks *why* in one line, because "no" costs another generation and
  "no, too flat" costs one that is right. The chat session reads `verdicts.json` before it generates
  anything else: KEEP moves the frame to its scene folder, REDO regenerates with the note.

## The rules it keeps

- **One writer per file.** The chat session owns `review/` and its frames and `latest.json`; the Mac
  reads them and writes ONLY `verdicts.json`. Nothing here deletes from `review/` or writes
  `latest.json`.
- **A frame is never downloaded twice.** The cache is keyed by filename; names never repeat.
- **The raw url, not the API.**

## Running it

It is one of the apps in the star menu (`MANTRA_STAR`): tick **Image review wall** and the window
opens; Cmd+Alt+R brings it forward. To run it by hand:

```
python3 ~/Developer/BRAIN_BRAKE_REVIEW/review_wall.py
```

Then open `http://127.0.0.1:8777`. `k` keeps, `r` redoes, the arrows step through the strip.

State lives under `~/BRAIN_BRAKE_REVIEW/` (frames, verdicts.json, state.json); nothing generated is
in this repository.
