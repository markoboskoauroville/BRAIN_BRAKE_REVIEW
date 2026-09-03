#!/usr/bin/env python3
"""
review_wall.py   STEP 92, the review wall.

Frames appear on Baba's screen by themselves. The chat session, in its sandbox,
generates a frame and pushes it to BRAIN_BRAKE_ORIGINALS/review/ with, in the
same commit, a rewritten review/latest.json. That file is the doorbell.

This process:

  1  polls review/latest.json over the RAW url every 3 s (never the GitHub API,
     which is rate limited). Nothing changed, nothing happens. When `updated`
     moves, it downloads any frame in `frames` it does not already have.
  2  serves a page on localhost:8777 that fills the screen with the newest
     frame, its name and the chat session's verdict under it, and a strip of the
     previous few. The page refreshes itself over SSE, so Baba never reloads and
     never clicks a link.
  3  KEEP and REDO under the picture write ~/BRAIN_BRAKE_REVIEW/verdicts.json and
     push it to review/verdicts.json. REDO asks why in one line, because "no"
     costs another generation and "no, too flat" costs one that is right.

THE RULES FROM THE STEP.
  * One writer per file. The chat session owns review/ and its frames and
    latest.json; the Mac reads them and writes ONLY verdicts.json. Nothing here
    ever deletes from review/ or writes latest.json.
  * A frame is never downloaded twice. The cache is keyed by filename and names
    never repeat: a second attempt is always a new version.
  * The raw url, not the API. Polling all day on the API would be throttled.
"""

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request

from flask import Flask, Response, jsonify, request, send_from_directory

REPO = "markoboskoauroville/BRAIN_BRAKE_ORIGINALS"
BRANCH = "main"
# THE RAW BASE, overridable for a local test so the poller can be pointed at a
# mock doorbell without touching the chat session's real files.
RAW = os.environ.get("REVIEW_RAW", "https://raw.githubusercontent.com/%s/%s" % (REPO, BRANCH))
PORT = 8777
POLL_SECONDS = 3

HOME = os.path.expanduser("~")
DIR = os.path.join(HOME, "BRAIN_BRAKE_REVIEW")
FRAMES = os.path.join(DIR, "frames")
VERDICTS = os.path.join(DIR, "verdicts.json")
STATE = os.path.join(DIR, "state.json")
os.makedirs(FRAMES, exist_ok=True)

app = Flask(__name__)

# ---------------------------------------------------------------- the state
# One lock guards everything a request or the poller reads or writes. The frame
# list is newest first. `seen` is the download cache, keyed by filename.
LOCK = threading.Lock()
FRAME_LIST = []          # [{name, at, bytes, verdict, prompt_head}], newest first
SEEN = set()             # filenames already on disk
LAST_UPDATED = None      # the `updated` stamp of the last latest.json acted on
SUBSCRIBERS = []         # SSE queues, one per open page
POLL_STATUS = "starting" # one line for the page's status corner


def load_local():
    """Frames already downloaded, and verdicts already cast, survive a restart."""
    global FRAME_LIST, SEEN, LAST_UPDATED
    if os.path.exists(STATE):
        try:
            with open(STATE, encoding="utf-8") as f:
                s = json.load(f)
            FRAME_LIST = s.get("frames", [])
            LAST_UPDATED = s.get("updated")
            SEEN = {f["name"] for f in FRAME_LIST if os.path.exists(os.path.join(FRAMES, f["name"]))}
            FRAME_LIST = [f for f in FRAME_LIST if f["name"] in SEEN]
        except (OSError, ValueError):
            pass


def save_local():
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated": LAST_UPDATED, "frames": FRAME_LIST}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def read_verdicts():
    if os.path.exists(VERDICTS):
        try:
            with open(VERDICTS, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}
    return {}


# ---------------------------------------------------------------- the doorbell

def fetch_json(path):
    """review/latest.json over the raw url, with a cache-buster so a CDN edge
    never hands back yesterday's doorbell. Returns the parsed object, or None on
    404 (the chat session has not started a review yet) or any transient error."""
    url = "%s/%s?t=%d" % (RAW, path, int(time.time()))
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache",
                                               "Pragma": "no-cache",
                                               "User-Agent": "review-wall"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.getcode() != 200:
                return None
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except (urllib.error.URLError, ValueError, OSError):
        return None


def download_frame(name):
    """review/<name> over the raw url, to FRAMES/<name>, atomically. Returns the
    size, or -1 on failure. Never overwrites: a name that is already on disk is
    a frame we already have (names never repeat)."""
    dest = os.path.join(FRAMES, name)
    if os.path.exists(dest):
        return os.path.getsize(dest)
    url = "%s/review/%s" % (RAW, name)
    req = urllib.request.Request(url, headers={"User-Agent": "review-wall"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.getcode() != 200:
                return -1
            data = r.read()
    except (urllib.error.URLError, OSError):
        return -1
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return len(data)


def notify(kind):
    """Wake every open page. The page decides what to do with the nudge."""
    dead = []
    for q in SUBSCRIBERS:
        try:
            q.put_nowait(kind)
        except queue.Full:
            dead.append(q)
    for q in dead:
        if q in SUBSCRIBERS:
            SUBSCRIBERS.remove(q)


def poll_once():
    global LAST_UPDATED, POLL_STATUS
    latest = fetch_json("review/latest.json")
    if latest is None:
        with LOCK:
            POLL_STATUS = "waiting for the first frame" if not FRAME_LIST else "up to date"
        return
    updated = latest.get("updated")
    if updated == LAST_UPDATED:
        with LOCK:
            POLL_STATUS = "up to date"
        return
    # `updated` moved. Take every frame we do not already have, newest last in
    # the file but we sort by `at` so the list stays newest first regardless.
    incoming = latest.get("frames", [])
    added = 0
    for fr in incoming:
        name = fr.get("name")
        if not name or name in SEEN:
            continue
        with LOCK:
            POLL_STATUS = "downloading %s" % name
        size = download_frame(name)
        if size < 0:
            continue
        with LOCK:
            SEEN.add(name)
            FRAME_LIST.append({
                "name": name,
                "at": fr.get("at", updated),
                "bytes": fr.get("bytes", size),
                "verdict": fr.get("verdict", ""),
                "prompt_head": fr.get("prompt_head", ""),
            })
            FRAME_LIST.sort(key=lambda f: f.get("at", ""), reverse=True)
        added += 1
    with LOCK:
        LAST_UPDATED = updated
        save_local()
        POLL_STATUS = "up to date"
    if added:
        notify("frame")


def poller():
    load_local()
    while True:
        try:
            poll_once()
        except Exception as e:                       # a bad poll must never kill the loop
            with LOCK:
                POLL_STATUS = "poll error: %s" % e
        time.sleep(POLL_SECONDS)


# ------------------------------------------------------------- pushing verdicts
# verdicts.json is the ONE file the Mac owns. A KEEP or REDO writes it locally at
# once (so the click is instant and survives a crash) and hands the push to a
# background worker, so the network is never in the way of a thumb.

PUSH_Q = queue.Queue()


def gh_api(args, payload=None):
    cmd = ["gh", "api"] + args
    if payload is not None:
        cmd += ["--input", "-"]
        out = subprocess.run(cmd, input=json.dumps(payload), capture_output=True, text=True)
    else:
        out = subprocess.run(cmd, capture_output=True, text=True)
    return out.returncode, out.stdout, out.stderr


def push_verdicts():
    """PUT review/verdicts.json to ORIGINALS through the contents API, with the
    previous sha when it already exists. gh carries the auth; no clone of the
    709 MB repo is needed. Same shape as push_original.py."""
    import base64
    with LOCK:
        body = json.dumps(read_verdicts(), ensure_ascii=False, indent=1).encode("utf-8")
    content = base64.b64encode(body).decode("ascii")
    code, out, err = gh_api(["repos/%s/contents/review/verdicts.json?ref=%s" % (REPO, BRANCH), "--jq", ".sha"])
    prev = out.strip() if code == 0 and out.strip() else None
    payload = {"message": "review verdicts, from the review wall", "content": content, "branch": BRANCH}
    if prev:
        payload["sha"] = prev
    code, out, err = gh_api(["-X", "PUT", "repos/%s/contents/review/verdicts.json" % REPO], payload)
    return code == 0, (err.strip() if code != 0 else "")


def pusher():
    while True:
        PUSH_Q.get()
        # coalesce: if several verdicts were cast in a burst, one push covers all
        try:
            while True:
                PUSH_Q.get_nowait()
        except queue.Empty:
            pass
        for attempt in range(5):
            ok, err = push_verdicts()
            if ok:
                with LOCK:
                    global PUSH_STATUS
                    PUSH_STATUS = "verdicts pushed"
                break
            with LOCK:
                PUSH_STATUS = "push retry: %s" % err
            time.sleep(4)


PUSH_STATUS = ""


def record_verdict(name, verdict, note):
    v = read_verdicts()
    v[name] = {"verdict": verdict, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "note": note or ""}
    tmp = VERDICTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(v, f, ensure_ascii=False, indent=1)
    os.replace(tmp, VERDICTS)
    PUSH_Q.put(name)


# ------------------------------------------------------------------ the page

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>THE REVIEW WALL</title>
<style>
  :root{ --black:#0B0D10; --panel:#141A21; --slate:#23303D; --sand:#F2DDB4;
         --amber:#F59E0B; --dim:#6E7681; --green:#22C55E; --red:#EF4444; }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%;background:var(--black);color:var(--sand);overflow:hidden;
    font:15px/1.4 ui-sans-serif,-apple-system,"Helvetica Neue",Arial,sans-serif}
  #wall{display:flex;flex-direction:column;height:100%}
  /* THE PICTURE FILLS THE SCREEN. object-fit contain, so a 16:9 frame is whole,
     never cropped, on a monitor of any shape. It never jumps: the stage is
     always here, empty and dim until the first frame arrives. */
  #stage{flex:1;min-height:0;position:relative;display:flex;align-items:center;justify-content:center;
    background:radial-gradient(ellipse at center, #10151b 0%, #0B0D10 75%)}
  #big{max-width:100%;max-height:100%;object-fit:contain;display:none;
    box-shadow:0 24px 80px rgba(0,0,0,.6);border-radius:4px}
  #empty{color:var(--dim);font-size:17px;text-align:center;padding:40px}
  /* the frame's name and the chat session's verdict, over the foot of the stage */
  #caption{position:absolute;left:0;right:0;bottom:0;padding:18px 26px 22px;
    background:linear-gradient(0deg, rgba(7,9,12,.92) 0%, rgba(7,9,12,0) 100%);
    display:none;align-items:flex-end;gap:26px}
  #cap-l{flex:1;min-width:0}
  #name{font:600 22px/1.2 ui-monospace,Menlo,monospace;color:var(--sand);letter-spacing:.02em;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  #verdict{margin-top:6px;color:var(--amber);font-size:15px;line-height:1.4;max-width:70ch}
  #kept{margin-top:6px;font:600 13px/1 ui-monospace,Menlo,monospace;letter-spacing:.08em;display:none}
  /* KEEP and REDO. Green keep, red-outlined redo. Big targets, his thumb. */
  #buttons{display:flex;gap:14px;flex:0 0 auto}
  .act{border:0;border-radius:12px;padding:16px 26px;cursor:pointer;
    font:700 15px/1 ui-monospace,Menlo,monospace;letter-spacing:.1em}
  #keep{background:var(--green);color:#06210f}
  #redo{background:transparent;color:var(--red);border:2px solid var(--red)}
  .act:disabled{opacity:.4;cursor:default}
  /* the redo reason, one line, appears in place of the buttons. rule 1: it does
     not push the picture; it sits over the caption. */
  #why{display:none;gap:10px;flex:0 0 auto;align-items:center}
  #why input{width:340px;background:#0e141a;color:var(--sand);border:1px solid var(--slate);
    border-radius:10px;padding:14px 14px;font:15px/1 inherit;outline:none}
  #why input:focus{border-color:var(--amber)}
  #why button{border:0;border-radius:10px;padding:14px 18px;cursor:pointer;font:700 13px/1 ui-monospace,Menlo,monospace}
  #whysend{background:var(--red);color:#fff}
  #whycancel{background:var(--slate);color:var(--sand)}
  /* THE STRIP of the previous few, along the bottom. The current one is lit. */
  #strip{flex:0 0 auto;height:96px;display:flex;gap:8px;padding:8px 12px;overflow-x:auto;
    background:var(--panel);border-top:1px solid var(--slate)}
  .thumb{height:100%;flex:0 0 auto;border-radius:4px;cursor:pointer;position:relative;
    border:2px solid transparent;opacity:.6}
  .thumb.on{border-color:var(--amber);opacity:1}
  .thumb img{height:100%;border-radius:2px;display:block}
  .thumb .tv{position:absolute;top:3px;right:3px;width:12px;height:12px;border-radius:50%}
  .thumb .tv.keep{background:var(--green)} .thumb .tv.redo{background:var(--red)}
  /* the status corner, bottom right, small and quiet, never mid screen */
  #status{position:fixed;right:12px;bottom:104px;font:11px/1.4 ui-monospace,Menlo,monospace;
    color:var(--dim);text-align:right;pointer-events:none;z-index:5}
  #dot{position:fixed;right:12px;top:12px;width:9px;height:9px;border-radius:50%;background:var(--slate)}
  #dot.on{background:var(--green)}
</style></head><body>
<div id="wall">
  <div id="stage">
    <img id="big" alt="">
    <div id="empty">The wall is up. Frames appear here the moment the chat session makes them.</div>
    <div id="caption">
      <div id="cap-l">
        <div id="name"></div><div id="verdict"></div>
        <div id="kept"></div>
      </div>
      <div id="buttons">
        <button class="act" id="keep">KEEP</button>
        <button class="act" id="redo">REDO</button>
      </div>
      <div id="why">
        <input id="whytext" placeholder="why? e.g. too flat, no throat" autocomplete="off">
        <button id="whysend">SEND REDO</button>
        <button id="whycancel">CANCEL</button>
      </div>
    </div>
  </div>
  <div id="strip"></div>
</div>
<div id="dot" title="live"></div>
<div id="status">starting</div>
<script>
let FRAMES = [], CUR = null;
const big = document.getElementById('big'), empty = document.getElementById('empty');
const cap = document.getElementById('caption'), nameEl = document.getElementById('name');
const verdictEl = document.getElementById('verdict'), keptEl = document.getElementById('kept');
const strip = document.getElementById('strip'), statusEl = document.getElementById('status');
const dot = document.getElementById('dot');
const buttons = document.getElementById('buttons'), why = document.getElementById('why');
const whytext = document.getElementById('whytext');

function setStatus(s){ statusEl.textContent = s; }

function show(name){
  const f = FRAMES.find(x => x.name === name); if (!f) return;
  CUR = name;
  big.src = '/frames/' + encodeURIComponent(name) + '?v=' + encodeURIComponent(f.at);
  big.style.display = 'block'; empty.style.display = 'none'; cap.style.display = 'flex';
  nameEl.textContent = f.name;
  verdictEl.textContent = f.verdict || '';
  const v = f.mine;
  if (v){
    keptEl.style.display = 'block';
    keptEl.style.color = v.verdict === 'keep' ? 'var(--green)' : 'var(--red)';
    keptEl.textContent = v.verdict === 'keep' ? 'KEPT' : ('REDO' + (v.note ? ' — ' + v.note : ''));
  } else { keptEl.style.display = 'none'; }
  hideWhy();
  document.querySelectorAll('.thumb').forEach(t => t.classList.toggle('on', t.dataset.name === name));
}

function renderStrip(){
  strip.innerHTML = '';
  FRAMES.forEach(f => {
    const t = document.createElement('div'); t.className = 'thumb'; t.dataset.name = f.name;
    const img = document.createElement('img'); img.src = '/frames/' + encodeURIComponent(f.name) + '?v=' + encodeURIComponent(f.at);
    t.appendChild(img);
    if (f.mine){ const d = document.createElement('div'); d.className = 'tv ' + (f.mine.verdict === 'keep' ? 'keep' : 'redo'); t.appendChild(d); }
    t.onclick = () => show(f.name);
    strip.appendChild(t);
  });
  document.querySelectorAll('.thumb').forEach(t => t.classList.toggle('on', t.dataset.name === CUR));
}

function apply(state, keepCurrent){
  const newest = FRAMES[0] ? FRAMES[0].name : null;
  FRAMES = state.frames || [];
  setStatus(state.status || '');
  renderStrip();
  if (!FRAMES.length){ big.style.display = 'none'; cap.style.display = 'none'; empty.style.display = 'block'; CUR = null; return; }
  // A NEW newest frame takes the screen. If the newest did not change, keep
  // what Baba is looking at, so a poll never yanks the picture from under him.
  const stillThere = FRAMES.find(f => f.name === CUR);
  if (!keepCurrent || !stillThere || (FRAMES[0].name !== newest)) show(FRAMES[0].name);
  else show(CUR);
}

function load(keepCurrent){
  fetch('/api/state').then(r => r.json()).then(s => apply(s, keepCurrent)).catch(() => setStatus('server not reachable'));
}

function verdict(kind, note){
  if (!CUR) return;
  const name = CUR;
  fetch('/api/verdict', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name, verdict: kind, note: note || ''})})
    .then(r => r.json()).then(() => { setStatus(kind === 'keep' ? 'kept ' + name : 'redo sent'); load(true); })
    .catch(() => setStatus('could not save the verdict'));
}
function showWhy(){ buttons.style.display = 'none'; why.style.display = 'flex'; whytext.value = ''; whytext.focus(); }
function hideWhy(){ why.style.display = 'none'; buttons.style.display = 'flex'; }

document.getElementById('keep').onclick = () => verdict('keep', '');
document.getElementById('redo').onclick = showWhy;
document.getElementById('whysend').onclick = () => verdict('redo', whytext.value.trim());
document.getElementById('whycancel').onclick = hideWhy;
whytext.addEventListener('keydown', e => { if (e.key === 'Enter'){ e.preventDefault(); verdict('redo', whytext.value.trim()); } if (e.key === 'Escape') hideWhy(); });
document.addEventListener('keydown', e => {
  if (why.style.display === 'flex') return;
  if (e.key === 'k' || e.key === 'K') verdict('keep', '');
  if (e.key === 'r' || e.key === 'R') showWhy();
  if (e.key === 'ArrowLeft' || e.key === 'ArrowRight'){
    const i = FRAMES.findIndex(f => f.name === CUR); if (i < 0) return;
    const j = e.key === 'ArrowLeft' ? i + 1 : i - 1;
    if (FRAMES[j]) show(FRAMES[j].name);
  }
});

// SSE: the server nudges when a frame lands, and the page reloads its state at
// once. A dropped stream falls back to a slow poll so the wall is never dead.
function connect(){
  const es = new EventSource('/api/events');
  es.onopen = () => dot.classList.add('on');
  es.onerror = () => dot.classList.remove('on');
  es.onmessage = () => load(true);
}
load(false); connect();
setInterval(() => load(true), 5000);
</script></body></html>"""


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.route("/health")
def health():
    with LOCK:
        return jsonify(ok=True, port=PORT, frames=len(FRAME_LIST), status=POLL_STATUS)


@app.route("/api/state")
def api_state():
    mine = read_verdicts()
    with LOCK:
        frames = [dict(f, mine=mine.get(f["name"])) for f in FRAME_LIST]
        status = POLL_STATUS + ((" · " + PUSH_STATUS) if PUSH_STATUS else "")
    return jsonify(frames=frames, status=status)


@app.route("/frames/<path:name>")
def api_frame(name):
    return send_from_directory(FRAMES, name)


@app.route("/api/verdict", methods=["POST"])
def api_verdict():
    d = request.get_json(force=True, silent=True) or {}
    name = d.get("name")
    verdict = d.get("verdict")
    if not name or verdict not in ("keep", "redo"):
        return jsonify(ok=False, error="need name and verdict keep|redo"), 400
    with LOCK:
        known = any(f["name"] == name for f in FRAME_LIST)
    if not known:
        return jsonify(ok=False, error="unknown frame"), 404
    record_verdict(name, verdict, d.get("note", ""))
    return jsonify(ok=True)


@app.route("/api/events")
def api_events():
    q = queue.Queue(maxsize=8)
    SUBSCRIBERS.append(q)

    def stream():
        yield "retry: 3000\n\n"
        try:
            while True:
                try:
                    kind = q.get(timeout=20)
                    yield "data: %s\n\n" % kind
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            if q in SUBSCRIBERS:
                SUBSCRIBERS.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def main():
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=pusher, daemon=True).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
