# lecture-recorder
Records class lectures in Chrome with a live transcript, saved to a local folder.

## After class: clean transcripts (phase 2)
`tools/transcribe_lectures.py` re-transcribes finished lectures with ElevenLabs Scribe v2, using the class keyterm lists in `tools/keyterms/` (or `Fall2026\<class>\keyterms.txt` if you make one). It writes `transcript.clean.md` next to the audio and a copy to the vault at `raw/school/<COURSE>/lectures/`. Needs Python 3 and the `ELEVENLABS_API_KEY` environment variable; double-click `tools/Transcribe lectures.bat`. Costs about $0.27 per hour of audio (pricing checked 2026-11-08).

## Before studying: pull Canvas (phase 3)

`tools/Pull Canvas.bat` (double-click) runs `tools/canvas_pull.py`. It only reads Canvas (GET requests); it never submits or starts anything.

One-time setup:
1. Canvas > Account > Settings > **+ New Access Token** (purpose: study pull). Copy the token.
2. In a terminal: `setx CANVAS_TOKEN "paste-the-token"`, then close and reopen the terminal.
3. Optional: `py -m pip install pypdf` (PDF text) and `py -m pip install yt-dlp` (video transcripts).

Each run pulls only what is new or changed since last time (state in `Fall2026\_canvas_state.json`):
- BIOL 1402, BIOL 1402L, MATH 1304, CIS 3367: module files and pages, slide text in order with speaker notes, slide pictures saved next to each deck, due dates, exams, announcements, video links.
- SPAN 1312, ENGL 1311: due dates, exams and announcements only.

Output: `Fall2026\<class>\canvas\` on the laptop, and `raw\school\<COURSE>\decks\` plus `raw\school\<COURSE>\canvas\overview.md` in the vault. Options: `--list` (show what's new), `--course BIOL1402`, `--videos` (transcribe linked YouTube videos with the same ElevenLabs key).

## Automatic after class (phase 4)

The recorder now also lives on the study guides site: **https://study-guides-ten.vercel.app/lectures/** (Record tab picks the class from your schedule; Lectures tab shows each lecture). It saves the same files to `Fall2026` as this page.

`tools/auto_lectures.py` does the after-class work with no clicks. Double-click `tools/Turn on auto lectures.bat` once; Windows then runs it every 10 minutes while you're logged in. Each pass:
1. Transcribes finished lectures with ElevenLabs Scribe v2 (same as `transcribe_lectures.py`), only while this month's spend is under **$10** (Brandon 2026-11-09; change with `--cap`). Spend log: `Fall2026\_elevenlabs_spend.json`. Run log: `Fall2026\_auto_log.txt`.
2. Matches the transcript to that day's slides. Decks come from the vault (`raw\school\<COURSE>\decks\*.md`), `Fall2026\<class>\decks\*.md` and `Fall2026\<class>\canvas\files\`. Your S taps help it find slide changes.
3. Writes `insights` into `lecture.json` (slides covered, minutes per slide, "on the exam" moments, announcements, your marks by slide) and a lecture page to the vault at `school\sources\lectures\<COURSE>\<date title>.md`. `sync-vault.bat` pushes it.

`py auto_lectures.py --dry-run` shows what it would do without spending. `--rebuild` redoes slide matching (for example after new decks arrive). `Turn off auto lectures.bat` stops it.
