# TAKEOVER: the review wall on a fresh Mac

1. Python 3 with Flask: `python3 -m pip install flask`.
2. `gh` logged in as the account that owns BRAIN_BRAKE_ORIGINALS (`gh auth status`). The verdict
   push goes through `gh api`, so no clone of the 709 MB film repo is needed.
3. Clone: `git clone https://github.com/markoboskoauroville/BRAIN_BRAKE_REVIEW ~/Developer/BRAIN_BRAKE_REVIEW`
4. The star menu (`MANTRA_STAR`) runs it; or `python3 ~/Developer/BRAIN_BRAKE_REVIEW/review_wall.py`
   and open http://127.0.0.1:8777.

## How the two sides meet, exactly

The chat session writes, in one commit to BRAIN_BRAKE_ORIGINALS:

    review/<frame>.png
    review/latest.json     { "updated": "<iso>", "frames": [ {name, at, bytes, verdict, prompt_head} ] }

The Mac writes only:

    review/verdicts.json   { "<frame>.png": {"verdict": "keep"|"redo", "at": "<iso>", "note": ""} }

`updated` moving is the only signal; the poller does nothing until it changes. One writer per file:
the Mac never touches latest.json or the frames, the chat session never touches verdicts.json.

## Testing without the chat session

`review_wall.py` reads its raw base from `REVIEW_RAW` (default the real ORIGINALS raw url). Point it
at a local mock to prove the download and display path without touching the chat session's files:

    cd /tmp/mock && python3 -m http.server 8899     # serving review/latest.json + frames
    REVIEW_RAW=http://127.0.0.1:8899 python3 review_wall.py

The verdict push always goes to the real repo (it is the Mac's own file); to test it without
polluting the real verdicts.json, push to a throwaway path and delete it after.
