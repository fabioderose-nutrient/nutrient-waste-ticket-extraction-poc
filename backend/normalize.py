"""Deterministic normalisation of extracted ticket values towards the
waste collection record of the site waste register: EWC code to waste type, carrier
name to the carrier list, dates, quantities and container types.

Every match carries a score and a `review` flag so the reviewer's attention
goes to the values the tool is not sure about.
"""

import difflib
import json
import re
from datetime import datetime
from pathlib import Path

REFERENCE = Path(__file__).resolve().parent / "reference"
WASTE_TYPES = json.loads((REFERENCE / "waste_types.json").read_text())
CARRIERS = json.loads((REFERENCE / "carriers.json").read_text()) if (REFERENCE / "carriers.json").exists() else []

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean(text):
    if text is None:
        return None
    text = _CONTROL.sub("", str(text))
    return re.sub(r"\s+", " ", text).strip() or None


def _words(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split()


def _similarity(a, b):
    a, b = " ".join(_words(a)), " ".join(_words(b))
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# Ticket wording that maps onto the vocabulary of the waste types list.
_SYNONYMS = {
    "builder": "general", "builders": "general", "construction": "general", "demolition": "general",
    "c&d": "general", "rubble": "hardcore", "muck": "soil", "earth": "soil", "timber": "wood",
    "scrap": "metals", "metal": "metals", "hardcore": "concrete", "cardboard": "cardboard",
    "board": "plasterboard", "gypsum": "plasterboard", "fridges": "refrigerant", "fridge": "refrigerant",
    "freezers": "refrigerant", "tubes": "fluorescent", "lamps": "fluorescent", "weee": "electronics",
    "electrical": "electronics", "electrics": "electronics", "oil": "oil", "paints": "paint",
    "canteen": "canteen", "food": "canteen", "carpet": "textiles", "carpets": "textiles",
}
_STOP = {"waste", "and", "of", "the", "inc", "incl", "including", "with", "mixed", "type", "other"}


def _tokens(text):
    out = set()
    for w in _words(text):
        w = _SYNONYMS.get(w, w)
        if w in _STOP or len(w) < 3:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def _description_similarity(description, label):
    """Word-overlap similarity between a ticket description and a waste type
    label; character similarity alone is misled by long common words."""
    a, b = _tokens(description), _tokens(label)
    mixed_bonus = 0.15 if "mixed" in _words(description) and "mixed" in _words(label) else 0.0
    if not a or not b:
        return mixed_bonus
    overlap = len(a & b) / len(a | b)
    return min(1.0, max(overlap + mixed_bonus, 0.6 * _similarity(description, label)))


# ------------------------------------------------------------------ EWC

def normalise_ewc(text):
    """'170904' / '17-09-04' / '17 09 04*' -> ('17 09 04', hazardous)."""
    text = clean(text) or ""
    hazardous = "*" in text
    digits = re.sub(r"\D", "", text)
    if len(digits) != 6:
        return None, hazardous
    return f"{digits[0:2]} {digits[2:4]} {digits[4:6]}", hazardous


def match_waste_type(ewc_text, description):
    """Map an extracted (EWC code, description) pair onto a waste type of the list."""
    code, hazardous = normalise_ewc(ewc_text)
    description = clean(description) or ""
    result = {"ewc": code, "hazardous_marker": hazardous, "type": None, "method": None, "score": 0.0, "review": True}

    def label_similarity(t):
        # the label carries the code in brackets; compare the wording only
        return _description_similarity(description, re.sub(r"\(.*?\)", "", t["label"]))

    if code:
        # Lowest id first so the generic type (General mixed waste, id 1) wins ties.
        same_code = sorted((t for t in WASTE_TYPES if normalise_ewc(t["ewc"])[0] == code), key=lambda t: t["id"])
        if len(same_code) == 1:
            t = same_code[0]
            result.update(type=t, method="ewc", score=1.0, review=False)
            return result
        if len(same_code) > 1:
            best = max(same_code, key=label_similarity)
            score = label_similarity(best)
            result.update(type=best, method="ewc+description", score=round(score, 2),
                          review=score < 0.35 and bool(description))
            return result

    # Unknown or missing code: nearest description, preferring the same EWC chapter.
    if description or code:
        scored = []
        for t in WASTE_TYPES:
            s = label_similarity(t) if description else 0.0
            if code and t["ewc"][:5] == code[:5]:
                s += 0.25
            elif code and t["ewc"][:2] == code[:2]:
                s += 0.1
            scored.append((s, t))
        score, best = max(scored, key=lambda x: x[0])
        if score >= 0.45:
            result.update(type=best, method="nearest" if code else "description", score=round(min(score, 1.0), 2), review=True)
    return result


# -------------------------------------------------------------- carrier

def match_carrier(name):
    name = clean(name)
    result = {"raw": name, "carrier": None, "score": 0.0, "review": True}
    if not name or not CARRIERS:
        return result
    best, best_score = None, 0.0
    for c in CARRIERS:
        for alias in [c["name"]] + c.get("aliases", []):
            s = _similarity(name, alias)
            if s > best_score:
                best, best_score = c, s
    if best and best_score >= 0.55:
        result.update(carrier=best["name"], score=round(best_score, 2), review=best_score < 0.8)
    return result


# ----------------------------------------------------------------- date

_DATE_FORMATS = ["%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%d.%m.%Y", "%d.%m.%y",
                 "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d %b %y", "%d %B %y",
                 "%d-%b-%Y", "%d-%B-%Y", "%d-%b-%y", "%d-%B-%y", "%d/%b/%Y", "%d/%B/%Y"]


def parse_date(text):
    text = clean(text)
    result = {"raw": text, "iso": None, "review": True}
    if not text:
        return result
    candidate = re.split(r"[ T]\d{1,2}:\d{2}", text)[0].strip()  # drop a trailing time
    candidate = re.sub(r"(\d)(st|nd|rd|th)", r"\1", candidate)
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(candidate, fmt)
            if d.year < 100:
                d = d.replace(year=d.year + 2000)
            result.update(iso=d.strftime("%Y-%m-%d"), review=False)
            return result
        except ValueError:
            continue
    m = re.search(r"(\d{1,2})\D{1,3}(\d{1,2})\D{1,3}(\d{2,4})", candidate)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        y = y + 2000 if y < 100 else y
        try:
            result.update(iso=datetime(y, mo, d).strftime("%Y-%m-%d"), review=True)
        except ValueError:
            pass
    return result


# ------------------------------------------------------------ container

_YARD = re.compile(r"(\d{1,2}(?:\.\d)?)\s*(?:yard|yd|yrd)s?\b", re.I)
_LITRE = re.compile(r"\b(\d{3,4})\s*(?:l|ltr|litre|liter)s?\b|\b(?:euro|eurobin|bin(?: size)?[: ]*)\s*(\d{3,4})\b|\b(240|360|660|1100)\b", re.I)


def parse_container(text):
    text = clean(text)
    result = {"raw": text, "kind": None, "size": None, "label": None}
    if not text:
        return result
    low = text.lower()
    kind = None
    if re.search(r"roll[\s-]*on|ro\s*ro\b|hook", low):
        kind = "Roll-on/off"
    elif re.search(r"wait\s*(&|and)\s*load|grab", low):
        kind = "Wait & load"
    elif "ibc" in low:
        kind = "IBC"
    elif "compactor" in low:
        kind = "Compactor"
    elif re.search(r"caged|cage\b", low):
        kind = "Caged lorry"
    elif re.search(r"drum|barrel", low):
        kind = "Drum"
    elif re.search(r"\bbag|sack", low):
        kind = "Bag"
    elif re.search(r"skip|yard|yd\b", low):
        kind = "Skip"
    elif re.search(r"bin|euro|wheelie|\b(240|360|660|1100)\b", low):
        kind = "Bin"
    elif re.search(r"\b\d{1,2}\s*\(?(open|enclosed|enc|closed|covered)\b", low):
        kind = "Skip"
    size = None
    m = _YARD.search(text) or re.search(r"\b(\d{1,2})\s*\(?(?:open|enclosed|enc|closed|covered)\b", text, re.I)
    if m:
        size = f"{m.group(1)} yd"
        kind = kind or "Skip"
    else:
        m = _LITRE.search(text)
        if m:
            size = f"{next(g for g in m.groups() if g)} L"
            kind = kind or "Bin"
    result.update(kind=kind, size=size, label=" ".join(x for x in (kind, size) if x) or text)
    return result


# ------------------------------------------------------------- quantity

_WEIGHT = re.compile(r"\d[\d,]*\.\d+|\b(kg|kgs|t|tonne|tonnes|ton|tons|ltr|litre|litres|l)\b", re.I)


def is_count(text):
    """True for a plain small count ('1', 'Qty 2', '4 bins', 'x 2'), False for weights, prices or decimals."""
    text = clean(text)
    return bool(text) and not _WEIGHT.search(text) and bool(re.fullmatch(
        r"(?:qty|quantity|no\.?|x|×)?[:\s]*\d{1,3}\s*(?:x|×|bins?|skips?|containers?|loads?)?", text, re.I))


def parse_quantity(text, container_text=None):
    """'9 x 660' -> 9, 'Qty 1' -> 1, 'IBC x 1' -> 1, '1' -> 1; weights and decimals are not counts."""
    text = clean(text)
    result = {"raw": text, "value": None, "review": True}
    if text and _WEIGHT.search(text) and not re.search(r"\d+\s*[x×]\s*\d", text):
        return result
    if text:
        m = re.search(r"(\d+)\s*[x×]\s*\d", text) or re.search(r"[x×]\s*(\d+)\b", text) or re.search(r"\b(\d+)\b", text)
    else:
        # From the container wording only an explicit "9 x 660" / "IBC x 1" counts.
        source = clean(container_text) or ""
        m = re.search(r"(\d+)\s*[x×]\s*\d", source) or re.search(r"[x×]\s*(\d+)\b", source)
    if m:
        value = int(m.group(1))
        if 0 < value < 1000:
            result.update(value=value, review=text is None)
    return result


# ------------------------------------------------------- tick-box tables

def tick_row_quantity(qty, ticked):
    """The rule for tick-box waste tables: the Tick column says the
    waste is there, the QTY column says how many containers. A row is
    collected when either is marked; a tick with no number is one container.
    Returns the count as a string, or None when the row was not collected."""
    qty = clean(qty)
    if qty in ("|", "I", "l"):  # a handwritten 1 is often a single stroke
        qty = "1"
    m = re.fullmatch(r"(\d{1,2})\s*[x×]\s*\d{3,4}\s*(?:l|ltr|litres?)?", qty or "", re.I)
    if m:  # '7 x 660': seven 660 L bins
        qty = m.group(1)
    m = re.fullmatch(r"(?:x\s*)?(\d{1,2})(?:\s*x)?", qty or "", re.I)
    if m and int(m.group(1)) > 0:
        return m.group(1)
    if ticked is True or (isinstance(ticked, str) and ticked.strip().lower() in ("true", "yes", "tick", "ticked", "1")):
        return "1"
    return None


def totals_check(line_counts, total):
    """Compares the per-line counts with the total printed on the note (e.g.
    '9 x 660'). Returns None when they agree or cannot be compared, else a
    message for the reviewer."""
    counts = [c for c in line_counts if c is not None]
    if total is None or not counts or len(counts) != len(line_counts):
        return None
    if sum(counts) == total:
        return None
    return f"The waste lines add up to {sum(counts)} containers but the note says {total} in total"


# ------------------------------------------------- retention and checks

# Retention used by the demo: waste transfer notes 2 years, hazardous
# consignment notes 3 years.
RETENTION_YEARS = {"controlled": 2, "hazardous": 3}
WASTE_CATEGORIES = ["Fit-out", "Strip-out"]
STORAGE_KINDS = ["Skip", "Roll-on/off", "Wait & load", "Compactor", "Caged lorry", "Bin", "IBC", "Drum", "Bag"]


def is_hazardous(waste_type_id=None, ewc=None):
    if waste_type_id is not None:
        for t in WASTE_TYPES:
            if t["id"] == waste_type_id:
                return t["category"] == "hazardous"
    return bool(ewc and "*" in ewc)


def retention_until(iso_date, hazardous):
    if not iso_date:
        return None
    try:
        d = datetime.strptime(iso_date, "%Y-%m-%d")
    except ValueError:
        return None
    years = RETENTION_YEARS["hazardous" if hazardous else "controlled"]
    try:
        return d.replace(year=d.year + years).strftime("%Y-%m-%d")
    except ValueError:  # 29 February
        return d.replace(year=d.year + years, day=28).strftime("%Y-%m-%d")


def date_plausibility(iso_date, today=None):
    """A collection date in the future, or older than the longest retention
    period, is almost always a misread handwritten year."""
    if not iso_date:
        return None
    today = today or datetime.now()
    d = datetime.strptime(iso_date, "%Y-%m-%d")
    if (d - today).days > 7:
        return "Date is in the future, the year may be misread"
    if (today - d).days > 365 * max(RETENTION_YEARS.values()):
        return "Date is several years old, the year may be misread"
    return None
