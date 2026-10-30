"""Waste Ticket Reader: FastAPI backend, packaged for Azure Container Apps.

Serves the frontend, the sample tickets, and the AI reading endpoint backed
by the Nutrient Python SDK (structured extraction with native confidence and
source bounding boxes).
"""

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path

import httpx
import nutrient_sdk as ns
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import extraction
import normalize
import records

ROOT = Path(__file__).resolve().parent.parent
INPUTS = Path(os.environ.get("INPUT_DIR") or ROOT / "inputs")
OUTPUTS = Path(os.environ.get("OUTPUT_DIR") or ROOT / "outputs")
FRONTEND = ROOT / "frontend"
RECORDS = OUTPUTS / "records"
ARCHIVE = OUTPUTS / "archive"
for folder in (INPUTS, OUTPUTS, RECORDS, ARCHIVE):
    folder.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Waste Ticket Reader")

_page_counts = {}
_progress = {}
_progress_lock = threading.Lock()


@app.middleware("http")
async def no_cache(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    return response


def _safe_input(name):
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    path = INPUTS / name
    return path if path.is_file() else None


def _page_count(path):
    stamp = path.stat().st_mtime
    cached = _page_counts.get(path.name)
    if cached and cached[0] == stamp:
        return cached[1]
    extraction._ensure_sdk()
    pages = 0
    try:
        doc = ns.Document.open(str(path))
        try:
            pages = doc.get_page_count()
        finally:
            doc.close()
    except Exception:
        pages = 0
    _page_counts[path.name] = (stamp, pages)
    return pages


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def config():
    """Browser configuration: the Web SDK license key (empty = evaluation mode)."""
    return {"web_sdk_license_key": os.environ.get("NUTRIENT_WEB_SDK_LICENSE_KEY", "")}


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")


@app.post("/internal/ollama/v1/chat/completions")
async def ollama_relay(request: Request):
    """Relay between the SDK and a local Ollama server (AI_PROVIDER=ollama).

    The SDK asks for token log-probabilities to compute its confidence score;
    Ollama's answer to that request is rejected by the SDK, so the relay drops
    it and the SDK falls back to its grounding score. It also caps the answer
    length, because a small model can loop inside a JSON array until the
    context limit. Only reachable from the machine itself."""
    if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "Internal endpoint")
    body = await request.json()
    body.pop("logprobs", None)
    body.pop("top_logprobs", None)
    body.setdefault("max_tokens", int(os.environ.get("OLLAMA_MAX_TOKENS", "2000")))
    body.setdefault("presence_penalty", float(os.environ.get("OLLAMA_PRESENCE_PENALTY", "0.6")))

    async def forward():
        async with httpx.AsyncClient(timeout=1200.0) as client:
            return await client.post(f"{OLLAMA_URL}/v1/chat/completions", json=body)

    # Shielded: when the SDK gives up, the Ollama call still completes instead
    # of leaving the model runner busy with an abandoned request.
    upstream = await asyncio.shield(asyncio.ensure_future(forward()))
    return JSONResponse(upstream.json(), status_code=upstream.status_code)


def _records():
    out = []
    for path in RECORDS.glob("*.json"):
        try:
            out.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: r.get("confirmed_at") or "")


@app.get("/api/files")
def files():
    confirmed = {}
    for r in _records():
        confirmed.setdefault(r["document"]["source_file"], []).append(r["document"]["ticket_index"])
    result = []
    for path in sorted(INPUTS.glob("*.pdf"), key=lambda p: p.name.lower()):
        cached = OUTPUTS / f"{path.name}.json"
        result.append({
            "name": path.name,
            "pages": _page_count(path),
            # A previous reading of the same file, so a demo can reopen it instantly.
            "cached": cached.is_file() and cached.stat().st_mtime >= path.stat().st_mtime,
            "confirmed": sorted(confirmed.get(path.name, [])),
        })
    return result


@app.get("/api/reference")
def reference():
    return {
        "waste_types": [{k: t[k] for k in ("id", "label", "ewc", "category")} for t in normalize.WASTE_TYPES],
        "carriers": [c["name"] for c in normalize.CARRIERS],
        "storage_kinds": normalize.STORAGE_KINDS,
        "waste_categories": normalize.WASTE_CATEGORIES,
        "retention_years": normalize.RETENTION_YEARS,
    }


@app.get("/api/result")
def cached_result(file: str):
    path = _safe_input(file)
    cached = OUTPUTS / f"{file}.json" if path else None
    if cached is None or not cached.is_file():
        raise HTTPException(404, "No previous reading")
    return JSONResponse(json.loads(cached.read_text()))


@app.post("/api/upload")
async def upload(request: Request, name: str):
    """Adds a ticket to the inputs (raw PDF body). In production this is the
    call the waste register makes when a waste ticket is attached to a collection."""
    clean = re.sub(r"[^A-Za-z0-9 ._&()-]+", "_", Path(name).name).strip() or "ticket.pdf"
    if not clean.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF tickets are accepted")
    body = await request.body()
    if not body.startswith(b"%PDF"):
        raise HTTPException(400, "The file is not a PDF")
    if len(body) > 50 * 1024 * 1024:
        raise HTTPException(413, "The file is larger than 50 MB")
    (INPUTS / clean).write_bytes(body)
    return {"name": clean, "pages": _page_count(INPUTS / clean)}


@app.get("/api/pdf")
def pdf(name: str):
    path = _safe_input(name)
    if path is None:
        raise HTTPException(404, "Unknown file")
    return FileResponse(path, media_type="application/pdf")


@app.get("/api/progress")
def progress(job: str):
    with _progress_lock:
        state = _progress.get(job)
    if not state:
        return {"known": False}
    return {"known": True, **state}


@app.post("/api/process")
def process(file: str, job: str | None = None):
    path = _safe_input(file)
    if path is None:
        raise HTTPException(404, "Unknown file")

    def report(stage, done, total):
        if job:
            with _progress_lock:
                _progress[job] = {"stage": stage, "done": done, "total": total,
                                  "started": _progress.get(job, {}).get("started", time.time())}

    report("Opening the document", 0, 2)
    started = time.time()
    try:
        result = extraction.process_document(str(path), progress=report)
    except Exception as exc:
        raise HTTPException(500, f"Processing failed: {exc}") from exc
    finally:
        if job:
            with _progress_lock:
                _progress.pop(job, None)

    result["file"] = path.name
    result["processing_seconds"] = round(time.time() - started, 1)
    try:
        (OUTPUTS / f"{path.name}.json").write_text(json.dumps(result, indent=1))
    except OSError:
        pass
    return JSONResponse(result)


@app.post("/api/confirm")
async def confirm(request: Request):
    """The reviewer accepted a ticket: build the waste register entries, archive the
    ticket pages with the data embedded, and keep the record. This is where
    the workflow would post the entries to the waste register API."""
    payload = await request.json()
    path = _safe_input(payload.get("file"))
    if path is None:
        raise HTTPException(404, "Unknown file")
    try:
        record = records.build(path, payload)
    except records.RecordError as exc:
        raise HTTPException(422, str(exc)) from exc
    # A ticket confirmed again (after an edit) replaces its previous record.
    for old in RECORDS.glob("*.json"):
        try:
            prev = json.loads(old.read_text())
        except (OSError, ValueError):
            continue
        doc = prev.get("document", {})
        if doc.get("source_file") == path.name and doc.get("ticket_index") == record["document"]["ticket_index"]:
            old.unlink(missing_ok=True)
            if doc.get("archive_file"):
                (ARCHIVE / doc["archive_file"]).unlink(missing_ok=True)
    try:
        records.archive_pdf(path, record, ARCHIVE / record["document"]["archive_file"])
    except Exception as exc:
        record["document"]["archive_error"] = str(exc).split(" (Error Code")[0]
        record["document"]["archive_url"] = None
    (RECORDS / f"{record['id']}.json").write_text(json.dumps(record, indent=1))
    return record


@app.get("/api/records")
def list_records():
    return _records()


@app.delete("/api/records")
def withdraw(id: str):
    """Reopens a confirmed ticket for editing."""
    path = RECORDS / f"{Path(id).name}.json"
    if not path.is_file():
        raise HTTPException(404, "Unknown record")
    path.unlink()
    (ARCHIVE / f"{Path(id).name}.pdf").unlink(missing_ok=True)
    return {"withdrawn": id}


@app.get("/api/archive")
def archive(id: str, data: bool = False):
    path = ARCHIVE / f"{Path(id).name}.pdf"
    if not path.is_file():
        raise HTTPException(404, "Unknown archive")
    if data:
        # Reads the record back from the PDF itself.
        return JSONResponse(records.read_embedded(path))
    return FileResponse(path, media_type="application/pdf", filename=path.name)


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
