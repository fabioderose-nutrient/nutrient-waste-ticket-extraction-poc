"""Waste ticket extraction pipeline on the Nutrient Python SDK.

Pass 1 (text): one structured-extraction call per document returns every
distinct note it contains, with the SDK's native metadata for each field:
the model's confidence components (log-probability, margin, groundedness),
the ICR recognition score for handwritten values, and the source bounding
boxes the frontend uses to highlight the value on the ticket.

Pass 2 (image, only when needed): handwritten or badly scanned notes leave
key fields empty in pass 1. The page is then rendered by the SDK and read by
the vision model through the SDK's image description API, and the missing
values are filled from that reading. Values read from the image carry no
position and are always flagged for review.

Each ticket is finally normalised into waste collection entries of the
site waste register (one per waste line) by the deterministic rules in normalize.py.
"""

import glob
import json
from datetime import datetime
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

import nutrient_sdk as ns

import normalize
import ticket_schema

# The native layer mis-encodes some characters (an ampersand, for example, so
# that after JSON parsing a NUL byte is left followed by the two hex digits of
# the real character). NUL is never legitimate in extracted text, so rewriting
# NUL + 2 hex digits back to that character is safe.
_NUL_ESCAPE = re.compile(chr(0) + "([0-9a-fA-F]{2})")

REVIEW_BELOW = int(os.environ.get("REVIEW_BELOW", "70"))
LOW_CONFIDENCE = 40
IMAGE_PASS = os.environ.get("IMAGE_PASS", "1") != "0"

IMAGE_PROMPT = (
    "This image is one UK waste transfer note or hazardous waste consignment note, possibly handwritten or "
    "poorly scanned. Read it carefully and return ONLY a JSON object (no prose, no code fence) with these keys, "
    "each a string exactly as written or null when blank: ticket_number (the Ticket No / Note No / Advice No / "
    "Job Number of the note, never an account or order number), collection_date (the handwritten DATE of the "
    "collection; read every digit of the year), waste_carrier (the company issuing the note and collecting the "
    "waste, never the customer), site (the customer's site or address the waste comes from), container (skip / "
    "bin / caged lorry / roll-on-off / compactor / IBC and its size, including a ticked container box or a "
    "written bin size), quantity (the total number of containers collected, e.g. '9 x 660'), vehicle "
    "(registration).\n"
    "Also return 'tick_table': true when the waste section is a pre-printed list of materials with QTY and/or "
    "Tick columns, else false, and 'rows':\n"
    "- when tick_table is true: EVERY printed row of that list, in every column block, each as {description, "
    "ewc_code (as printed), qty (the handwritten number in that row's QTY cell, or null), ticked (true when "
    "that row's Tick cell carries a handwritten tick, else false)}. Look at each QTY and Tick cell on its own. "
    "A long diagonal line drawn across the table only crosses out unused rows: where it passes through a cell "
    "it is neither a quantity nor a tick. A single vertical stroke in a QTY cell is the number 1.\n"
    "- when tick_table is false: one {description, ewc_code, qty, ticked: null} per waste line written on the "
    "note.\n"
    "Finally return 'positions': for each header key above that has a value, and for every row with a qty or a "
    "tick (key 'row:<index in rows>'), the box around the handwritten value as [x0, y0, x1, y1] in thousandths "
    "of the image width and height (0-1000, origin top-left)."
)


def _clean(value):
    if isinstance(value, str):
        return normalize.clean(_NUL_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), value))
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    return value


_sdk_lock = threading.Lock()
_initialized = False


def _ensure_sdk():
    global _initialized
    if _initialized:
        return
    key = os.environ.get("NUTRIENT_LICENSE_KEY", "").strip()
    if key:
        ns.License.register_key(key)
    _initialized = True


def _ai_config():
    provider = os.environ.get("AI_PROVIDER", "openai").strip().lower()
    if provider == "ollama":
        # A local model served by Ollama, reached through the app's own relay
        # (app.py, /internal/ollama), which drops the log-probability request
        # Ollama cannot answer. Nothing leaves the machine.
        return {
            "provider": "local",
            "api_key": "ollama",
            "model": os.environ.get("AI_MODEL", "") or "qwen2.5:7b-instruct",
            "endpoint": f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/internal/ollama/v1",
        }
    return {
        "provider": provider,
        "api_key": os.environ.get("AI_API_KEY", ""),
        "model": os.environ.get("AI_MODEL", "") or "gpt-4o",
        "endpoint": os.environ.get("AI_ENDPOINT", "").strip(),
    }


def _configure_vlm(settings, model=None):
    """Point the SDK's vision-language provider at the configured AI service."""
    cfg = _ai_config()
    vision = settings.vision_settings
    if cfg["provider"] in ("anthropic", "claude"):
        vision.provider = ns.VlmProvider.ANTHROPIC
        api = settings.anthropic_api_settings
    elif cfg["provider"] == "local":
        vision.provider = ns.VlmProvider.CUSTOM
        api = settings.custom_vlm_api_settings
    else:
        vision.provider = ns.VlmProvider.OPEN_AI
        api = settings.open_ai_api_endpoint_settings
    api.api_key = cfg["api_key"]
    api.model = model or cfg["model"]
    # Reasoning models (gpt-5.x) otherwise think at length on every page.
    if os.environ.get("VLM_FAST_MODE", "1") != "0" and hasattr(api, "fast_mode"):
        api.fast_mode = True
    if cfg["endpoint"]:
        api.api_endpoint = cfg["endpoint"]


def _document_settings():
    s = ns.DocumentSettings()
    vision = s.vision_settings
    # VISION_FEATURES: comma-separated VisionFeatures names the licence
    # allows (TABLE, FORM, KEY_VALUE_REGION, HANDWRITING...). TABLE makes the
    # per-row waste tables (material, EWC, bins per size) come through intact.
    features = 0
    for name in [n.strip().upper() for n in os.environ.get("VISION_FEATURES", "TABLE").split(",") if n.strip()]:
        features |= getattr(ns.VisionFeatures, name).value
    vision.features = features if features else ns.VisionFeatures.UNKNOWN  # the setter takes combined ints
    # The layout model's default threshold (0.5) drops whole regions of
    # form-style pages; 0.2 keeps the answer cells of the notes.
    s.inference_layout_settings.confidence_threshold = float(os.environ.get("LAYOUT_CONFIDENCE", "0.2"))
    # VISION_ENGINE=vlm runs the SDK's "VLM Enhanced ICR" engine (the page
    # images go through the cloud model during OCR); slower, off by default.
    if os.environ.get("VISION_ENGINE", "icr").lower() == "vlm":
        vision.engine = ns.VisionEngine.VLM_ENHANCED_ICR
        _configure_vlm(s)
    else:
        vision.engine = ns.VisionEngine.ICR

    cfg = _ai_config()
    ai = s.ai_processing_settings
    ai.provider = cfg["provider"]
    ai.api_key = cfg["api_key"]
    ai.model = cfg["model"]
    if cfg["endpoint"]:
        ai.endpoint = cfg["endpoint"]
    ai.include_confidence = True
    ai.include_source_locations = True
    ai.include_groundedness = True
    ai.temperature = 0.0
    return s


# ------------------------------------------------------------ pass 2

class PageImages:
    """Renders the document's pages once (lazily) through the SDK."""

    def __init__(self, path):
        self._path = path
        self._dir = None
        self.error = None

    def close(self):
        if self._dir:
            self._dir.cleanup()
            self._dir = None

    def path(self, page):
        if self._dir is None:
            self._dir = tempfile.TemporaryDirectory(prefix="ticket-pages-")
            settings = ns.DocumentSettings()
            settings.image_settings.export_format = ns.ImageExportFormat.PNG
            settings.image_settings.rasterization_dpi = 200
            try:
                doc = ns.Document.open(self._path, settings)
                try:
                    doc.export_as_image(os.path.join(self._dir.name, "page.png"))
                finally:
                    doc.close()
            except Exception as exc:  # typically a license without pdf_to_image_api
                self.error = str(exc).split(" (Error Code")[0]
        # The SDK writes <base>-<n>.png for multi-page documents but plain
        # <base>.png for a single page.
        for name in (f"page-{page}.png", "page.png" if page == 1 else None):
            candidate = os.path.join(self._dir.name, name) if name else None
            if candidate and os.path.exists(candidate):
                return candidate
        return None


IMAGE_WORKERS = int(os.environ.get("IMAGE_WORKERS", "4"))


def _read_pages(images, dims, pages, progress=None):
    """Reads several pages as images at once: each call waits on the vision
    model, so the pages of a multi-ticket scan are read in parallel."""
    if not pages:
        return {}
    if progress is not None:
        progress(f"Reading handwriting on {len(pages)} page{'s' if len(pages) > 1 else ''}", 1, 2)
    pngs = {p: images.path(p) for p in pages}  # rendered once, before the threads start
    for p, png in pngs.items():
        _page_size(dims, p, png)

    def read(page):
        try:
            return page, (_read_page_image(pngs[page]) if pngs[page] else None)
        except Exception:
            return page, None

    with ThreadPoolExecutor(max_workers=max(1, min(IMAGE_WORKERS, len(pages)))) as pool:
        return dict(pool.map(read, pages))


def _page_size(dims, page, png_path):
    """Page space for an image-only page: the rendered image's pixels (the
    text extraction reports pages at the same 200 dpi)."""
    if page in dims or not png_path:
        return
    try:
        from PIL import Image
        with Image.open(png_path) as im:
            dims[page] = im.size
    except Exception:
        pass


def _read_page_image(png_path):
    """Ask the vision model to transcribe one rendered page; returns a dict or None."""
    settings = ns.DocumentSettings()
    _configure_vlm(settings, os.environ.get("IMAGE_MODEL") or None)
    # The handwritten year is the hardest digit pair on these notes; today's
    # date tells the model which years are plausible without forcing one.
    settings.vision_descriptor_settings.standard_prompt = IMAGE_PROMPT + (
        f"\nToday is {datetime.now():%d %B %Y}: collection dates are recent, usually this year or last year; "
        "read the year's digits as written.")
    settings.vision_descriptor_settings.level = ns.DescriptionLevel.STANDARD
    doc = ns.Document.open(png_path, settings)
    try:
        text = ns.Vision.set(doc).describe()
    finally:
        doc.close()
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return _clean(data) if isinstance(data, dict) else None


# ----------------------------------------------------------- building

def _confidence_percent(meta):
    if not meta:
        return 0
    comp = meta.get("confidenceComponents") or {}
    prob = comp.get("probabilityScore")
    grounding = comp.get("groundingScore")
    if prob is None and grounding is None:
        return 0
    score = (1.0 if prob is None else float(prob)) * (1.0 if grounding is None else float(grounding))
    recognition = meta.get("recognitionScore")
    if recognition is not None:
        score *= float(recognition)
    return round(score * 100)


def _boxes(meta):
    if not meta:
        return []
    boxes = []
    for src in meta.get("source_bboxes") or []:
        bbox = src.get("bbox") or {}
        if bbox.get("width") and bbox.get("height"):
            boxes.append({"page": src.get("page"), **{k: bbox[k] for k in ("x", "y", "width", "height")}})
    if not boxes and meta.get("bbox"):
        bbox = meta["bbox"]
        if bbox.get("width") and bbox.get("height"):
            boxes.append({"page": meta.get("page"), **{k: bbox[k] for k in ("x", "y", "width", "height")}})
    primary = meta.get("page")
    on_primary = [b for b in boxes if b.get("page") == primary]
    return (on_primary or boxes)[:8]


def _field(value, meta):
    meta = meta if isinstance(meta, dict) else None
    value = value if isinstance(value, str) and value.strip() else None
    confidence = _confidence_percent(meta) if value else 0
    return {
        "value": value,
        "confidence": confidence,
        "page": (meta or {}).get("page") if value else None,
        "boxes": _boxes(meta) if value else [],
        "image": False,
    }


def _image_field(value, page, boxes=None):
    """A value read by the vision model from the page image. It has no
    confidence score; its position is the model's approximate box (or the
    text reading's box for the same field) and is flagged as approximate."""
    value = str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
    value = value if isinstance(value, str) and value.strip() else None
    boxes = boxes if value and boxes else []
    return {"value": value, "confidence": 0, "page": page if value else None, "boxes": boxes, "image": True,
            "approx": bool(boxes)}


def _vlm_box(positions, key, page, dims):
    """[x0, y0, x1, y1] in thousandths of the page -> a box in the page space
    of the extraction (the one the frontend maps onto the viewer)."""
    box = (positions or {}).get(key) if isinstance(positions, dict) else None
    size = dims.get(page)
    if not size or not isinstance(box, list) or len(box) != 4:
        return []
    try:
        x0, y0, x1, y1 = (max(0.0, min(1000.0, float(v))) for v in box)
    except (TypeError, ValueError):
        return []
    if x1 <= x0 or y1 <= y0:
        return []
    w, h = size
    pad = 6  # the model's boxes are tight and slightly off; a margin reads better
    return [{"page": page, "x": max(0, x0 * w / 1000 - pad), "y": max(0, y0 * h / 1000 - pad),
             "width": (x1 - x0) * w / 1000 + 2 * pad, "height": (y1 - y0) * h / 1000 + 2 * pad}]


def _image_lines(data, page, dims):
    """Waste lines from the image reading. On a tick-box table every printed
    row comes back with its QTY and Tick cells, and the collected rows are
    chosen here with a fixed rule (tick = there, QTY = how many; a tick
    alone is one container) rather than left to the model."""
    rows = [r for r in (data.get("rows") or data.get("waste_lines") or []) if isinstance(r, dict)]
    positions = data.get("positions")
    tick_table = data.get("tick_table") is True
    lines = []
    for i, row in enumerate(rows):
        if not (row.get("description") or row.get("ewc_code")):
            continue
        qty = row.get("qty", row.get("quantity"))
        qty = str(qty) if isinstance(qty, (int, float)) and not isinstance(qty, bool) else qty
        tick_only = False
        if tick_table:
            tick_only = normalize.tick_row_quantity(qty, None) is None
            qty = normalize.tick_row_quantity(qty, row.get("ticked"))
            if qty is None:
                continue
        boxes = _vlm_box(positions, f"row:{i}", page, dims)
        lines.append({
            "description": _image_field(row.get("description"), page, boxes),
            "ewc_code": _image_field(row.get("ewc_code"), page, boxes),
            "quantity": _image_field(qty, page, boxes),
            "container": _image_field(None, page),
            "ticked": row.get("ticked") is True,
            "tick_only": tick_only,
        })
    # A diagonal line crossing out the unused rows of a tick table is often
    # read as a mark in every row: a flood of identical counts is not a reading.
    counts = [normalize.clean(l["quantity"]["value"]) for l in lines]
    if len(lines) > 6 and len(set(counts)) == 1:
        return [], tick_table
    return lines, tick_table


TICKET_FIELDS = ("ticket_number", "ticket_number_label", "collection_date", "waste_carrier", "carrier_licence",
                 "site", "container", "quantity", "vehicle")
# A number taken from one of these labels is not the note's reference.
_NOT_A_TICKET_LABEL = re.compile(r"account|order|customer|client|sic|licen|weigh|quantity|qty|vat|reg", re.I)
LINE_FIELDS = ("description", "ewc_code", "quantity", "container")
SINGLE_CONTAINER_KINDS = {"Skip", "Roll-on/off", "Wait & load", "Compactor", "Caged lorry"}


def _text_ticket(data, meta):
    meta = meta if isinstance(meta, dict) else {}
    fields = {key: _field(data.get(key), meta.get(key)) for key in TICKET_FIELDS}
    lines = []
    line_metas = meta.get("waste_lines") if isinstance(meta.get("waste_lines"), list) else []
    for i, line in enumerate(data.get("waste_lines") or []):
        if not isinstance(line, dict):
            continue
        lm = line_metas[i] if i < len(line_metas) and isinstance(line_metas[i], dict) else {}
        lines.append({key: _field(line.get(key), lm.get(key)) for key in LINE_FIELDS})
    page = next((f["page"] for f in fields.values() if f["page"]), None)
    return {"fields": fields, "lines": lines, "page": page}


def _weak(field):
    return field["value"] is None or field["confidence"] < LOW_CONFIDENCE


def _needs_image_pass(ticket):
    f = ticket["fields"]
    if _weak(f["ticket_number"]) or _weak(f["collection_date"]):
        return True
    if f["collection_date"]["value"]:
        iso = normalize.parse_date(f["collection_date"]["value"])["iso"]
        if iso is None or normalize.date_plausibility(iso):
            return True
    if not ticket["lines"] or all(_weak(l["ewc_code"]) and _weak(l["description"]) for l in ticket["lines"]):
        return True
    return _weak(f["container"]) and _weak(f["quantity"])


def _merge_image(ticket, data, page, dims):
    """Fill the weak fields of a text ticket from the image reading. A ticket
    that needed the image pass is a handwritten or badly scanned note: on a
    tick-box table the rows read cell by cell from the image replace the text
    ones (the OCR of a handwritten tick table is not trustworthy); otherwise
    they replace them only when the text lines are weak."""
    f = ticket["fields"]
    positions = data.get("positions")
    for key in TICKET_FIELDS:
        replace = _weak(f[key])
        if key == "collection_date" and f[key]["value"]:
            text_date = normalize.parse_date(f[key]["value"])
            image_date = normalize.parse_date(data.get("collection_date"))
            implausible = text_date["iso"] is None or normalize.date_plausibility(text_date["iso"]) is not None
            if implausible and image_date["iso"] and normalize.date_plausibility(image_date["iso"]) is None:
                replace = True
        if replace and data.get(key):
            # The text reading usually found the right place even when it
            # misread the handwriting: keep its position when the model gives none.
            boxes = _vlm_box(positions, key, page, dims) or f[key]["boxes"]
            f[key] = _image_field(data.get(key), page, boxes)
    image_lines, tick_table = _image_lines(data, page, dims)
    text_lines_weak = all(_weak(l["ewc_code"]) and _weak(l["description"]) for l in ticket["lines"])
    if image_lines and (tick_table or text_lines_weak):
        ticket["lines"] = image_lines
    elif text_lines_weak:
        ticket["lines"] = []
    ticket["tick_table"] = tick_table
    ticket["page"] = ticket["page"] or page
    ticket["image_pass"] = True


def _dedupe(tickets):
    """The same note returned twice (e.g. its waste table split across two
    elements) is one ticket: keep the reading with the most values."""
    kept, seen = [], {}
    for t in tickets:
        number = re.sub(r"\W", "", t["fields"]["ticket_number"]["value"] or "").lower()
        if not number:
            kept.append(t)
            continue
        filled = sum(1 for f in t["fields"].values() if f["value"]) + len(t["lines"])
        if number in seen:
            i, best = seen[number]
            if filled > best:
                kept[i] = t
                seen[number] = (i, filled)
            continue
        seen[number] = (len(kept), filled)
        kept.append(t)
    return kept


def _image_ticket(data, page, dims):
    positions = data.get("positions")
    fields = {key: _image_field(data.get(key), page, _vlm_box(positions, key, page, dims)) for key in TICKET_FIELDS}
    lines, tick_table = _image_lines(data, page, dims)
    return {"fields": fields, "lines": lines, "page": page, "image_pass": True, "tick_table": tick_table}


def _finish_ticket(index, ticket):
    fields, lines = ticket["fields"], ticket["lines"]
    date = normalize.parse_date(fields["collection_date"]["value"])
    carrier = normalize.match_carrier(fields["waste_carrier"]["value"])
    container = normalize.parse_container(fields["container"]["value"])
    quantity = normalize.parse_quantity(fields["quantity"]["value"], fields["container"]["value"])

    def needs_review(field, extra=False):
        return extra or field["image"] or field["value"] is None or field["confidence"] < REVIEW_BELOW

    checks = []
    date_warning = normalize.date_plausibility(date["iso"])
    if date_warning:
        checks.append({"field": "collection_date", "message": date_warning})
    line_counts = [normalize.parse_quantity(l["quantity"]["value"])["value"] if normalize.is_count(l["quantity"]["value"])
                   else None for l in lines]
    totals = normalize.totals_check(line_counts, quantity["value"]) if lines else None
    suspects = set()
    if totals:
        # A tick with no number that makes the count overflow is most often
        # the diagonal crossing-out line read as a tick.
        tick_only = [i for i, l in enumerate(lines) if l.get("tick_only")]
        if tick_only and sum(line_counts) - len(tick_only) == quantity["value"]:
            suspects = set(tick_only)
            names = ", ".join(lines[i]["description"]["value"] or "?" for i in tick_only)
            totals += f"; the tick on {names} has no number and may be the crossing-out line"
        checks.append({"field": "bins_collected", "message": totals})
    totals_agree = bool(lines) and quantity["value"] is not None and totals is None and None not in line_counts

    entries = []
    for li, line in enumerate(lines or [None]):
        waste = normalize.match_waste_type(line["ewc_code"]["value"], line["description"]["value"]) if line else \
            normalize.match_waste_type(None, None)
        # A per-line container (one column per bin size) overrides the ticket's.
        line_container = normalize.parse_container(line["container"]["value"]) if line and line["container"]["value"] else None
        entry_container = line_container if line_container and line_container["kind"] else container
        line_count = None
        if line and normalize.is_count(line["quantity"]["value"]):
            line_count = normalize.parse_quantity(line["quantity"]["value"])["value"]
        assumed = False
        if line_count is not None:
            bins = line_count
        elif quantity["value"] is not None and len(lines) <= 1:
            bins = quantity["value"]
        elif quantity["value"] is None and entry_container["kind"] in SINGLE_CONTAINER_KINDS and len(lines) <= 1:
            bins, assumed = 1, True   # one skip / roll-on-off exchange, no count printed
        else:
            bins = None
        review = []
        if needs_review(fields["collection_date"], date["review"] or bool(date_warning)):
            review.append("collection_date")
        label = fields["ticket_number_label"]["value"] or ""
        if needs_review(fields["ticket_number"], bool(_NOT_A_TICKET_LABEL.search(label))):
            review.append("waste_ticket_number")
        if needs_review(fields["waste_carrier"], carrier["review"]):
            review.append("waste_carrier")
        if waste["review"] or not line or (line["ewc_code"]["image"] or line["description"]["image"]) or \
                (needs_review(line["ewc_code"]) and needs_review(line["description"])):
            review.append("waste_type")
        if not entry_container["kind"] or (entry_container is container and needs_review(fields["container"])):
            review.append("storage_type")
        # Line counts that add up to the total printed on the note confirm
        # each other, even when read from handwriting.
        if bins is None or bins > 20 or assumed or totals or \
                (line_count is None and quantity["value"] is not None and needs_review(fields["quantity"])) or \
                (line is not None and line_count is not None and line["quantity"]["image"] and not totals_agree):
            review.append("bins_collected")
        wt = waste["type"]
        hazardous = normalize.is_hazardous(wt["id"] if wt else None, line["ewc_code"]["value"] if line else None)
        entries.append({
            "collection_date": date["iso"],
            "waste_ticket_number": fields["ticket_number"]["value"],
            "waste_carrier": carrier["carrier"] or fields["waste_carrier"]["value"],
            "waste_type": wt["label"] if wt else None,
            "waste_type_id": wt["id"] if wt else None,
            "ewc_code": waste["ewc"] or (wt["ewc"] if wt else None),
            "storage_type": entry_container["label"],
            "bins_collected": bins,
            "bins_assumed": assumed,
            # Fit-out or strip-out: not printed on tickets, chosen by the reviewer.
            "waste_category": None,
            "hazardous": hazardous,
            "retain_until": normalize.retention_until(date["iso"], hazardous),
            "review": review + ["waste_category"],
            "suspect": li in suspects,
            "match": {
                "waste_type": {"method": waste["method"], "score": waste["score"]},
                "carrier": {"score": carrier["score"]},
            },
        })
    pages = sorted({b["page"] for f in list(fields.values()) + [v for l in lines for k, v in l.items() if k in LINE_FIELDS]
                    for b in f["boxes"] if b.get("page")} | ({ticket["page"]} if ticket["page"] else set()))
    return {
        "index": index,
        "page": ticket["page"],
        "pages": pages,
        "image_pass": bool(ticket.get("image_pass")),
        "tick_table": bool(ticket.get("tick_table")),
        "totals_agree": totals_agree,
        "checks": checks,
        "fields": fields,
        "lines": lines,
        "entries": entries,
    }


def process_document(path, progress=None):
    _ensure_sdk()
    images = PageImages(path)
    with _sdk_lock:
        settings = _document_settings()
        doc = ns.Document.open(path, settings)
        try:
            page_count = doc.get_page_count()
            if progress is not None:
                progress("Reading the note", 0, 2)
            vision = ns.Vision.set(doc)
            request = ns.StructuredExtractionRequest()
            request.schema = json.dumps(ticket_schema.build_schema())
            request.instructions = ticket_schema.INSTRUCTIONS
            result = _clean(json.loads(vision.extract_structured(request)))
        finally:
            doc.close()

        extraction = result.get("extraction") or {}
        metadata = result.get("metadata") or {}
        raw_tickets = [t for t in (extraction.get("tickets") or []) if isinstance(t, dict)]
        ticket_metas = metadata.get("tickets") if isinstance(metadata.get("tickets"), list) else []
        tickets = [_text_ticket(data, ticket_metas[i] if i < len(ticket_metas) else {})
                   for i, data in enumerate(raw_tickets)]
        tickets = _dedupe(tickets)

        dims = {p.get("page"): (p.get("width"), p.get("height")) for p in result.get("pages") or []
                if p.get("width") and p.get("height")}
        image_pages = 0
        try:
            if IMAGE_PASS and tickets:
                todo = []
                for i, ticket in enumerate(tickets):
                    if _needs_image_pass(ticket):
                        todo.append((ticket, ticket["page"] or (i + 1 if i + 1 <= page_count else 1)))
                readings = _read_pages(images, dims, sorted({p for _, p in todo}), progress)
                for ticket, page in todo:
                    if readings.get(page):
                        image_pages += 1
                        _merge_image(ticket, readings[page], page, dims)
            elif IMAGE_PASS and not tickets:
                readings = _read_pages(images, dims, list(range(1, page_count + 1)), progress)
                for page, data in sorted(readings.items()):
                    if data and (data.get("ticket_number") or data.get("rows") or data.get("waste_lines")):
                        image_pages += 1
                        tickets.append(_image_ticket(data, page, dims))
        finally:
            images.close()

    built = [_finish_ticket(i, t) for i, t in enumerate(tickets)]
    entries = [e for t in built for e in t["entries"]]
    return {
        "page_count": page_count,
        "pages": result.get("pages") or [],
        "tickets": built,
        "summary": {
            "tickets": len(built),
            "entries": len(entries),
            # The waste category (fit-out / strip-out) is always the reviewer's
            # choice; "to review" counts the values the reader is unsure of.
            "to_review": sum(1 for e in entries if [r for r in e["review"] if r != "waste_category"]),
            "image_pages": image_pages,
            "image_pass_error": images.error,
        },
    }
