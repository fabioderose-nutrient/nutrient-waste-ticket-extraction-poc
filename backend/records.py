"""Confirmed waste collections: what the reviewer accepts is turned into
waste collection entries for the site waste register and the ticket is
archived.

The archive is the ticket's own pages saved as a PDF by the Nutrient SDK with
the confirmed record embedded in the file (document information dictionary,
key "WasteCollectionRecord"), so the scan and its data travel together and the
record can be read back from the PDF alone. The record keeps a link to that
PDF, which is what the waste register stores next to the entry.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import nutrient_sdk as ns

import extraction
import normalize

EMBED_KEY = "WasteCollectionRecord"
_TYPES = {t["id"]: t for t in normalize.WASTE_TYPES}


class RecordError(ValueError):
    pass


def record_id(file_name, ticket_index, ticket_number):
    stem = re.sub(r"[^A-Za-z0-9]+", "-", Path(file_name).stem).strip("-")[:40] or "ticket"
    number = re.sub(r"[^A-Za-z0-9]+", "", ticket_number or "")[:20] or "noref"
    return f"{stem}-{ticket_index + 1}-{number}"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(source_path, payload):
    """Validates the reviewer's confirmed ticket and returns the record:
    one waste register entry per waste line plus the document reference."""
    date = (payload.get("collection_date") or "").strip()
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise RecordError("A valid collection date is required") from None
    number = normalize.clean(payload.get("waste_ticket_number"))
    carrier = normalize.clean(payload.get("waste_carrier"))
    category = payload.get("waste_category")
    if not number:
        raise RecordError("The waste ticket number is required")
    if not carrier:
        raise RecordError("The waste carrier is required")
    if category not in normalize.WASTE_CATEGORIES:
        raise RecordError("Choose the waste category: " + " or ".join(normalize.WASTE_CATEGORIES))

    entries = []
    for line in payload.get("entries") or []:
        wt = _TYPES.get(line.get("waste_type_id"))
        if wt is None:
            raise RecordError("Every waste line needs a waste type from the waste types list")
        try:
            bins = int(line.get("bins_collected"))
        except (TypeError, ValueError):
            raise RecordError(f"Enter the number of bins/skips for {wt['label']}") from None
        if bins < 1:
            raise RecordError(f"The number of bins/skips for {wt['label']} must be at least 1")
        storage = normalize.clean(line.get("storage_type")) or normalize.clean(payload.get("storage_type"))
        if not storage:
            raise RecordError("The storage type is required")
        entries.append({
            "collection_date": date,
            "waste_ticket_number": number,
            "waste_carrier": carrier,
            "waste_type": wt["label"],
            "waste_type_id": wt["id"],
            "ewc_code": wt["ewc"],
            "storage_type": storage,
            "bins_collected": bins,
            "waste_category": category,
            "hazardous": wt["category"] == "hazardous",
        })
    if not entries:
        raise RecordError("Add at least one waste line")

    hazardous = any(e["hazardous"] for e in entries)
    file_name = payload["file"]
    index = int(payload.get("ticket_index") or 0)
    rid = record_id(file_name, index, number)
    pages = sorted({int(p) for p in payload.get("pages") or [] if str(p).isdigit()}) or [1]
    return {
        "id": rid,
        "status": "confirmed",
        "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "confirmed_by": normalize.clean(payload.get("reviewer")) or None,
        "entries": entries,
        "site": normalize.clean(payload.get("site")),
        "retention": {
            "hazardous": hazardous,
            "years": normalize.RETENTION_YEARS["hazardous" if hazardous else "controlled"],
            "retain_until": normalize.retention_until(date, hazardous),
        },
        "document": {
            "source_file": file_name,
            "source_sha256": _sha256(source_path),
            "ticket_index": index,
            "pages": pages,
            "archive_file": f"{rid}.pdf",
            "archive_url": f"/api/archive?id={rid}",
            "data_embedded_as": EMBED_KEY,
        },
        # What the reader proposed versus what the reviewer saved: the
        # feedback that shows where the reading needs tuning.
        "corrections": [c for c in payload.get("corrections") or [] if isinstance(c, dict)],
    }


def archive_pdf(source_path, record, out_path):
    """Saves the ticket's pages as a PDF with the record embedded in it."""
    extraction._ensure_sdk()
    keep = set(record["document"]["pages"])
    with extraction._sdk_lock:
        doc = ns.Document.open(str(source_path))
        try:
            editor = ns.PdfEditor.edit(doc)
            try:
                pages = editor.get_page_collection()
                count = pages.get_count()
                if keep and all(1 <= p <= count for p in keep) and len(keep) < count:
                    for i in reversed(range(count)):
                        if i + 1 not in keep:
                            pages.remove_at(i)
                first = record["entries"][0]
                meta = editor.get_metadata()
                meta.set_title(f"Waste transfer note {first['waste_ticket_number']}")
                meta.set_subject(f"{first['waste_carrier']} - collected {first['collection_date']} - "
                                 f"keep until {record['retention']['retain_until']}")
                meta.set_keywords(";".join(["waste ticket", first["waste_ticket_number"], first["waste_carrier"],
                                            first["waste_category"]] + [e["ewc_code"] for e in record["entries"]]))
                meta.set_custom_pdf_information(EMBED_KEY, json.dumps(record, separators=(",", ":")))
                editor.save_as(str(out_path))
            finally:
                editor.close()
        finally:
            doc.close()


def read_embedded(pdf_path):
    """Reads the record back from an archived PDF (proof the PDF alone is enough)."""
    extraction._ensure_sdk()
    with extraction._sdk_lock:
        doc = ns.Document.open(str(pdf_path))
        try:
            editor = ns.PdfEditor.edit(doc)
            try:
                raw = editor.get_metadata().get_custom_pdf_information(EMBED_KEY)
            finally:
                editor.close()
        finally:
            doc.close()
    return json.loads(raw) if raw else None
