"""
Daily releases: new Supreme Court and High Court judgments from the public court-judgment release, added to JuneDesk search as one
small segment per run (n001, n002, ...). Run once a day by worker.py, or by hand: `python fresh.py` (FRESH_DRY=1 to only count).

Steps: find release metadata files changed since the last run -> rows not already in the library (same CNR and decision date) ->
fetch each PDF (single file, or out of the release folder's tar parts) -> text with pdfium -> passages, catalog rows, relation rows,
postings, CNR/case-number lookups -> upload -> ask JuneDesk to publish (it checks the files against the summary before the
segment becomes searchable) -> update the de-duplication lists and state. JuneDesk is reached only through
/api/public/worker/fresh with the worker key; uploads and downloads use short-lived signed links.
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

import requests

log = logging.getLogger("fresh")
URL = os.environ.get("JUNEDESK_URL", "").rstrip("/")
KEY = os.environ.get("JUNEDESK_WORKER_KEY", "")
HC = "https://indian-high-court-judgments.s3.ap-south-1.amazonaws.com/"
SC = "https://indian-supreme-court-judgments.s3.amazonaws.com/"
FRESH_FROM = 19331 * 1024
MAX_DOCS = int(os.environ.get("FRESH_MAX_DOCS", "20000"))
PROCS = int(os.environ.get("FRESH_PROCS", "2"))
PACK = 32 << 20
TARGET, HARD = 3500, 5000
ZX_TEXT = "zxtext"
STOP = set("the of and to in that is by for be on as was it with this or has are which not an at from have been he his no any had were under such said also shall may they there would its their we so if all who but into other being than upon these them her she him our can will did do does only after before thereof herein whether where when what same should could very about because between through then those while each both more most some".split())
http = requests.Session()
http.mount("https://", requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=2))


# ---------- JuneDesk API ----------
def api(payload: dict, timeout: int = 180) -> dict:
    for a in range(5):
        try:
            r = http.post(f"{URL}/api/public/worker/fresh", json=payload, headers={"Authorization": f"Bearer {KEY}"}, timeout=timeout)
            if r.status_code in (400, 401, 403, 409):
                raise RuntimeError(f"JuneDesk refused ({r.status_code}): {r.text[:300]}")
            r.raise_for_status()
            return r.json()
        except RuntimeError:
            raise
        except Exception as e:
            if a == 4:
                raise
            log.warning("fresh api retry: %s", e)
            time.sleep(5 * (a + 1))
    return {}


def unpack(b: bytes) -> bytes:
    return gzip.decompress(b) if b[:2] == b"\x1f\x8b" else b


def read_many(paths: list[str]) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for s in range(0, len(paths), 500):
        part = paths[s:s + 500]
        urls = api({"op": "read", "paths": part})["urls"]
        def get(u):
            if not u:
                return None
            for a in range(4):
                try:
                    r = http.get(u, timeout=300)
                    if r.status_code in (400, 404):
                        return None
                    r.raise_for_status()
                    return unpack(r.content)
                except Exception:
                    time.sleep(3 * (a + 1))
            return None
        with ThreadPoolExecutor(12) as ex:
            for p, b in zip(part, ex.map(get, urls)):
                out[p] = b
    return out


def read_json(path: str):
    b = read_many([path]).get(path)
    return json.loads(b) if b else None


def upload_many(files: dict[str, bytes]):
    items = list(files.items())
    for s in range(0, len(items), 500):
        part = items[s:s + 500]
        urls = api({"op": "sign", "paths": [p for p, _ in part]})["urls"]
        def put(pu):
            (path, data), url = pu
            for a in range(6):
                try:
                    r = http.put(url, data=data, headers={"Content-Type": "application/octet-stream", "x-upsert": "true", "cache-control": "max-age=0"}, timeout=600)
                    if r.status_code < 300:
                        return
                    raise RuntimeError(f"{r.status_code} {r.text[:120]}")
                except Exception as e:
                    if a == 5:
                        raise RuntimeError(f"upload failed {path}: {e}")
                    time.sleep(4 * (a + 1))
        with ThreadPoolExecutor(12) as ex:
            list(ex.map(put, zip(part, urls)))


def gz_json(v) -> bytes:
    return gzip.compress(json.dumps(v, ensure_ascii=False, separators=(",", ":")).encode(), 6)


# ---------- release discovery ----------
def s3_list(base: str, prefix: str) -> list[tuple[str, str]]:
    ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
    out, token = [], None
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        r = http.get(base, params=params, timeout=60)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for c in root.findall("s:Contents", ns):
            out.append((c.find("s:Key", ns).text, c.find("s:LastModified", ns).text))
        t = root.find("s:NextContinuationToken", ns)
        if t is None:
            return out
        token = t.text


def changed_files(state: dict, years: list[int]) -> list[tuple[str, str, str]]:
    """(base, key, last modified) of release metadata files newer than the stored watermark."""
    wm = state.get("watermarks", {})
    found = []
    for y in years:
        for key, lm in s3_list(HC, f"metadata/parquet/year={y}/"):
            # metadata-mobile.parquet holds interim orders from the court app; the library carries final orders/judgments.
            if key.endswith("/metadata.parquet") and lm > wm.get(key, ""):
                found.append((HC, key, lm))
        for key, lm in s3_list(SC, f"metadata/parquet/year={y}/"):
            if key.endswith("metadata.parquet") and lm > wm.get("sc:" + key, ""):
                found.append((SC, key, lm))
    return found


NUMRE = re.compile(r"^(.*?)(\d{1,7})\s*(?:of|/|-|\s|,)\s*((?:18|19|20)\d{2})(?!\d)")
TITLE_NUM = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 .()&\-]{0,30}?)\s*/\s*(\d{1,7})\s*/\s*((?:18|19|20)\d{2})(?!\d)")


def numkey(s):
    if not s:
        return None
    m = NUMRE.search(s.lower())
    if not m:
        return None
    t = re.sub(r"(?:nos|no|number)$", "", re.sub(r"[^a-z]", "", m.group(1)))
    if not t or len(t) > 24:
        return None
    return f"{t}:{int(m.group(2))}:{m.group(3)}"


def titlekey(s):
    m = TITLE_NUM.match(s or "")
    return numkey(f"{m.group(1)} {m.group(2)}/{m.group(3)}") if m else None


def cnrkey(s):
    """CNR: 16 characters, a 4-letter state/district code, then establishment code and number, ending in the filing year
    (Calcutta's establishment codes are letters, e.g. WBCHCA0000701967)."""
    k = re.sub(r"[\s-]", "", (s or "").upper())
    return k if re.match(r"^[A-Z]{4}[A-Z0-9]{8}(?:19|20)\d\d$", k) else None


# ---------- de-duplication ----------
# A release row is already in the library when any of its keys is: same CNR + decision date, same case number + decision date,
# or the same neutral citation. Keys are kept as 64-bit hashes with their decision day (days since 2000-01-01), sorted, per court,
# in search/v2/fresh/x/{court}.bin (gzip: uint32 count, count x uint64 hash, count x uint16 day). Only recent years are kept.
EPOCH = dt.date(2000, 1, 1)
KEEP_YEARS = 2  # keys of decisions from Jan 1 of (this year - KEEP_YEARS) on


def day_of(date: str) -> int:
    return max(0, min(65535, (dt.date.fromisoformat(date[:10]) - EPOCH).days))


def cutoff_day(now: dt.datetime | None = None) -> int:
    y = (now or dt.datetime.now(dt.timezone.utc)).year - KEEP_YEARS
    return (dt.date(y, 1, 1) - EPOCH).days


def dedup_keys(cnr, date, num, cit, raw_title=None) -> list[str]:
    ks = []
    c = cnrkey(cnr)
    if c and date:
        ks.append(f"{c}|{date[:10]}")
    for n in (numkey(num), titlekey(raw_title)):
        if n and date:
            ks.append(f"#{n}|{date[:10]}")
    k = re.sub(r"[^a-z0-9]", "", (cit or "").lower())
    if re.match(r"^(?:19|20)\d\dinsc\d+$", k):
        ks.append(f"@{k}")
    return list(dict.fromkeys(ks))


def near_days(keys: list[str]) -> list[str]:
    out = []
    for k in keys:
        out.append(k)
        m = re.match(r"^(.*\|)(\d{4}-\d\d-\d\d)$", k)
        if m:
            d = dt.date.fromisoformat(m.group(2))
            out += [m.group(1) + (d + dt.timedelta(days=x)).isoformat() for x in (-1, 1)]
    return out


def h64(k: str) -> int:
    return int.from_bytes(hashlib.md5(k.encode()).digest()[:8], "little")


class Seen:
    """One court's known keys: a sorted hash array from storage plus keys added during this run."""
    def __init__(self, blob: bytes | None = None):
        import numpy as np
        self.np = np
        if blob:
            b = unpack(blob)
            n = int.from_bytes(b[:4], "little")
            self.h = np.frombuffer(b, dtype="<u8", count=n, offset=4)
            self.d = np.frombuffer(b, dtype="<u2", count=n, offset=4 + 8 * n)
        else:
            self.h = np.zeros(0, dtype="<u8")
            self.d = np.zeros(0, dtype="<u2")
        self.added: dict[int, int] = {}

    def has(self, keys: list[str]) -> bool:
        # Dates of the same decision differ by a day between some older copies and the release, so a key also matches the
        # day before and after.
        for k in near_days(keys):
            v = h64(k)
            if v in self.added:
                return True
            i = int(self.np.searchsorted(self.h, v))
            if i < len(self.h) and int(self.h[i]) == v:
                return True
        return False

    def add(self, keys: list[str], day: int):
        for k in keys:
            self.added[h64(k)] = day

    def discard(self, keys: list[str]):
        for k in keys:
            self.added.pop(h64(k), None)

    def dump(self, cut: int) -> bytes:
        np = self.np
        h = np.concatenate([self.h, np.fromiter(self.added.keys(), dtype="<u8", count=len(self.added))])
        d = np.concatenate([self.d, np.fromiter(self.added.values(), dtype="<u2", count=len(self.added))])
        keep = d >= cut
        h, d = h[keep], d[keep]
        h, idx = np.unique(h, return_index=True)
        d = d[idx]
        return gzip.compress(len(h).to_bytes(4, "little") + h.astype("<u8").tobytes() + d.astype("<u2").tobytes(), 6)


def clean_names(raw: str) -> list[str]:
    out = []
    raw = re.sub(r"(?i)hon'?\s*ble|honourable|honorable|\bthe\b|\bmr\.?|\bmrs\.?|\bms\.?|\bdr\.?|\bsmt\.?|\bshri\b|justice|chief|acting", " ", raw or "")
    for part in re.split(r"\s*(?:,|&|;|\band\b|\n)\s*", raw):
        n = re.sub(r"[^A-Za-z. ]+", " ", part)
        n = re.sub(r"\s+", " ", n).strip(" .")
        if 4 <= len(n) <= 40 and 1 <= len(n.split()) <= 5:
            out.append(n.upper())
    return list(dict.fromkeys(out))[:7]


def iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (dt.date, dt.datetime)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)", s)
    if m:
        return m.group(0)
    m = re.match(r"^(\d\d)-(\d\d)-(\d{4})$", s)
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def release_rows(base: str, key: str, courts: dict) -> list[dict]:
    import pyarrow.parquet as pq
    r = http.get(base + key, timeout=300)
    r.raise_for_status()
    t = pq.read_table(io.BytesIO(r.content)).to_pylist()
    rows = []
    if base == HC:
        d = key[len("metadata/parquet/"):].rsplit("/", 1)[0]
        code = re.search(r"court=([0-9_]+)", d).group(1)
        court = courts.get(code)
        if not court:
            log.warning("fresh: unknown release court %s", code)
            return []
        for x in t:
            link = (x.get("pdf_link") or "").rsplit("/", 1)[-1]
            cnr, date = cnrkey(x.get("cnr")), iso(x.get("decision_date"))
            if not link.endswith(".pdf") or not cnr or not date:
                continue
            raw_title = re.sub(r"\s+", " ", x.get("title") or "").strip()
            m = re.match(r"^(.+?/\d{1,7}/\d{4})\s+of\s+(.*)$", raw_title)
            pet, res = (x.get("petitioner") or "").strip(), (x.get("respondent") or "").strip()
            title = f"{pet} v. {res}" if pet and res else (m.group(2) if m else raw_title)
            num = m.group(1) if m else (f"{x.get('case_type')}/{x.get('case_no')}" if x.get("case_type") and x.get("case_no") else None)
            rows.append({"court": court, "cnr": cnr, "date": date, "title": re.sub(r"\s+vs\.?\s+", " v. ", title, flags=re.I)[:300], "num": num, "cit": None,
                         "judges": clean_names(x.get("judge") or ""), "bench": None, "dir": d, "file": link, "base": HC, "raw_title": raw_title})
    else:
        y = re.search(r"year=(\d{4})", key).group(1)
        for x in t:
            cnr, date, path = cnrkey(x.get("cnr")), iso(x.get("decision_date")), (x.get("path") or "").strip()
            if not cnr or not date or not path:
                continue
            html = x.get("raw_html") or ""
            m = re.search(r"Case No\s*:\s*</span><font[^>]*>\s*([^<]+)", html)
            title = re.sub(r"\s+versus\s+", " v. ", re.sub(r"\s+", " ", x.get("title") or "").strip(), flags=re.I)
            rows.append({"court": "sci", "cnr": cnr, "date": date, "title": title[:300], "num": (m.group(1).strip()[:120] if m else None),
                         "cit": (x.get("case_id") or x.get("citation") or None), "judges": clean_names(x.get("judge") or ""), "bench": None,
                         "dir": f"year={y}/english", "file": f"{path}_EN.pdf", "base": SC, "raw_title": title})
    return rows


# ---------- PDFs and text ----------
def fetch_pdf(url: str) -> bytes | None:
    for a in range(3):
        try:
            r = http.get(url, timeout=90)
            if r.status_code in (403, 404):
                return None
            r.raise_for_status()
            return r.content if r.content[:4] == b"%PDF" else None
        except Exception:
            time.sleep(2 * (a + 1))
    return None


def tar_members(base: str, d: str, files: set[str]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    try:
        idx = http.get(f"{base}data/tar/{d}/data.index.json", timeout=90)
        parts = idx.json().get("parts", []) if idx.ok else []
    except Exception:
        parts = []
    for part in parts:
        need = files & set(part.get("files", []))
        if not need:
            continue
        try:
            with http.get(f"{base}data/tar/{d}/{part['name']}", stream=True, timeout=900) as r:
                r.raise_for_status()
                r.raw.decode_content = True
                with tarfile.open(fileobj=r.raw, mode="r|") as tf:
                    for m in tf:
                        name = m.name.rsplit("/", 1)[-1]
                        if name in need and m.isfile():
                            b = tf.extractfile(m).read()
                            if b[:4] == b"%PDF":
                                out[name] = b
                                need.discard(name)
                        if not need:
                            break
        except Exception as e:
            log.warning("fresh: tar part %s/%s failed: %s", d, part.get("name"), e)
    return out


def clean(t: str) -> str:
    t = t.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def extract(blob: bytes):
    import pypdfium2 as pdfium
    try:
        doc = pdfium.PdfDocument(blob)
        pages = []
        for k in range(min(len(doc), 400)):
            p = doc[k]
            tp = p.get_textpage()
            pages.append(clean(tp.get_text_range()))
            tp.close()
            p.close()
        doc.close()
        n = sum(len(p) for p in pages)
        return pages if n >= 200 and n >= 40 * max(1, len(pages)) else []
    except Exception:
        return None


def passages(pages: list[str]) -> list[list]:
    out, buf, st = [], [], {"size": 0, "a": None, "b": None}
    def push():
        if buf:
            out.append([len(out), st["a"], st["b"], "\n\n".join(buf)])
        buf.clear()
        st.update(size=0, a=None, b=None)
    for pno, txt in enumerate(pages, 1):
        for para in re.split(r"\n\s*\n|\n(?=\s*\d{1,3}[.)]\s)", txt):
            para = para.strip()
            while para:
                piece = para
                if len(piece) > HARD:
                    cut = piece.rfind(" ", TARGET // 2, TARGET)
                    cut = cut if cut > 0 else TARGET
                    piece, para = piece[:cut].rstrip(), piece[cut:].lstrip()
                else:
                    para = ""
                if buf and st["size"] + len(piece) > TARGET:
                    push()
                if st["a"] is None:
                    st["a"] = pno
                buf.append(piece)
                st["size"] += len(piece) + 2
                st["b"] = pno
    push()
    return out


def words(s: str):
    for t in re.split(r"[^a-z0-9]+", (s or "").lower()):
        if 1 <= len(t) <= 24 and (len(t) > 1 or t.isdigit()) and t not in STOP:
            yield t


def varint_deltas(ids: list[int]) -> bytes:
    out, prev = bytearray(), 0
    for i in ids:
        v = i - prev
        prev = i
        while v >= 128:
            out.append((v & 127) | 128)
            v >>= 7
        out.append(v)
    return bytes(out)


def prefetch(items, fn, ahead: int = 2):
    """Yields (item, fn(item)) in order while reading up to `ahead` items in advance (bounded memory)."""
    with ThreadPoolExecutor(ahead) as ex:
        futs = [ex.submit(fn, it) for it in items[:ahead]]
        for k, it in enumerate(items):
            res = futs[k].result()
            if k + ahead < len(items):
                futs.append(ex.submit(fn, items[k + ahead]))
            futs[k] = None
            yield it, res


# ---------- one run ----------
def run(dry: bool = False) -> dict:
    t0 = time.time()
    state = read_json("search/v2/fresh/state.json")
    man = read_json("search/v2/manifest.json")
    if not state or not man or state.get("version") != 2:
        raise RuntimeError("fresh: state or manifest unavailable, or seeded with an older format (run fresh_seed.py)")
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    years = sorted({now.year, now.year - 1}, reverse=True)
    files = changed_files(state, years)
    log.info("fresh: %d release files changed", len(files))
    seen: dict[str, Seen] = {}
    cut = cutoff_day(now)
    def seen_for(court) -> Seen:
        if court not in seen:
            seen[court] = Seen(read_many([f"search/v2/fresh/x/{court}.bin"]).get(f"search/v2/fresh/x/{court}.bin"))
        return seen[court]
    # A run that published but stopped before its bookkeeping: take that segment's judgments into the lists now (no duplicates).
    recorded = set(state.get("published", []))
    dirty: set[str] = set()
    for sg, (lo, cnt) in (man.get("freshBlocks") or {}).items():
        if sg in recorded:
            continue
        shards = read_many([f"search/v2/meta/m{k}.bin" for k in range(lo >> 10, ((lo + cnt - 1) >> 10) + 1)])
        for b in shards.values():
            for i, e in (json.loads(b) if b else {}).items():
                if lo <= int(i) < lo + cnt and e.get("date"):
                    seen_for(e["court"]).add(dedup_keys(e.get("cnr"), e["date"], e.get("num"), e.get("cit"), e.get("title")), day_of(e["date"]))
                    dirty.add(e["court"])
        state.setdefault("published", []).append(sg)
        log.info("fresh: recovered bookkeeping for %s", sg)
    picked, done_keys, cand_total = [], [], 0
    for (base, key, lm), rows in prefetch(files, lambda f: release_rows(f[0], f[1], state.get("courts", {}))):
        new = []
        for r in rows:
            r["keys"] = dedup_keys(r["cnr"], r["date"], r["num"], r["cit"], r["raw_title"])
            s = seen_for(r["court"])
            if not r["keys"] or s.has(r["keys"]):
                continue
            s.add(r["keys"], day_of(r["date"]))
            new.append(r)
        cand_total += len(new)
        log.info("fresh: %s: %d rows, %d new", key.replace("metadata/parquet/", ""), len(rows), len(new))
        if len(picked) + len(new) > MAX_DOCS:
            # Over this run's budget: take the newest that fit; the file is read again next run (its watermark stays) for the rest.
            new.sort(key=lambda r: r["date"], reverse=True)
            room = max(0, MAX_DOCS - len(picked))
            for r in new[room:]:
                seen_for(r["court"]).discard(r["keys"])
            picked += new[:room]
            break
        picked += new
        done_keys.append(("sc:" + key if base == SC else key, lm))
    summary = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "files": len(files), "new": len(picked)}
    log.info("fresh: %d new judgments to add (%d release files fully covered)", len(picked), len(done_keys))
    if dry or not picked:
        if not dry:  # record the run even when nothing was new, so the clock waits a day before checking again
            if dirty:
                upload_many({f"search/v2/fresh/x/{c}.bin": seen[c].dump(cut) for c in dirty})
            state.setdefault("watermarks", {}).update(dict(done_keys))
            state["runs"] = (state.get("runs", []) + [summary])[-30:]
            upload_many({"search/v2/fresh/state.json": json.dumps(state).encode()})
        return summary
    # PDFs: single files first, then the folder's tar parts for the rest.
    by_dir: dict[tuple, list] = {}
    for r in picked:
        by_dir.setdefault((r["base"], r["dir"]), []).append(r)
    blobs: dict[int, bytes] = {}
    for (base, d), rs in by_dir.items():
        with ThreadPoolExecutor(16) as ex:
            got = list(ex.map(lambda r: fetch_pdf(f"{base}data/pdf/{d}/{r['file']}"), rs))
        miss = {}
        for r, b in zip(rs, got):
            if b:
                blobs[id(r)] = b
            else:
                miss[r["file"]] = r
        if miss:
            for name, b in tar_members(base, d, set(miss)).items():
                blobs[id(miss[name])] = b
    docs = [r for r in picked if id(r) in blobs]
    for r in picked:
        if id(r) not in blobs:  # retried when the release changes again
            seen_for(r["court"]).discard(r["keys"])
    if not docs:
        return {**summary, "new": 0}
    with ProcessPoolExecutor(PROCS) as ex:
        pages = list(ex.map(extract, [blobs[id(r)] for r in docs], chunksize=4))
    blobs.clear()
    # Newest first within the segment; ids start at the next free catalog shard.
    order = sorted(range(len(docs)), key=lambda i: ("".join(chr(255 - ord(c)) for c in docs[i]["date"]), docs[i]["court"], docs[i]["cnr"]))
    listed = [int(x[1:]) for x in man.get("segments", []) if re.match(r"^n\d{3}$", x)]
    seg = f"n{max(listed + [int(state.get('seg', 0))]) + 1:03d}"
    first = max(int(man.get("freshNext") or FRESH_FROM), FRESH_FROM)
    files_out: dict[str, bytes] = {}
    meta: dict[int, dict] = {}
    rel: dict[int, dict] = {}
    post: dict[str, list[int]] = {}
    cnr_k: dict[str, dict] = {}
    num_k: dict[str, dict] = {}
    pack, pk, chunks, with_text = bytearray(), 0, 0, 0
    courts: dict[str, int] = {}
    years: dict = {}
    for n, i in enumerate(order):
        r, pg, doc = docs[i], pages[i], first + n
        ps = passages(pg) if pg else []
        e = {"court": r["court"], "title": r["title"], "date": r["date"], "num": r["num"], "cnr": r["cnr"], "cit": r["cit"], "n": 0, "f": "", "o": 0, "l": 0, "s": ""}
        if ps:
            blob = gz_json(ps)
            if len(pack) + len(blob) > PACK:
                files_out[f"search/v2/text/{seg}p{pk}.bin"] = bytes(pack)
                pk += 1
                pack = bytearray()
            body = next((x[3] for x in ps if len(x[3]) > 200), ps[0][3])
            e.update(n=len(ps), f=f"{seg}p{pk}", o=len(pack), l=len(blob), s=" ".join(body.split())[:400])
            pack += blob
            chunks += len(ps)
            with_text += 1
        meta.setdefault(doc >> 10, {})[str(doc)] = e
        rel.setdefault(doc >> 10, {})[str(doc)] = {"p": f"{r['base']}data/pdf/{r['dir']}/{r['file']}", "nc": r["cit"] if r["court"] == "sci" else None, "rc": None,
                                                    "j": [[j, "zj" + re.sub(r"[^a-z]", "", j.lower())] for j in r["judges"]] or None, "c": None, "s": None, "b": None, "bt": None}
        toks = set(words(" ".join(filter(None, [r["title"], r["num"], r["cnr"], r["cit"], r["bench"]]))))
        for p in ps:
            toks.update(words(p[3]))
        toks.add("zc" + re.sub(r"[^a-z0-9]", "", r["court"].lower()))
        toks.update("zj" + re.sub(r"[^a-z]", "", j.lower()) for j in r["judges"])
        if ps:
            toks.add(ZX_TEXT)
        for t in toks:
            post.setdefault(t, []).append(doc)
        for pre, store, k in (("cnr", cnr_k, r["cnr"]), ("num", num_k, numkey(r["num"])), ("num", num_k, titlekey(r["raw_title"]))):
            if k:
                store.setdefault(hashlib.md5(k.encode()).hexdigest()[:2], {}).setdefault(k, [])
                if doc not in store[hashlib.md5(k.encode()).hexdigest()[:2]][k]:
                    store[hashlib.md5(k.encode()).hexdigest()[:2]][k].append(doc)
        courts[r["court"]] = courts.get(r["court"], 0) + 1
        y = int(r["date"][:4])
        lo, hi = years.get(y, (doc, doc))
        years[y] = (min(lo, doc), max(hi, doc))
    if pack:
        files_out[f"search/v2/text/{seg}p{pk}.bin"] = bytes(pack)
    upload_many(files_out)
    files_out = {f"search/v2/meta/m{k}.bin": gz_json(v) for k, v in meta.items()}
    files_out.update({f"search/v2/rel/r{k}.bin": gz_json(v) for k, v in rel.items()})
    upload_many(files_out)
    dicts: dict[str, dict] = {}
    posts: dict[str, bytearray] = {}
    for t in sorted(post):
        sub = hashlib.md5(t.encode()).hexdigest()[:2]
        b = varint_deltas(sorted(post[t]))
        pb = posts.setdefault(sub, bytearray())
        dicts.setdefault(sub, {})[t] = [len(pb), len(b), len(post[t])]
        pb += b
    files_out = {f"search/v2/post/{seg}{s}.bin": bytes(b) for s, b in posts.items()}
    files_out.update({f"search/v2/dict/{seg}{s}.bin": gz_json(d) for s, d in dicts.items()})
    files_out.update({f"search/v2/cnr/{seg}{s}.bin": gz_json(d) for s, d in cnr_k.items()})
    files_out.update({f"search/v2/num/{seg}{s}.bin": gz_json(d) for s, d in num_k.items()})
    upload_many(files_out)
    spans = sorted(years.items(), key=lambda kv: kv[1][0])
    res = api({"op": "publish", "seg": seg, "first": first, "docs": len(docs), "text": with_text, "chunks": chunks,
               "courts": sorted(courts.items(), key=lambda kv: -kv[1]), "years": [[y, lo, hi] for y, (lo, hi) in spans], "at": summary["at"]})
    log.info("fresh: published %s (%d judgments, %d with text, %d passages) -> %s", seg, len(docs), with_text, chunks, res)
    # Bookkeeping after the segment is live: de-duplication lists, watermarks, run log.
    touched = {r["court"] for r in docs} | dirty
    upload_many({f"search/v2/fresh/x/{c}.bin": seen[c].dump(cut) for c in touched})
    state["seg"] = int(seg[1:])
    state.setdefault("published", []).append(seg)
    state.setdefault("watermarks", {}).update(dict(done_keys))
    summary.update(new=len(docs), text=with_text, chunks=chunks, seg=seg, seconds=int(time.time() - t0))
    state["runs"] = (state.get("runs", []) + [summary])[-30:]
    upload_many({"search/v2/fresh/state.json": json.dumps(state).encode()})
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(json.dumps(run(dry=os.environ.get("FRESH_DRY") == "1")))
