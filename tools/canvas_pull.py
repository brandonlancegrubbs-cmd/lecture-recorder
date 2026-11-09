#!/usr/bin/env python3
"""Pull every class's Canvas material onto the laptop and into the vault (read-only).

Run on the laptop (standard library only; nothing to pip install):

    py canvas_pull.py              # pull everything new since last time
    py canvas_pull.py --list       # only show what is new, download nothing
    py canvas_pull.py --course BIOL1402
    py canvas_pull.py --videos     # also transcribe linked YouTube videos (needs yt-dlp + ELEVENLABS_API_KEY)

What it does, per class:
  1. Reads modules, files, pages, assignments, quizzes and announcements through the Canvas API.
     Only GET requests: it never submits, starts a quiz or changes anything.
  2. Downloads new or changed files to Fall2026\\<class>\\canvas\\files\\<module>\\.
  3. Extracts text in slide order: .pptx (slide text, speaker notes, which slides are pictures),
     .docx, .pdf (needs `py -m pip install pypdf`, optional), old .ppt (best effort, or exact if
     LibreOffice is installed). Slide pictures are saved next to the file so figures can match the slides.
  4. Writes Fall2026\\<class>\\canvas\\overview.md: modules in order, due dates (Central time),
     exams, announcements, and every video link.
  5. Copies text to the vault: raw\\school\\<COURSE>\\decks\\<file>.md and raw\\school\\<COURSE>\\canvas\\overview.md.
     Your vault sync pushes them; Claude builds guides from there.

Classes set to "dates" (SPAN 1312, ENGL 1311) only get overview.md: due dates, exams, announcements.

The token is read from the CANVAS_TOKEN environment variable (Canvas > Account > Settings >
New Access Token, then `setx CANVAS_TOKEN "..."`). It is never written to a file.
"""
import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = os.environ.get("CANVAS_BASE", "https://canvas.uiw.edu").rstrip("/")
HERE = Path(__file__).resolve().parent
# Canvas course ids, Fall 2026 (Verified 2026-11-09 from /api/v1/courses). mode: full = pull files and text,
# dates = due dates, exams and announcements only (Brandon 11/9: Spanish is mostly paper, English bans AI).
COURSES = [
    {"code": "BIOL 1402", "id": 56274, "folder": "BIOL-1402-Lecture", "mode": "full"},
    {"code": "BIOL 1402L", "id": 56279, "folder": "BIOL-1402L-Lab", "mode": "full"},
    {"code": "MATH 1304", "id": 57098, "folder": "MATH-College-Algebra", "mode": "full"},
    {"code": "CIS 3367", "id": 57879, "folder": "NET-Networking-Telecom", "mode": "full"},
    {"code": "SPAN 1312", "id": 57625, "folder": "SPAN-2-Spanish", "mode": "dates"},
    {"code": "ENGL 1311", "id": 56612, "folder": "COMP-1-Composition", "mode": "dates"},
]
TEXT_TYPES = {".pptx", ".docx", ".pdf", ".ppt", ".txt", ".md"}
SKIP_BIGGER_THAN = 200 * 1024 * 1024
VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "jove.com", "kaltura", "mediaspace", "panopto", "loom.com")
CT = timezone(timedelta(hours=-6))  # Central time in November (CST). Good enough for due dates.


def vault_slug(code):
    return code.replace(" ", "")


def default_root():
    home = Path.home()
    for p in (home / "Desktop" / "UIW" / "Fall2026", home / "OneDrive" / "Desktop" / "UIW" / "Fall2026"):
        if p.is_dir():
            return p
    return home / "Desktop" / "UIW" / "Fall2026"


def safe(name, limit=120):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name).strip().rstrip(".")
    return re.sub(r"\s+", " ", name)[:limit] or "untitled"


# ---------------------------------------------------------------- Canvas API (GET only)
class Canvas:
    def __init__(self, token, base=BASE):
        self.token, self.base = token, base

    def _open(self, url, accept="application/json"):
        if not url.startswith("http"):
            url = self.base + url
        req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {self.token}", "Accept": accept})
        for attempt in range(4):
            try:
                return urllib.request.urlopen(req, timeout=120)
            except urllib.error.HTTPError as e:
                if e.code in (403, 429, 500, 502, 503) and attempt < 3 and e.code != 401:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise
            except urllib.error.URLError:
                if attempt < 3:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise

    def get(self, path, params=None):
        url = path + (("&" if "?" in path else "?") + urllib.parse.urlencode(params, doseq=True) if params else "")
        out = []
        while url:
            with self._open(url) as r:
                data = json.loads(r.read().decode("utf-8"))
                link = r.headers.get("Link", "") or ""
            if not isinstance(data, list):
                return data
            out.extend(data)
            m = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = m.group(1) if m else None
        return out

    def download(self, url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with self._open(url, accept="*/*") as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.replace(dest)


# ---------------------------------------------------------------- text extraction
def _xml_text(xml, tag):
    return [html.unescape("".join(re.findall(rf"<{tag}(?: [^>]*)?>([^<]*)</{tag}>", p)))
            for p in re.split(r"</a:p>|</w:p>", xml)]


def pptx_text(path, media_dir=None):
    """Slides in order: text per paragraph, speaker notes (matched through the slide's rels), pictures saved."""
    z = zipfile.ZipFile(path)
    names = set(z.namelist())
    order = []
    try:  # real slide order lives in presentation.xml + its rels
        pres = z.read("ppt/presentation.xml").decode("utf-8", "replace")
        rels = z.read("ppt/_rels/presentation.xml.rels").decode("utf-8", "replace")
        rmap = dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', rels))
        rmap.update({k: v for v, k in re.findall(r'Target="([^"]+)"[^>]*Id="([^"]+)"', rels)})
        for rid in re.findall(r'<p:sldId [^>]*r:id="([^"]+)"', pres):
            t = rmap.get(rid, "")
            if t:
                order.append("ppt/" + t.lstrip("/").replace("ppt/", "", 1) if not t.startswith("ppt/") else t)
    except KeyError:
        pass
    if not order:
        order = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=lambda n: int(re.findall(r"\d+", n)[0]))
    lines, pics = [], 0
    for i, sname in enumerate(order, 1):
        if sname not in names:
            continue
        xml = z.read(sname).decode("utf-8", "replace")
        paras = [p.strip() for p in _xml_text(xml, "a:t") if p.strip()]
        rel_name = sname.replace("slides/", "slides/_rels/") + ".rels"
        notes, imgs = "", []
        if rel_name in names:
            rels = z.read(rel_name).decode("utf-8", "replace")
            for target in re.findall(r'Target="([^"]+)"', rels):
                full = os.path.normpath(os.path.join(os.path.dirname(sname), target)).replace("\\", "/")
                if "notesSlide" in target and full in names:
                    nx = z.read(full).decode("utf-8", "replace")
                    notes = " ".join(p.strip() for p in _xml_text(nx, "a:t") if p.strip())
                    notes = re.sub(r"\s*\b\d{1,3}\s*$", "", notes)  # drop the trailing slide number
                elif "/media/" in full and full in names:
                    imgs.append(full)
        lines.append(f"## Slide {i}")
        lines += [f"- {p}" for p in paras] or ["(picture only)"]
        if imgs:
            lines.append(f"[{len(imgs)} picture(s): " + ", ".join(f"slide{i}-{k}{Path(m).suffix}" for k, m in enumerate(imgs, 1)) + "]")
            if media_dir:
                media_dir.mkdir(parents=True, exist_ok=True)
                for k, m in enumerate(imgs, 1):
                    (media_dir / f"slide{i}-{k}{Path(m).suffix}").write_bytes(z.read(m))
                    pics += 1
        if notes:
            lines.append(f"Notes: {notes}")
        lines.append("")
    return "\n".join(lines), pics


def docx_text(path):
    z = zipfile.ZipFile(path)
    xml = z.read("word/document.xml").decode("utf-8", "replace")
    return "\n".join(p.strip() for p in _xml_text(xml, "w:t") if p.strip()), 0


def pdf_text(path):
    try:
        from pypdf import PdfReader  # optional: py -m pip install pypdf
    except ImportError:
        return "(PDF text not extracted: run `py -m pip install pypdf` once, then pull again.)", 0
    reader = PdfReader(str(path))
    pages, empty = [], 0
    for i, pg in enumerate(reader.pages, 1):
        t = (pg.extract_text() or "").strip()
        if not t:
            empty += 1
        pages.append(f"## Page {i}\n{t or '(no text layer: scanned page, needs OCR)'}\n")
    note = f"\n> {empty} of {len(pages)} pages have no text layer (scanned). Claude reads those pages as images.\n" if empty else ""
    return "\n".join(pages) + note, 0


def ppt_text(path, media_dir=None):
    """Old binary .ppt. Exact if LibreOffice is installed (converts to .pptx first), else a best-effort text scan."""
    soffice = shutil.which("soffice") or shutil.which("soffice.exe")
    for guess in (r"C:\Program Files\LibreOffice\program\soffice.exe", r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
        if not soffice and Path(guess).is_file():
            soffice = guess
    if soffice:
        with tempfile.TemporaryDirectory() as td:
            subprocess.run([soffice, "--headless", "--convert-to", "pptx", "--outdir", td, str(path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
            out = Path(td) / (path.stem + ".pptx")
            if out.is_file():
                return pptx_text(out, media_dir)
    data = path.read_bytes()
    lines, i, n = [], 0, len(data)
    while i + 8 <= n:
        ver = data[i] & 0x0F
        rtype = int.from_bytes(data[i + 2:i + 4], "little")
        ln = int.from_bytes(data[i + 4:i + 8], "little")
        if ver == 0 and rtype in (0x0FA0, 0x0FA8) and 0 < ln < 20000 and i + 8 + ln <= n:
            raw = data[i + 8:i + 8 + ln]
            s = raw.decode("utf-16-le", "replace") if rtype == 0x0FA0 else raw.decode("latin-1")
            if not re.search(r"[\x00-\x08]", s):
                lines += [f"- {x.strip()}" for x in re.split(r"[\r\v]", s) if x.strip() and x.strip() != "*"]
                i += 8 + ln
                continue
        i += 1
    head = "> Old .ppt file: text pulled in reading order without slide numbers. Install LibreOffice for exact slides.\n\n"
    return head + "\n".join(lines), 0


def extract(path, media_dir):
    ext = path.suffix.lower()
    if ext == ".pptx":
        return pptx_text(path, media_dir)
    if ext == ".docx":
        return docx_text(path)
    if ext == ".pdf":
        return pdf_text(path)
    if ext == ".ppt":
        return ppt_text(path, media_dir)
    if ext in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace"), 0
    return None, 0


def strip_html(h):
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h or "")
    h = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", h)
    return html.unescape(re.sub(r"<[^>]+>", " ", h)).replace("\xa0", " ")


def links_in(h):
    return re.findall(r'(?:href|src)="(https?://[^"]+)"', h or "")


def when(iso):
    if not iso:
        return "no date"
    d = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(CT)
    return d.strftime("%a %m/%d %I:%M %p CT").replace(" 0", " ")


# ---------------------------------------------------------------- per course
def pull_course(cv, c, root, vault, state, list_only, log):
    cdir = root / c["folder"] / "canvas"
    slug = vault_slug(c["code"])
    cstate = state.setdefault(str(c["id"]), {})
    modules = cv.get(f"/api/v1/courses/{c['id']}/modules", {"include[]": "items", "per_page": 100})
    assignments = cv.get(f"/api/v1/courses/{c['id']}/assignments", {"per_page": 100, "order_by": "due_at"})
    since = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%d")
    try:
        anns = cv.get("/api/v1/announcements", {"context_codes[]": f"course_{c['id']}", "per_page": 50, "start_date": since})
    except urllib.error.HTTPError:
        anns = []
    videos, new_files, ov = [], [], [f"# {c['code']} · Canvas overview", "", f"Pulled {datetime.now(CT).strftime('%Y-%m-%d %I:%M %p CT')} by canvas_pull.py (read-only). Canvas course {c['id']}.", ""]

    exams = [a for a in assignments if re.search(r"exam|midterm|final|test|quiz", a.get("name", ""), re.I)]
    ov += ["## Exams and quizzes", "| Due | Name | Points |", "|---|---|---|"]
    ov += [f"| {when(a.get('due_at'))} | {a['name']} | {a.get('points_possible') or ''} |" for a in exams] or ["| none posted | | |"]
    ov += ["", "## Due dates (next 60 days)", "| Due | Name |", "|---|---|"]
    soon = datetime.now(timezone.utc) + timedelta(days=60)
    upcoming = [a for a in assignments if a.get("due_at") and datetime.now(timezone.utc) - timedelta(days=1)
                <= datetime.fromisoformat(a["due_at"].replace("Z", "+00:00")) <= soon]
    ov += [f"| {when(a['due_at'])} | {a['name']} |" for a in upcoming] or ["| nothing due | |"]
    ov += ["", "## Announcements (last 45 days)"]
    for a in anns or []:
        ov.append(f"### {(a.get('posted_at') or '')[:10]} · {a.get('title', '')}")
        ov.append(re.sub(r"\n\s*\n+", "\n", strip_html(a.get("message", ""))).strip()[:1500])
        videos += [u for u in links_in(a.get("message")) if any(h in u for h in VIDEO_HOSTS)]
    if not anns:
        ov.append("none")

    if c["mode"] == "full":
        ov += ["", "## Modules (in Canvas order)"]
        for m in modules:
            ov.append(f"### {m.get('name', '')}")
            for it in m.get("items", []):
                kind, title = it.get("type"), it.get("title", "")
                ov.append(f"- {kind}: {title}")
                if kind in ("ExternalUrl", "ExternalTool"):
                    url = it.get("external_url") or it.get("html_url", "")
                    if any(h in url for h in VIDEO_HOSTS) or kind == "ExternalTool" and re.search(r"video|jove", title, re.I):
                        videos.append(f"{title} · {url}")
                elif kind == "Page" and it.get("page_url"):
                    page = cv.get(f"/api/v1/courses/{c['id']}/pages/{it['page_url']}")
                    body = page.get("body") or ""
                    videos += [u for u in links_in(body) if any(h in u for h in VIDEO_HOSTS)]
                    key = f"page:{it['page_url']}"
                    if cstate.get(key) != page.get("updated_at"):
                        new_files.append((m, it, None, "page", body, key, page.get("updated_at")))
                elif kind == "File" and it.get("content_id"):
                    try:
                        f = cv.get(f"/api/v1/files/{it['content_id']}")
                    except urllib.error.HTTPError as e:
                        log(f"  ! {title}: Canvas said {e.code} (locked or hidden), skipped")
                        continue
                    key = f"file:{f['id']}"
                    if cstate.get(key) != f.get("updated_at"):
                        new_files.append((m, it, f, "file", None, key, f.get("updated_at")))
                    if (f.get("content-type") or "").startswith("video/"):
                        videos.append(f"{f.get('display_name')} · Canvas file {f['id']} (downloaded)")
    ov += ["", "## Videos linked from Canvas"] + ([f"- {v}" for v in dict.fromkeys(videos)] or ["none"])

    log(f"{c['code']}: {len(modules)} modules, {len(assignments)} assignments, {len(new_files)} new or changed item(s)")
    if list_only:
        for m, it, f, kind, _, _, _ in new_files:
            log(f"  new: {m.get('name')} / {it.get('title')}")
        return
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "overview.md").write_text("\n".join(ov) + "\n", encoding="utf-8")
    vdeck = vault / "raw" / "school" / slug / "decks" if vault else None
    vcan = vault / "raw" / "school" / slug / "canvas" if vault else None
    if vcan:
        vcan.mkdir(parents=True, exist_ok=True)
        (vcan / "overview.md").write_text("\n".join(ov) + "\n", encoding="utf-8")

    for m, it, f, kind, body, key, stamp in new_files:
        mdir = cdir / "files" / safe(m.get("name", "module"), 60)
        if kind == "page":
            text, name, src = re.sub(r"\n\s*\n+", "\n\n", strip_html(body)).strip(), safe(it["title"]), f"Canvas page {it['page_url']}"
            local = mdir / (name + ".page.md")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(text, encoding="utf-8")
            pics = 0
        else:
            name = safe(f.get("display_name") or f.get("filename") or it["title"])
            local = mdir / name
            if (f.get("size") or 0) > SKIP_BIGGER_THAN:
                log(f"  skip (over 200 MB): {name}")
                continue
            try:
                cv.download(f["url"], local)
            except urllib.error.HTTPError as e:
                log(f"  ! {name}: download failed ({e.code})")
                continue
            src = f"Canvas file {f['id']}"
            text, pics = (None, 0)
            if local.suffix.lower() in TEXT_TYPES:
                try:
                    text, pics = extract(local, mdir / (local.stem + "_slides"))
                except (zipfile.BadZipFile, KeyError, ValueError, OSError) as e:
                    log(f"  ! {name}: could not read text ({e.__class__.__name__})")
        cstate[key] = stamp
        if text is None:
            log(f"  saved: {local.relative_to(root)}")
            continue
        md = (f"---\ncourse_code: {c['code']}\nsource: \"{src} · module: {m.get('name', '')} · {it.get('title', '')}\"\n"
              f"canvas_updated: {stamp}\npulled: {datetime.now(CT).strftime('%Y-%m-%d')}\nslide_pictures: {pics}\n---\n"
              f"# {it.get('title') or name}\n\nModule: {m.get('name', '')}\n\n{text}\n")
        (local.parent / (local.stem + ".md")).write_text(md, encoding="utf-8") if kind == "file" else None
        if vdeck:
            vdeck.mkdir(parents=True, exist_ok=True)
            (vdeck / (safe(local.stem.replace(".page", "")) + ".md")).write_text(md, encoding="utf-8")
        log(f"  {'page' if kind == 'page' else 'file'}: {name}" + (f" ({pics} slide pictures)" if pics else ""))


def transcribe_videos(root, courses, log):
    """YouTube links from overview.md → audio via yt-dlp → ElevenLabs (same engine as the lecture transcripts)."""
    ytdlp = shutil.which("yt-dlp")
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not ytdlp or not key:
        log("Videos: need yt-dlp (py -m pip install yt-dlp) and ELEVENLABS_API_KEY. Skipped.")
        return
    sys.path.insert(0, str(HERE))
    import transcribe_lectures as tl
    for c in courses:
        ov = root / c["folder"] / "canvas" / "overview.md"
        if not ov.is_file():
            continue
        for url in dict.fromkeys(re.findall(r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)[\w-]+", ov.read_text(encoding="utf-8"))):
            vid = re.sub(r".*[=/]", "", url)
            out = root / c["folder"] / "canvas" / "videos" / f"{vid}.md"
            if out.exists():
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            audio = out.with_suffix(".m4a")
            subprocess.run([ytdlp, "-f", "bestaudio[ext=m4a]/bestaudio", "-o", str(audio), url], check=False)
            if not audio.exists():
                log(f"  ! could not download {url}")
                continue
            terms, _ = tl.load_keyterms(root, c["folder"])
            res = tl.transcribe(audio, key, terms)
            out.write_text(f"# Video transcript · {url}\n\n{res.get('text', '')}\n", encoding="utf-8")
            audio.unlink(missing_ok=True)
            log(f"  video transcribed: {url}")


def main():
    ap = argparse.ArgumentParser(description="Pull Canvas material for every class (read-only).")
    ap.add_argument("--root", type=Path, default=default_root(), help="Fall2026 folder")
    ap.add_argument("--vault", type=Path, default=Path.home() / "Documents" / "agent-team", help="agent-team vault folder")
    ap.add_argument("--course", help="only this class, e.g. BIOL1402 or CIS3367")
    ap.add_argument("--list", action="store_true", help="show what's new, download nothing")
    ap.add_argument("--videos", action="store_true", help="also transcribe linked YouTube videos")
    a = ap.parse_args()
    token = os.environ.get("CANVAS_TOKEN")
    if not token:
        print("No CANVAS_TOKEN. In Canvas: Account > Settings > + New Access Token, then in a terminal:\n"
              '  setx CANVAS_TOKEN "paste-the-token"\nClose and reopen the terminal, then run this again.')
        return 2
    vault = a.vault if a.vault.is_dir() else None
    if not vault:
        print(f"(no vault folder at {a.vault}; only the laptop copy is written)")
    courses = [c for c in COURSES if not a.course or vault_slug(c["code"]).lower() == a.course.replace(" ", "").lower()]
    sfile = a.root / "_canvas_state.json"
    state = json.loads(sfile.read_text(encoding="utf-8")) if sfile.is_file() else {}
    cv = Canvas(token)
    try:
        me = cv.get("/api/v1/users/self")
    except urllib.error.HTTPError as e:
        print(f"Canvas refused the token ({e.code}). Make a new one and run setx again.")
        return 2
    print(f"Signed in to Canvas as {me.get('name', 'you')}. Read-only pull.\n")
    for c in courses:
        try:
            pull_course(cv, c, a.root, vault, state, a.list, print)
        except urllib.error.HTTPError as e:
            print(f"{c['code']}: Canvas said {e.code}; skipped this class")
        if not a.list:
            a.root.mkdir(parents=True, exist_ok=True)
            sfile.write_text(json.dumps(state, indent=1), encoding="utf-8")
    if a.videos and not a.list:
        transcribe_videos(a.root, courses, print)
    print("\nDone." + (" Your vault sync will push the new files; tell Claude to build or update the guide." if vault and not a.list else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
