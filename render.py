"""
"True view" renderer for the JuneDesk processing worker.

Runs after a document has been read and indexed, so the Source panel can show the real file instead of extracted text:
  pdf    Word, RTF, ODT and PowerPoint files -> view.pdf (LibreOffice), shown with a selectable text layer
  grid   spreadsheets (xlsx/xlsm/xls/xlsb/ods/csv/tsv) -> grid/meta.json + grid/s{i}c{k}.json chunks with values,
         number formatting, fonts, fills, borders, alignment, column widths, row heights and merged cells
  boxes  scanned PDF pages and images -> boxes/p{n}.json word positions (Tesseract), used only to place highlights
All outputs are plain JSON (no gzip) so the browser can read them through short-lived links.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from convert import Permanent, SHEET_EXT, SLIDE_EXT, WORD_EXT, IMAGE_EXT, decode_text, soffice_convert

log = logging.getLogger("render")

CHUNK_ROWS = 500
MAX_ROWS = int(os.environ.get("MAX_GRID_ROWS", "100000"))
MAX_COLS = 200
STYLED_CELL_LIMIT = int(os.environ.get("STYLED_CELL_LIMIT", "400000"))
OCR_DPI = 200
SCANNED_PAGE_CHARS = 40
MAX_OCR_PAGES = int(os.environ.get("MAX_BOX_PAGES", "1500"))

LANGS = {"en": "eng", "hi": "hin", "mr": "mar", "bn": "ben", "gu": "guj", "pa": "pan", "ta": "tam", "te": "tel", "kn": "kan", "ml": "mal", "ur": "urd", "or": "ori", "as": "asm", "ne": "nep", "sa": "san"}


@dataclass
class Rendered:
    uploads: dict[str, Path] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)


def mode_for(ext: str, kind: str) -> str | None:
    if ext in WORD_EXT or ext in SLIDE_EXT:
        return "pdf"
    if ext in SHEET_EXT:
        return "grid"
    if ext == "pdf" or ext in IMAGE_EXT or kind in ("pdf", "image"):
        return "boxes"
    return None


def render(path: Path, doc: dict, mode: str, work: Path) -> Rendered:
    ext = (doc.get("ext") or path.suffix.lstrip(".")).lower()
    if mode == "pdf":
        return render_pdf(path, ext, work)
    if mode == "grid":
        return render_grid(path, ext, work)
    if mode == "boxes":
        return render_boxes(path, ext, work, doc.get("language") or "")
    raise Permanent(f"Unknown render mode {mode}")


# --------------------------------------------------------------------------- view.pdf

def render_pdf(path: Path, ext: str, work: Path) -> Rendered:
    pdf = soffice_convert(path, work, "pdf")
    size = pdf.stat().st_size
    if size < 200:
        raise Permanent("The document could not be laid out as pages.")
    try:
        import pypdfium2 as pdfium
        pages = len(pdfium.PdfDocument(str(pdf)))
    except Exception:
        pages = 0
    return Rendered(uploads={"view.pdf": pdf}, summary={"pdf": True, "pdfBytes": size, "pdfPages": pages})


# --------------------------------------------------------------------------- spreadsheets

class Styles:
    def __init__(self):
        self.list: list[dict] = [{}]
        self.index: dict[str, int] = {"{}": 0}

    def add(self, s: dict) -> int:
        if not s:
            return 0
        key = json.dumps(s, sort_keys=True)
        k = self.index.get(key)
        if k is None:
            k = len(self.list)
            self.list.append(s)
            self.index[key] = k
        return k


THEME: list[str] = []


def load_theme(wb) -> list[str]:
    """Theme palette in Excel's index order: lt1, dk1, lt2, dk2, accent1-6, hlink, folHlink."""
    xml = getattr(wb, "loaded_theme", None)
    if not xml:
        return []
    if isinstance(xml, bytes):
        xml = xml.decode("utf8", "ignore")
    scheme = re.search(r"<a:clrScheme[^>]*>(.*?)</a:clrScheme>", xml, re.S)
    if not scheme:
        return []
    found = {}
    for name in ("dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"):
        m = re.search(rf"<a:{name}>(.*?)</a:{name}>", scheme.group(1), re.S)
        if m:
            v = re.search(r'srgbClr val="([0-9A-Fa-f]{6})"', m.group(1)) or re.search(r'lastClr="([0-9A-Fa-f]{6})"', m.group(1))
            found[name] = v.group(1).upper() if v else None
    order = ["lt1", "dk1", "lt2", "dk2", "accent1", "accent2", "accent3", "accent4", "accent5", "accent6", "hlink", "folHlink"]
    return [found.get(k) or ("FFFFFF" if k.startswith("lt") else "000000") for k in order]


def _tint(rgb: str, tint: float) -> str:
    import colorsys
    r, g, b = (int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = l * (1 + tint) if tint < 0 else l * (1 - tint) + tint
    r, g, b = colorsys.hls_to_rgb(h, max(0, min(1, l)), s)
    return "".join(f"{round(x * 255):02X}" for x in (r, g, b))


def _hex(color) -> str | None:
    try:
        if color is None:
            return None
        rgb = None
        if color.type == "rgb" and isinstance(color.rgb, str):
            if color.rgb.upper() == "00000000":
                return None
            rgb = color.rgb[-6:]
        elif color.type == "theme" and THEME and isinstance(color.theme, int) and 0 <= color.theme < len(THEME):
            rgb = THEME[color.theme]
        elif color.type == "indexed" and isinstance(color.indexed, int):
            from openpyxl.styles.colors import COLOR_INDEX
            if color.indexed in (64, 65):
                return None
            if 0 <= color.indexed < len(COLOR_INDEX):
                rgb = COLOR_INDEX[color.indexed][-6:]
        if not rgb or not re.fullmatch(r"[0-9A-Fa-f]{6}", rgb):
            return None
        tint = float(getattr(color, "tint", 0) or 0)
        if tint:
            rgb = _tint(rgb, tint)
        return "#" + rgb.upper()
    except Exception:
        return None


def _style(cell) -> dict:
    s: dict = {}
    f = cell.font
    if f is not None:
        if f.b:
            s["b"] = 1
        if f.i:
            s["i"] = 1
        if f.u and f.u != "none":
            s["u"] = 1
        if f.strike:
            s["s"] = 1
        if f.sz and abs(float(f.sz) - 11) > 0.1:
            s["fs"] = round(float(f.sz), 1)
        c = _hex(f.color)
        if c and c != "#000000":
            s["fc"] = c
    fill = cell.fill
    if fill is not None and fill.fill_type == "solid":
        c = _hex(fill.fgColor) or _hex(fill.start_color)
        if c and c != "#FFFFFF":
            s["bg"] = c
    a = cell.alignment
    if a is not None:
        if a.horizontal in ("center", "right", "left", "justify", "centerContinuous"):
            s["ha"] = "center" if a.horizontal == "centerContinuous" else a.horizontal
        if a.vertical in ("top", "center"):
            s["va"] = a.vertical
        if a.wrap_text:
            s["wr"] = 1
        if a.indent:
            s["in"] = int(a.indent)
    b = cell.border
    if b is not None:
        for side, key in ((b.top, "bt"), (b.bottom, "bb"), (b.left, "bl"), (b.right, "br")):
            if side is not None and side.style:
                s[key] = (_hex(side.color) or "#000000") + ("2" if side.style in ("medium", "thick", "double") else "")
    return s


def _excel_num(v: float, fmt: str) -> str:
    fmt = (fmt or "General").split(";")[0]
    if fmt in ("General", "@"):
        if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return f"{v:.10g}"
    pct = "%" in fmt
    if pct:
        v = v * 100
    dec = 0
    m = re.search(r"0\.(0+)", fmt)
    if m:
        dec = len(m.group(1))
    grouped = "#,##" in fmt or "#,#" in fmt
    indian = "##,##,##" in fmt
    s = f"{abs(v):,.{dec}f}" if grouped else f"{abs(v):.{dec}f}"
    if indian:
        whole, _, frac = f"{abs(v):.{dec}f}".partition(".")
        if len(whole) > 3:
            head, tail = whole[:-3], whole[-3:]
            head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
            whole = f"{head},{tail}"
        s = whole + (f".{frac}" if frac else "")
    sym = ""
    for c in ("₹", "$", "€", "£", "Rs"):
        if c in fmt:
            sym = c + (" " if c == "Rs" else "")
            break
    out = f"{sym}{s}{'%' if pct else ''}"
    if v < 0:
        out = f"({out})" if "(" in fmt else f"-{out}"
    return out


def _excel_date(v, fmt: str) -> str:
    f = (fmt or "").lower()
    if "mmm" in f:
        out = v.strftime("%d-%b-%Y" if "yy" in f else "%d-%b")
    elif "yyyy-mm-dd" in f or "yyyy/mm/dd" in f:
        out = v.strftime("%Y-%m-%d")
    elif f.startswith("m/") or "mm/dd" in f:
        out = v.strftime("%m/%d/%Y")
    else:
        out = v.strftime("%d-%m-%Y")
    if isinstance(v, dt.datetime) and ("h:" in f or "hh" in f):
        out += v.strftime(" %H:%M")
    return out


def _display(cell) -> str:
    v = cell.value
    if v is None:
        return ""
    fmt = getattr(cell, "number_format", "General") or "General"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (dt.datetime, dt.date)):
        return _excel_date(v, fmt)
    if isinstance(v, dt.time):
        return v.strftime("%H:%M")
    if isinstance(v, (int, float)):
        try:
            return _excel_num(float(v), fmt)
        except Exception:
            return str(v)
    return str(v)


def _px_width(w) -> int:
    return int(round((w or 8.43) * 7 + 5))


def render_grid(path: Path, ext: str, work: Path) -> Rendered:
    out = Rendered()
    gdir = work / "grid"
    gdir.mkdir(exist_ok=True)
    styles = Styles()
    sheets: list[dict] = []

    def write_chunk(si: int, k: int, rows: list):
        p = gdir / f"s{si}c{k}.json"
        p.write_text(json.dumps({"v": 1, "rows": rows}, ensure_ascii=False, separators=(",", ":")), encoding="utf8")
        out.uploads[f"grid/s{si}c{k}.json"] = p

    if ext in ("csv", "tsv"):
        text = decode_text(path)
        sample = text[:4096]
        delim = "\t" if ext == "tsv" else (csv.Sniffer().sniff(sample, delimiters=",;|\t").delimiter if sample.strip() else ",")
        rows_iter = csv.reader(io.StringIO(text), delimiter=delim)
        buf, k, nrows, ncols = [], 0, 0, 0
        for r, row in enumerate(rows_iter):
            if r >= MAX_ROWS:
                break
            cells = [[c, v.strip(), 1 if r == 0 else 0] for c, v in enumerate(row[:MAX_COLS]) if v.strip()]
            ncols = max(ncols, min(len(row), MAX_COLS))
            nrows = r + 1
            if cells:
                buf.append([r, 0, cells])
            if nrows % CHUNK_ROWS == 0:
                write_chunk(0, k, buf); buf, k = [], k + 1
        if buf or k == 0:
            write_chunk(0, k, buf); k += 1
        styles.add({"b": 1, "bg": "#F3F4F6"})
        sheets.append({"name": path.stem[:60] or "Sheet1", "rows": nrows, "cols": ncols, "widths": [], "heights": {}, "merges": [], "freeze": [1, 0], "chunks": k})
    else:
        src = path
        if ext not in ("xlsx", "xlsm"):
            src = soffice_convert(path, work, "xlsx")
        import openpyxl
        try:
            probe = openpyxl.load_workbook(src, read_only=True, data_only=True)
        except Exception as e:
            raise Permanent(f"This spreadsheet could not be opened ({type(e).__name__}).")
        cells_total = 0
        for ws in probe.worksheets:
            try:
                cells_total += (ws.max_row or 0) * min(ws.max_column or 0, MAX_COLS)
            except Exception:
                pass
        probe.close()
        styled = cells_total <= STYLED_CELL_LIMIT
        wb = openpyxl.load_workbook(src, read_only=not styled, data_only=True)
        THEME[:] = load_theme(wb) if styled else []
        fwb = openpyxl.load_workbook(src, read_only=True, data_only=False) if styled else None
        for si, ws in enumerate(wb.worksheets):
            if si >= 60:
                break
            nrows = min(ws.max_row or 0, MAX_ROWS)
            ncols = min(ws.max_column or 0, MAX_COLS)
            info: dict = {"name": ws.title[:80], "rows": nrows, "cols": ncols, "widths": [], "heights": {}, "merges": [], "freeze": [0, 0], "chunks": 0}
            if styled:
                info["hidden"] = ws.sheet_state != "visible"
                from openpyxl.utils import get_column_letter, column_index_from_string
                widths = []
                for c in range(1, ncols + 1):
                    d = ws.column_dimensions.get(get_column_letter(c))
                    widths.append(0 if (d is not None and d.hidden) else _px_width(d.width if d is not None and d.width else None))
                info["widths"] = widths
                info["heights"] = {str(r - 1): int(round(d.height * 4 / 3)) for r, d in ws.row_dimensions.items() if d.height and r <= nrows}
                info["merges"] = [[m.min_row - 1, m.min_col - 1, m.max_row - 1, m.max_col - 1] for m in ws.merged_cells.ranges if m.min_row <= nrows][:5000]
                if ws.freeze_panes:
                    try:
                        letters = re.match(r"([A-Z]+)(\d+)", ws.freeze_panes)
                        if letters:
                            info["freeze"] = [int(letters.group(2)) - 1, column_index_from_string(letters.group(1)) - 1]
                    except Exception:
                        pass
            formulas = {}
            if fwb is not None:
                fws = fwb.worksheets[si]
                for r, row in enumerate(fws.iter_rows(min_row=1, max_row=nrows, max_col=ncols)):
                    for c, cell in enumerate(row):
                        if isinstance(cell.value, str) and cell.value.startswith("="):
                            formulas[(r, c)] = cell.value[:500]
            buf, k = [], 0
            for r, row in enumerate(ws.iter_rows(min_row=1, max_row=nrows, max_col=ncols)):
                cells = []
                for c, cell in enumerate(row):
                    if cell is None or getattr(cell, "value", None) is None and not styled:
                        continue
                    text = _display(cell)
                    st = styles.add(_style(cell)) if styled else 0
                    f = formulas.get((r, c))
                    if not text and not st and not f:
                        continue
                    num = isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool)
                    item: list = [c, text, st]
                    if num or f:
                        item.append(1 if num else 0)
                    if f:
                        item.append(f)
                    cells.append(item)
                if cells:
                    buf.append([r, 0, cells])
                if (r + 1) % CHUNK_ROWS == 0:
                    write_chunk(si, k, buf); buf, k = [], k + 1
            if buf or k == 0 or nrows % CHUNK_ROWS:
                write_chunk(si, k, buf); k += 1
            info["chunks"] = k
            sheets.append(info)
        wb.close()
        if fwb is not None:
            fwb.close()
    meta = {"v": 1, "chunk": CHUNK_ROWS, "sheets": sheets, "styles": styles.list}
    mp = gdir / "meta.json"
    mp.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf8")
    out.uploads["grid/meta.json"] = mp
    out.summary = {"grid": True, "sheets": len(sheets)}
    return out


# --------------------------------------------------------------------------- word boxes for scans

def _tess_lang(language: str) -> str:
    code = (language or "").lower().split("-")[0]
    extra = LANGS.get(code)
    langs = ["eng"] + ([extra] if extra and extra != "eng" else ["hin"])
    return "+".join(dict.fromkeys(langs))


def _ocr_words(img, lang: str) -> dict:
    import pytesseract
    w, h = img.size
    data = pytesseract.image_to_data(img, lang=lang, config="--psm 3", output_type=pytesseract.Output.DICT)
    words = []
    for i, t in enumerate(data["text"]):
        t = (t or "").strip()
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1
        if not t or conf < 0:
            continue
        words.append([round(data["left"][i] / w, 4), round(data["top"][i] / h, 4), round(data["width"][i] / w, 4), round(data["height"][i] / h, 4), t])
    return {"v": 1, "w": w, "h": h, "words": words}


def render_boxes(path: Path, ext: str, work: Path, language: str) -> Rendered:
    from PIL import Image, ImageOps
    lang = _tess_lang(language)
    bdir = work / "boxes"
    bdir.mkdir(exist_ok=True)
    out = Rendered()
    done: list[int] = []

    def save(n: int, img):
        img = img.convert("L")
        if max(img.size) > 3400:
            img.thumbnail((3400, 3400))
        res = _ocr_words(img, lang)
        p = bdir / f"p{n}.json"
        p.write_text(json.dumps(res, ensure_ascii=False, separators=(",", ":")), encoding="utf8")
        out.uploads[f"boxes/p{n}.json"] = p
        done.append(n)

    if ext == "pdf":
        import pypdfium2 as pdfium
        try:
            pdf = pdfium.PdfDocument(str(path))
        except Exception:
            raise Permanent("This PDF could not be opened for highlighting.")
        for i in range(len(pdf)):
            if len(done) >= MAX_OCR_PAGES:
                break
            page = pdf[i]
            tp = page.get_textpage()
            visible = len(re.sub(r"\s", "", tp.get_text_bounded() or ""))
            tp.close()
            if visible < SCANNED_PAGE_CHARS:
                bitmap = page.render(scale=OCR_DPI / 72)
                save(i + 1, bitmap.to_pil())
            page.close()
        pdf.close()
    else:
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        try:
            img = Image.open(path)
        except Exception:
            raise Permanent("This image could not be opened for highlighting.")
        frames = getattr(img, "n_frames", 1)
        pages = []
        for i in range(min(frames, MAX_OCR_PAGES)):
            img.seek(i)
            frame = ImageOps.exif_transpose(img.copy()).convert("RGB")
            save(i + 1, frame)
            pages.append(frame)
        # Formats a browser can't show (TIFF, HEIC, BMP, multi-page scans) also get a PDF copy to display.
        if ext not in ("jpg", "jpeg", "png", "webp", "gif") and pages:
            view = work / "view.pdf"
            for p in pages:
                p.thumbnail((2400, 2400))
            pages[0].save(view, save_all=True, append_images=pages[1:], resolution=150)
            out.uploads["view.pdf"] = view
            out.summary.update({"pdf": True, "pdfBytes": view.stat().st_size, "pdfPages": len(pages)})
    out.summary["boxes"] = done
    return out
