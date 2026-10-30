"""Generates the fictitious waste tickets in inputs/.

Every company, site, person, number and registration on these tickets is
invented. Each ticket is drawn as a paper form, then turned into a phone
photo of it: slightly rotated and keystoned on a desk, with uneven light,
sensor noise and JPEG compression. The photos are saved as one PDF per
ticket, which is what the reader accepts.

    pip install -r samples/requirements.txt
    python samples/generate_tickets.py
"""

import io
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent.parent / "inputs"
DPI = 150
W, H = int(8.27 * DPI), int(11.0 * DPI)  # A4 width, a slightly short sheet

FONT_CANDIDATES = {
    "sans": ["/System/Library/Fonts/Supplemental/Arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "C:/Windows/Fonts/arial.ttf"],
    "bold": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "mono": ["/System/Library/Fonts/Supplemental/Courier New.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "C:/Windows/Fonts/cour.ttf"],
    "hand": ["/System/Library/Fonts/Supplemental/Bradley Hand Bold.ttf", "/System/Library/Fonts/Noteworthy.ttc",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf", "C:/Windows/Fonts/segoepr.ttf"],
}
_fonts = {}


def font(kind, size):
    key = (kind, size)
    if key not in _fonts:
        for path in FONT_CANDIDATES[kind]:
            if Path(path).exists():
                _fonts[key] = ImageFont.truetype(path, size)
                break
        else:
            _fonts[key] = ImageFont.load_default(size=size)
    return _fonts[key]


INK = (25, 30, 45)
HAND_INK = (20, 40, 120)   # blue ballpoint
RED = (170, 30, 35)


class Sheet:
    """A paper form drawn with Pillow, in pixels at DPI."""

    def __init__(self, paper=(252, 251, 246)):
        self.img = Image.new("RGB", (W, H), paper)
        self.d = ImageDraw.Draw(self.img)

    def text(self, x, y, s, size=22, kind="sans", fill=INK, anchor="la"):
        self.d.text((x, y), s, font=font(kind, size), fill=fill, anchor=anchor)

    def hand(self, x, y, s, size=30, rotate=0.0):
        """Handwriting: drawn on its own layer so it can sit a little crooked."""
        f = font("hand", size)
        box = self.d.textbbox((0, 0), s, font=f)
        layer = Image.new("RGBA", (box[2] + 20, box[3] + 20), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((10, 10), s, font=f, fill=HAND_INK + (235,))
        layer = layer.rotate(rotate, resample=Image.BICUBIC, expand=True)
        self.img.paste(layer, (int(x), int(y)), layer)

    def box(self, x0, y0, x1, y1, width=2, fill=None):
        self.d.rectangle((x0, y0, x1, y1), outline=INK, width=width, fill=fill)

    def hline(self, x0, x1, y, width=2):
        self.d.line((x0, y, x1, y), fill=INK, width=width)

    def vline(self, x, y0, y1, width=2):
        self.d.line((x, y0, x, y1), fill=INK, width=width)

    def field(self, x, y, w, label, value=None, size=21, hand=False):
        """A labelled box with a printed (or handwritten) value."""
        self.box(x, y, x + w, y + 64)
        self.text(x + 8, y + 6, label, size=15, kind="bold")
        if value:
            if hand:
                self.hand(x + 10, y + 18, value, size=30, rotate=random.uniform(-2, 2))
            else:
                self.text(x + 10, y + 30, value, size=size)

    def tick(self, x, y, size=26):
        pts = [(x, y + size * 0.55), (x + size * 0.35, y + size), (x + size * 1.1, y - size * 0.1)]
        self.d.line(pts, fill=HAND_INK, width=5, joint="curve")

    def signature(self, x, y, w=220):
        pts, cx = [], x
        for i in range(14):
            cx += w / 14
            pts.append((cx, y + random.uniform(-16, 16) + (8 if i % 3 == 0 else 0)))
        self.d.line(pts, fill=HAND_INK, width=3, joint="curve")


def letterhead(s, name, lines, title, number_label, number):
    s.text(70, 60, name, size=40, kind="bold")
    for i, line in enumerate(lines):
        s.text(70, 112 + i * 26, line, size=18)
    s.box(W - 430, 60, W - 70, 190, width=3)
    s.text(W - 250, 76, title, size=24, kind="bold", anchor="ma")
    s.text(W - 410, 128, number_label, size=20, kind="bold")
    # Long consignment codes go on their own line under the label.
    if len(number) > 8:
        s.text(W - 90, 152, number, size=28, kind="mono", fill=RED, anchor="ra")
    else:
        s.text(W - 90, 122, number, size=34, kind="mono", fill=RED, anchor="ra")


def declaration(s, y, driver, producer):
    s.text(70, y, "DECLARATION: I confirm that I have fulfilled my duty to apply the waste hierarchy as required by",
           size=16)
    s.text(70, y + 22, "Regulation 12 of the Waste (England and Wales) Regulations 2011, and that the information given",
           size=16)
    s.text(70, y + 44, "on this note is correct.", size=16)
    s.text(70, y + 90, "Transferor (producer) signature", size=16, kind="bold")
    s.text(W / 2 + 20, y + 90, "Transferee (carrier) signature", size=16, kind="bold")
    s.hline(70, W / 2 - 40, y + 170)
    s.hline(W / 2 + 20, W - 70, y + 170)
    s.signature(90, y + 140)
    s.signature(W / 2 + 40, y + 140)
    s.hand(90, y + 180, producer, size=26)
    s.hand(W / 2 + 40, y + 180, driver, size=26)
    small_print(s)


def small_print(s):
    lines = [
        "CONDITIONS. The customer warrants that the waste described above is accurately described and contains no",
        "hazardous, liquid or prohibited items unless stated. Containers must not be filled above the rim. The carrier",
        "may refuse or reclassify any load that does not match its description; additional charges may apply.",
        "Keep this note for at least two years (three years for hazardous waste consignment notes).",
    ]
    y = H - 230
    s.hline(70, W - 70, y - 16, width=1)
    for i, line in enumerate(lines):
        s.text(70, y + i * 22, line, size=15)


# --------------------------------------------------------------- tickets

def skip_ticket():
    """Printed note for one skip exchange: one waste line, one container."""
    s = Sheet()
    letterhead(s, "Brackenford Skip Hire Ltd",
               ["Unit 7, Millbrook Trading Estate, Northbridge NB2 6LF", "Tel 0100 555 0142",
                "Waste carrier registration CBDU000123"],
               "WASTE TRANSFER NOTE", "Ticket No", "104271")
    y = 240
    s.field(70, y, 360, "Date", "14/10/2026")
    s.field(440, y, 360, "Account No", "BRK-2291")
    s.field(810, y, W - 880, "Vehicle reg", "KD71 WXR")
    y += 90
    s.field(70, y, W - 140, "Customer", "Northbridge Interiors Ltd (main contractor)")
    y += 80
    s.field(70, y, W - 140, "Site address", "Harbour Point offices, 12 Tannery Lane, Northbridge NB4 2QT")
    y += 100
    s.text(70, y, "DESCRIPTION OF WASTE", size=22, kind="bold")
    y += 36
    cols = [70, 560, 760, 990, W - 70]
    s.box(cols[0], y, cols[-1], y + 110)
    s.hline(cols[0], cols[-1], y + 40)
    for x in cols[1:-1]:
        s.vline(x, y, y + 110)
    for x, label in zip(cols, ["Waste description", "EWC code", "Container", "Qty"]):
        s.text(x + 10, y + 10, label, size=18, kind="bold")
    s.text(cols[0] + 10, y + 60, "Mixed Construction & Demolition", size=21)
    s.text(cols[1] + 10, y + 60, "17 09 04", size=21)
    s.text(cols[2] + 10, y + 60, "8 Yard Enclosed Skip", size=19)
    s.text(cols[3] + 10, y + 60, "1", size=21)
    y += 140
    s.field(70, y, 520, "SIC code of producer", "41201")
    s.field(600, y, W - 670, "Weighbridge net weight", "1.84 T")
    y += 90
    s.field(70, y, 520, "Disposal / transfer site", "Millbrook Transfer Station")
    s.field(600, y, W - 670, "Permit number", "EPR/XX0000AA")
    y += 110
    declaration(s, y, "J. Pell (driver)", "R. Okafor")
    s.text(W / 2, H - 60, "White copy: customer   Yellow copy: carrier   Pink copy: office", size=15, anchor="ma")
    return [s.img]


def summary_ticket():
    """Several materials on one note, each with its own bin size column."""
    s = Sheet(paper=(255, 251, 226))  # yellow carbonless copy
    letterhead(s, "Greywell Waste Services Ltd",
               ["Commercial waste and recycling collections", "4 Greywell Road, Eastwick EW5 1RB",
                "Upper tier carrier CBDU000456"],
               "Waste Transfer Note", "Note No.", "5581093")
    y = 240
    s.field(70, y, 330, "Collection date", "21-10-2026")
    s.field(410, y, 330, "Customer order no.", "PO-44821")
    s.field(750, y, W - 820, "Vehicle", "GW19 RTA")
    y += 90
    s.field(70, y, W - 140, "Customer & site",
            "Northbridge Interiors Ltd - Level 3 fit-out, Quayside House, 40 Dock Street, Northbridge NB1 7HS",
            size=18)
    y += 100
    s.text(70, y, "Summary of the waste removed", size=22, kind="bold")
    y += 36
    cols = [70, 480, 660, 820, 980, W - 70]
    rows = [("Cardboard", "15 01 01", "", "", "2"), ("Plasterboard", "17 08 02", "", "3", ""),
            ("Insulation", "17 06 04", "4", "", ""), ("Mixed metals", "17 04 07", "", "1", "")]
    height = 44 * (len(rows) + 1)
    s.box(cols[0], y, cols[-1], y + height)
    for x in cols[1:-1]:
        s.vline(x, y, y + height)
    for i in range(1, len(rows) + 1):
        s.hline(cols[0], cols[-1], y + 44 * i, width=1)
    for x, label in zip(cols, ["Material", "EWC", "240ltr", "660ltr", "1100ltr"]):
        s.text(x + 10, y + 12, label, size=18, kind="bold")
    for r, row in enumerate(rows, start=1):
        for x, value in zip(cols, row):
            s.text(x + 10, y + 44 * r + 11, value, size=20)
    y += height + 30
    s.field(70, y, 520, "Type of load", "Mixed recyclables")
    s.field(600, y, W - 670, "Total containers", "10")
    y += 90
    s.field(70, y, W - 140, "Destination", "Greywell Materials Recovery Facility, Eastwick")
    y += 110
    declaration(s, y, "M. Daley", "S. Whitcombe")
    return [s.img]


def hazardous_ticket():
    """Hazardous waste consignment note with asterisked EWC codes."""
    s = Sheet(paper=(250, 250, 250))
    letterhead(s, "Halvergate Environmental Ltd",
               ["Specialist hazardous waste collections", "Halvergate Works, Fen Road, Marshby MB3 9DD",
                "Carrier registration CBDU000789"],
               "HAZARDOUS WASTE", "Consignment", "HALV/HZ7731A")
    s.text(W - 250, 104, "CONSIGNMENT NOTE", size=16, kind="bold", anchor="ma")
    y = 240
    s.text(70, y, "Part A  Notification details", size=20, kind="bold")
    y += 34
    s.field(70, y, 420, "Date of removal", "23 Oct 2026")
    s.field(500, y, W - 570, "Premises code", "NBINT1")
    y += 84
    s.field(70, y, W - 140, "Address of origin of waste",
            "Northbridge Interiors Ltd, Harbour Point offices, 12 Tannery Lane, Northbridge NB4 2QT", size=18)
    y += 94
    s.text(70, y, "Part B  Description of the waste", size=20, kind="bold")
    y += 34
    cols = [70, 470, 660, 860, W - 70]
    rows = [("Fluorescent tubes", "20 01 21*", "Glass tubes", "42 kg"),
            ("WEEE - small electricals", "20 01 35*", "Mixed", "118 kg")]
    height = 46 * (len(rows) + 1)
    s.box(cols[0], y, cols[-1], y + height)
    for x in cols[1:-1]:
        s.vline(x, y, y + height)
    for i in range(1, len(rows) + 1):
        s.hline(cols[0], cols[-1], y + 46 * i, width=1)
    for x, label in zip(cols, ["Description", "EWC code", "Form", "Quantity"]):
        s.text(x + 10, y + 12, label, size=18, kind="bold")
    for r, row in enumerate(rows, start=1):
        for x, value in zip(cols, row):
            s.text(x + 10, y + 46 * r + 12, value, size=19)
    y += height + 30
    s.field(70, y, 520, "Container type and number", "Caged lorry x 1")
    s.field(600, y, W - 670, "Vehicle registration", "HV68 LUX")
    y += 90
    s.text(70, y, "Part C  Carrier's certificate", size=20, kind="bold")
    y += 34
    s.field(70, y, W - 140, "Carrier name", "Halvergate Environmental Ltd")
    y += 110
    declaration(s, y, "T. Brannigan", "R. Okafor")
    return [s.img]


def tick_box_ticket():
    """Handwritten note on a pre-printed tick-box list of materials."""
    s = Sheet(paper=(255, 240, 243))  # pink carbonless copy
    letterhead(s, "Copperfield Compactor Hire Ltd",
               ["Compactors, wheelie bins and recycling", "Yard 3, Canal Side, Westford WF8 2PL",
                "Carrier registration CBDU000654"],
               "WASTE TRANSFER NOTE", "No.", "WT-20417")
    y = 240
    s.field(70, y, 330, "DATE", "27/10/26", hand=True)
    s.field(410, y, 330, "BIN SIZE", "660", hand=True)
    s.field(750, y, W - 820, "VEHICLE", "CC21 BNX", hand=True)
    y += 90
    s.field(70, y, W - 140, "SITE", "Quayside House, 40 Dock St, Northbridge", hand=True)
    y += 100
    s.text(70, y, "TICK THE WASTE COLLECTED AND ENTER THE NUMBER OF CONTAINERS", size=19, kind="bold")
    y += 34
    left = [("General mixed", "17 09 04"), ("Cardboard", "15 01 01"), ("Paper", "20 01 01"),
            ("Plastics", "17 02 03"), ("Wood", "17 02 01"), ("Metals", "17 04 07")]
    right = [("Plasterboard", "17 08 02"), ("Glass", "17 02 02"), ("Insulation", "17 06 04"),
             ("Canteen waste", "20 01 08"), ("Textiles", "20 01 11"), ("Ceiling tiles", "17 09 04")]
    marks = {"General mixed": ("2", True), "Cardboard": ("3", True), "Plastics": (None, True)}
    block_w = (W - 160) // 2
    for bx, block in ((70, left), (90 + block_w, right)):
        cols = [bx, bx + 250, bx + 400, bx + 480, bx + block_w]
        s.box(cols[0], y, cols[-1], y + 46 * 7)
        for x in cols[1:-1]:
            s.vline(x, y, y + 46 * 7)
        for i in range(1, 7):
            s.hline(cols[0], cols[-1], y + 46 * i, width=1)
        for x, label in zip(cols, ["Material", "EWC", "QTY", "Tick"]):
            s.text(x + 8, y + 12, label, size=17, kind="bold")
        for r, (name, ewc) in enumerate(block, start=1):
            s.text(cols[0] + 8, y + 46 * r + 12, name, size=19)
            s.text(cols[1] + 8, y + 46 * r + 12, ewc, size=19)
            qty, ticked = marks.get(name, (None, False))
            if qty:
                s.hand(cols[2] + 18, y + 46 * r - 2, qty, size=34, rotate=random.uniform(-6, 6))
            if ticked:
                s.tick(cols[3] + 22, y + 46 * r + 8)
    y += 46 * 7 + 30
    s.field(70, y, 520, "TOTAL CONTAINERS", "6 x 660", hand=True)
    s.field(600, y, W - 670, "CUSTOMER", "Northbridge Interiors", hand=True)
    y += 110
    declaration(s, y, "D. Harker", "S. Whitcombe")
    return [s.img]


def two_copies_ticket():
    """Driver copy and customer copy of the same note scanned as two pages."""
    pages = []
    for copy, paper in (("DRIVER COPY", (252, 251, 246)), ("CUSTOMER COPY", (255, 251, 226))):
        s = Sheet(paper=paper)
        letterhead(s, "Lindenmoor Recycling Ltd",
                   ["Roll-on/off and grab hire", "Lindenmoor Quarry Lane, Hatchley HY6 3NE",
                    "Waste carrier licence CBDU000321"],
                   "WASTE TRANSFER NOTE", "Job Number", "88412")
        s.text(70, 200, copy, size=22, kind="bold", fill=RED)
        y = 240
        s.field(70, y, 360, "Date", "28/10/2026")
        s.field(440, y, 360, "Vehicle", "LM20 HKS")
        s.field(810, y, W - 880, "Order ref", "NI-5532")
        y += 90
        s.field(70, y, W - 140, "Delivery address",
                "Northbridge Interiors Ltd, Riverside Quarter strip-out, 3 Wharf Road, Northbridge NB3 8AF", size=18)
        y += 100
        s.text(70, y, "WASTE DETAILS", size=22, kind="bold")
        y += 36
        s.field(70, y, 600, "Description of waste", "Wood - stud partitions and doors")
        s.field(680, y, W - 750, "EWC code", "17 02 01")
        y += 84
        s.field(70, y, 600, "Container", "35YD Roll-on-off")
        s.field(680, y, W - 750, "No of containers", "1")
        y += 84
        s.field(70, y, 600, "Disposal site", "Hatchley Wood Recycling")
        s.field(680, y, W - 750, "Weight", "3.20 T")
        y += 110
        declaration(s, y, "A. Varga", "P. Linnell")
        pages.append(s.img)
    return pages


# ---------------------------------------------------------- photo effect

def photograph(sheet, seed):
    """Turns a flat sheet into a phone photo of it lying on a desk."""
    rng = np.random.default_rng(seed)
    angle = float(rng.uniform(1.2, 3.5)) * (1 if seed % 2 else -1)
    margin = 90
    bg_w, bg_h = W + 2 * margin, H + 2 * margin

    # Keystone: the far edge of the sheet looks a little narrower.
    inset = int(rng.uniform(10, 26))
    quad = (-inset, 0, -inset // 3, H, W + inset // 3, H, W + inset, 0)
    paper = sheet.convert("RGBA").transform((W, H), Image.QUAD, quad, resample=Image.BICUBIC,
                                            fillcolor=(0, 0, 0, 0))
    paper = paper.rotate(angle, resample=Image.BICUBIC, expand=True)

    desk = np.zeros((bg_h, bg_w, 3), dtype=np.float32)
    desk[:] = (118, 104, 92)
    desk += rng.normal(0, 6, desk.shape)
    photo = Image.fromarray(np.clip(desk, 0, 255).astype(np.uint8))
    # Soft shadow under the sheet.
    shadow = Image.new("RGBA", paper.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 110), mask=paper.split()[3])
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    ox, oy = (bg_w - paper.width) // 2, (bg_h - paper.height) // 2
    photo.paste(shadow, (ox + 10, oy + 14), shadow)
    photo.paste(paper, (ox, oy), paper)

    # Uneven light: brighter towards one corner, plus a faint vignette.
    a = np.asarray(photo).astype(np.float32)
    yy, xx = np.mgrid[0:bg_h, 0:bg_w].astype(np.float32)
    cx, cy = rng.uniform(0.2, 0.8) * bg_w, rng.uniform(0.1, 0.4) * bg_h
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / np.hypot(bg_w, bg_h)
    light = 1.06 - 0.32 * dist
    a *= light[..., None]
    a += rng.normal(0, 7.5, a.shape)  # sensor noise
    photo = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    photo = photo.filter(ImageFilter.GaussianBlur(0.7))

    buf = io.BytesIO()
    photo.save(buf, "JPEG", quality=72)
    return Image.open(io.BytesIO(buf.getvalue()))


def save_pdf(name, pages, seed):
    photos = [photograph(p, seed + i) for i, p in enumerate(pages)]
    path = OUT / name
    photos[0].save(path, "PDF", resolution=DPI, save_all=True, append_images=photos[1:])
    print(f"{path.name}: {len(photos)} page(s), {path.stat().st_size // 1024} KB")


def main():
    random.seed(7)
    OUT.mkdir(exist_ok=True)
    save_pdf("brackenford-skip-104271.pdf", skip_ticket(), 11)
    save_pdf("greywell-mixed-recycling-5581093.pdf", summary_ticket(), 22)
    save_pdf("halvergate-hazardous-consignment.pdf", hazardous_ticket(), 33)
    save_pdf("copperfield-tick-box-WT-20417.pdf", tick_box_ticket(), 44)
    save_pdf("lindenmoor-driver-and-customer-copy-88412.pdf", two_copies_ticket(), 55)


if __name__ == "__main__":
    main()
