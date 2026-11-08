# lecture-recorder
Records class lectures in Chrome with a live transcript, saved to a local folder.

## After class: clean transcripts (phase 2)
`tools/transcribe_lectures.py` re-transcribes finished lectures with ElevenLabs Scribe v2, using the class keyterm lists in `tools/keyterms/` (or `Fall2026\<class>\keyterms.txt` if you make one). It writes `transcript.clean.md` next to the audio and a copy to the vault at `raw/school/<COURSE>/lectures/`. Needs Python 3 and the `ELEVENLABS_API_KEY` environment variable; double-click `tools/Transcribe lectures.bat`. Costs about $0.27 per hour of audio (pricing checked 2026-11-08).
