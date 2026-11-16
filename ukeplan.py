#!/usr/bin/env python3
"""
Ukeplan - a printable weekly timetable maker (desktop app, Qt / PySide6).

Run:            python3 ukeplan.py [file.timetable]
Headless export: python3 ukeplan.py file.timetable --export out.pdf   (or out.png)

Everything on the page (screen preview, PDF, printer) is drawn by the same
code (compute_layout + paint), so what you see is what you print.
"""
from __future__ import annotations

import base64
import copy
import hashlib
from html import escape as html_escape
import json
import os
import re
import sys
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import (QBuffer, QByteArray, QIODevice, QMarginsF, QPointF,
                            QRectF, QSettings, QSize, QStandardPaths, Qt,
                            QTextBoundaryFinder, QTime, Signal)
from PySide6.QtGui import (QAbstractTextDocumentLayout, QAction, QColor, QFont,
                           QFontMetricsF, QIcon, QImage, QImageReader,
                           QKeySequence, QPageLayout, QPageSize, QPainter,
                           QPainterPath, QPalette, QPdfWriter, QPen, QPixmap,
                           QTextDocument, QTextOption)
from PySide6.QtPrintSupport import QPrintDialog, QPrinter
from PySide6.QtSvg import QSvgGenerator
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QColorDialog, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QRadioButton, QSizePolicy, QSpinBox,
                               QTableWidget, QTimeEdit, QToolBar, QVBoxLayout,
                               QWidget)

APP_NAME = "Ukeplan"
FILE_EXT = ".timetable"
FILE_FILTER = "Timetable (*.timetable);;All files (*)"

DAYS = ["Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag"]
DAYS_SHORT = ["Man", "Tir", "Ons", "Tor", "Fre", "Lør", "Søn"]

MM = 100                       # layout units per millimetre (1 unit = 0.01 mm)
PAGES = {"A4": (210.0, 297.0), "A3": (297.0, 420.0)}   # portrait size in mm
MIN_SCALE = 0.55               # smallest text scale before a block is flagged

PALETTE = ["#FECACA", "#FED7AA", "#FDE68A", "#BBF7D0", "#A7F3D0", "#BAE6FD",
           "#BFDBFE", "#C7D2FE", "#E9D5FF", "#FBCFE8", "#E5E7EB", "#FFFFFF"]

EMOJI_FONTS = ["Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji",
               "Twemoji Mozilla", "EmojiOne Color", "Symbola"]


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------
def col_letters(i: int) -> str:
    """0 -> A, 1 -> B, ... 25 -> Z, 26 -> AA"""
    out, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


def col_names(names: list) -> list:
    """Names to show: a blank column gets the default 'Column A', 'Column B', ... by position."""
    return [n.strip() or f"Column {col_letters(i)}" for i, n in enumerate(names)]


def fmt_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class Event:
    title: str = ""
    description: str = ""
    day: int = 0                 # 0 = Mandag ... 6 = Søndag
    start: int = 9 * 60          # minutes since midnight
    end: int = 10 * 60
    icon: str = ""               # "" | "emoji:<char>" | "img:<id>"
    color: str = "#BFDBFE"
    hide_end: bool = False       # print only the start time; the end still sets the block size
    col: int = -1                # column within the day (-1 = spans all columns of the day)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def to_dict(self) -> dict:
        return dict(id=self.id, title=self.title, description=self.description,
                    day=self.day, start=self.start, end=self.end,
                    icon=self.icon, color=self.color, hide_end=self.hide_end, col=self.col)

    @staticmethod
    def from_dict(d: dict) -> "Event":
        start = max(0, min(1439, int(d.get("start", 540))))
        end = max(start + 1, min(1440, int(d.get("end", start + 60))))
        return Event(title=str(d.get("title", "")),
                     description=str(d.get("description", "")),
                     day=max(0, int(d.get("day", 0))),
                     start=start, end=end, icon=str(d.get("icon", "")),
                     color=str(d.get("color", "#BFDBFE")),
                     hide_end=bool(d.get("hide_end", False)),
                     col=max(-1, int(d.get("col", -1))),
                     id=str(d.get("id") or uuid.uuid4().hex[:10]))


@dataclass
class Planner:
    title: str = "Ukeplan"
    page: str = "A4"
    landscape: bool = True
    days: list = field(default_factory=lambda: [0, 1, 2, 3, 4])
    auto_range: bool = True
    range_start: int = 8         # hours (used when auto_range is off)
    range_end: int = 16
    events: list = field(default_factory=list)
    subcols: dict = field(default_factory=dict)  # day -> [column names] (blank = default name)
    day_names: list = field(default_factory=lambda: list(DAYS))   # the planner's days
    note: str = ""               # small italic text printed under the timetable
    icons: dict = field(default_factory=dict)   # id -> base64 PNG

    def day_label(self, d: int) -> str:
        return self.day_names[d] if 0 <= d < len(self.day_names) else f"Dag {d + 1}"

    def day_short(self, d: int) -> str:
        name = self.day_label(d)
        if d < len(DAYS) and name == DAYS[d]:
            return DAYS_SHORT[d]
        return name if len(name) <= 6 else name[:5] + "…"

    def page_mm(self) -> tuple:
        w, h = PAGES.get(self.page, PAGES["A4"])
        return (h, w) if self.landscape else (w, h)

    def time_range(self) -> tuple:
        evs = [e for e in self.events if e.day in self.days]
        if self.auto_range:
            if evs:
                lo = min(e.start for e in evs) // 60
                hi = -(-max(e.end for e in evs) // 60)
            else:
                lo, hi = 8, 16
        else:
            lo, hi = self.range_start, self.range_end
        lo = max(0, min(23, lo))
        hi = max(lo + 1, min(24, hi))
        return lo, hi

    def used_icon_ids(self) -> list:
        seen = []
        for e in self.events:
            if e.icon.startswith("img:") and e.icon[4:] not in seen:
                seen.append(e.icon[4:])
        return seen

    def ensure_icons(self) -> None:
        """Copy the image of every icon used by a block into the planner, so the
        saved file is self-contained (large images are linked instead)."""
        for i in self.used_icon_ids():
            if i not in self.icons and i in PREFS.icons:
                self.icons[i] = PREFS.planner_value(i)

    def to_dict(self) -> dict:
        self.ensure_icons()
        used = {e.icon[4:] for e in self.events if e.icon.startswith("img:")}
        return {"format": "ukeplan", "version": 1, "title": self.title,
                "page": self.page, "landscape": self.landscape,
                "days": sorted(self.days), "auto_range": self.auto_range,
                "range_start": self.range_start, "range_end": self.range_end,
                "events": [e.to_dict() for e in self.events],
                "subcols": {str(d): list(n) for d, n in sorted(self.subcols.items()) if n},
                "day_names": list(self.day_names), "note": self.note,
                "icons": {k: v for k, v in self.icons.items() if k in used}}

    @staticmethod
    def from_dict(d: dict) -> "Planner":
        if not isinstance(d, dict) or d.get("format") != "ukeplan":
            raise ValueError("This is not a Ukeplan file.")
        events = [Event.from_dict(e) for e in d.get("events", [])]
        names = [str(x) for x in d.get("day_names", DAYS)] or list(DAYS)
        shown = [int(x) for x in d.get("days", [0, 1, 2, 3, 4]) if int(x) >= 0]
        need = max([e.day for e in events] + shown + [-1]) + 1
        while len(names) < need:                       # a file that uses days it doesn't name
            names.append(f"Dag {len(names) + 1}")
        return Planner(
            title=str(d.get("title", "")),
            page=d.get("page", "A4") if d.get("page") in PAGES else "A4",
            landscape=bool(d.get("landscape", True)),
            days=sorted(set(shown)),
            auto_range=bool(d.get("auto_range", True)),
            range_start=int(d.get("range_start", 8)),
            range_end=int(d.get("range_end", 16)),
            events=events,
            subcols={int(k): [str(x) for x in v] for k, v in dict(d.get("subcols", {})).items()
                     if 0 <= int(k) < len(names) and v},
            day_names=names, note=str(d.get("note", "")),
            icons=dict(d.get("icons", {})))


def write_planner(pl: Planner, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pl.to_dict(), f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def read_planner(path: str) -> Planner:
    with open(path, "r", encoding="utf-8") as f:
        return Planner.from_dict(json.load(f))


# --------------------------------------------------------------------------
# Icons
# --------------------------------------------------------------------------
_img_cache: dict = {}


def _render_emoji(ch: str) -> QImage:
    size = 256
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    f = QFont()
    f.setFamilies(EMOJI_FONTS)
    f.setPixelSize(int(size * 0.78))
    p.setFont(f)
    p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, ch)
    p.end()
    return img


ICON_MAX_PX = 384          # longest side of a stored icon (about 170 dpi on a 7 mm icon at A3)
EMBED_LIMIT = 300_000      # bytes; bigger icons are linked instead of embedded


def load_icon_image(path: str, max_px: int = ICON_MAX_PX) -> QImage:
    """Read an image file (png/jpg/svg/...) and shrink it to a sensible size."""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and size.width() > 0 and size.height() > 0:
        longest = max(size.width(), size.height())
        is_svg = reader.format().data().lower() in (b"svg", b"svgz")
        if longest > max_px or is_svg:
            f = max_px / longest
            reader.setScaledSize(QSize(max(1, round(size.width() * f)),
                                       max(1, round(size.height() * f))))
    img = reader.read()
    if img.isNull():
        raise ValueError(f"Could not read image: {reader.errorString()}")
    if max(img.width(), img.height()) > max_px:
        img = img.scaled(max_px, max_px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return img


def encode_icon(img: QImage) -> Optional[bytes]:
    """PNG (or JPEG for opaque photos) bytes if they stay under EMBED_LIMIT, else None."""
    for px in (ICON_MAX_PX, 256):
        cand = img if max(img.width(), img.height()) <= px else \
            img.scaled(px, px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        for fmt, quality in (("PNG", -1), ("JPEG", 88)):
            if fmt == "JPEG" and cand.hasAlphaChannel():
                continue
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.WriteOnly)
            cand.save(buf, fmt, quality)
            buf.close()
            data = bytes(ba.data())
            if 0 < len(data) <= EMBED_LIMIT:
                return data
    return None


class Prefs:
    """Per-user settings that live outside planner files: remembered icon images and
    saved time slots. Stored as JSON in the user's application-data folder."""

    def __init__(self):
        self.path: Optional[str] = None
        self.time_presets: list = []       # [{"name": str, "start": min, "end": min}]
        self.icons: dict = {}              # id -> {"name", "data"(b64) | "path"}, oldest first

    def _file(self) -> str:
        if self.path:
            return self.path
        env = os.environ.get("UKEPLAN_PREFS")
        if env:
            return env
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation) \
            or os.path.join(os.path.expanduser("~"), ".ukeplan")
        return os.path.join(base, "prefs.json")

    def load(self) -> None:
        self.time_presets, self.icons = [], {}
        try:
            with open(self._file(), "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        for t in d.get("time_presets", []):
            try:
                st, en = int(t["start"]), int(t["end"])
                if 0 <= st < en <= 1440:
                    self.time_presets.append({"name": str(t.get("name", "")), "start": st, "end": en})
            except (KeyError, TypeError, ValueError):
                pass
        for k, v in d.get("icons", {}).items():
            if isinstance(v, dict) and (isinstance(v.get("data"), str) or isinstance(v.get("path"), str)):
                self.icons[str(k)] = v

    def save(self) -> None:
        path = self._file()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"time_presets": self.time_presets, "icons": self.icons},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except OSError as ex:
            print(f"Could not save settings: {ex}", file=sys.stderr)

    # -- time slots
    def set_time_presets(self, presets: list) -> None:
        self.time_presets = sorted(presets, key=lambda p: (p["start"], p["end"], p["name"]))
        self.save()

    # -- icons
    def add_icon_file(self, path: str) -> tuple:
        """Remember an image file. Returns (icon id, embedded?)."""
        img = load_icon_image(path)
        data = encode_icon(img)
        name = os.path.splitext(os.path.basename(path))[0]
        if data:
            iid = hashlib.sha1(data).hexdigest()[:10]
            entry = {"name": name, "data": base64.b64encode(data).decode("ascii")}
        else:
            absp = os.path.abspath(path)
            iid = hashlib.sha1(absp.encode("utf-8")).hexdigest()[:10]
            entry = {"name": name, "path": absp}
        self.icons.pop(iid, None)          # re-adding moves it to the front
        self.icons[iid] = entry
        self.save()
        return iid, data is not None

    def add_from_planner(self, iid: str, value) -> None:
        name = "icon"
        if isinstance(value, dict):
            self.icons[iid] = {"name": os.path.splitext(os.path.basename(value.get("path", "")))[0] or name,
                               "path": value.get("path", "")}
        elif isinstance(value, str):
            self.icons[iid] = {"name": name, "data": value}
        self.save()

    def touch_icon(self, iid: str) -> None:
        """Mark an icon as just used (it moves to the front of the gallery)."""
        e = self.icons.pop(iid, None)
        if e is not None:
            self.icons[iid] = e
            self.save()

    def remove_icon(self, iid: str) -> None:
        if self.icons.pop(iid, None) is not None:
            self.save()

    def planner_value(self, iid: str):
        e = self.icons[iid]
        return e["data"] if isinstance(e.get("data"), str) else {"path": e.get("path", "")}


PREFS = Prefs()


def icon_image(pl: Planner, key: str) -> Optional[QImage]:
    if not key:
        return None
    img = _img_cache.get(key)
    if img is not None:
        return img
    if key.startswith("emoji:"):
        img = _render_emoji(key[6:])
    elif key.startswith("img:"):
        iid = key[4:]
        v = pl.icons.get(iid)
        if v is None and iid in PREFS.icons:
            v = PREFS.planner_value(iid)
        if isinstance(v, str):
            try:
                img = QImage.fromData(base64.b64decode(v))
            except ValueError:
                return None
        elif isinstance(v, dict) and v.get("path") and os.path.isfile(v["path"]):
            try:
                img = load_icon_image(v["path"])
            except ValueError:
                return None
        else:
            return None
        if img is None or img.isNull():
            return None
    else:
        return None
    _img_cache[key] = img
    return img


def missing_icons(pl: Planner) -> list:
    return [i for i in pl.used_icon_ids() if icon_image(pl, "img:" + i) is None]


def graphemes(text: str) -> list:
    """Split text into user-perceived characters (so one emoji = one item,
    even flags, skin tones and joined emoji)."""
    if not text:
        return []
    f = QTextBoundaryFinder(QTextBoundaryFinder.Grapheme, text)
    b16 = text.encode("utf-16-le")       # Qt positions are UTF-16 code units
    raw, start = [], 0
    while True:
        pos = f.toNextBoundary()
        if pos < 0:
            break
        raw.append(b16[start * 2:pos * 2].decode("utf-16-le"))
        start = pos

    def is_ri(g):       # regional indicator = half of a flag
        return len(g) == 1 and 0x1F1E6 <= ord(g) <= 0x1F1FF
    out, i = [], 0      # Qt sometimes splits a flag after another emoji: re-pair them
    while i < len(raw):
        if is_ri(raw[i]) and i + 1 < len(raw) and is_ri(raw[i + 1]):
            out.append(raw[i] + raw[i + 1])
            i += 2
        else:
            out.append(raw[i])
            i += 1
    return out


# --------------------------------------------------------------------------
# Layout engine
# --------------------------------------------------------------------------
@dataclass
class Fit:
    scale: float
    mode: str          # "side" (icon left of title), "stack" (icon above) or "row" (one line)
    overflow: bool
    pad: float = 0.0   # padding used (0 = the default); "row" blocks drop to less


@dataclass
class Placed:
    ev: Event
    rect: QRectF
    fit: Fit
    img: Optional[QImage]
    time_txt: str


@dataclass
class Layout:
    W: float
    H: float
    k: float
    m: float
    title_h: float
    head_h: float
    time_w: float
    left: float
    right: float
    top: float
    bottom: float
    lo: int
    hi: int
    days: list
    col_w: float
    hour_h: float
    placed: list
    sub_h: float = 0.0           # height of the column-name header row (0 = none)
    subs: list = None            # per shown day: list of column names (defaults filled in)
    day_labels: list = None      # per shown day: its name
    note_h: float = 0.0          # room reserved under the grid for the note (0 = none)
    note_px: float = 0.0         # font size the note is drawn at


def make_doc(text: str, px: float, bold: bool, width: float) -> QTextDocument:
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    f = QFont()
    f.setPixelSize(max(1, int(round(px))))
    f.setBold(bold)
    doc.setDefaultFont(f)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    doc.setDefaultTextOption(opt)
    doc.setPlainText(text)
    doc.setTextWidth(max(width, 1.0))
    return doc


def make_note_doc(text: str, px: float, width: float) -> QTextDocument:
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    f = QFont()
    f.setPixelSize(max(1, int(round(px))))
    f.setItalic(True)
    doc.setDefaultFont(f)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
    doc.setDefaultTextOption(opt)
    doc.setPlainText(text)
    doc.setTextWidth(max(width, 1.0))
    return doc


def draw_doc(p: QPainter, doc: QTextDocument, x: float, y: float, color: QColor) -> None:
    p.save()
    p.translate(x, y)
    ctx = QAbstractTextDocumentLayout.PaintContext()
    pal = QPalette()
    pal.setColor(QPalette.Text, color)
    ctx.palette = pal
    doc.documentLayout().draw(p, ctx)
    p.restore()


def time_text(ev: Event) -> str:
    if ev.hide_end:
        return fmt_time(ev.start)
    return f"{fmt_time(ev.start)}–{fmt_time(ev.end)}"


def _words_fit(text: str, px: float, bold: bool, width: float) -> bool:
    """True if every whitespace-separated word fits the width (no mid-word breaks)."""
    f = QFont()
    f.setPixelSize(max(1, int(round(px))))
    f.setBold(bold)
    fm = QFontMetricsF(f)
    return all(fm.horizontalAdvance(w) <= width for w in text.split())


def build_block(ev: Event, time_txt: str, has_icon: bool, iw: float, k: float,
                scale: float, mode: str):
    """Lay out the contents of one block.
    Returns (items, total_height, words_ok); words_ok is False if a word had to be broken."""
    u = MM * k * scale
    tpx, spx = 3.7 * u, 3.0 * u
    gap = 0.6 * u
    icon_s = 5.2 * u if has_icon else 0.0
    title = ev.title.strip()
    desc = ev.description.strip()
    items = []
    y = 0.0
    words_ok = True

    def add(text, px, bold):
        nonlocal y, words_ok
        if y > 0:
            y += gap
        if not _words_fit(text, px, bold, iw):
            words_ok = False
        d = make_doc(text, px, bold, iw)
        items.append(("doc", 0.0, y, d))
        y += d.size().height()

    if has_icon and mode == "side":
        side = 1.3 * u
        if title and not _words_fit(title, tpx, True, iw - icon_s - side):
            words_ok = False
        td = make_doc(title, tpx, True, iw - icon_s - side) if title else None
        th = td.size().height() if td else 0.0
        head = max(icon_s, th)
        items.append(("icon", 0.0, (head - icon_s) / 2, icon_s))
        if td:
            items.append(("doc", icon_s + side, (head - th) / 2, td))
        y = head
    else:
        if has_icon:
            items.append(("icon", 0.0, 0.0, icon_s))
            y = icon_s
        if title:
            add(title, tpx, True)
    tf = QFont()
    tf.setPixelSize(max(1, int(round(spx))))
    if "–" in time_txt and QFontMetricsF(tf).horizontalAdvance(time_txt) > iw:
        time_txt = time_txt.replace("–", "–\n")     # "08:30–" / "10:00" on two lines
    add(time_txt, spx, False)
    if desc:
        add(desc, spx, False)
    return items, y, words_ok


MIN_ROW_SCALE = 0.5            # smallest text scale for one-line blocks
ROW_PAD_X = 0.8                # mm: side padding of one-line blocks
ROW_PAD_Y_MIN = 0.15           # mm: least top/bottom padding of one-line blocks


def build_row(ev: Event, time_txt: str, has_icon: bool, w: float, h: float,
              k: float, scale: float, pad_x: float):
    """One-line layout for very slim blocks: [icon] title  time  description.
    Positions are relative to the block's top-left corner. Returns (items, line_h, ok);
    ok is False when text had to be shortened or left out."""
    u = MM * k * scale
    ft, fs = QFont(), QFont()
    ft.setPixelSize(max(1, int(round(3.7 * u))))
    ft.setBold(True)
    fs.setPixelSize(max(1, int(round(3.0 * u))))
    mt, ms = QFontMetricsF(ft), QFontMetricsF(fs)
    line_h = max(mt.height(), ms.height())
    y = (h - line_h) / 2
    gap = 1.3 * u
    limit = w - pad_x
    x, ok, items = pad_x, True, []
    if has_icon:
        items.append(("icon", x, y, line_h))
        x += line_h + gap
    parts = [(ev.title.strip(), ft, mt, "title"), (time_txt, fs, ms, "time"),
             (" ".join(ev.description.split()), fs, ms, "desc")]
    for text, font, fm, kind in parts:
        if not text:
            continue
        rem = limit - x
        adv = fm.horizontalAdvance(text)
        if adv <= rem:
            items.append(("line", x, y, adv + 1, line_h, text, font))
            x += adv + gap
            continue
        ok = False
        if kind != "time" and rem >= fm.horizontalAdvance("M…"):
            items.append(("line", x, y, rem, line_h, fm.elidedText(text, Qt.ElideRight, rem), font))
        break                      # nothing after a shortened piece
    return items, line_h, ok


def fit_row(ev: Event, time_txt: str, has_icon: bool, w: float, h: float, k: float,
            allow_cut: bool) -> Optional[Fit]:
    pad_x = ROW_PAD_X * MM * k
    room = h - 2 * ROW_PAD_Y_MIN * MM * k
    scale = 1.0
    while scale >= MIN_ROW_SCALE - 1e-9:
        _, line_h, ok = build_row(ev, time_txt, has_icon, w, h, k, scale, pad_x)
        if line_h <= room and ok:
            return Fit(round(scale, 3), "row", False, pad_x)
        scale -= 0.05
    _, line_h, _ = build_row(ev, time_txt, has_icon, w, h, k, MIN_ROW_SCALE, pad_x)
    if allow_cut and line_h <= room:
        return Fit(MIN_ROW_SCALE, "row", True, pad_x)       # fits the block, text shortened
    return None


_fit_cache: dict = {}


def fit_block(ev: Event, time_txt: str, has_icon: bool, w: float, h: float, k: float) -> Fit:
    key = (ev.title, ev.description, time_txt, has_icon, round(w), round(h), round(k, 4))
    hit = _fit_cache.get(key)
    if hit:
        return hit
    pad = 1.2 * MM * k
    iw, ih = max(w - 2 * pad, 1.0 * MM), h - 2 * pad
    modes = ["side", "stack"] if has_icon else ["side"]
    result = None
    # Pass 1: everything fits and no word is broken. Pass 2 (flagged): fits only
    # by breaking words mid-word. Otherwise: clipped at the smallest scale (flagged).
    for require_words in (True, False):
        scale = 1.0
        while result is None and scale >= MIN_SCALE - 1e-9:
            for mode in modes:
                _, total, ok = build_block(ev, time_txt, has_icon, iw, k, scale, mode)
                if total <= ih + 0.3 * MM and (ok or not require_words):
                    result = Fit(round(scale, 3), mode, not ok)
                    break
            scale -= 0.05
        if result:
            break
    clipped = result is None
    if clipped:
        best = min(modes, key=lambda m: build_block(ev, time_txt, has_icon, iw, k, MIN_SCALE, m)[1])
        result = Fit(MIN_SCALE, best, True)
    if clipped or result.overflow or result.scale < 1.0:
        # slim block: try everything on one line, with less padding
        row = fit_row(ev, time_txt, has_icon, w, h, k, allow_cut=clipped)
        if row and (clipped or result.overflow or row.scale > result.scale):
            result = row
    if len(_fit_cache) > 4000:
        _fit_cache.clear()
    _fit_cache[key] = result
    return result


def pack(items):
    """items: [(ev, start, end)] for one day -> [(ev, s, e, col, span, ncols)].
    Overlapping blocks share the width; blocks expand into free columns."""
    items = sorted(items, key=lambda t: (t[1], -t[2]))
    clusters, cur, cur_end = [], [], -1
    for it in items:
        if cur and it[1] >= cur_end:
            clusters.append(cur)
            cur, cur_end = [], -1
        cur.append(it)
        cur_end = max(cur_end, it[2])
    if cur:
        clusters.append(cur)

    out = []
    for cl in clusters:
        cols, assign = [], {}
        for ev, s, e in cl:
            for i, end in enumerate(cols):
                if end <= s:
                    cols[i] = e
                    assign[ev.id] = i
                    break
            else:
                cols.append(e)
                assign[ev.id] = len(cols) - 1
        n = len(cols)
        for ev, s, e in cl:
            i = assign[ev.id]
            span = 1
            while i + span < n and not any(
                    assign[o.id] == i + span and os_ < e and s < oe
                    for o, os_, oe in cl):
                span += 1
            out.append((ev, s, e, i, span, n))
    return out


def time_clusters(items):
    """Group (ev, start, end) items into chains that overlap in time."""
    items = sorted(items, key=lambda t: (t[1], -t[2]))
    out, cur, cur_end = [], [], -1
    for it in items:
        if cur and it[1] >= cur_end:
            out.append(cur)
            cur, cur_end = [], -1
        cur.append(it)
        cur_end = max(cur_end, it[2])
    if cur:
        out.append(cur)
    return out


def compute_layout(pl: Planner, W: float, H: float) -> Layout:
    k = min(W, H) / (210 * MM)
    m = 10 * MM * k
    title_h = 15 * MM * k if pl.title.strip() else 0.0
    head_h = 9 * MM * k
    time_w = 13 * MM * k
    lo, hi = pl.time_range()
    days = sorted(pl.days)
    subs = [col_names(pl.subcols.get(d, [])) for d in days]
    sub_h = 6.5 * MM * k if any(subs) else 0.0
    left, right = m + time_w, W - m
    note = pl.note.strip()
    note_h = note_px = 0.0
    if note:                     # as large as is comfortable, smaller if there is a lot of text
        for sc in (1.0, 0.9, 0.8, 0.7, 0.6):
            note_px = 3.3 * MM * k * sc
            note_h = make_note_doc(note, note_px, right - left).size().height()
            if note_h <= 0.12 * H:
                break
        note_h += 2.0 * MM * k
    top, bottom = m + title_h + head_h + sub_h, H - m - note_h
    n = max(1, len(days))
    col_w = (right - left) / n
    hour_h = (bottom - top) / (hi - lo)

    placed = []
    pad = 0.5 * MM * k
    for di, d in enumerate(days):
        items = []
        for ev in pl.events:
            if ev.day != d:
                continue
            s = max(ev.start, lo * 60)
            e = min(max(ev.end, ev.start + 1), hi * 60)
            if e > s:
                items.append((ev, s, e))
        names = subs[di]
        n_sub = len(names)
        day_x = left + di * col_w
        placements = []          # (ev, s, e, lane, span, lanes, region_x, region_w)
        if n_sub == 0:
            placements = [t + (day_x, col_w) for t in pack(items)]
        else:
            def colof(ev):
                return ev.col if 0 <= ev.col < n_sub else -1
            for cl in time_clusters(items):
                spans = [t for t in cl if colof(t[0]) < 0]
                has_cols = len(spans) < len(cl)
                strip = 0.0
                if spans:                # blocks over all columns: a strip of their own where
                    strip = col_w / (n_sub + 1) if has_cols else col_w     # they overlap column blocks
                    placements += [t + (day_x, strip) for t in pack(spans)]
                rest = col_w - strip
                for c in range(n_sub):
                    ci = [t for t in cl if colof(t[0]) == c]
                    placements += [t + (day_x + strip + c * rest / n_sub, rest / n_sub)
                                   for t in pack(ci)]
        for ev, s, e, idx, span, ncols, rx, rw in placements:
            sub = rw / ncols
            x = rx + idx * sub + pad
            w = span * sub - 2 * pad
            y = top + (s - lo * 60) / 60 * hour_h + pad * 0.6
            h = (e - s) / 60 * hour_h - pad * 1.2
            img = icon_image(pl, ev.icon)
            tt = time_text(ev)
            fit = fit_block(ev, tt, img is not None, w, h, k)
            placed.append(Placed(ev, QRectF(x, y, w, h), fit, img, tt))
    return Layout(W, H, k, m, title_h, head_h, time_w, left, right, top, bottom,
                  lo, hi, days, col_w, hour_h, placed, sub_h, subs,
                  [pl.day_label(d) for d in days], note_h, note_px)


def contrast(c: QColor) -> QColor:
    lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
    return QColor("#111827") if lum > 150 else QColor("#FFFFFF")


def _fit_font(text: str, px: float, bold: bool, max_w: float) -> QFont:
    f = QFont()
    f.setBold(bold)
    f.setPixelSize(max(1, int(px)))
    adv = QFontMetricsF(f).horizontalAdvance(text)
    if adv > max_w > 0:
        f.setPixelSize(max(1, int(px * max_w / adv)))
    return f


def draw_block(p: QPainter, pb: Placed, k: float) -> None:
    ev, r, f = pb.ev, pb.rect, pb.fit
    base = QColor(ev.color)
    if not base.isValid():
        base = QColor("#BFDBFE")
    rad = 1.2 * MM * k
    p.setPen(QPen(base.darker(140), 0.3 * MM * k))
    p.setBrush(base)
    p.drawRoundedRect(r, rad, rad)

    fg = contrast(base)
    p.save()
    clip = QPainterPath()
    clip.addRoundedRect(r, rad, rad)
    p.setClipPath(clip)
    if f.mode == "row":
        items, _, _ = build_row(ev, pb.time_txt, pb.img is not None, r.width(), r.height(),
                                k, f.scale, f.pad)
        p.setPen(fg)
        for it in items:
            if it[0] == "icon":
                _, ix, iy, s = it
                img = pb.img
                sc = s / max(img.width(), img.height())
                w, h = img.width() * sc, img.height() * sc
                p.drawImage(QRectF(r.x() + ix + (s - w) / 2, r.y() + iy + (s - h) / 2, w, h), img)
            else:
                _, lx, ly, lw, lh, text, font = it
                p.setFont(font)
                p.drawText(QRectF(r.x() + lx, r.y() + ly, lw, lh),
                           Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine, text)
        p.restore()
        return
    pad = 1.2 * MM * k
    iw = max(r.width() - 2 * pad, 1.0 * MM)
    items, _, _ = build_block(ev, pb.time_txt, pb.img is not None, iw, k, f.scale, f.mode)
    for it in items:
        if it[0] == "icon":
            _, ix, iy, s = it
            img = pb.img
            sc = s / max(img.width(), img.height())
            w, h = img.width() * sc, img.height() * sc
            p.drawImage(QRectF(r.x() + pad + ix + (s - w) / 2,
                               r.y() + pad + iy + (s - h) / 2, w, h), img)
        else:
            _, dx, dy, doc = it
            draw_doc(p, doc, r.x() + pad + dx, r.y() + pad + dy, fg)
    p.restore()


def paint(p: QPainter, pl: Planner, L: Layout, selected: Optional[str] = None,
          preview: bool = False) -> None:
    """Draw the whole page. Painter must already be scaled to layout units."""
    k = L.k
    p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing
                     | QPainter.SmoothPixmapTransform)
    p.fillRect(QRectF(0, 0, L.W, L.H), QColor("white"))
    ink = QColor("#1F2937")

    if pl.title.strip():
        maxw = L.W - 2 * L.m
        p.setFont(_fit_font(pl.title, 9 * MM * k, True, maxw))
        p.setPen(ink)
        p.drawText(QRectF(L.m, L.m, maxw, L.title_h), Qt.AlignCenter, pl.title)

    if L.note_h:
        doc = make_note_doc(pl.note.strip(), L.note_px, L.right - L.left)
        draw_doc(p, doc, L.left, L.bottom + 2.0 * MM * k, QColor("#4B5563"))

    if not L.days:
        p.setFont(_fit_font("Ingen dager valgt", 5 * MM * k, False, L.W - 2 * L.m))
        p.setPen(QColor("#6B7280"))
        p.drawText(QRectF(0, 0, L.W, L.H), Qt.AlignCenter, "Ingen dager valgt")
        return

    full_h = L.head_h + L.sub_h
    hy = L.top - full_h
    # header background
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#E5E7EB"))
    p.drawRect(QRectF(L.left, hy, L.right - L.left, full_h))

    # horizontal lines + time labels
    lab_font = QFont()
    lab_font.setPixelSize(int(2.8 * MM * k))
    for h in range(L.lo, L.hi + 1):
        y = L.top + (h - L.lo) * L.hour_h
        p.setPen(QPen(QColor("#9CA3AF"), 0.25 * MM * k))
        p.drawLine(QPointF(L.left, y), QPointF(L.right, y))
        p.setFont(lab_font)
        p.setPen(ink)
        p.drawText(QRectF(L.m, y - 2.2 * MM * k, L.time_w - 1.8 * MM * k, 4.4 * MM * k),
                   Qt.AlignRight | Qt.AlignVCenter, f"{h:02d}:00")
        if h < L.hi and L.hour_h >= 9 * MM * k:
            pen = QPen(QColor("#D1D5DB"), 0.15 * MM * k)
            pen.setStyle(Qt.DotLine)
            p.setPen(pen)
            p.drawLine(QPointF(L.left, y + L.hour_h / 2), QPointF(L.right, y + L.hour_h / 2))

    # vertical lines + day names
    p.setPen(QPen(QColor("#6B7280"), 0.3 * MM * k))
    for i in range(len(L.days) + 1):
        x = L.left + i * L.col_w
        p.drawLine(QPointF(x, hy), QPointF(x, L.bottom))
    for i, names in enumerate(L.subs):               # lines between a day's columns
        for c in range(1, len(names)):
            x = L.left + i * L.col_w + c * L.col_w / len(names)
            p.setPen(QPen(QColor("#9CA3AF"), 0.25 * MM * k))
            p.drawLine(QPointF(x, hy + L.head_h), QPointF(x, L.bottom))
    p.setPen(ink)
    for i, d in enumerate(L.days):
        names = L.subs[i]
        p.setFont(_fit_font(L.day_labels[i], 4.2 * MM * k, True, L.col_w * 0.9))
        p.drawText(QRectF(L.left + i * L.col_w, hy, L.col_w, L.head_h if names else full_h),
                   Qt.AlignCenter, L.day_labels[i])
        for c, nm in enumerate(names):
            cw = L.col_w / len(names)
            if nm.strip():
                p.setFont(_fit_font(nm, 3.6 * MM * k, True, cw * 0.9))
                p.drawText(QRectF(L.left + i * L.col_w + c * cw, hy + L.head_h, cw, L.sub_h),
                           Qt.AlignCenter, nm)
        if names:
            p.setPen(QPen(QColor("#6B7280"), 0.3 * MM * k))
            p.drawLine(QPointF(L.left + i * L.col_w, hy + L.head_h),
                       QPointF(L.left + (i + 1) * L.col_w, hy + L.head_h))
            p.setPen(ink)
    p.setPen(QPen(QColor("#6B7280"), 0.3 * MM * k))
    p.drawLine(QPointF(L.left, hy), QPointF(L.right, hy))

    for pb in L.placed:
        draw_block(p, pb, k)

    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor("#374151"), 0.5 * MM * k))
    p.drawRect(QRectF(L.left, hy, L.right - L.left, L.bottom - hy))

    if preview:    # on-screen only: never printed
        for pb in L.placed:
            if pb.fit.overflow:
                pen = QPen(QColor("#DC2626"), 0.7 * MM * k)
                pen.setStyle(Qt.DashLine)
                p.setPen(pen)
                p.drawRoundedRect(pb.rect.adjusted(-0.2 * MM * k, -0.2 * MM * k,
                                                   0.2 * MM * k, 0.2 * MM * k),
                                  1.2 * MM * k, 1.2 * MM * k)
        for pb in L.placed:
            if pb.ev.id == selected:
                p.setPen(QPen(QColor("#2563EB"), 0.9 * MM * k))
                p.drawRoundedRect(pb.rect.adjusted(-0.3 * MM * k, -0.3 * MM * k,
                                                   0.3 * MM * k, 0.3 * MM * k),
                                  1.2 * MM * k, 1.2 * MM * k)


# --------------------------------------------------------------------------
# Export / print
# --------------------------------------------------------------------------
def _page_layout(pl: Planner) -> QPageLayout:
    size = QPageSize(QPageSize.A3 if pl.page == "A3" else QPageSize.A4)
    orient = QPageLayout.Landscape if pl.landscape else QPageLayout.Portrait
    return QPageLayout(size, orient, QMarginsF(0, 0, 0, 0))


def export_pdf(pl: Planner, path: str) -> None:
    w_mm, h_mm = pl.page_mm()
    dpi = 600
    writer = QPdfWriter(path)
    writer.setResolution(dpi)
    writer.setPageLayout(_page_layout(pl))
    writer.setTitle(pl.title or APP_NAME)
    writer.setCreator(APP_NAME)
    p = QPainter(writer)
    s = dpi / 25.4 / MM
    p.scale(s, s)
    paint(p, pl, compute_layout(pl, w_mm * MM, h_mm * MM))
    p.end()


def export_png(pl: Planner, path: str, dpi: int = 200) -> None:
    w_mm, h_mm = pl.page_mm()
    s = dpi / 25.4
    img = QImage(round(w_mm * s), round(h_mm * s), QImage.Format_ARGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.scale(s / MM, s / MM)
    paint(p, pl, compute_layout(pl, w_mm * MM, h_mm * MM))
    p.end()
    img.save(path)


HTML_ICON_PX = 128


def export_html(pl: Planner, path: str, icons: str = "embed") -> None:
    """HTML export: the page as vector graphics (same drawing code as the PDF), with print CSS so a
    browser prints it at the planner's paper size. icons="embed": one self-contained file;
    icons="folder": icons are saved as PNGs in '<name>_icons' next to the file (smaller HTML)."""
    w_mm, h_mm = pl.page_mm()
    W, H = w_mm * MM, h_mm * MM
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    gen = QSvgGenerator()
    gen.setOutputDevice(buf)
    gen.setSize(QSize(int(W), int(H)))
    gen.setViewBox(QRectF(0, 0, W, H))
    gen.setTitle(pl.title or APP_NAME)
    L = compute_layout(pl, W, H)
    small = {}
    for pb in L.placed:                                  # shrink icons: 128 px is plenty at print size
        if pb.img is not None:
            key = id(pb.img)
            if key not in small:
                im = pb.img
                if max(im.width(), im.height()) > HTML_ICON_PX:
                    im = im.scaled(HTML_ICON_PX, HTML_ICON_PX, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                small[key] = im
            pb.img = small[key]
    p = QPainter(gen)
    paint(p, pl, L)
    p.end()
    buf.close()
    svg = bytes(buf.data().data()).decode("utf-8")
    svg = svg[svg.index("<svg"):]                       # drop the XML prolog
    svg = re.sub(r'<svg\b([^>]*?)\swidth="[^"]*"([^>]*?)\sheight="[^"]*"', r'<svg\1\2', svg, count=1)
    if icons == "folder":
        base = os.path.splitext(path)[0]
        folder, files = base + "_icons", {}

        def to_file(m):
            raw = base64.b64decode(m.group(1))
            name = "icon_" + hashlib.md5(raw).hexdigest()[:10] + ".png"
            if name not in files:
                os.makedirs(folder, exist_ok=True)
                with open(os.path.join(folder, name), "wb") as f:
                    f.write(raw)
                files[name] = True
            return 'xlink:href="' + urllib.parse.quote(os.path.basename(folder)) + "/" + name + '"'
        svg = re.sub(r'xlink:href="data:image/png;base64,([^"]*)"', to_file, svg)
    title = html_escape(pl.title or APP_NAME)
    doc = f"""<!DOCTYPE html>
<html lang="nb">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  @page {{ size: {w_mm:g}mm {h_mm:g}mm; margin: 0; }}
  html, body {{ margin: 0; background: #d1d5db; }}
  .page {{ width: min(100%, {w_mm / h_mm * 100:.2f}vh); margin: 0 auto; background: #fff;
           box-shadow: 0 2px 12px rgba(0,0,0,.25); }}
  .page svg {{ display: block; width: 100%; height: auto; }}
  @media print {{
    html, body {{ background: #fff; }}
    .page {{ width: {w_mm:g}mm; height: {h_mm:g}mm; box-shadow: none; }}
  }}
</style>
</head>
<body>
<div class="page">
{svg}
</div>
</body>
</html>
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def ask_html_icons(parent) -> Optional[str]:
    """Ask how to store icons in an HTML export. Returns "embed", "folder" or None (cancelled)."""
    st = QSettings()
    dlg = QDialog(parent)
    dlg.setWindowTitle("Export HTML")
    lay = QVBoxLayout(dlg)
    lay.addWidget(QLabel("How should the icons be stored?"))
    r_embed = QRadioButton("Embed in the HTML file\nOne self-contained file, but larger.")
    r_folder = QRadioButton("Icon folder next to the file\nSmaller HTML. Keep the folder together with the file.")
    (r_folder if st.value("html_icons", "embed") == "folder" else r_embed).setChecked(True)
    lay.addWidget(r_embed)
    lay.addWidget(r_folder)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    if dlg.exec() != QDialog.Accepted:
        return None
    choice = "folder" if r_folder.isChecked() else "embed"
    st.setValue("html_icons", choice)
    return choice


def print_planner(pl: Planner, parent: QWidget) -> None:
    printer = QPrinter(QPrinter.HighResolution)
    printer.setPageLayout(_page_layout(pl))
    printer.setDocName(pl.title or APP_NAME)
    dlg = QPrintDialog(printer, parent)
    if dlg.exec() != QDialog.Accepted:
        return
    # Some system dialogs reset paper and orientation to the printer's defaults (A4 portrait):
    # the planner's own settings win. Layout follows whatever page the printer then reports.
    printer.setPageLayout(_page_layout(pl))
    rect = printer.pageRect(QPrinter.Millimeter)
    p = QPainter(printer)
    s = printer.resolution() / 25.4 / MM
    p.scale(s, s)
    paint(p, pl, compute_layout(pl, rect.width() * MM, rect.height() * MM))
    p.end()


# --------------------------------------------------------------------------
# Preview widget
# --------------------------------------------------------------------------
class Preview(QWidget):
    eventClicked = Signal(str)
    eventDoubleClicked = Signal(str)
    cellDoubleClicked = Signal(int, int, int)  # day index, minutes, column (-1 = none)

    def __init__(self):
        super().__init__()
        self.setMinimumSize(520, 380)
        self.pl: Optional[Planner] = None
        self.L: Optional[Layout] = None
        self.selected: Optional[str] = None
        self._xf = (0.0, 0.0, 1.0)

    def set_data(self, pl, L, selected):
        self.pl, self.L, self.selected = pl, L, selected
        self.update()

    def set_selected(self, sid):
        self.selected = sid
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#D1D5DB"))
        if not self.L:
            return
        W, H = self.L.W, self.L.H
        pad = 18
        s = min((self.width() - 2 * pad) / W, (self.height() - 2 * pad) / H)
        ox, oy = (self.width() - W * s) / 2, (self.height() - H * s) / 2
        self._xf = (ox, oy, s)
        p.translate(ox, oy)
        p.scale(s, s)
        p.fillRect(QRectF(4 / s, 5 / s, W, H), QColor(0, 0, 0, 55))
        paint(p, self.pl, self.L, self.selected, True)

    def _page_pt(self, pos: QPointF) -> QPointF:
        ox, oy, s = self._xf
        return QPointF((pos.x() - ox) / s, (pos.y() - oy) / s)

    def _hit(self, pos):
        if not self.L:
            return None
        q = self._page_pt(pos)
        for pb in reversed(self.L.placed):
            if pb.rect.contains(q):
                return pb
        return None

    def mousePressEvent(self, e):
        pb = self._hit(e.position())
        if pb:
            self.eventClicked.emit(pb.ev.id)

    def mouseDoubleClickEvent(self, e):
        pb = self._hit(e.position())
        if pb:
            self.eventDoubleClicked.emit(pb.ev.id)
            return
        L = self.L
        if not L or not L.days:
            return
        q = self._page_pt(e.position())
        if L.left <= q.x() < L.right and L.top <= q.y() < L.bottom:
            di = min(len(L.days) - 1, int((q.x() - L.left) / L.col_w))
            minutes = L.lo * 60 + (q.y() - L.top) / L.hour_h * 60
            names = L.subs[di] if L.subs else []
            col = -1
            if names:
                col = min(len(names) - 1, int((q.x() - L.left - di * L.col_w) / (L.col_w / len(names))))
            self.cellDoubleClicked.emit(L.days[di], int(minutes // 15 * 15), col)


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------
class ColorRow(QWidget):
    def __init__(self, color: str):
        super().__init__()
        self.color = color
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        self.btns = []
        for i, c in enumerate(PALETTE):
            b = QPushButton()
            b.setFixedSize(26, 26)
            b.clicked.connect(lambda _=False, c=c: self.set_color(c))
            grid.addWidget(b, i // 6, i % 6)
            self.btns.append((b, c))
        self.custom = QPushButton("Custom…")
        self.custom.clicked.connect(self.pick)
        grid.addWidget(self.custom, 0, 6, 2, 1)
        self.restyle()

    def restyle(self):
        for b, c in self.btns:
            sel = c.lower() == self.color.lower()
            b.setStyleSheet(f"background:{c}; border:{'3px solid #111827' if sel else '1px solid #9CA3AF'};"
                            "border-radius:4px;")
        in_pal = any(self.color.lower() == c.lower() for c in PALETTE)
        self.custom.setStyleSheet(
            f"background:{self.color}; color:{contrast(QColor(self.color)).name()};"
            f"border:{'1px solid #9CA3AF' if in_pal else '3px solid #111827'}; border-radius:4px; padding:4px 8px;")

    def set_color(self, c: str):
        self.color = c
        self.restyle()

    def pick(self):
        c = QColorDialog.getColor(QColor(self.color), self, "Block colour")
        if c.isValid():
            self.set_color(c.name())


ICON_FILE_FILTER = "Images (*.png *.jpg *.jpeg *.gif *.bmp *.svg *.webp);;All files (*)"


def qtime_from_minutes(m: int) -> QTime:
    return QTime((m // 60) % 24, m % 60)          # 24:00 is shown as 00:00


def minutes_from_qtime(t: QTime, is_end: bool = False) -> int:
    m = t.hour() * 60 + t.minute()
    return 1440 if is_end and m == 0 else m       # an end of 00:00 means midnight


def slot_label(p: dict) -> str:
    base = f"{fmt_time(p['start'])}–{fmt_time(p['end'])}"
    return f"{base}  ·  {p['name']}" if p.get("name") else base


class TimeSlotsDialog(QDialog):
    """Settings: the saved timespans offered in the block dialog."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Saved timespans")
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name (optional)", "From", "To", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.setColumnWidth(3, 40)
        lay.addWidget(self.table, 1)
        for p in PREFS.time_presets:
            self.add_row(p["name"], p["start"], p["end"])
        row = QHBoxLayout()
        add = QPushButton("Add timespan")
        add.clicked.connect(lambda: self.add_row())
        row.addWidget(add)
        row.addStretch(1)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(480, 440)

    def row_times(self, r: int) -> tuple:
        return (minutes_from_qtime(self.table.cellWidget(r, 1).time()),
                minutes_from_qtime(self.table.cellWidget(r, 2).time(), True))

    def add_row(self, name: str = "", start: Optional[int] = None, end: Optional[int] = None):
        n = self.table.rowCount()
        if start is None:                       # continue after the previous row, same length
            if n:
                ps, pe = self.row_times(n - 1)
                length = max(5, pe - ps)
                start = pe if pe + length <= 1440 else 8 * 60
                end = min(1440, start + length)
            else:
                start, end = 8 * 60, 9 * 60
        self.table.insertRow(n)
        name_edit = QLineEdit(name)
        name_edit.setPlaceholderText("e.g. Time 1")
        t1, t2 = QTimeEdit(qtime_from_minutes(start)), QTimeEdit(qtime_from_minutes(end))
        for t in (t1, t2):
            t.setDisplayFormat("HH:mm")
        t2.setToolTip("00:00 means midnight (end of the day)")
        rm = QPushButton("✕")
        rm.setToolTip("Remove")
        rm.clicked.connect(self.remove_clicked)
        for c, w in enumerate((name_edit, t1, t2, rm)):
            self.table.setCellWidget(n, c, w)
        self.table.setRowHeight(n, 34)
        self.table.scrollToBottom()

    def remove_clicked(self):
        idx = self.table.indexAt(self.sender().pos())
        if idx.isValid():
            self.table.removeRow(idx.row())

    def accept(self):
        slots, seen = [], set()
        for r in range(self.table.rowCount()):
            sm, em = self.row_times(r)
            if em <= sm:
                QMessageBox.warning(self, "Saved timespans",
                                    f"Row {r + 1}: the end time must be after the start time.")
                return
            if (sm, em) in seen:
                continue
            seen.add((sm, em))
            slots.append({"name": self.table.cellWidget(r, 0).text().strip(), "start": sm, "end": em})
        PREFS.set_time_presets(slots)
        super().accept()


class DaysDialog(QDialog):
    """Rename, add, remove and reorder the days of this planner (any number of days)."""

    def __init__(self, parent, pl: Planner):
        super().__init__(parent)
        self.setWindowTitle("Edit days")
        self.pl = pl
        self.names: list = []
        self.mapping: dict = {}       # old day index -> new index (removed days are missing)
        self.added: list = []         # new indices of days that are new
        lay = QVBoxLayout(self)
        body = QHBoxLayout()
        self.list = QListWidget()
        self.list.setMinimumHeight(230)
        self.list.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                  | QAbstractItemView.AnyKeyPressed)
        for i, n in enumerate(pl.day_names):
            self.add_item(i, n)
        body.addWidget(self.list, 1)
        side = QVBoxLayout()
        for text, fn in (("Add day", self.add_day), ("Remove", self.remove_day),
                         ("Move up", lambda: self.move(-1)), ("Move down", lambda: self.move(1)),
                         ("Reset to Mandag–Søndag", self.reset_week)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, fn=fn: fn())
            side.addWidget(b)
        side.addStretch(1)
        body.addLayout(side)
        lay.addLayout(body, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(500, 380)

    def add_item(self, orig: int, name: str):
        it = QListWidgetItem(name)
        it.setData(Qt.UserRole, orig)
        it.setFlags(it.flags() | Qt.ItemIsEditable)
        it.setSizeHint(QSize(100, 30))
        self.list.addItem(it)
        return it

    def texts(self) -> list:
        return [self.list.item(i).text().strip() for i in range(self.list.count())]

    def add_day(self):
        used = set(self.texts())
        name = next((d for d in DAYS if d not in used), f"Dag {self.list.count() + 1}")
        it = self.add_item(-1, name)
        self.list.setCurrentItem(it)
        self.list.editItem(it)

    def remove_day(self):
        r = self.list.currentRow()
        if r >= 0:
            self.list.takeItem(r)

    def move(self, delta: int):
        r = self.list.currentRow()
        if r < 0 or not 0 <= r + delta < self.list.count():
            return
        it = self.list.takeItem(r)
        self.list.insertItem(r + delta, it)
        self.list.setCurrentRow(r + delta)

    def reset_week(self):
        old_n = len(self.pl.day_names)
        self.list.clear()
        for i, name in enumerate(DAYS):             # keep blocks on days 1-7 where they exist
            self.add_item(i if i < old_n else -1, name)

    def accept(self):
        n = self.list.count()
        if n == 0:
            QMessageBox.warning(self, "Edit days", "A planner needs at least one day.")
            return
        names, mapping, added = [], {}, []
        for i in range(n):
            it = self.list.item(i)
            names.append(it.text().strip() or f"Dag {i + 1}")
            orig = it.data(Qt.UserRole)
            if orig >= 0:
                mapping[orig] = i
            else:
                added.append(i)
        lost = sum(1 for e in self.pl.events if e.day not in mapping)
        if lost and QMessageBox.question(
                self, "Remove days",
                f"{lost} block{'s' if lost != 1 else ''} on the removed days will be deleted "
                "(you can undo this). Continue?") != QMessageBox.Yes:
            return
        self.names, self.mapping, self.added = names, mapping, added
        super().accept()


class NoteEdit(QPlainTextEdit):
    editingFinished = Signal()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.editingFinished.emit()


class ColumnsDialog(QDialog):
    """Split days into named columns (for example one per class). Names are free text and
    start out blank; blocks can sit in one column or span the whole day."""

    def __init__(self, parent, pl: Planner):
        super().__init__(parent)
        self.setWindowTitle("Columns within days")
        self.pl = pl
        # working copy per day: [original column index or -1, name]
        self.work = {d: [[i, n] for i, n in enumerate(pl.subcols.get(d, []))]
                     for d in range(len(pl.day_names))}
        self.cur = None
        self.new_subcols: dict = {}
        self.remap: dict = {}

        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("Day"))
        self.day_combo = QComboBox()
        for d in sorted(set(pl.days) | {d for d, n in pl.subcols.items() if n}):
            self.day_combo.addItem(pl.day_label(d), d)
        top.addWidget(self.day_combo, 1)
        lay.addLayout(top)

        body = QHBoxLayout()
        self.list = QListWidget()
        self.list.setMinimumHeight(180)
        self.list.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                  | QAbstractItemView.AnyKeyPressed)
        self.list.itemChanged.connect(self.on_changed)
        body.addWidget(self.list, 1)
        side = QVBoxLayout()
        for text, fn in (("Add column", self.add_col), ("Remove", self.remove_col),
                         ("Move up", lambda: self.move(-1)), ("Move down", lambda: self.move(1)),
                         ("Copy to all days", self.copy_all)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, fn=fn: fn())
            side.addWidget(b)
        side.addStretch(1)
        body.addLayout(side)
        lay.addLayout(body, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(460, 340)
        self.day_combo.currentIndexChanged.connect(self.switch_day)
        self.switch_day()

    def commit(self):
        if self.cur is not None:
            self.work[self.cur] = [[it.data(Qt.UserRole), "" if it.data(Qt.UserRole + 1) else it.text().strip()]
                                   for it in (self.list.item(i) for i in range(self.list.count()))]

    def load(self):
        self.list.clear()
        for orig, name in self.work.get(self.cur, []):
            self.add_item(orig, name)
        self.relabel()

    def relabel(self):
        """Blank columns show their default name (Column A, B, ...) in grey italics."""
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            it = self.list.item(i)
            auto = bool(it.data(Qt.UserRole + 1))
            if auto:
                it.setText(f"Column {col_letters(i)}")
            f = it.font()
            f.setItalic(auto)
            it.setFont(f)
            it.setData(Qt.ForegroundRole, QColor("#6B7280") if auto else None)
        self.list.blockSignals(False)

    def on_changed(self, it):
        it.setData(Qt.UserRole + 1, it.text().strip() == "")     # cleared = back to the default name
        self.relabel()

    def add_item(self, orig: int, name: str):
        it = QListWidgetItem(name)
        it.setData(Qt.UserRole, orig)
        it.setData(Qt.UserRole + 1, not name.strip())
        it.setFlags(it.flags() | Qt.ItemIsEditable)
        it.setSizeHint(QSize(100, 30))
        self.list.addItem(it)
        return it

    def switch_day(self, *_):
        self.commit()
        self.cur = self.day_combo.currentData()
        self.load()

    def add_col(self):
        it = self.add_item(-1, "")                    # starts with its default name; type to rename
        self.relabel()
        self.list.setCurrentItem(it)
        self.list.editItem(it)

    def remove_col(self):
        r = self.list.currentRow()
        if r >= 0:
            self.list.takeItem(r)
            self.relabel()

    def move(self, delta: int):
        r = self.list.currentRow()
        if r < 0 or not 0 <= r + delta < self.list.count():
            return
        it = self.list.takeItem(r)
        self.list.insertItem(r + delta, it)
        self.list.setCurrentRow(r + delta)
        self.relabel()

    def copy_all(self):
        self.commit()
        src = [n for _, n in self.work[self.cur]]
        for i in range(self.day_combo.count()):
            d = self.day_combo.itemData(i)
            if d == self.cur:
                continue
            old = self.work[d]
            used, new = set(), []
            for name in src:             # keep the link to existing blocks when names match
                match = next((j for j, (o, n) in enumerate(old) if n == name and j not in used and o >= 0), None)
                if match is not None:
                    used.add(match)
                new.append([old[match][0] if match is not None else -1, name])
            self.work[d] = new

    def accept(self):
        self.commit()
        orphans = 0
        subcols, remap = {}, {}
        for d in range(len(self.pl.day_names)):
            entries = self.work[d]
            old_n = len(self.pl.subcols.get(d, []))
            subcols[d] = [n for _, n in entries]
            remap[d] = {o: i for i, (o, _) in enumerate(entries) if o >= 0}
            if entries:
                orphans += sum(1 for e in self.pl.events
                               if e.day == d and 0 <= e.col < old_n and e.col not in remap[d])
        if orphans and QMessageBox.question(
                self, "Remove columns",
                f"{orphans} block{'s' if orphans != 1 else ''} in the removed columns will be deleted "
                "(you can undo this). Continue?") != QMessageBox.Yes:
            return
        self.new_subcols, self.remap = subcols, remap
        super().accept()


class EventDialog(QDialog):
    def __init__(self, parent, pl: Planner, ev: Optional[Event] = None,
                 day: Optional[int] = None, start: Optional[int] = None,
                 col: Optional[int] = None):
        super().__init__(parent)
        self.pl = pl
        self.settings = QSettings("Ukeplan", "Ukeplan")
        self.editing = ev is not None
        self.setWindowTitle("Edit block" if self.editing else "New block")
        if ev is None:
            d = day if day is not None else (pl.days[0] if pl.days else 0)
            s = start if start is not None else 9 * 60
            ev = Event(day=d, start=s, end=min(1440, s + 60),
                       color=PALETTE[len(pl.events) % len(PALETTE)])
        self.ev = copy.copy(ev)
        self.file_key = ev.icon if ev.icon.startswith("img:") else ""
        self.result_events: list = []
        self._preset_vals: list = []

        form = QFormLayout()
        self.title = QLineEdit(ev.title)
        form.addRow("Title", self.title)
        self.desc = QPlainTextEdit(ev.description)
        self.desc.setMinimumHeight(90)
        form.addRow("Description", self.desc)

        if self.editing:
            self.day_combo = QComboBox()
            for d in sorted(set(pl.days) | {ev.day}):
                self.day_combo.addItem(pl.day_label(d), d)
            self.day_combo.setCurrentIndex(self.day_combo.findData(ev.day))
            form.addRow("Day", self.day_combo)
        else:
            row = QHBoxLayout()
            self.day_boxes = []
            for d in (pl.days or [ev.day]):
                cb = QCheckBox(pl.day_short(d))
                cb.setChecked(d == ev.day)
                row.addWidget(cb)
                self.day_boxes.append((d, cb))
            row.addStretch(1)
            w = QWidget()
            w.setLayout(row)
            row.setContentsMargins(0, 0, 0, 0)
            form.addRow("Day(s)", w)

        # column within the day (only shown for days that are split into columns)
        self.form = form
        self.col_combo = QComboBox()
        self._col_opts: list = []
        self._wanted_col = ev.col if self.editing else (col if col is not None else -1)
        form.addRow("Column", self.col_combo)
        if self.editing:
            self.day_combo.currentIndexChanged.connect(lambda *_: self.refresh_cols())
        else:
            for _d, cb in self.day_boxes:
                cb.toggled.connect(lambda *_: self.refresh_cols())
        self.refresh_cols(first=True)

        # saved timespans
        srow = QHBoxLayout()
        self.preset = QComboBox()
        self.preset.activated.connect(self.on_preset)
        self.save_slot_btn = QPushButton("Save…")
        self.save_slot_btn.setToolTip("Save the current From/To as a timespan to pick later\n"
                                      "(manage them under Settings ▸ Saved timespans)")
        self.save_slot_btn.clicked.connect(self.save_slot)
        srow.addWidget(self.preset, 1)
        srow.addWidget(self.save_slot_btn)
        sw = QWidget()
        sw.setLayout(srow)
        srow.setContentsMargins(0, 0, 0, 0)
        form.addRow("Saved timespan", sw)

        times = QHBoxLayout()
        self.start = QTimeEdit(qtime_from_minutes(ev.start))
        self.end = QTimeEdit(qtime_from_minutes(ev.end))
        for t in (self.start, self.end):
            t.setDisplayFormat("HH:mm")
            t.timeChanged.connect(self.sync_preset)
        self.end.setToolTip("00:00 means midnight (end of the day)")
        times.addWidget(QLabel("From"))
        times.addWidget(self.start)
        times.addSpacing(12)
        times.addWidget(QLabel("To"))
        times.addWidget(self.end)
        times.addSpacing(12)
        self.hide_end = QCheckBox("Hide end time")
        self.hide_end.setChecked(ev.hide_end)
        self.hide_end.setToolTip("Only the start time is printed; the end time still sets the block's size")
        times.addWidget(self.hide_end)
        times.addStretch(1)
        tw = QWidget()
        tw.setLayout(times)
        times.setContentsMargins(0, 0, 0, 0)
        form.addRow("Time", tw)
        self.fill_presets()

        # icon: none / emoji / image file (with remembered images)
        top = QHBoxLayout()
        self.icon_mode = QComboBox()
        self.icon_mode.addItems(["None", "Emoji", "Image file"])
        self.emoji_edit = QLineEdit(ev.icon[6:] if ev.icon.startswith("emoji:") else "")
        self.emoji_edit.setPlaceholderText("Type or paste an emoji")
        self.add_img_btn = QPushButton("Add image…")
        self.icon_preview = QLabel()
        self.icon_preview.setFixedSize(38, 38)
        self.icon_preview.setAlignment(Qt.AlignCenter)
        self.icon_preview.setStyleSheet("border:1px solid #D1D5DB; border-radius:4px;")
        top.addWidget(self.icon_mode)
        top.addWidget(self.emoji_edit, 100)
        top.addWidget(self.add_img_btn)
        top.addStretch(1)
        top.addWidget(self.icon_preview)
        self.gallery = QListWidget()
        self.gallery.setViewMode(QListWidget.IconMode)
        self.gallery.setIconSize(QSize(36, 36))
        self.gallery.setGridSize(QSize(48, 48))
        self.gallery.setResizeMode(QListWidget.Adjust)
        self.gallery.setMovement(QListWidget.Static)
        self.gallery.setUniformItemSizes(True)
        self.gallery.setSelectionMode(QAbstractItemView.SingleSelection)
        self.gallery.setFixedHeight(112)
        self.gallery.setStyleSheet(
            "QListWidget::item { border: 2px solid transparent; border-radius: 6px; }"
            "QListWidget::item:selected { border: 2px solid #2563EB; background: transparent; }"
            "QListWidget::item:hover { background: rgba(37, 99, 235, 0.10); }")
        self.gallery.setContextMenuPolicy(Qt.CustomContextMenu)
        self.gallery.customContextMenuRequested.connect(self.gallery_menu)
        self.gallery.currentItemChanged.connect(self.on_gallery_selected)
        ibox = QVBoxLayout()
        ibox.setContentsMargins(0, 0, 0, 0)
        ibox.addLayout(top)
        ibox.addWidget(self.gallery)
        iw = QWidget()
        iw.setLayout(ibox)
        iw.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)   # spare height goes to the description
        form.addRow("Icon", iw)
        self.icon_mode.setCurrentIndex(1 if ev.icon.startswith("emoji:") else 2 if self.file_key else 0)
        self.icon_mode.currentIndexChanged.connect(self.update_icon_ui)
        self.emoji_edit.textEdited.connect(self.on_emoji_edited)
        self.add_img_btn.clicked.connect(self.add_images)
        self.populate_gallery()
        self.update_icon_ui()

        self.colors = ColorRow(ev.color)
        form.addRow("Colour", self.colors)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.setMinimumWidth(520)
        self.title.setFocus()

    # ---- column ------------------------------------------------------------
    def ticked_days(self) -> list:
        if self.editing:
            return [self.day_combo.currentData()]
        return [d for d, cb in self.day_boxes if cb.isChecked()]

    def refresh_cols(self, first: bool = False):
        days = self.ticked_days()
        prev = None if first else self.col_combo.currentText()
        opts = [("All columns", lambda d: -1)]
        if len(days) == 1:
            for i, nm in enumerate(col_names(self.pl.subcols.get(days[0], []))):
                opts.append((nm, lambda d, i=i: i))
        else:                                    # several days: match columns by name
            seen = []
            for d in days:
                for nm in col_names(self.pl.subcols.get(d, [])):
                    if nm not in seen:
                        seen.append(nm)
            for nm in seen:
                opts.append((nm, lambda d, nm=nm: (col_names(self.pl.subcols.get(d, [])).index(nm)
                                                   if nm in col_names(self.pl.subcols.get(d, [])) else None)))
        self._col_opts = opts
        self.col_combo.blockSignals(True)
        self.col_combo.clear()
        for label, _fn in opts:
            self.col_combo.addItem(label)
        idx = 0
        if first and 0 <= self._wanted_col < len(opts) - 1:
            idx = self._wanted_col + 1
        elif prev is not None and prev in [o[0] for o in opts]:
            idx = [o[0] for o in opts].index(prev)
        self.col_combo.setCurrentIndex(idx)
        self.col_combo.blockSignals(False)
        self.form.setRowVisible(self.col_combo, len(opts) > 1)

    # ---- saved timespans ---------------------------------------------------
    def times(self) -> tuple:
        return (minutes_from_qtime(self.start.time()),
                minutes_from_qtime(self.end.time(), True))

    def fill_presets(self):
        self.preset.blockSignals(True)
        self.preset.clear()
        self._preset_vals = []
        self.preset.addItem("Custom", -1)
        for p in PREFS.time_presets:
            self.preset.addItem(slot_label(p), len(self._preset_vals))
            self._preset_vals.append((p["start"], p["end"]))
        self.preset.blockSignals(False)
        self.sync_preset()

    def sync_preset(self, *_):
        cur, idx = self.times(), 0
        for i in range(1, self.preset.count()):
            if self._preset_vals[self.preset.itemData(i)] == cur:
                idx = i
                break
        self.preset.blockSignals(True)
        self.preset.setCurrentIndex(idx)
        self.preset.blockSignals(False)

    def on_preset(self, index: int):
        i = self.preset.itemData(index)
        if i is None or i < 0:
            return
        st, en = self._preset_vals[i]
        for w in (self.start, self.end):
            w.blockSignals(True)
        self.start.setTime(qtime_from_minutes(st))
        self.end.setTime(qtime_from_minutes(en))
        for w in (self.start, self.end):
            w.blockSignals(False)
        self.sync_preset()

    def save_slot(self):
        sm, em = self.times()
        if em <= sm:
            QMessageBox.warning(self, "Time", "The end time must be after the start time.")
            return
        if any(p["start"] == sm and p["end"] == em for p in PREFS.time_presets):
            QMessageBox.information(self, "Saved timespan", "This timespan is already saved.")
            return
        label = f"{fmt_time(sm)}–{fmt_time(em)}"
        name, ok = QInputDialog.getText(self, "Save timespan", f"Name for {label} (optional):")
        if not ok:
            return
        PREFS.set_time_presets(PREFS.time_presets + [{"name": name.strip(), "start": sm, "end": em}])
        self.fill_presets()

    # ---- icon ----------------------------------------------------------------
    def current_icon_key(self) -> str:
        mode = self.icon_mode.currentIndex()
        if mode == 1:
            t = self.emoji_edit.text().strip()
            return f"emoji:{t}" if t else ""
        if mode == 2:
            return self.file_key
        return ""

    def update_icon_ui(self):
        mode = self.icon_mode.currentIndex()
        self.emoji_edit.setVisible(mode == 1)
        self.add_img_btn.setVisible(mode == 2)
        self.gallery.setVisible(mode == 2)
        self.update_icon_preview()

    def update_icon_preview(self):
        img = icon_image(self.pl, self.current_icon_key())
        if img is None:
            self.icon_preview.clear()
        else:
            self.icon_preview.setPixmap(QPixmap.fromImage(
                img.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)))

    def on_emoji_edited(self, text: str):
        g = graphemes(text.strip())
        new = g[-1] if g else ""          # keep only the newest emoji
        if new != text:
            self.emoji_edit.setText(new)
        self.update_icon_preview()

    def gallery_ids(self) -> list:
        ids = list(reversed(list(PREFS.icons)))          # most recently used first
        for i in self.pl.used_icon_ids():                # icons that came with a shared planner
            if i not in ids:
                ids.append(i)
        return ids

    def thumb(self, iid: str) -> QIcon:
        img = icon_image(self.pl, "img:" + iid)
        if img is None:                                  # linked file is gone
            pm = QPixmap(36, 36)
            pm.fill(QColor("#E5E7EB"))
            p = QPainter(pm)
            p.setPen(QColor("#9CA3AF"))
            p.drawText(pm.rect(), Qt.AlignCenter, "?")
            p.end()
        else:
            pm = QPixmap.fromImage(img.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        ic = QIcon()
        ic.addPixmap(pm, QIcon.Normal)
        ic.addPixmap(pm, QIcon.Selected)                 # no grey-blue tint when selected
        return ic

    def tip_for(self, iid: str) -> str:
        e = PREFS.icons.get(iid)
        v = self.pl.icons.get(iid)
        path = (e or {}).get("path") or (v.get("path") if isinstance(v, dict) else "")
        parts = [(e or {}).get("name") or "Icon"]
        if path:
            parts.append(f"Linked to {path} (too large to store inside the planner)"
                         + ("" if os.path.isfile(path) else " — file not found"))
        if e is None:
            parts.append("Only in this planner")
        parts.append("Right-click for options")
        return "\n".join(parts)

    def populate_gallery(self):
        self.gallery.blockSignals(True)
        self.gallery.clear()
        for iid in self.gallery_ids():
            it = QListWidgetItem(self.thumb(iid), "")
            it.setData(Qt.UserRole, iid)
            it.setToolTip(self.tip_for(iid))
            self.gallery.addItem(it)
            if "img:" + iid == self.file_key:
                self.gallery.setCurrentItem(it)
        self.gallery.blockSignals(False)

    def on_gallery_selected(self, cur, _prev):
        if cur is not None:
            self.file_key = "img:" + cur.data(Qt.UserRole)
            self.update_icon_preview()

    def gallery_menu(self, pos):
        it = self.gallery.itemAt(pos)
        if it is None:
            return
        iid = it.data(Qt.UserRole)
        menu = QMenu(self)
        if iid in PREFS.icons:
            stays = iid in self.pl.used_icon_ids()
            act = menu.addAction("Remove from my icons" + ("  (stays in this planner)" if stays else ""))
            act.triggered.connect(lambda *_: self.remove_icon(iid))
        elif iid in self.pl.icons:
            act = menu.addAction("Add to my icons")
            act.triggered.connect(lambda *_: self.add_to_my_icons(iid))
        else:
            return
        menu.exec(self.gallery.viewport().mapToGlobal(pos))

    def add_to_my_icons(self, iid: str):
        PREFS.add_from_planner(iid, self.pl.icons[iid])
        self.populate_gallery()

    def remove_icon(self, iid: str):
        self.pl.ensure_icons()                 # blocks using it keep their own copy
        PREFS.remove_icon(iid)
        if iid not in self.pl.used_icon_ids() and self.file_key == "img:" + iid:
            self.file_key = ""
        self.populate_gallery()
        self.update_icon_preview()

    def add_images(self):
        last = self.settings.value("last_icon_dir", "", type=str)
        paths, _ = QFileDialog.getOpenFileNames(self, "Add icon images", last, ICON_FILE_FILTER)
        if not paths:
            return
        self.settings.setValue("last_icon_dir", os.path.dirname(paths[0]))
        linked, errors, last_id = [], [], None
        for path in paths:
            try:
                iid, embedded = PREFS.add_icon_file(path)
            except Exception as ex:      # noqa: BLE001
                errors.append(f"{os.path.basename(path)}: {ex}")
                continue
            last_id = iid
            if not embedded:
                linked.append(os.path.basename(path))
        if last_id:
            self.file_key = "img:" + last_id
        self.populate_gallery()
        self.update_icon_preview()
        if errors:
            QMessageBox.warning(self, "Icon", "\n".join(errors))
        if linked:
            QMessageBox.information(
                self, "Large image",
                "These images are too large to store inside the planner file, so the planner "
                "links to them instead:\n\n• " + "\n• ".join(linked) +
                "\n\nThey won't show if the planner is opened on another computer "
                "or if the image files are moved.")

    # ---- accept ----------------------------------------------------------------
    def accept(self):
        title = self.title.text().strip()
        desc = self.desc.toPlainText().strip()
        sm, em = self.times()
        if em <= sm:
            QMessageBox.warning(self, "Time", "The end time must be after the start time.")
            return
        if not title and not desc:
            QMessageBox.warning(self, "Block", "Please enter a title or a description.")
            return
        if self.editing:
            days = [self.day_combo.currentData()]
        else:
            days = [d for d, cb in self.day_boxes if cb.isChecked()]
            if not days:
                QMessageBox.warning(self, "Day", "Select at least one day.")
                return
        key = self.current_icon_key()
        out = []
        pick = self._col_opts[max(0, self.col_combo.currentIndex())][1]
        skipped = []
        for i, d in enumerate(days):
            c = pick(d)
            if c is None:
                skipped.append(self.pl.day_label(d))
                continue
            ev = Event(title=title, description=desc, day=d, start=sm, end=em,
                       icon=key, color=self.colors.color, hide_end=self.hide_end.isChecked(),
                       col=c)
            if self.editing and i == 0:
                ev.id = self.ev.id
            out.append(ev)
        self.result_events = out
        if skipped:
            QMessageBox.information(self, "Column", "Not added on " + ", ".join(skipped) +
                                    ": no column with that name.")
        if key.startswith("img:"):
            PREFS.touch_icon(key[4:])
        super().accept()


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, path: Optional[str] = None):
        super().__init__()
        self.settings = QSettings("Ukeplan", "Ukeplan")
        self.pl = Planner()
        self.path: Optional[str] = None
        self.dirty = False
        self.undo: list = []
        self.redo: list = []
        self._syncing = False
        self._title_undo_pushed = False
        self._note_undo_pushed = False
        self.L: Optional[Layout] = None
        self._bad_icon_events: set = set()
        self.resize(1280, 800)
        self._build_ui()
        self._build_actions()
        if path:
            self.load_path(path)
        self.refresh()

    # ---- UI ------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        root = QHBoxLayout(central)
        side = QWidget()
        side.setFixedWidth(340)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)

        box = QGroupBox("Planner")
        form = QFormLayout(box)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Title shown at the top of the page")
        self.title_edit.textEdited.connect(self.on_title)
        self.title_edit.editingFinished.connect(lambda: setattr(self, "_title_undo_pushed", False))
        form.addRow("Title", self.title_edit)
        self.note_edit = NoteEdit()
        self.note_edit.setPlaceholderText("Small note under the timetable (optional)")
        self.note_edit.setFixedHeight(62)
        self.note_edit.textChanged.connect(self.on_note)
        self.note_edit.editingFinished.connect(lambda: setattr(self, "_note_undo_pushed", False))
        form.addRow("Note", self.note_edit)
        prow = QHBoxLayout()
        self.page_combo = QComboBox()
        self.page_combo.addItems(list(PAGES))
        self.page_combo.currentIndexChanged.connect(self.on_page)
        self.orient_combo = QComboBox()
        self.orient_combo.addItems(["Landscape", "Portrait"])
        self.orient_combo.currentIndexChanged.connect(self.on_page)
        prow.addWidget(self.page_combo)
        prow.addWidget(self.orient_combo)
        form.addRow("Paper", prow)

        self.day_grid = QGridLayout()
        self.day_boxes = []
        form.addRow("Days", self.day_grid)
        self.rebuild_day_boxes()
        self.cols_btn = QPushButton("Columns…")
        self.cols_btn.setToolTip("Split days into named columns, for example one per class")
        self.cols_btn.clicked.connect(self.edit_columns)
        form.addRow("", self.cols_btn)

        self.auto_chk = QCheckBox("Automatic (fit to blocks)")
        self.auto_chk.toggled.connect(self.on_range)
        form.addRow("Hours", self.auto_chk)
        rrow = QHBoxLayout()
        self.from_spin, self.to_spin = QSpinBox(), QSpinBox()
        self.from_spin.setRange(0, 23)
        self.to_spin.setRange(1, 24)
        for sp in (self.from_spin, self.to_spin):
            sp.setSuffix(":00")
            sp.valueChanged.connect(self.on_range)
        rrow.addWidget(self.from_spin)
        rrow.addWidget(QLabel("to"))
        rrow.addWidget(self.to_spin)
        form.addRow("", rrow)
        sl.addWidget(box)

        ebox = QGroupBox("Blocks")
        el = QVBoxLayout(ebox)
        self.listw = QListWidget()
        self.listw.setSelectionMode(QAbstractItemView.SingleSelection)
        self.listw.currentItemChanged.connect(lambda *_: self.preview.set_selected(self.selected_id()))
        self.listw.itemDoubleClicked.connect(lambda it: self.edit_event(it.data(Qt.UserRole)))
        el.addWidget(self.listw, 1)
        brow = QHBoxLayout()
        for text, fn in (("Add…", lambda: self.add_event()), ("Edit…", lambda: self.edit_event()),
                         ("Duplicate", self.duplicate_event), ("Delete", self.delete_event)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            brow.addWidget(b)
        el.addLayout(brow)
        self.warn = QLabel()
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("color:#B91C1C;")
        el.addWidget(self.warn)
        sl.addWidget(ebox, 1)

        self.preview = Preview()
        self.preview.eventClicked.connect(self.select_event)
        self.preview.eventDoubleClicked.connect(lambda i: self.edit_event(i))
        self.preview.cellDoubleClicked.connect(lambda d, m, c: self.add_event(day=d, start=m, col=c))
        root.addWidget(side)
        root.addWidget(self.preview, 1)
        self.setCentralWidget(central)

    def _build_actions(self):
        def act(text, slot, shortcut=None, icon_text=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(shortcut)
            return a
        self.a_new = act("New", self.new_planner, QKeySequence.New)
        self.a_open = act("Open…", self.open_planner, QKeySequence.Open)
        self.a_save = act("Save", self.save, QKeySequence.Save)
        self.a_saveas = act("Save as…", self.save_as, "Ctrl+Shift+S")
        self.a_pdf = act("Export PDF…", self.do_export_pdf, "Ctrl+E")
        self.a_png = act("Export PNG…", self.do_export_png)
        self.a_html = act("Export HTML…", self.do_export_html)
        self.a_print = act("Print…", self.do_print, QKeySequence.Print)
        self.a_undo = act("Undo", self.do_undo, QKeySequence.Undo)
        self.a_redo = act("Redo", self.do_redo, QKeySequence.Redo)
        self.a_add = act("Add block…", lambda: self.add_event(), "Ctrl+B")
        self.a_del = act("Delete block", self.delete_event, QKeySequence.Delete)
        self.a_quit = act("Quit", self.close, QKeySequence.Quit)
        self.a_slots = act("Saved timespans…", self.edit_timespans)
        self.a_cols = act("Columns within days…", self.edit_columns)
        self.a_days = act("Edit days…", self.edit_days)

        fm = self.menuBar().addMenu("&File")
        for a in (self.a_new, self.a_open, "recent", self.a_save, self.a_saveas, None,
                  self.a_pdf, self.a_png, self.a_html, self.a_print, None, self.a_quit):
            if a is None:
                fm.addSeparator()
            elif a == "recent":
                self.recent_menu = fm.addMenu("Open recent")
            else:
                fm.addAction(a)
        self.rebuild_recent_menu()
        em = self.menuBar().addMenu("&Edit")
        for a in (self.a_undo, self.a_redo, None, self.a_add, self.a_del, None, self.a_cols):
            em.addSeparator() if a is None else em.addAction(a)

        sm = self.menuBar().addMenu("&Settings")
        sm.addAction(self.a_days)
        sm.addAction(self.a_slots)

        tb = QToolBar("Main")
        tb.setMovable(False)
        for a in (self.a_new, self.a_open, self.a_save, self.a_saveas, None, self.a_add, None,
                  self.a_pdf, self.a_print, None, self.a_undo, self.a_redo):
            tb.addSeparator() if a is None else tb.addAction(a)
        self.addToolBar(tb)

    # ---- state helpers ---------------------------------------------------
    def selected_id(self) -> Optional[str]:
        it = self.listw.currentItem()
        return it.data(Qt.UserRole) if it else None

    def find(self, eid: Optional[str]) -> Optional[Event]:
        return next((e for e in self.pl.events if e.id == eid), None)

    def set_dirty(self, v: bool):
        self.dirty = v
        name = os.path.basename(self.path) if self.path else "Untitled"
        self.setWindowTitle(f"{name}{' *' if v else ''} — {APP_NAME}")

    def _snapshot(self) -> str:
        return json.dumps(self.pl.to_dict())

    def push_undo(self):
        self.undo.append(self._snapshot())
        del self.undo[:-60]
        self.redo.clear()

    def mutate(self, fn):
        self.push_undo()
        fn()
        self.set_dirty(True)
        self.refresh()

    def do_undo(self):
        if self.undo:
            self.redo.append(self._snapshot())
            self.pl = Planner.from_dict(json.loads(self.undo.pop()))
            self.set_dirty(True)
            self.refresh()

    def do_redo(self):
        if self.redo:
            self.undo.append(self._snapshot())
            self.pl = Planner.from_dict(json.loads(self.redo.pop()))
            self.set_dirty(True)
            self.refresh()

    def refresh(self):
        self.pl.ensure_icons()
        self.sync_ui()
        self.relayout()

    def rebuild_day_boxes(self):
        for cb in self.day_boxes:
            self.day_grid.removeWidget(cb)
            cb.deleteLater()
        self.day_boxes = []
        for i, name in enumerate(self.pl.day_names):
            cb = QCheckBox(name.replace("&", "&&"))
            cb.setProperty("day_name", name)
            cb.toggled.connect(self.on_days)
            self.day_grid.addWidget(cb, i // 2, i % 2)
            self.day_boxes.append(cb)

    def sync_ui(self):
        self._syncing = True
        try:
            pl = self.pl
            if [cb.property("day_name") for cb in self.day_boxes] != pl.day_names:
                self.rebuild_day_boxes()
            if self.title_edit.text() != pl.title:
                self.title_edit.setText(pl.title)
            if self.note_edit.toPlainText() != pl.note:
                self.note_edit.setPlainText(pl.note)
            self.page_combo.setCurrentText(pl.page)
            self.orient_combo.setCurrentIndex(0 if pl.landscape else 1)
            for i, cb in enumerate(self.day_boxes):
                cb.setChecked(i in pl.days)
            self.auto_chk.setChecked(pl.auto_range)
            self.from_spin.setValue(pl.range_start)
            self.to_spin.setValue(pl.range_end)
            self.from_spin.setEnabled(not pl.auto_range)
            self.to_spin.setEnabled(not pl.auto_range)
        finally:
            self._syncing = False

    def relayout(self):
        W, H = (v * MM for v in self.pl.page_mm())
        self.L = compute_layout(self.pl, W, H)
        gone = set(missing_icons(self.pl))
        bad = [e for e in self.pl.events if e.icon.startswith("img:") and e.icon[4:] in gone]
        self._bad_icon_events = {e.id for e in bad}
        self.update_list()
        self.preview.set_data(self.pl, self.L, self.selected_id())
        n = sum(1 for pb in self.L.placed if pb.fit.overflow)
        msgs = []
        if n:
            msgs.append(
                f"⚠ {n} block{'s' if n != 1 else ''} (red dashed in the preview) "
                "don't have room for all their text. Shorten the text, give the block "
                "more time or width, narrow the hour range or switch to A3.")
        if bad:
            msgs.append(
                f"⚠ {len(bad)} block{'s' if len(bad) != 1 else ''} (marked with ! in the list) "
                "use an icon image that can't be found: it was linked and the file has moved "
                "or been deleted. Edit the block and pick the icon again.")
        self.warn.setText("\n\n".join(msgs))

    def update_list(self):
        sel = self.selected_id()
        over = {pb.ev.id for pb in self.L.placed if pb.fit.overflow} | self._bad_icon_events
        self.listw.blockSignals(True)
        self.listw.clear()
        for ev in sorted(self.pl.events, key=lambda e: (e.day, e.col, e.start, e.end)):
            cols = col_names(self.pl.subcols.get(ev.day, []))
            where = self.pl.day_short(ev.day)
            if 0 <= ev.col < len(cols):
                where += "/" + cols[ev.col]
            text = f"{where}  {fmt_time(ev.start)}–{fmt_time(ev.end)}  {ev.title or '(no title)'}"
            hidden = ev.day not in self.pl.days
            if hidden:
                text += "  (day hidden)"
            if ev.id in over:
                text = "! " + text
            pm = QPixmap(14, 14)
            pm.fill(QColor(ev.color))
            item = QListWidgetItem(QIcon(pm), text)
            item.setData(Qt.UserRole, ev.id)
            if hidden:
                item.setForeground(QColor("#9CA3AF"))
            elif ev.id in over:
                item.setForeground(QColor("#DC2626"))
            self.listw.addItem(item)
            if ev.id == sel:
                self.listw.setCurrentItem(item)
        self.listw.blockSignals(False)

    # ---- planner settings handlers --------------------------------------
    def on_title(self, text):
        if self._syncing:
            return
        if not self._title_undo_pushed:
            self.push_undo()
            self._title_undo_pushed = True
        self.pl.title = text
        self.set_dirty(True)
        self.relayout()

    def on_note(self):
        if self._syncing:
            return
        if not self._note_undo_pushed:
            self.push_undo()
            self._note_undo_pushed = True
        self.pl.note = self.note_edit.toPlainText()
        self.set_dirty(True)
        self.relayout()

    def on_page(self):
        if self._syncing:
            return
        page = self.page_combo.currentText()
        land = self.orient_combo.currentIndex() == 0
        self.mutate(lambda: (setattr(self.pl, "page", page), setattr(self.pl, "landscape", land)))

    def on_days(self):
        if self._syncing:
            return
        days = [i for i, cb in enumerate(self.day_boxes) if cb.isChecked()]
        self.mutate(lambda: setattr(self.pl, "days", days))

    def on_range(self):
        if self._syncing:
            return
        auto = self.auto_chk.isChecked()
        a, b = self.from_spin.value(), self.to_spin.value()
        if b <= a:
            if self.sender() is self.to_spin:
                a = max(0, b - 1)
            else:
                b = min(24, a + 1)

        def apply():
            self.pl.auto_range, self.pl.range_start, self.pl.range_end = auto, a, b
        self.mutate(apply)

    # ---- events -----------------------------------------------------------
    def select_event(self, eid):
        for i in range(self.listw.count()):
            if self.listw.item(i).data(Qt.UserRole) == eid:
                self.listw.setCurrentRow(i)
                break

    def add_event(self, day=None, start=None, col=None):
        if day is None:
            cur = self.find(self.selected_id())
            day = cur.day if cur else None
        dlg = EventDialog(self, self.pl, day=day, start=start, col=col)
        if dlg.exec() == QDialog.Accepted:
            new = dlg.result_events
            self.mutate(lambda: self.pl.events.extend(new))
            self.select_event(new[0].id)

    def edit_event(self, eid=None):
        ev = self.find(eid or self.selected_id())
        if not ev:
            return
        dlg = EventDialog(self, self.pl, ev)
        if dlg.exec() == QDialog.Accepted:
            new = dlg.result_events[0]

            def apply():
                i = self.pl.events.index(ev)
                self.pl.events[i] = new
            self.mutate(apply)
            self.select_event(new.id)

    def duplicate_event(self):
        ev = self.find(self.selected_id())
        if ev:
            dup = copy.copy(ev)
            dup.id = uuid.uuid4().hex[:10]
            self.mutate(lambda: self.pl.events.append(dup))
            self.select_event(dup.id)

    def delete_event(self):
        ev = self.find(self.selected_id())
        if ev:
            self.mutate(lambda: self.pl.events.remove(ev))

    # ---- files -------------------------------------------------------------
    def _dir(self) -> str:
        return self.settings.value("last_dir", os.path.expanduser("~"), type=str)

    def _remember(self, path: str):
        self.settings.setValue("last_dir", os.path.dirname(path))
        path = os.path.abspath(path)
        rec = [path] + [r for r in self.recent_files() if r != path]
        self.settings.setValue("recent", rec[:10])
        self.rebuild_recent_menu()

    def recent_files(self) -> list:
        v = self.settings.value("recent", [])
        if isinstance(v, str):
            v = [v]
        return [str(x) for x in (v or [])]

    def rebuild_recent_menu(self):
        self.recent_menu.clear()
        files = [f for f in self.recent_files() if os.path.isfile(f)]
        for f in files:
            a = self.recent_menu.addAction(os.path.basename(f))
            a.setToolTip(f)
            a.triggered.connect(lambda _=False, f=f: self.open_recent(f))
        if files:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("Clear menu").triggered.connect(self.clear_recent)
        self.recent_menu.setEnabled(bool(files))

    def clear_recent(self):
        self.settings.setValue("recent", [])
        self.rebuild_recent_menu()

    def open_recent(self, path: str):
        if self.maybe_save():
            self.load_path(path)

    def maybe_save(self) -> bool:
        if not self.dirty:
            return True
        r = QMessageBox.question(self, "Unsaved changes", "Save changes to this planner?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard

    def new_planner(self):
        if not self.maybe_save():
            return
        self.pl, self.path = Planner(), None
        self.undo.clear()
        self.redo.clear()
        self.set_dirty(False)
        self.refresh()

    def open_planner(self):
        if not self.maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open planner", self._dir(), FILE_FILTER)
        if path:
            self.load_path(path)

    def load_path(self, path: str):
        try:
            self.pl = read_planner(path)
        except Exception as ex:      # noqa: BLE001
            QMessageBox.critical(self, "Open", f"Could not open the file:\n{ex}")
            return
        self.path = path
        self._remember(path)
        self.undo.clear()
        self.redo.clear()
        self.set_dirty(False)
        self.refresh()

    def save(self) -> bool:
        if not self.path:
            return self.save_as()
        return self._write(self.path)

    def save_as(self) -> bool:
        default = self.path or os.path.join(self._dir(), safe_name(self.pl.title) + FILE_EXT)
        path, _ = QFileDialog.getSaveFileName(self, "Save planner as", default, FILE_FILTER)
        if not path:
            return False
        if not path.lower().endswith(FILE_EXT):
            path += FILE_EXT
        return self._write(path)

    def _write(self, path: str) -> bool:
        try:
            write_planner(self.pl, path)
        except OSError as ex:
            QMessageBox.critical(self, "Save", f"Could not save:\n{ex}")
            return False
        self.path = path
        self._remember(path)
        self.set_dirty(False)
        self.statusBar().showMessage(f"Saved {path}", 4000)
        return True

    def do_export_pdf(self):
        default = os.path.join(self._dir(), safe_name(self.pl.title) + ".pdf")
        path, _ = QFileDialog.getSaveFileName(self, "Export PDF", default, "PDF (*.pdf)")
        if path:
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            export_pdf(self.pl, path)
            self.statusBar().showMessage(f"Exported {path}", 5000)

    def do_export_png(self):
        default = os.path.join(self._dir(), safe_name(self.pl.title) + ".png")
        path, _ = QFileDialog.getSaveFileName(self, "Export PNG", default, "PNG (*.png)")
        if path:
            if not path.lower().endswith(".png"):
                path += ".png"
            export_png(self.pl, path, 200)
            self.statusBar().showMessage(f"Exported {path}", 5000)

    def do_export_html(self):
        icons = "embed"
        if any(e.icon.startswith("img:") for e in self.pl.events):
            icons = ask_html_icons(self)
            if icons is None:
                return
        default = os.path.join(self._dir(), safe_name(self.pl.title) + ".html")
        path, _ = QFileDialog.getSaveFileName(self, "Export HTML", default, "HTML (*.html)")
        if path:
            if not path.lower().endswith((".html", ".htm")):
                path += ".html"
            try:
                export_html(self.pl, path, icons)
            except OSError as ex:
                QMessageBox.critical(self, "Export", f"Could not export:\n{ex}")
                return
            self.statusBar().showMessage(f"Exported {path}", 5000)

    def do_print(self):
        print_planner(self.pl, self)

    def edit_days(self):
        dlg = DaysDialog(self, self.pl)
        if dlg.exec() != QDialog.Accepted:
            return
        m, names, added = dlg.mapping, dlg.names, dlg.added

        def apply():
            keep = []
            for ev in self.pl.events:
                if ev.day in m:
                    ev.day = m[ev.day]
                    keep.append(ev)
            self.pl.events[:] = keep
            self.pl.subcols = {m[d]: n for d, n in self.pl.subcols.items() if d in m}
            shown = {m[d] for d in self.pl.days if d in m} | set(added)
            self.pl.days = sorted(shown)
            self.pl.day_names = names
        self.mutate(apply)

    def edit_columns(self):
        dlg = ColumnsDialog(self, self.pl)
        if dlg.exec() != QDialog.Accepted:
            return
        new, remap = dlg.new_subcols, dlg.remap

        def apply():
            keep = []
            for ev in self.pl.events:
                names = new.get(ev.day, [])
                old_n = len(self.pl.subcols.get(ev.day, []))
                if ev.col >= 0:
                    if not names or ev.col >= old_n:
                        ev.col = -1                      # day no longer split
                    elif ev.col in remap[ev.day]:
                        ev.col = remap[ev.day][ev.col]
                    else:
                        continue                         # its column was removed
                keep.append(ev)
            self.pl.events[:] = keep
            self.pl.subcols = {d: n for d, n in new.items() if n}
        self.mutate(apply)

    def edit_timespans(self):
        TimeSlotsDialog(self).exec()

    def closeEvent(self, e):
        if self.maybe_save():
            e.accept()
        else:
            e.ignore()


def safe_name(title: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "_", title.strip()) or "Ukeplan"
    return s


# --------------------------------------------------------------------------
def main(argv: list) -> int:
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    PREFS.load()
    args = argv[1:]
    export = None
    if "--export" in args:
        i = args.index("--export")
        export = args[i + 1]
        del args[i:i + 2]
    path = args[0] if args else None

    if export:
        pl = read_planner(path) if path else Planner()
        if export.lower().endswith(".png"):
            export_png(pl, export, 200)
        elif export.lower().endswith((".html", ".htm")):
            export_html(pl, export, "folder" if "--icons-folder" in argv else "embed")
        else:
            export_pdf(pl, export)
        return 0

    win = MainWindow(path)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
