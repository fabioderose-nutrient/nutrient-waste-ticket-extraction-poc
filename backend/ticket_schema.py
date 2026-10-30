"""Extraction schema for UK waste transfer notes and hazardous waste
consignment notes, aligned with the waste collection record of a site waste
register.

A document may hold several distinct notes (one per page, or a driver copy
and a customer copy of the same note), so the root is a list of tickets and
every ticket holds a list of waste lines (one per waste type collected).
"""


def _field(description):
    return {"type": ["string", "null"], "description": description}


WASTE_LINE = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "description": _field(
            "the waste description / material for this line as printed (e.g. 'Mixed Construction & "
            "Demolition', 'General builders waste', 'Mixed metals', 'Plasterboard')"),
        "ewc_code": _field(
            "the European Waste Catalogue (EWC) code of this line, six digits as printed (e.g. '17 09 04', "
            "'170904', '20 01 21*'); keep the asterisk when printed"),
        "quantity": _field(
            "the quantity stated for this line only, as printed: a number of bins / containers (e.g. '4 bins', "
            "'2') or a weight (e.g. '1800 Kg', '1.260 T'); null when the line has none"),
        "container": _field(
            "the container size given for this line when the table has one per line (e.g. a '240ltr' column "
            "with a count in it); null when the note states the container once for the whole ticket"),
    },
}

TICKET = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ticket_number": _field(
            "the reference number of this waste transfer note: the field labelled Ticket No, Note No, Advice No, "
            "Job Number or Consignment number, digits and letters only without the label (e.g. '104271', "
            "'5581093', 'WT-20417', 'HZ/7731A'), taken from the note's header. Never the account "
            "number, customer or order number, SIC code, licence number, weighbridge ticket, a weight or a "
            "quantity printed in a disposal section"),
        "ticket_number_label": _field(
            "the label printed right next to the number returned as ticket_number, exactly as printed (e.g. "
            "'Ticket No', 'No.', 'Advice No', 'Job Number', 'Docket no.', 'Account No', 'Quantity')"),
        "collection_date": _field(
            "the date of the collection / transfer as printed on the note (e.g. '15/07/2025', '16-9-26', "
            "'2026-07-03'); when several dates are printed prefer the collection or ticket date over the "
            "creation or review date"),
        "waste_carrier": _field(
            "the company name of the waste carrier that issued this note and collected the waste, as printed "
            "in its letterhead (e.g. 'Greywell Waste Services Ltd', 'Copperfield Compactor Hire Ltd'); never "
            "the customer (the main contractor the waste is collected for) and never the site"),
        "carrier_licence": _field(
            "the waste carrier's licence / registration number (e.g. 'CBDU000123', 'CB/DU000456')"),
        "site": _field(
            "the site the waste was removed from: the customer's site / project name and address as printed "
            "(labelled Site Address, Delivery Address, Address of origin of waste, Customer & Site; e.g. "
            "'Harbour Point offices, 12 Tannery Lane, Northbridge'). Never the carrier's own address and "
            "never the disposal site, transfer station or tip the waste is taken to"),
        "container": _field(
            "the container / storage type and size as printed (e.g. '12 Yard Enclosed Skip', 'EURO660', "
            "'35YD', 'IBC x 1', 'Bin Size: 660', 'Roll-on-off', 'Wait & Load', 'Caged Lorry'); it may only appear "
            "in the order or product details, e.g. 'SK 8 (Open)' or 'BIN 660 (Wheelie)'"),
        "quantity": _field(
            "the number of containers / bins / skips collected on this note, as printed (e.g. 'Qty 1', "
            "'No of Containers: 1', 'Quantity: 2', '9 x 660' meaning nine 660 bins). Never a weight in kg or "
            "tonnes, a price, a time or a tally; null when not stated"),
        "vehicle": _field("the vehicle registration as printed (e.g. 'AB12 CDE')"),
        "waste_lines": {
            "type": "array",
            "items": WASTE_LINE,
            "description": (
                "one element per waste type recorded on this note: every row of a 'summary of the waste "
                "removed' table (material, EWC code, number of bins, bin size column) is its own element, even "
                "when a 'type of load' or a total is also printed. On tick-box tables listing many materials "
                "with EWC codes, return ONLY the rows that carry a tick, a quantity or a handwritten mark; "
                "rows that are empty or crossed out are not collected. Never return the whole printed list. "
                "A row with a tick but no number in its QTY cell is one container (quantity '1')."),
        },
    },
}


def build_schema():
    return {"schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tickets": {
                "type": "array",
                "items": TICKET,
                "description": (
                    "one element per DISTINCT waste transfer note or consignment note in the document. A "
                    "note printed twice (for example a 'Driver Copy' and a 'Customer Copy' carrying the same "
                    "ticket number) is ONE ticket: return it once. A multi-page document can hold several "
                    "different notes with different ticket numbers: return each of them."),
            }
        },
    }}


INSTRUCTIONS = (
    "You are extracting UK waste transfer notes (WTN) and hazardous waste consignment notes issued by waste "
    "carriers to a main contractor, the customer and waste producer. The values feed the contractor's site "
    "waste management plan, so copy them exactly as printed or handwritten; read handwriting carefully and return your best "
    "reading rather than null when something is written. Return null only when a field is genuinely absent "
    "from the note.\n\n"
    "Do not confuse the parties: the waste carrier is the company whose letterhead is on the note and who "
    "collects the waste; the customer is the main contractor; the site is where the waste comes from.\n\n"
    "Tick-box waste tables: the note pre-prints a list of materials with their EWC codes; only the rows with a "
    "tick, a quantity or a mark next to them were actually collected. The Tick column says the waste is there and "
    "the QTY column says how many containers; a tick without a number is one container. A diagonal line drawn "
    "across the table crosses out the unused rows. Return only the collected rows.\n\n"
    "Summary tables listing several materials with their EWC codes and bin counts (e.g. Insulation 17 06 04, "
    "4 bins in the 240ltr column) give one waste line per row, each with its own count and bin size; do not "
    "collapse them into the 'type of load' or the total.\n\n"
    "Notes printed twice on one page or across pages with the same ticket number (driver copy / customer copy) "
    "count once. Two different notes can also be scanned side by side or one above the other on the same page: "
    "return each of them with its own ticket number. A weighbridge or disposal-point receipt attached for the same load (same docket or ticket "
    "number, or a photo of a till receipt from the transfer station) is part of that ticket, not a separate one."
)
