#!/usr/bin/env python3
"""Lecture Recorder auto job: runs by itself every 10 minutes (Windows Task Scheduler), no clicks.

For every finished lecture in Fall2026\\<class>\\lectures\\<date title>\\:
  1. Clean transcript: sends the audio to ElevenLabs Scribe v2 (same as transcribe_lectures.py),
     but only while this month's spend stays under the cap (default $10, Brandon 2026-11-09).
     Spend is logged in Fall2026\\_elevenlabs_spend.json.
  2. Slide match: finds that day's deck and works out which slide he was on, minute by minute.
     Decks are read from the vault (raw\\school\\<COURSE>\\decks\\*.md), Fall2026\\<class>\\decks\\*.md
     and Fall2026\\<class>\\canvas\\files\\**\\*.md.
  3. Writes the results into lecture.json ("insights"), which the Lectures tab on the guides site shows,
     and a lecture page into the vault: school\\sources\\lectures\\<COURSE>\\<date title>.md.

Standard library only. Usage:
    py auto_lectures.py              # one pass (what the scheduled task runs)
    py auto_lectures.py --dry-run    # show what it would do, spend nothing
    py auto_lectures.py --rebuild    # redo slide matching for lectures that already have it
    py auto_lectures.py --cap 15     # different monthly cap in dollars
"""
import argparse
import json
import math
import os
import re
import sys
import traceback
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import transcribe_lectures as T  # noqa: E402

CAP_DEFAULT = 10.0
SETTLE_MIN = 2  # wait this long after Stop before touching a lecture
WINDOW = 45     # seconds of speech per matching window
MIN_FIT = 0.12  # below this the lecture doesn't match any deck we have

STOP = set("""a about above after again all also am an and any are as at be because been before being below between both but by
can could did do does doing down during each few for from further get got had has have having he her here hers him his how i if in
into is it its itself just let like me more most my no nor not now of off on once only or other our out over own really right same
say says said see she should so some such than that the their them then there these they this those through to too under until up
us very was we were what when where which while who why will with would you your yeah okay ok um uh going gonna know want one two
thing things go come look here's that's it's don't let's we're you're they're i'm we'll you'll actually basically kind sort""".split())


def log(root, msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M')}  {msg}"
    print(line, flush=True)
    try:
        with open(root / "_auto_log.txt", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------- spend cap ----------------
def spend_file(root):
    return root / "_elevenlabs_spend.json"


def month_spent(root):
    try:
        d = json.loads(spend_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return d, float(d.get(datetime.now().strftime("%Y-%m"), {}).get("usd", 0))


def add_spend(root, usd, what):
    d, _ = month_spent(root)
    m = d.setdefault(datetime.now().strftime("%Y-%m"), {"usd": 0, "items": []})
    m["usd"] = round(m["usd"] + usd, 4)
    m["items"].append({"at": datetime.now().isoformat(timespec="minutes"), "usd": round(usd, 4), "lecture": what})
    spend_file(root).write_text(json.dumps(d, indent=1), encoding="utf-8")


# ---------------- text helpers ----------------
def tokens(s):
    out = []
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9'\-]+|\d+", (s or "").lower()):
        w = w.strip("'-")
        if len(w) < 3 or w in STOP:
            continue
        if w.endswith("ies") and len(w) > 5:
            w = w[:-3] + "y"
        elif w.endswith("s") and not w.endswith("ss") and len(w) > 4:
            w = w[:-1]
        out.append(w)
    return out


def clean_md_text(s):
    s = re.sub(r"\*\*|__|`|>|\[\[|\]\]", "", s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------- decks ----------------
def _numbered(body):
    """Slides from "## Slide N" blocks, "N. text" lines or "- **N text**" bullets."""
    slides = []
    blocks = re.split(r"^##\s+Slide\s+(\d+)\s*$", body, flags=re.M)
    if len(blocks) > 2:
        for i in range(1, len(blocks), 2):
            lines = [clean_md_text(l.lstrip("- ")) for l in blocks[i + 1].splitlines() if l.strip() and not l.startswith("[")]
            lines = [l for l in lines if l and l != "(picture only)"]
            title = next((l for l in lines if not l.lower().startswith("notes:")), "")
            slides.append({"n": int(blocks[i]), "title": title[:90], "text": " ".join(lines)})
        return slides
    for line in body.splitlines():
        m = re.match(r"^\s*(?:-\s*)?\*{0,2}(\d{1,3})(?:[.)]\s|\s+)\*{0,2}\s*(.+)", line)
        if m and not re.match(r"^\s*\d{1,3}\.\d", line):
            text = clean_md_text(m.group(2))
            title = re.split(r"\s[\u2014\u2013-]\s|\.\s(?=[A-Z])(?<!Fig\.\s)", text, maxsplit=1)[0]
            if len(title) > 70:
                title = title[:70].rsplit(" ", 1)[0] + "\u2026"
            slides.append({"n": int(m.group(1)), "title": title[:90], "text": text})
    ok, last = [], 0
    for sl in slides:  # keep an increasing run (drops numbered lists that aren't slides)
        if sl["n"] > last:
            ok.append(sl)
            last = sl["n"]
    return ok if len(ok) >= 3 else []


def parse_decks(path):
    """One file can hold one deck or several (one per '## ' heading). Returns [(name, slides)]."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    body = re.sub(r"^---\n.*?\n---\n", "", raw, flags=re.S)
    base = re.sub(r"\.(pptx?|pdf|page)$", "", path.stem, flags=re.I).replace("_", " ")
    if re.search(r"^##\s+Slide\s+\d+", body, flags=re.M):
        sl = _numbered(body)
        return [(base, sl)] if sl else []
    parts = re.split(r"^##\s+(.+)$", body, flags=re.M)
    found = []
    for i in range(1, len(parts), 2):
        sl = _numbered(parts[i + 1])
        if len(sl) >= 3:
            found.append((re.sub(r"\s*\(\d+ slides?\).*$|\s+[\u2014-]\s+file\b.*$", "", clean_md_text(parts[i]))[:80], sl))
    if len(found) > 1:
        return found
    if len(found) == 1:
        return [(base, found[0][1])]
    sl = _numbered(body)
    return [(base, sl)] if sl else []


def find_decks(root, vault, course_folder):
    code = T.vault_course(course_folder)
    places = [vault / "raw" / "school" / code / "decks", root / course_folder / "decks", root / course_folder / "canvas" / "files"]
    decks = []
    for p in places:
        if not p.is_dir():
            continue
        for f in sorted(p.rglob("*.md")):
            if f.name.startswith("overview"):
                continue
            try:
                for name, sl in parse_decks(f):
                    decks.append({"path": f, "name": name, "slides": sl})
            except OSError:
                continue
    return decks


# ---------------- transcript ----------------
def transcript_segments(lec_dir, meta):
    """[(t_seconds, text)] from transcript.clean.md (professor lines) or the live transcript."""
    clean = lec_dir / "transcript.clean.md"
    segs = []
    if clean.is_file():
        for line in clean.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\*\*\[(?:(\d+):)?(\d{1,2}):(\d{2})\]\s*(Prof|Student)?:?\*\*\s*(.*)", line)
            if m and m.group(4) != "Student":
                t = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                txt = re.sub(r"\*\*\[[^\]]*\]\*\*", " ", m.group(5))
                # long paragraphs: spread sentences across the next ~90 s so windows stay fine-grained
                sents = [s for s in re.split(r"(?<=[.?!])\s+", txt) if s.strip()]
                total = max(1, sum(len(s) for s in sents))
                acc = 0
                for s in sents:
                    segs.append((t + 90 * acc / total, s.strip()))
                    acc += len(s)
        if segs:
            return segs, "clean"
    return [(s["t"], s["x"]) for s in meta.get("segs") or []], "live"


EXAM_RE = re.compile(r"\b(on the (?:exam|test|quiz|final)|(?:exam|test|quiz) question|will be (?:on|tested)|i(?:'ll| will) ask|know this|need to know|"
                     r"make sure (?:you|that you) (?:know|understand|remember)|remember this|very important|this is important|important (?:point|concept)|"
                     r"pay attention|write this down|high[- ]yield|favorite question|guaranteed)\b", re.I)
ANN_RE = re.compile(r"\b(exam|quiz|midterm|final|due|deadline|turn (?:it )?in|homework|assignment|no class|extra credit|office hours)\b", re.I)
WHEN_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|next week|next class|tomorrow|tonight|today|january|february|march|april|may|"
                     r"june|july|august|september|october|november|december|\d{1,2}(?:st|nd|rd|th)|\d{1,2}/\d{1,2}|midnight|noon)\b", re.I)


# ---------------- slide matching ----------------
def match_slides(segs, slides, marks, duration):
    """Viterbi over 45-second windows. slides = one sequence (several decks back to back, each slide has 'deck').
    Moving forward is cheap, staying is free, a jump to another place costs more; your S taps make a change cheaper."""
    if not segs or not slides:
        return None
    end = max(duration or 0, segs[-1][0] + 5)
    nwin = max(1, int(math.ceil(end / WINDOW)))
    win_text = [[] for _ in range(nwin)]
    for t, x in segs:
        win_text[min(nwin - 1, int(t // WINDOW))].append(x)
    docs = [Counter(tokens(s["title"] + " " + s["text"])) for s in slides]
    df = Counter()
    for d in docs:
        df.update(d.keys())
    N = len(docs)
    idf = {w: math.log((N + 1) / (c + 0.5)) for w, c in df.items()}

    def vec(c):
        v = {w: (1 + math.log(n)) * idf[w] for w, n in c.items() if w in idf}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1
        return {w: x / norm for w, x in v.items()}

    svec = [vec(d) for d in docs]
    sims = []
    for txt in win_text:
        wv = vec(Counter(tokens(" ".join(txt))))
        sims.append([sum(wv.get(w, 0) * x for w, x in sv.items()) for sv in svec])
    taps = {int(m["t"] // WINDOW) for m in marks if m.get("k") == "slide"}
    S = len(slides)
    JUMP = 0.45
    cost = [-sims[0][j] for j in range(S)]
    backs = []
    for i in range(1, nwin):
        tap = i in taps
        gmin = min(range(S), key=lambda k: cost[k])
        row, back = [0.0] * S, [0] * S
        for j in range(S):
            best, arg = cost[j], j  # stay
            for step in (1, 2, 3):
                k = j - step
                if k >= 0 and slides[k]["deck"] == slides[j]["deck"]:
                    c = cost[k] + (0.05 if tap else 0.12) + 0.06 * (step - 1)
                    if c < best:
                        best, arg = c, k
            if j + 1 < S and slides[j + 1]["deck"] == slides[j]["deck"] and cost[j + 1] + 0.3 < best:
                best, arg = cost[j + 1] + 0.3, j + 1
            if cost[gmin] + JUMP < best:
                best, arg = cost[gmin] + JUMP, gmin
            row[j], back[j] = best - sims[i][j], arg
        cost = row
        backs.append(back)
    j = min(range(S), key=lambda k: cost[k])
    path = [0] * nwin
    for i in range(nwin - 1, -1, -1):
        path[i] = j
        if i:
            j = backs[i - 1][j]
    spoken = [i for i in range(nwin) if win_text[i]]
    fit = sum(sims[i][path[i]] for i in spoken) / max(1, len(spoken))
    return path, fit, win_text


def build_insights(lec_dir, meta, decks, course):
    segs, source = transcript_segments(lec_dir, meta)
    if not segs:
        return None
    marks = meta.get("marks") or []
    dur = meta.get("durationSec") or (segs[-1][0] + 30)
    all_tok = Counter(tokens(" ".join(x for _, x in segs)))
    # candidate decks: the ones that share the most vocabulary with this lecture, back to back in name order
    scored = []
    for d in decks:
        dt = Counter(tokens(" ".join(s["title"] + " " + s["text"] for s in d["slides"])))
        common = sum(min(n, dt[w]) for w, n in all_tok.items() if w in dt)
        scored.append((common / math.sqrt(sum(dt.values()) + 50), d))
    scored.sort(key=lambda x: -x[0])
    pool = sorted([d for _, d in scored[:5]], key=lambda d: d["name"])
    seq = [{**s, "deck": d["name"], "file": str(d["path"])} for d in pool for s in d["slides"]]
    ins = {"version": 1, "builtAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "transcript": source,
           "deck": None, "decks": [], "covered": None, "slides": [], "emphasis": [], "announcements": [], "marks": []}
    slide_at = lambda t: None
    res = match_slides(segs, seq, marks, dur) if seq else None
    if res:
        path, fit, win_text = res
        ins["fit"] = round(fit, 3)
    if res and fit >= MIN_FIT:
        per = {}
        for i, j in enumerate(path):
            if win_text[i]:
                p = per.setdefault(j, {"sec": 0, "start": i * WINDOW, "text": []})
                p["sec"] += WINDOW
                p["text"] += win_text[i]
        by_deck = Counter()
        for j, p in per.items():
            by_deck[seq[j]["deck"]] += p["sec"]
        # a deck that got under 3 minutes is probably noise: fold those minutes into "unmatched"
        keep = {d for d, sec in by_deck.items() if sec >= 180}
        main_deck = by_deck.most_common(1)[0][0]
        ins["deck"] = main_deck
        ins["decks"] = [{"name": d, "sec": sec} for d, sec in by_deck.most_common() if d in keep]
        ins["deckFile"] = next(s["file"] for s in seq if s["deck"] == main_deck)
        mine = sorted(j for j in per if seq[j]["deck"] == main_deck)
        ins["covered"] = [seq[mine[0]]["n"], seq[mine[-1]]["n"]]
        multi = len(keep) > 1
        ins["slides"] = [{"n": seq[j]["n"], "title": seq[j]["title"], "sec": per[j]["sec"], "start": per[j]["start"],
                          "text": " ".join(per[j]["text"])[:4000], "exam": False, **({"deck": seq[j]["deck"]} if multi else {})}
                         for j in sorted(per, key=lambda j: per[j]["start"]) if seq[j]["deck"] in keep]

        def slide_at(t):  # noqa: F811
            sl = seq[path[min(len(path) - 1, int(t // WINDOW))]]
            return sl if sl["deck"] in keep else None
    for t, x in segs:
        if EXAM_RE.search(x) and not any(e.get("quote") == x[:220] or abs(e["t"] - t) < 15 for e in ins["emphasis"]):
            s = slide_at(t)
            ins["emphasis"].append({"t": round(t), "quote": x[:220], "src": "said", **({"n": s["n"], "title": s["title"]} if s else {})})
        if ANN_RE.search(x) and WHEN_RE.search(x):
            if not ins["announcements"] or t - ins["announcements"][-1]["t"] > 20:
                ins["announcements"].append({"t": round(t), "text": x[:220]})
    for m in marks:
        if m.get("k") in ("star", "exam", "confused", "example"):
            s = slide_at(m["t"])
            ins["marks"].append({"t": m["t"], "k": m["k"], **({"n": s["n"], "title": s["title"]} if s else {})})
            if m["k"] == "exam":
                ins["emphasis"].append({"t": m["t"], "src": "tap", **({"n": s["n"], "title": s["title"]} if s else {})})
    ins["emphasis"].sort(key=lambda e: e["t"])
    hot = {e.get("n") for e in ins["emphasis"] if e.get("n")}
    for s in ins["slides"]:
        s["exam"] = s["n"] in hot
    return ins


# ---------------- vault lecture page ----------------
def lecture_page(meta, ins, course, name):
    code = T.course_code(course)
    fm = ["---", "type: lecture", "area: school", "status: active", f"course_code: {code}", f"lecture_date: {meta.get('date', '')}",
          f'title: "{str(meta.get("title", "")).replace(chr(34), chr(39))}"',
          f'slides: "{ins.get("deck") or ""}"', f'transcript: "raw/school/{T.vault_course(course)}/lectures/{name}.clean.md"',
          f"duration_sec: {meta.get('durationSec', 0)}", f"updated: {datetime.now().strftime('%Y-%m-%d')}", "---", ""]
    out = fm + [f"# {code} {meta.get('date', '')} {meta.get('title', '')}", "",
                (f"Deck: {ins['deck']}, slides {ins['covered'][0]} to {ins['covered'][1]}. " if ins.get("covered") else "Deck: not matched yet. ") +
                f"Back to [[school/entities/courses/{code}|{code}]]. Built automatically from the {ins['transcript']} transcript; slides stay the source of wording.", ""]
    if ins["slides"]:
        out += ["## Most time", ""]
        for s in sorted(ins["slides"], key=lambda s: -s["sec"])[:6]:
            out.append(f"- Slide {s['n']} {s['title']}: {round(s['sec'] / 60)} min" + (" (exam signal)" if s["exam"] else ""))
        out.append("")
    out += ["## What he stressed", ""]
    out += [f"- [{T.fmt(e['t'])}] " + (f"\"{e['quote']}\"" if e.get("quote") else "Brandon tapped On the exam") + (f" (slide {e['n']})" if e.get("n") else "")
            for e in ins["emphasis"]] or ["- Nothing flagged."]
    out += ["", "## Announcements", ""]
    out += [f"- [{T.fmt(a['t'])}] {a['text']}" for a in ins["announcements"]] or ["- None caught."]
    if ins["marks"]:
        out += ["", "## Brandon's marks", ""]
        out += [f"- [{T.fmt(m['t'])}] {T.MARK_LABELS.get(m['k'], m['k'])}" + (f" (slide {m['n']} {m['title']})" if m.get("n") else "") for m in ins["marks"]]
    if ins["slides"]:
        out += ["", "## Slide by slide", ""]
        for s in ins["slides"]:
            out += [f"### Slide {s['n']} · {s['title']} ({T.fmt(s['start'])}, {round(s['sec'] / 60)} min)", "", s["text"][:1500], ""]
    out += ["", "## Log", f"- {datetime.now().strftime('%Y-%m-%d')} · built by tools/auto_lectures.py (lecture-recorder)"]
    return "\n".join(out) + "\n"


# ---------------- main ----------------
def lectures(root):
    for cdir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        ldir = cdir / "lectures"
        if ldir.is_dir():
            for lec in sorted(p for p in ldir.iterdir() if p.is_dir()):
                mj = lec / "lecture.json"
                if mj.is_file():
                    try:
                        yield cdir.name, lec, json.loads(mj.read_text(encoding="utf-8"))
                    except ValueError:
                        continue


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=T.default_root())
    ap.add_argument("--vault", type=Path, default=Path.home() / "Documents" / "agent-team")
    ap.add_argument("--cap", type=float, default=float(os.environ.get("LECTURE_SPEND_CAP", CAP_DEFAULT)))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="redo slide matching even where it was done")
    ap.add_argument("--no-transcribe", action="store_true", help="slide matching only, never call ElevenLabs")
    a = ap.parse_args()
    root = a.root
    if not root.is_dir():
        sys.exit(f"Can't find the Fall2026 folder at {root}. Use --root.")
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    deck_cache = {}
    for course, lec, meta in list(lectures(root)):
        name = lec.name
        if meta.get("status") != "done":
            continue
        ended = meta.get("endedAt")
        try:
            if ended and datetime.now(timezone.utc) - datetime.fromisoformat(ended.replace("Z", "+00:00")) < timedelta(minutes=SETTLE_MIN):
                continue
        except ValueError:
            pass
        changed = False
        # 1) clean transcript
        parts = [p for p in (meta.get("audio") or []) if (lec / p["file"]).is_file()]
        if parts and not (lec / "transcript.clean.md").exists() and not a.no_transcribe:
            est = (meta.get("durationSec") or 0) / 3600 * T.PRICE_PER_HOUR
            _, spent = month_spent(root)
            if spent + est > a.cap:
                if meta.get("transcribeError") != "cap":
                    meta["transcribeError"] = "cap"
                    meta["transcribeNote"] = f"Monthly cap ${a.cap:.0f} reached (${spent:.2f} spent). Run transcribe_lectures.py by hand or raise --cap."
                    changed = True
                    log(root, f"cap: skipped {course}/{name} (${spent:.2f} spent, needs ${est:.2f})")
            elif not key:
                log(root, "no ELEVENLABS_API_KEY; skipping clean transcripts")
            elif a.dry_run:
                log(root, f"would transcribe {course}/{name} (~${est:.2f}; ${spent:.2f} spent this month)")
            else:
                terms, _ = T.load_keyterms(root, course)
                words, offset = [], 0.0
                try:
                    for part in parts:
                        if part.get("start") is not None:
                            offset = float(part["start"])
                        got = T.words_from(T.transcribe(lec / part["file"], key, terms), offset)
                        words += got
                        if part.get("start") is None and got:
                            offset = got[-1]["t"] + 1
                    add_spend(root, est, f"{course}/{name}")
                    (lec / "transcript.clean.md").write_text(T.build_markdown(meta, words, course), encoding="utf-8")
                    meta["cleanSource"] = f"elevenlabs-{T.MODEL}"
                    meta["cleanAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    meta["cleanWords"] = sum(1 for w in words if not w["sp"])
                    meta.pop("transcribeError", None)
                    meta.pop("transcribeNote", None)
                    changed = True
                    if a.vault.is_dir():
                        vdir = a.vault / "raw" / "school" / T.vault_course(course) / "lectures"
                        vdir.mkdir(parents=True, exist_ok=True)
                        front = ["---", f"course: {T.course_code(course)}", f"date: {meta.get('date', '')}",
                                 'title: "' + str(meta.get("title", "")).replace('"', "'") + '"', f"duration_sec: {meta.get('durationSec', 0)}",
                                 f"words: {meta['cleanWords']}", f'source: "Fall2026/{course}/lectures/{name}"',
                                 f"transcript: clean (ElevenLabs {T.MODEL} + keyterms)", "---", ""]
                        (vdir / f"{name}.clean.md").write_text("\n".join(front) + T.build_markdown(meta, words, course, prof_only=True), encoding="utf-8")
                    log(root, f"transcribed {course}/{name} ({meta['cleanWords']:,} words, ~${est:.2f})")
                except RuntimeError as e:
                    meta["transcribeError"] = str(e)[:300]
                    changed = True
                    log(root, f"FAILED transcribe {course}/{name}: {e}")
        # 2) slide matching (free); wait for the clean transcript when one is coming
        clean_ready = (lec / "transcript.clean.md").exists() or not parts or meta.get("transcribeError")
        if clean_ready and (a.rebuild or not meta.get("insights") or meta["insights"].get("transcript") == "live" and (lec / "transcript.clean.md").exists()):
            if course not in deck_cache:
                deck_cache[course] = find_decks(root, a.vault, course)
            try:
                ins = build_insights(lec, meta, deck_cache[course], course)
            except Exception:  # never let one odd lecture stop the job
                log(root, f"FAILED matching {course}/{name}: {traceback.format_exc(limit=1).strip()}")
                ins = None
            if ins:
                if a.dry_run:
                    log(root, f"would write insights for {course}/{name}: deck {ins['deck']}, {len(ins['slides'])} slides, {len(ins['emphasis'])} exam signals")
                else:
                    meta["insights"] = ins
                    changed = True
                    if a.vault.is_dir():
                        pdir = a.vault / "school" / "sources" / "lectures" / T.vault_course(course)
                        pdir.mkdir(parents=True, exist_ok=True)
                        (pdir / f"{name}.md").write_text(lecture_page(meta, ins, course, name), encoding="utf-8")
                    log(root, f"matched {course}/{name}: {ins['deck'] or 'no deck found'}, {len(ins['emphasis'])} exam signals")
        if changed and not a.dry_run:
            (lec / "lecture.json").write_text(json.dumps(meta), encoding="utf-8")


if __name__ == "__main__":
    main()
