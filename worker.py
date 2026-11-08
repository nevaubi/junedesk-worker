"""
JuneDesk processing worker.

Runs on Render (or any Docker host). It asks JuneDesk for files that are too large or too unusual for the app to read
in place, converts them (Docling layout + tables, LibreOffice for legacy Office files, ffmpeg for long recordings,
7-Zip for archives, readpst for Outlook mailboxes) and hands the results back. It never receives storage keys:
every download and upload goes through short-lived signed links issued by JuneDesk for one job at a time.

Environment:
  JUNEDESK_URL          e.g. https://case-cosmos-research-desk.lovable.app
  JUNEDESK_WORKER_KEY   the same value saved in JuneDesk as JUNEDESK_WORKER_KEY
  WORKER_CONCURRENCY    files processed at once (default 1; use 2 on 4 GB+ instances)
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from convert import Child, Output, Permanent, convert, sha256_file
from render import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")

URL = os.environ.get("JUNEDESK_URL", "").rstrip("/")
KEY = os.environ.get("JUNEDESK_WORKER_KEY", "")
CONCURRENCY = max(1, int(os.environ.get("WORKER_CONCURRENCY", "1")))
WORK_DIR = Path(os.environ.get("WORK_DIR", tempfile.gettempdir()))
IDLE_SLEEP = (10, 60)
HEARTBEAT_SECONDS = 240
stopping = threading.Event()
session = requests.Session()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


def api(action: str, payload: dict, timeout: int = 120) -> dict:
    for attempt in range(5):
        try:
            r = session.post(f"{URL}/api/public/worker/{action}", json=payload, headers={"Authorization": f"Bearer {KEY}"}, timeout=timeout)
        except requests.RequestException as e:
            if attempt == 4:
                raise
            log.warning("%s: network error (%s), retrying", action, e)
            time.sleep(2 ** attempt)
            continue
        if r.status_code >= 500 and attempt < 4:
            time.sleep(2 ** attempt)
            continue
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", r.text)
            except ValueError:
                msg = r.text
            raise ApiError(r.status_code, str(msg)[:300])
        return r.json()
    raise ApiError(599, "unreachable")


def put_file(url: str, path: Path, content_type: str = "application/octet-stream"):
    for attempt in range(5):
        try:
            with open(path, "rb") as f:
                r = session.put(url, data=f, headers={"Content-Type": content_type, "x-upsert": "true"}, timeout=3600)
            if r.status_code < 300:
                return
            if r.status_code < 500:
                raise RuntimeError(f"upload refused ({r.status_code}): {r.text[:200]}")
        except requests.RequestException as e:
            log.warning("upload retry %s: %s", attempt + 1, e)
        time.sleep(2 ** attempt)
    raise RuntimeError("upload failed after retries")


def download(url: str, dest: Path):
    with session.get(url, stream=True, timeout=3600) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for block in r.iter_content(1 << 20):
                f.write(block)


class Heartbeat(threading.Thread):
    def __init__(self, job_id: str):
        super().__init__(daemon=True)
        self.job_id = job_id
        self.progress: float | None = None
        self.done = threading.Event()

    def run(self):
        while not self.done.wait(HEARTBEAT_SECONDS):
            try:
                payload = {"jobId": self.job_id}
                if self.progress is not None:
                    payload["progress"] = round(min(1.0, max(0.0, self.progress)), 3)
                api("heartbeat", payload, timeout=30)
            except Exception as e:
                log.warning("heartbeat failed for %s: %s", self.job_id, e)


def upload_children(job_id: str, kids: list[Child]) -> int:
    sent = 0
    for i in range(0, len(kids), 100):
        batch = kids[i:i + 100]
        reg = api("children", {"jobId": job_id, "files": [{"name": k.name[:500], "folder": k.folder[:1000], "size": k.path.stat().st_size, "mime": k.mime[:200]} for k in batch]})
        ids = []
        for k, f in zip(batch, reg["files"]):
            put_file(f["uploadUrl"], k.path, k.mime or "application/octet-stream")
            ids.append(f["id"])
        api("children-done", {"jobId": job_id, "ids": ids})
        sent += len(ids)
        log.info("job %s: %d/%d files sent", job_id, sent, len(kids))
    return sent


def finish(job: dict, out: Output, sha: str):
    job_id = job["jobId"]
    base = {"jobId": job_id, "sha256": sha}
    meta = {k: v for k, v in out.meta.items() if not k.startswith("_") and isinstance(v, (str, int, float, bool))}
    if out.unsupported:
        api("complete", {**base, "unsupported": out.unsupported[:300]})
        return
    if out.children is not None:
        n = upload_children(job_id, out.children)
        api("complete", {**base, "children": n, "meta": meta})
        return
    if out.uploads:
        upload_derivatives(job_id, out.uploads)
    if out.audio_units:
        api("complete", {**base, "audioUnits": out.audio_units, "meta": meta})
        return
    if out.image:
        api("complete", {**base, "image": True, "meta": meta})
        return
    pages_file = Path(job["_work"]) / "pages.json.gz"
    with gzip.open(pages_file, "wt", encoding="utf8") as f:
        json.dump({"v": 1, "kind": out.kind or "text", "pages": out.pages}, f, ensure_ascii=False)
    put_file(job["pagesUpload"], pages_file, "application/gzip")
    payload = {**base, "pages": len(out.pages), "scanned": out.scanned, "meta": meta}
    if out.kind in ("pdf", "word", "sheet", "email", "text", "html"):
        payload["kind"] = out.kind
    if out.date:
        payload["date"] = out.date
    api("complete", payload)


CONTENT_TYPES = {".pdf": "application/pdf", ".json": "application/json", ".mp3": "audio/mpeg", ".jpg": "image/jpeg"}


def upload_derivatives(job_id: str, uploads: dict[str, Path]):
    names = list(uploads)
    urls: dict[str, str] = {}
    for i in range(0, len(names), 200):
        urls.update(api("sign", {"jobId": job_id, "files": names[i:i + 200]})["urls"])
    for rel, local in uploads.items():
        put_file(urls[rel], local, CONTENT_TYPES.get(Path(rel).suffix, "application/octet-stream"))


def process_render(job: dict, src: Path, work: Path, hb: "Heartbeat"):
    """Build the document's true view (view.pdf, spreadsheet grid or scan word boxes) and report what was made."""
    out = render(src, job["doc"], job["mode"], work)
    hb.progress = 0.8
    upload_derivatives(job["jobId"], out.uploads)
    api("complete", {"jobId": job["jobId"], "rendered": out.summary})


def process(job: dict):
    job_id = job["jobId"]
    doc = job["doc"]
    work = Path(tempfile.mkdtemp(prefix="jd-", dir=WORK_DIR))
    job["_work"] = str(work)
    hb = Heartbeat(job_id)
    hb.start()
    started = time.time()
    try:
        ext = (doc.get("ext") or "bin").lower()
        src = work / f"original.{ext}"
        log.info("job %s: %s (%.1f MB)", job_id, doc["name"], (doc.get("bytes") or 0) / 1048576)
        download(job["downloadUrl"], src)
        if job.get("stage") == "render":
            process_render(job, src, work, hb)
            log.info("job %s: view rendered in %.0fs", job_id, time.time() - started)
            return
        sha = sha256_file(src)
        out = convert(src, doc, work, lambda f: setattr(hb, "progress", f))
        # Email attachments become their own documents; they are registered while the job is still leased.
        kids = out.meta.get("_children") or []
        if kids:
            upload_children(job_id, kids)
        finish(job, out, sha)
        log.info("job %s: done in %.0fs", job_id, time.time() - started)
    except ApiError as e:
        log.error("job %s: JuneDesk refused: %s", job_id, e)
    except Permanent as e:
        log.warning("job %s: %s", job_id, e)
        safe_fail(job_id, str(e), True)
    except Exception as e:  # retryable
        log.exception("job %s failed", job_id)
        msg = "Could not prepare the original-layout view." if job.get("stage") == "render" else "Something went wrong while reading this file on the processing worker. It will be retried."
        safe_fail(job_id, msg, False)
    finally:
        hb.done.set()
        shutil.rmtree(work, ignore_errors=True)


def safe_fail(job_id: str, message: str, permanent: bool):
    try:
        api("fail", {"jobId": job_id, "error": message[:400], "permanent": permanent})
    except Exception as e:
        log.error("could not report failure for %s: %s", job_id, e)


def main():
    if not URL or len(KEY) < 24:
        log.error("Set JUNEDESK_URL and JUNEDESK_WORKER_KEY (at least 24 characters).")
        sys.exit(2)
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    log.info("JuneDesk worker started: %s, concurrency %d", URL, CONCURRENCY)
    idle = IDLE_SLEEP[0]
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        running: set = set()
        while not stopping.is_set():
            running = {f for f in running if not f.done()}
            free = CONCURRENCY - len(running)
            if free <= 0:
                time.sleep(2)
                continue
            try:
                jobs = api("claim", {"limit": free})["jobs"]
            except ApiError as e:
                log.error("claim refused: %s", e)
                stopping.wait(60)
                continue
            except Exception as e:
                log.warning("claim failed: %s", e)
                stopping.wait(30)
                continue
            if not jobs:
                stopping.wait(idle)
                idle = min(IDLE_SLEEP[1], idle * 2)
                continue
            idle = IDLE_SLEEP[0]
            for job in jobs:
                running.add(pool.submit(process, job))
        log.info("stopping: waiting for %d running job(s)", len(running))


if __name__ == "__main__":
    main()
