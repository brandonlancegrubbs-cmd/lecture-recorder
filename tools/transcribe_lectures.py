#!/usr/bin/env python3
"""Lecture Recorder phase 2: re-transcribe saved lecture audio with ElevenLabs Scribe v2.

Run on the laptop after class (standard library only, no pip installs):

    python transcribe_lectures.py            # list what would run and the cost, then ask
    python transcribe_lectures.py --yes      # run without asking
    python transcribe_lectures.py --lecture "Ch 6"   # only lectures whose folder name contains this

What it does for every finished lecture in Fall2026\\<class>\\lectures\\<date title>\\ that has
audio parts and no transcript.clean.md yet:
  1. Sends each audio-NN.webm part to ElevenLabs Scribe v2 with the class keyterms
     (Fall2026\\<class>\\keyterms.txt if it exists, else tools\\keyterms\\<COURSE>.txt next to this file).
  2. Writes transcript.clean.md (timestamps, professor vs. student, your marks merged in)
     and adds cleanSource / cleanAt to lecture.json.
  3. Copies the clean transcript to the vault: raw\\school\\<COURSE>\\lectures\\<date title>.clean.md

The API key is read from the ELEVENLABS_API_KEY environment variable. It is never written to a file.
"""
import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

API_BASE = os.environ.get("ELEVENLABS_BASE", "https://api.elevenlabs.io")
MODEL = "scribe_v2"
# Verified 2026-11-08 on elevenlabs.io/pricing/api: $0.22/hour + $0.05/hour keyterm prompting.
PRICE_PER_HOUR = 0.22 + 0.05
COURSE_CODES = {
    "BIOL-1402-Lecture": "BIOL 1402", "BIOL-1402L-Lab": "BIOL 1402L", "COMP-1-Composition": "ENGL 1311",
    "MATH-College-Algebra": "MATH 1304", "NET-Networking-Telecom": "CIS 3367", "SPAN-2-Spanish": "SPAN 1312",
}
MARK_LABELS = {"star": "★", "exam": "‼ EXAM", "confused": "? CONFUSED", "example": "✎ EXAMPLE", "ann": "📅 ANNOUNCEMENT"}
HERE = Path(__file__).resolve().parent


def course_code(folder):
    return COURSE_CODES.get(folder, folder.replace("-", " "))


def vault_course(folder):
    return course_code(folder).replace(" ", "")


def fmt(sec):
    sec = max(0, int(sec))
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def default_root():
    home = Path.home()
    for p in (home / "Desktop" / "UIW" / "Fall2026", home / "OneDrive" / "Desktop" / "UIW" / "Fall2026"):
        if p.is_dir():
            return p
    return home / "Desktop" / "UIW" / "Fall2026"


def load_keyterms(root, folder):
    for p in (root / folder / "keyterms.txt", HERE / "keyterms" / f"{vault_course(folder)}.txt"):
        if p.is_file():
            terms, seen = [], set()
            for line in p.read_text(encoding="utf-8").splitlines():
                t = line.strip()
                if not t or t.startswith("#"):
                    continue
                # API rules: max 50 chars, max 5 words, none of < > { } [ ] \
                if len(t) > 50 or len(t.split()) > 5 or any(c in t for c in "<>{}[]\\"):
                    continue
                if t.lower() not in seen:
                    seen.add(t.lower())
                    terms.append(t)
            return terms[:1000], p
    return [], None


def find_lectures(root, only=None):
    out = []
    for cdir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        ldir = cdir / "lectures"
        if not ldir.is_dir():
            continue
        for lec in sorted(p for p in ldir.iterdir() if p.is_dir()):
            if only and only.lower() not in lec.name.lower():
                continue
            mj = lec / "lecture.json"
            if not mj.is_file():
                continue
            try:
                meta = json.loads(mj.read_text(encoding="utf-8"))
            except ValueError:
                continue
            parts = meta.get("audio") or [{"file": f.name, "start": None, "end": None} for f in sorted(lec.glob("audio-*.webm"))]
            parts = [a for a in parts if (lec / a["file"]).is_file()]
            if meta.get("status") != "done" or not parts or (lec / "transcript.clean.md").exists():
                continue
            out.append({"course": cdir.name, "dir": lec, "meta": meta, "parts": parts})
    return out


def multipart(fields, file_field, file_path):
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields:
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
    ctype = mimetypes.guess_type(file_path.name)[0] or "audio/webm"
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{file_path.name}"\r\n'
             f"Content-Type: {ctype}\r\n\r\n").encode("utf-8")
    body += file_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def transcribe(path, key, keyterms):
    fields = [("model_id", MODEL), ("language_code", "en"), ("diarize", "true"),
              ("timestamps_granularity", "word"), ("tag_audio_events", "false")]
    fields += [("keyterms", t) for t in keyterms]
    body, ctype = multipart(fields, "file", path)
    req = urllib.request.Request(f"{API_BASE}/v1/speech-to-text", data=body, method="POST",
                                 headers={"xi-api-key": key, "Content-Type": ctype})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"ElevenLabs said {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(10 * (attempt + 1))
                continue
            raise RuntimeError(f"Couldn't reach ElevenLabs: {e.reason}") from None


def words_from(result, offset):
    out = []
    for w in result.get("words") or []:
        if w.get("type", "word") == "audio_event":
            continue
        out.append({"t": float(w.get("start") or 0) + offset, "x": w.get("text", ""), "s": w.get("speaker_id") or "speaker_0",
                    "sp": w.get("type") == "spacing"})
    return out


def build_markdown(meta, words, course):
    # The speaker with the most words is the professor; everyone else is a student.
    counts = {}
    for w in words:
        if not w["sp"]:
            counts[w["s"]] = counts.get(w["s"], 0) + 1
    prof = max(counts, key=counts.get) if counts else None
    marks = sorted(meta.get("marks") or [], key=lambda m: m["t"])
    lines = [f"# {course_code(course)} · {meta.get('title', '')}", "",
             f"Date: {meta.get('date', '')} · Length {fmt(meta.get('durationSec', 0))} · {sum(counts.values()):,} words",
             f"Status: clean transcript from ElevenLabs {MODEL} with class keyterms, {datetime.now().strftime('%Y-%m-%d')}. "
             "Slides stay the source of wording.",
             "Speakers: **Prof** = the main speaker · **Student** = anyone else. "
             "Marks: [★] Important · [‼ EXAM] · [? CONFUSED] · [✎ EXAMPLE] · [📅 ANNOUNCEMENT] · [Slide n]", ""]
    para, cur_spk, para_start, mi = [], None, 0.0, 0

    def flush():
        nonlocal para
        text = "".join(para).strip()
        if text:
            who = "Prof" if cur_spk == prof else "Student"
            lines.append(f"**[{fmt(para_start)}] {who}:** {text}")
            lines.append("")
        para = []

    for w in words:
        while mi < len(marks) and marks[mi]["t"] <= w["t"]:
            m = marks[mi]
            tag = f"[Slide {m.get('n')} · {fmt(m['t'])}]" if m["k"] == "slide" else f"[{MARK_LABELS.get(m['k'], m['k'])} {fmt(m['t'])}]"
            if m["k"] == "slide":  # a slide change starts a new paragraph
                flush()
                lines += [tag, ""]
            else:  # other marks sit inline, right where you pressed the key
                para.append(f" **{tag}** ")
            mi += 1
        if w["sp"]:
            if para:
                para.append(" ")
            continue
        if w["s"] != cur_spk or (para and w["t"] - para_start > 90):
            flush()
            cur_spk, para_start = w["s"], w["t"]
        if not para:
            para_start = w["t"]
        para.append(w["x"])
    flush()
    for m in marks[mi:]:
        lines += [f"[Slide {m.get('n')} · {fmt(m['t'])}]" if m["k"] == "slide" else f"[{MARK_LABELS.get(m['k'], m['k'])} {fmt(m['t'])}]", ""]
    return "\n".join(lines).replace("  ", " ")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=default_root(), help="Fall2026 folder")
    ap.add_argument("--vault", type=Path, default=Path.home() / "Documents" / "agent-team", help="agent-team vault folder")
    ap.add_argument("--lecture", help="only lecture folders whose name contains this text")
    ap.add_argument("--yes", action="store_true", help="don't ask before spending")
    ap.add_argument("--dry-run", action="store_true", help="only list what would run")
    a = ap.parse_args()

    if not a.root.is_dir():
        sys.exit(f"Can't find the Fall2026 folder at {a.root}. Run again with --root \"<path to Fall2026>\".")
    todo = find_lectures(a.root, a.lecture)
    if not todo:
        print("Nothing to do: every finished lecture with audio already has transcript.clean.md.")
        return
    total_sec = 0
    print("Lectures to transcribe:")
    for L in todo:
        sec = L["meta"].get("durationSec") or 0
        total_sec += sec
        print(f"  {course_code(L['course']):<11} {L['dir'].name}  ({fmt(sec)}, {len(L['parts'])} audio part(s))")
    print(f"Estimated cost: ${total_sec / 3600 * PRICE_PER_HOUR:.2f} for {fmt(total_sec)} of audio "
          f"(Scribe v2 + keyterms, ${PRICE_PER_HOUR:.2f}/hour)")
    if a.dry_run:
        return
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        sys.exit("No ELEVENLABS_API_KEY found. Run setx ELEVENLABS_API_KEY \"<key>\" in PowerShell, then open a NEW window.")
    if not a.yes and input("Go? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Stopped. Nothing was sent.")
        return

    done = 0
    for L in todo:
        name, meta = L["dir"].name, L["meta"]
        terms, src = load_keyterms(a.root, L["course"])
        print(f"\n{course_code(L['course'])} · {name}: {len(terms)} keyterms" + (f" from {src}" if src else " (no keyterm list found)"))
        words, offset = [], 0.0
        try:
            for part in L["parts"]:
                start = part.get("start")
                if start is not None:
                    offset = float(start)
                print(f"  sending {part['file']} ...", flush=True)
                res = transcribe(L["dir"] / part["file"], key, terms)
                got = words_from(res, offset)
                words += got
                if start is None and got:
                    offset = got[-1]["t"] + 1
        except RuntimeError as e:
            print(f"  FAILED: {e}\n  Nothing was written for this lecture; run again later.")
            continue
        md = build_markdown(meta, words, L["course"])
        (L["dir"] / "transcript.clean.md").write_text(md, encoding="utf-8")
        meta["cleanSource"] = f"elevenlabs-{MODEL}"
        meta["cleanAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta["cleanWords"] = sum(1 for w in words if not w["sp"])
        (L["dir"] / "lecture.json").write_text(json.dumps(meta), encoding="utf-8")
        print(f"  wrote transcript.clean.md ({meta['cleanWords']:,} words)")
        if a.vault.is_dir():
            vdir = a.vault / "raw" / "school" / vault_course(L["course"]) / "lectures"
            vdir.mkdir(parents=True, exist_ok=True)
            front = ["---", f"course: {course_code(L['course'])}", f"date: {meta.get('date', '')}",
                     'title: "' + str(meta.get("title", "")).replace('"', "'") + '"',
                     f"duration_sec: {meta.get('durationSec', 0)}", f"words: {meta['cleanWords']}",
                     f'source: "Fall2026/{L["course"]}/lectures/{name}"', f"transcript: clean (ElevenLabs {MODEL} + keyterms)",
                     "---", ""]
            (vdir / f"{name}.clean.md").write_text("\n".join(front) + md, encoding="utf-8")
            print(f"  vault copy: raw/school/{vault_course(L['course'])}/lectures/{name}.clean.md")
        else:
            print(f"  (no vault folder at {a.vault}; skipped the vault copy)")
        done += 1
    print(f"\nDone: {done} of {len(todo)} lecture(s). Your vault sync will push the new files.")


if __name__ == "__main__":
    main()
