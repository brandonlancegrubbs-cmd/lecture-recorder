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
