# LESSONS — the review wall

1. **The doorbell, not a watchdog.** Nothing on the Mac can see inside the chat session's sandbox.
   So the session pushes a tiny `latest.json` and the Mac polls that one file. A few hundred bytes
   every 3 s over the raw url costs nothing; watching for the frames themselves would not work.

2. **The raw url, never the API.** GitHub's contents API is rate limited and this polls all day.
   The raw url with a `?t=<epoch>` cache-buster defeats the CDN edge without an API call.

3. **One writer per file, always.** Three sessions have run all week without colliding because each
   file has exactly one writer. The Mac writes verdicts.json and nothing else; the chat session
   writes latest.json and the frames and nothing else.

4. **A name is a version; never download twice.** Frame names never repeat (a second attempt is a
   new version), so the download cache is keyed by filename and a name already on disk is skipped.

5. **The click must not wait on the network.** KEEP writes verdicts.json locally at once and hands
   the push to a background worker, so the verdict survives a crash and the thumb never waits. Bursts
   of verdicts coalesce into one push.

6. **The picture is never yanked.** A poll that finds no new newest frame keeps whatever Baba is
   looking at; only a genuinely newer frame takes the screen. design-language rule 1: nothing jumps.
