"""
Format converters for the JuneDesk processing worker.

Every converter returns an Output whose pages follow the app's page model (pages.json.gz = DocText):
  {"v": 1, "kind": "pdf" | "word" | "sheet" | "email" | "text" | "html", "pages": [{"n", "label", "text", "ocr"?, "rows"?}]}
Structure is kept as Markdown (headings, tables, lists). Scanned pages are not OCR'd here: they are cut into small
PDF subsets that JuneDesk reads with its AI vision model (better for Indian scripts and handwriting).
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import json
import logging
import mailbox
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

log = logging.getLogger("convert")

SCANNED_PAGE_CHARS = 40
OCR_GROUP = 4
AUDIO_CHUNK_SECONDS = int(os.environ.get("AUDIO_CHUNK_SECONDS", "600"))
SHEET_BLOCK = 120
MAX_SHEET_ROWS = int(os.environ.get("MAX_SHEET_ROWS", "150000"))
PART_SIZE = 3200
DOCLING_MAX_PAGES = int(os.environ.get("DOCLING_MAX_PAGES", "400"))
USE_DOCLING = os.environ.get("USE_DOCLING", "1") == "1"

PDF_EXT = {"pdf"}
WORD_EXT = {"doc", "docx", "docm", "rtf", "odt", "wpd", "wps", "dot", "dotx"}
SLIDE_EXT = {"ppt", "pptx", "odp", "pps", "ppsx"}
SHEET_EXT = {"xlsx", "xlsm", "xls", "xlsb", "ods", "csv", "tsv"}
TEXT_EXT = {"txt", "md", "log", "json", "xml"}
HTML_EXT = {"html", "htm", "xhtml", "mht", "mhtml"}
EMAIL_EXT = {"eml", "msg"}
MAILBOX_EXT = {"pst", "ost", "mbox"}
ARCHIVE_EXT = {"zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz"}
AUDIO_EXT = {"mp3", "wav", "m4a", "ogg", "webm", "flac", "aac", "opus", "amr", "wma", "mp4a", "3ga"}
VIDEO_EXT = {"mp4", "mov", "mkv", "avi", "wmv", "m4v", "3gp", "mpeg", "mpg", "ts"}
IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "gif", "tif", "tiff", "heic", "heif", "bmp"}
IGNORE = re.compile(r"(^|/)(\.DS_Store|Thumbs\.db|desktop\.ini|__MACOSX|~\$[^/]*|\.[^/]*)$", re.I)


class Permanent(Exception):
    """A failure that retrying will not fix (damaged file, password-protected, unsupported)."""


@dataclass
class Child:
    path: Path
    name: str
    folder: str
    mime: str = ""


@dataclass
class Output:
    kind: str | None = None
    pages: list[dict] = field(default_factory=list)
    scanned: list[int] = field(default_factory=list)
    uploads: dict[str, Path] = field(default_factory=dict)  # relative path under the document folder -> local file
    audio_units: list[str] = field(default_factory=list)
    image: bool = False
    children: list[Child] | None = None
    unsupported: str | None = None
    date: str | None = None
    meta: dict = field(default_factory=dict)


Progress = Callable[[float], None]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr.decode(errors='ignore')[-400:]}")
    return r


# --------------------------------------------------------------------------- text helpers

def tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_markdown(md: str) -> str:
    md = re.sub(r"<!--\s*image\s*-->", "", md)
    return tidy(md)


def split_into_parts(text: str, size: int = PART_SIZE) -> list[dict]:
    """Port of the app's splitIntoParts: heading-aware parts of ~size characters labelled 'Part n · heading'."""
    paras = re.split(r"\n{2,}", text)
    pages: list[dict] = []
    buf: list[str] = []
    length = 0
    heading = ""
    part_heading = ""

    def flush():
        nonlocal buf, length, part_heading
        if not buf:
            return
        n = len(pages) + 1
        pages.append({"n": n, "label": f"Part {n} · {part_heading[:60]}" if part_heading else f"Part {n}", "text": "\n\n".join(buf)})
        buf, length, part_heading = [], 0, heading

    for p in paras:
        h = re.match(r"^#{1,6}\s+(.+)", p)
        only_headings = all(re.match(r"^#{1,6}\s", x) for x in buf)
        if h:
            if length > size * 0.4 and not only_headings:
                flush()
            heading = h.group(1).strip()
        elif length + len(p) > size and buf and not only_headings:
            flush()
        if not buf:
            part_heading = heading
        buf.append(p)
        length += len(p) + 2
    flush()
    return pages or [{"n": 1, "label": "Part 1", "text": ""}]


# --------------------------------------------------------------------------- docling (optional, loaded once)

_converter = None


def docling_converter():
    global _converter
    if _converter is None:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        opts = PdfPipelineOptions()
        opts.do_ocr = False  # scanned pages are read by JuneDesk's AI vision model
        opts.do_table_structure = True
        _converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    return _converter


def docling_available() -> bool:
    if not USE_DOCLING:
        return False
    try:
        import docling  # noqa: F401
        return True
    except Exception:
        return False


def docling_pdf_pages(path: Path, total: int, progress: Progress) -> dict[int, str]:
    """Per-page Markdown with layout and tables, converted in page batches to bound memory."""
    conv = docling_converter()
    out: dict[int, str] = {}
    batch = 40
    for start in range(1, total + 1, batch):
        end = min(total, start + batch - 1)
        res = conv.convert(str(path), page_range=(start, end), raises_on_error=False)
        doc = res.document
        keys = sorted(doc.pages.keys()) if getattr(doc, "pages", None) else []
        offset = start - 1 if keys and keys[0] == 1 and start > 1 else 0
        for k in keys:
            md = clean_markdown(doc.export_to_markdown(page_no=k))
            if md:
                out[k + offset] = md
        progress(end / total)
    return out


def docling_markdown(path: Path) -> tuple[str, dict[int, str]]:
    """Whole-document Markdown plus per-page Markdown where the format has pages (slides)."""
    doc = docling_converter().convert(str(path)).document
    pages: dict[int, str] = {}
    for k in sorted(getattr(doc, "pages", {}) or {}):
        md = clean_markdown(doc.export_to_markdown(page_no=k))
        if md:
            pages[k] = md
    return clean_markdown(doc.export_to_markdown()), pages


# --------------------------------------------------------------------------- PDF

def pdf_output(path: Path, work: Path, progress: Progress, min_chars: int = SCANNED_PAGE_CHARS) -> Output:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as e:
        msg = str(e).lower()
        if "password" in msg:
            raise Permanent("This file is password-protected. Remove the password and upload it again.")
        raise Permanent("This file looks damaged or is not a real PDF.")
    total = len(pdf)
    raw: list[str] = []
    for i in range(total):
        page = pdf[i]
        tp = page.get_textpage()
        raw.append(tp.get_text_bounded() or "")
        tp.close()
        page.close()
    progress(0.15)
    md: dict[int, str] = {}
    if docling_available() and total <= DOCLING_MAX_PAGES:
        try:
            md = docling_pdf_pages(path, total, lambda f: progress(0.15 + 0.75 * f))
        except Exception as e:  # layout model failure: keep plain text
            log.warning("docling failed, using plain text: %s", e)
    out = Output(kind="pdf", meta={"layout": "docling" if md else "text"})
    for i in range(total):
        n = i + 1
        visible = len(re.sub(r"\s", "", raw[i]))
        if visible < min_chars:
            out.pages.append({"n": n, "label": f"Page {n}", "text": "", "ocr": True})
            out.scanned.append(n)
        else:
            out.pages.append({"n": n, "label": f"Page {n}", "text": md.get(n) or tidy(raw[i])})
    for g in range(0, len(out.scanned), OCR_GROUP):
        nums = out.scanned[g:g + OCR_GROUP]
        unit = "p" + "_".join(map(str, nums))
        sub = pdfium.PdfDocument.new()
        sub.import_pages(pdf, [x - 1 for x in nums])
        dest = work / f"{unit}.pdf"
        sub.save(str(dest))
        sub.close()
        out.uploads[f"ocr/{unit}.pdf"] = dest
    pdf.close()
    return out


# --------------------------------------------------------------------------- office documents

def soffice_convert(path: Path, work: Path, fmt: str) -> Path:
    outdir = work / "converted"
    outdir.mkdir(exist_ok=True)
    run(["soffice", "--headless", "--norestore", f"-env:UserInstallation=file://{work}/lo-profile", "--convert-to", fmt, "--outdir", str(outdir), str(path)], timeout=1800)
    found = list(outdir.glob(f"*.{fmt.split(':')[0]}"))
    if not found:
        raise Permanent("This document could not be converted. It may be damaged or password-protected.")
    return found[0]


def word_output(path: Path, ext: str, work: Path) -> Output:
    src = path if ext == "docx" else soffice_convert(path, work, "docx")
    if docling_available():
        md, _ = docling_markdown(src)
    else:
        md = docx_plain(src)
    return Output(kind="word", pages=split_into_parts(md))


def docx_plain(path: Path) -> str:
    """Fallback when docling is not installed: paragraphs from document.xml."""
    import zipfile
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf8", errors="ignore")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S)
    texts = ["".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, flags=re.S)) for p in paras]
    return tidy("\n\n".join(t for t in texts if t.strip()))


def slides_output(path: Path, ext: str, work: Path) -> Output:
    src = path if ext == "pptx" else soffice_convert(path, work, "pptx")
    if not docling_available():
        pdf = soffice_convert(src, work, "pdf")
        out = pdf_output(pdf, work, lambda _f: None, min_chars=4)  # slides carry little text
        for p in out.pages:
            p["label"] = f"Slide {p['n']}"
        out.kind = "word"
        return out
    md, pages = docling_markdown(src)
    if pages:
        return Output(kind="word", pages=[{"n": i + 1, "label": f"Slide {k}", "text": t} for i, (k, t) in enumerate(sorted(pages.items()))])
    return Output(kind="word", pages=split_into_parts(md))


# --------------------------------------------------------------------------- spreadsheets

def _cell(v) -> str:
    s = "" if v is None else str(v)
    if s.lower() == "nan":
        s = ""
    return re.sub(r"\s+", " ", s).replace("|", "\\|").strip()[:300]


def _sheet_blocks(name: str, rows_iter, out: Output, budget: list[int]):
    header: list[str] | None = None
    width = 0
    block: list[list] = []
    first_row = 0
    row_no = 0

    def emit():
        nonlocal block
        if not block or header is None:
            return
        first = first_row
        last = first_row + len(block) - 1
        lines = [f'### Sheet "{name}" · rows {first}–{last}', "", f"| Row | {' | '.join(header)} |", f"| --- | {' | '.join('---' for _ in header)} |"]
        lines += [f"| {first + k} | {' | '.join(_cell(r[c]) if c < len(r) else '' for c in range(width))} |" for k, r in enumerate(block)]
        out.pages.append({"n": len(out.pages) + 1, "label": f"{name} · rows {first}–{last}", "text": "\n".join(lines), "rows": [first, last]})
        block = []

    for r in rows_iter:
        row_no += 1
        values = list(r)
        if header is None:
            if sum(1 for c in values if _cell(c)) >= min(2, max(1, len(values))):
                width = min(60, len(values))
                header = [_cell(c) or f"Col {i + 1}" for i, c in enumerate(values[:width])]
            continue
        if not any(_cell(c) for c in values):
            continue
        if budget[0] <= 0:
            out.meta["truncated"] = True
            break
        if not block:
            first_row = row_no
        block.append(values)
        budget[0] -= 1
        if len(block) >= SHEET_BLOCK:
            emit()
    emit()


def sheet_output(path: Path, ext: str) -> Output:
    import pandas as pd

    out = Output(kind="sheet")
    budget = [MAX_SHEET_ROWS]
    if ext in ("csv", "tsv"):
        sep = "\t" if ext == "tsv" else None
        reader = pd.read_csv(path, sep=sep, engine="python", header=None, dtype=str, chunksize=20000, on_bad_lines="skip", encoding_errors="replace")

        def rows():
            for chunk in reader:
                for r in chunk.itertuples(index=False, name=None):
                    yield r
        _sheet_blocks(path.stem[:60], rows(), out, budget)
    else:
        engine = {"xlsb": "pyxlsb", "ods": "odf", "xls": "xlrd"}.get(ext)
        try:
            sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=str, engine=engine)
        except Exception as e:
            if "encrypt" in str(e).lower() or "password" in str(e).lower():
                raise Permanent("This spreadsheet is password-protected. Remove the password and upload it again.")
            raise
        for name, df in sheets.items():
            if budget[0] <= 0:
                out.meta["truncated"] = True
                break
            _sheet_blocks(str(name)[:60], df.itertuples(index=False, name=None), out, budget)
    if out.meta.get("truncated"):
        out.meta["note"] = f"Only the first {MAX_SHEET_ROWS:,} rows are searchable; the full file is stored."
    if not out.pages:
        out.pages = [{"n": 1, "label": "Sheet", "text": ""}]
    return out


# --------------------------------------------------------------------------- text and web pages

def decode_text(path: Path) -> str:
    from charset_normalizer import from_path
    best = from_path(str(path)).best()
    return str(best) if best else path.read_bytes().decode("utf8", errors="replace")


def html_to_markdown(html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "head"]):
        t.decompose()
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            h.replace_with(f"\n\n{'#' * level} {h.get_text(' ', strip=True)}\n\n")
    for li in soup.find_all("li"):
        li.replace_with(f"\n- {li.get_text(' ', strip=True)}")
    for table in soup.find_all("table"):
        rows = [[c.get_text(" ", strip=True).replace("|", "\\|") for c in tr.find_all(["td", "th"])] for tr in table.find_all("tr")]
        rows = [r for r in rows if any(r)]
        if rows:
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            md = [f"| {' | '.join(rows[0])} |", f"| {' | '.join('---' for _ in rows[0])} |"] + [f"| {' | '.join(r)} |" for r in rows[1:]]
            table.replace_with("\n\n" + "\n".join(md) + "\n\n")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for p in soup.find_all(["p", "div"]):
        p.insert_after("\n\n")
    return tidy(soup.get_text())


def text_output(path: Path, ext: str) -> Output:
    text = decode_text(path)
    if ext in HTML_EXT:
        return Output(kind="html", pages=split_into_parts(html_to_markdown(text)))
    return Output(kind="text", pages=split_into_parts(tidy(text)))


# --------------------------------------------------------------------------- email

def _iso(dt) -> str | None:
    try:
        return dt.date().isoformat() if isinstance(dt, datetime) else None
    except Exception:
        return None


def eml_output(path: Path, work: Path, folder: str, name: str) -> Output:
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = ""
    if body is not None:
        content = body.get_content()
        text = html_to_markdown(content) if body.get_content_type() == "text/html" else tidy(content)
    head = [f"**{k}:** {msg[k]}" for k in ("From", "To", "Cc", "Date", "Subject") if msg[k]]
    out = Output(kind="email", pages=split_into_parts("\n".join(head) + "\n\n" + text))
    for i, p in enumerate(out.pages):
        p["label"] = f"Email · part {i + 1}" if len(out.pages) > 1 else "Email"
    try:
        out.date = _iso(msg["Date"].datetime) if msg["Date"] else None
    except Exception:
        out.date = None
    att_dir = work / "attachments"
    kids: list[Child] = []
    for k, part in enumerate(msg.iter_attachments()):
        fname = part.get_filename() or f"attachment-{k + 1}"
        data = part.get_payload(decode=True) or b""
        if not data:
            continue
        att_dir.mkdir(exist_ok=True)
        dest = att_dir / f"{k}-{re.sub(r'[^A-Za-z0-9._-]', '_', fname)[:120]}"
        dest.write_bytes(data)
        kids.append(Child(dest, fname[:500], "/".join(x for x in (folder, name) if x)[:1000], part.get_content_type()))
    out.meta["attachments"] = len(kids)
    out.meta["_children"] = kids  # uploaded by the worker after pages
    return out


def msg_output(path: Path, work: Path, folder: str, name: str) -> Output:
    import extract_msg

    m = extract_msg.openMsg(str(path))
    try:
        head = [f"**{k}:** {v}" for k, v in (("From", m.sender), ("To", m.to), ("Cc", m.cc), ("Date", m.date), ("Subject", m.subject)) if v]
        body = m.body or ""
        if not body.strip() and getattr(m, "htmlBody", None):
            body = html_to_markdown(m.htmlBody.decode("utf8", errors="ignore") if isinstance(m.htmlBody, bytes) else m.htmlBody)
        out = Output(kind="email", pages=split_into_parts("\n".join(head) + "\n\n" + tidy(body)))
        for i, p in enumerate(out.pages):
            p["label"] = f"Email · part {i + 1}" if len(out.pages) > 1 else "Email"
        out.date = _iso(m.date) if isinstance(m.date, datetime) else None
        att_dir = work / "attachments"
        kids: list[Child] = []
        for k, a in enumerate(m.attachments):
            data = getattr(a, "data", None)
            if not isinstance(data, (bytes, bytearray)) or not data:
                continue
            fname = a.longFilename or a.shortFilename or f"attachment-{k + 1}"
            att_dir.mkdir(exist_ok=True)
            dest = att_dir / f"{k}-{re.sub(r'[^A-Za-z0-9._-]', '_', fname)[:120]}"
            dest.write_bytes(data)
            kids.append(Child(dest, fname[:500], "/".join(x for x in (folder, name) if x)[:1000]))
        out.meta["attachments"] = len(kids)
        out.meta["_children"] = kids
        return out
    finally:
        m.close()


def mailbox_output(path: Path, ext: str, work: Path, folder: str, name: str) -> Output:
    dest = work / "mailbox"
    dest.mkdir()
    base_folder = "/".join(x for x in (folder, re.sub(r"\.[^.]+$", "", name)) if x)
    kids: list[Child] = []
    if ext == "mbox":
        for k, msg in enumerate(mailbox.mbox(str(path))):
            f = dest / f"{k:06d}.eml"
            f.write_bytes(msg.as_bytes())
            subject = re.sub(r"[^\w .,-]", "_", str(msg.get("subject") or "message"))[:80]
            kids.append(Child(f, f"{k + 1:06d} {subject}.eml", base_folder, "message/rfc822"))
    else:
        run(["readpst", "-e", "-q", "-o", str(dest), str(path)], timeout=6 * 3600)
        for f in sorted(dest.rglob("*")):
            if f.is_file():
                rel = f.parent.relative_to(dest).as_posix()
                kids.append(Child(f, f.name if f.suffix else f"{f.name}.eml", "/".join(x for x in (base_folder, rel) if x and x != ".")[:1000], "message/rfc822"))
    return Output(children=kids)


# --------------------------------------------------------------------------- archives

def archive_output(path: Path, work: Path, folder: str, name: str) -> Output:
    dest = work / "unpacked"
    dest.mkdir()
    try:
        run(["7z", "x", "-y", "-p", f"-o{dest}", str(path)], timeout=6 * 3600)
    except RuntimeError as e:
        if "Wrong password" in str(e) or "encrypted" in str(e).lower():
            raise Permanent("This archive is password-protected. Remove the password and upload it again.")
        if shutil.which("unar"):
            run(["unar", "-q", "-f", "-o", str(dest), str(path)], timeout=6 * 3600)
        else:
            raise Permanent("This archive could not be opened. It may be damaged.")
    # .tar.gz and friends: unpack the inner tar too.
    inner = [p for p in dest.iterdir()]
    if len(inner) == 1 and inner[0].suffix.lower() == ".tar":
        tar = inner[0]
        run(["7z", "x", "-y", f"-o{dest}", str(tar)])
        tar.unlink()
    base_folder = "/".join(x for x in (folder, re.sub(r"(\.tar)?\.[^.]+$", "", name)) if x)
    kids: list[Child] = []
    for f in sorted(dest.rglob("*")):
        rel = f.relative_to(dest).as_posix()
        if not f.is_file() or IGNORE.search(rel) or f.stat().st_size == 0:
            continue
        sub = f.parent.relative_to(dest).as_posix()
        kids.append(Child(f, f.name[:500], "/".join(x for x in (base_folder, sub) if x and x != ".")[:1000]))
    return Output(children=kids)


# --------------------------------------------------------------------------- audio and video

def media_output(path: Path, work: Path) -> Output:
    chunks = work / "audio"
    chunks.mkdir()
    run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "libmp3lame", "-b:a", "48k", "-f", "segment", "-segment_time", str(AUDIO_CHUNK_SECONDS), "-reset_timestamps", "1",
         str(chunks / "c%d.mp3")], timeout=6 * 3600)
    files = sorted(chunks.glob("c*.mp3"), key=lambda p: int(p.stem[1:]))
    if not files:
        raise Permanent("No audio track was found in this file.")
    out = Output()
    offset = 0.0
    for f in files:
        out.audio_units.append(f"{f.stem}@{round(offset, 3)}")
        out.uploads[f"audio/{f.name}"] = f
        offset += probe_seconds(f)
    out.meta["seconds"] = round(offset)
    return out


def probe_seconds(path: Path) -> float:
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)])
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return float(AUDIO_CHUNK_SECONDS)


# --------------------------------------------------------------------------- images

def image_output(path: Path, work: Path) -> Output:
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    try:
        img = Image.open(path)
    except Exception:
        raise Permanent("This image could not be opened. It may be damaged.")
    frames = getattr(img, "n_frames", 1)
    if frames > 1:
        # Multi-page scans (TIFF): treat as a scanned PDF.
        pdf = work / "frames.pdf"
        pages = []
        for i in range(frames):
            img.seek(i)
            pages.append(ImageOps.exif_transpose(img.convert("RGB")))
        pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=200)
        return pdf_output(pdf, work, lambda _f: None)
    rgb = ImageOps.exif_transpose(img).convert("RGB")
    rgb.thumbnail((2400, 2400))
    dest = work / "img.jpg"
    rgb.save(dest, "JPEG", quality=88)
    return Output(image=True, uploads={"ocr/img.jpg": dest})


# --------------------------------------------------------------------------- dispatch

def convert(path: Path, doc: dict, work: Path, progress: Progress) -> Output:
    ext = (doc.get("ext") or path.suffix.lstrip(".")).lower()
    name = doc.get("name") or path.name
    folder = doc.get("folder") or ""
    if ext in PDF_EXT:
        return pdf_output(path, work, progress)
    if ext in WORD_EXT:
        return word_output(path, ext, work)
    if ext in SLIDE_EXT:
        return slides_output(path, ext, work)
    if ext in SHEET_EXT:
        return sheet_output(path, ext)
    if ext in TEXT_EXT or ext in HTML_EXT:
        return text_output(path, ext)
    if ext == "eml":
        return eml_output(path, work, folder, name)
    if ext == "msg":
        return msg_output(path, work, folder, name)
    if ext in MAILBOX_EXT:
        return mailbox_output(path, ext, work, folder, name)
    if ext in ARCHIVE_EXT:
        return archive_output(path, work, folder, name)
    if ext in AUDIO_EXT or ext in VIDEO_EXT:
        return media_output(path, work)
    if ext in IMAGE_EXT:
        return image_output(path, work)
    return Output(unsupported=f"JuneDesk can't read .{ext or 'unknown'} files yet. The file is stored and can be downloaded.")
