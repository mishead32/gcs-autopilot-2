"""The overdue list as a picture, for WhatsApp.

WhatsApp will not show a table. It shows a photo, and a photo is read on a
phone in the three seconds somebody gives it between two other messages —
so this draws the same report the escalation email carries, as a PNG: the
three counts across the top, then Delegation, Checklist and FMS, each with
its own rows.

Drawn rather than screenshotted. A real browser is the obvious way to turn
HTML into an image and it does not fit: Render's free instance has 512 MB
and Chromium wants most of that to start, so the one morning it is needed
is the morning it is killed. Pillow is already here for shrinking proof
photos, draws this in a few milliseconds and cannot run out of memory on a
list of thirty rows.

Everything is sized from the text rather than guessed at, so a long task
title or a name in Devanagari makes the row taller instead of spilling out
of the picture.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

# The same palette as the email, so the two are recognisably one system.
INK = (31, 41, 55)
MUTED = (107, 114, 128)
LINE = (221, 227, 234)
BRAND = (15, 76, 129)
URGENT = (185, 28, 28)
STRIPE = (244, 247, 250)
PAPER = (255, 255, 255)
SHELL = (238, 242, 246)

WIDTH = 1000
PAD = 28
ROW_PAD = 12

# DejaVu ships with every Debian-based image, which is what this runs on.
# If it is ever missing, Pillow's built-in font keeps the picture coming —
# ugly, but a legible list beats no list at all.
_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
          "/usr/share/fonts/dejavu/DejaVuSans.ttf")
_BOLDS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
          "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf")


def _font(size: int, bold: bool = False):
    for path in (_BOLDS if bold else _FONTS):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


@dataclass
class Col:
    """One column: its heading, how wide, and whether it is right-aligned."""
    head: str
    width: int
    right: bool = False


COLS = [Col("TASK ID", 150), Col("PLANNED", 130), Col("PRIORITY", 100),
        Col("OVERDUE", 95, right=True), Col("TASK", 0)]   # 0 = whatever is left


def _wrap(draw, text: str, font, width: int) -> list[str]:
    """`text` broken into lines that fit, measured rather than estimated.

    Counting characters is the usual shortcut and it is wrong for every
    name that is not plain ASCII — a Hindi task title has fewer characters
    and takes more room.
    """
    words, lines, line = str(text or "").split(), [], ""
    for word in words:
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines or [""]


def _pill(draw, x, y, text, font, bg, fg):
    """A small rounded label, the same shape the email and the screen use."""
    w = draw.textlength(text, font=font)
    h = font.size + 8
    draw.rounded_rectangle([x, y, x + w + 16, y + h], radius=(h // 2), fill=bg)
    draw.text((x + 8, y + 4), text, font=fg[1], fill=fg[0])
    return h


PILL = {"high": ((254, 226, 226), (153, 27, 27)),
        "medium": ((224, 242, 254), (7, 89, 133)),
        "low": ((238, 242, 247), (71, 85, 105))}


def report_png(title: str, subtitle: str, counts: list[tuple[str, int]],
               sections: list[tuple[str, list]], note: str = "") -> bytes:
    """One report as PNG bytes.

    `counts` is [(label, number)] for the cards. `sections` is
    [(heading, [(task, days overdue)])] — already split and sorted by the
    caller, so this file decides nothing about what is in the picture and
    only about how it looks.

    Drawn twice: once onto a throwaway canvas to find out how tall it needs
    to be, then for real. Measuring first is the only way a wrapped title
    cannot push the last row off the bottom.
    """
    height = _draw(None, title, subtitle, counts, sections, note)
    img = Image.new("RGB", (WIDTH, height), SHELL)
    _draw(ImageDraw.Draw(img), title, subtitle, counts, sections, note)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _draw(draw, title, subtitle, counts, sections, note) -> int:
    """Draws when given a canvas, measures when given None. Returns the height.

    One function for both so the two can never disagree — a separate
    measuring pass is a second copy of the layout that drifts the first
    time somebody changes a margin.
    """
    f_title = _font(30, bold=True)
    f_sub = _font(17)
    f_num = _font(34, bold=True)
    f_label = _font(14, bold=True)
    f_head = _font(14, bold=True)
    f_cell = _font(17)
    f_mono = _font(17, bold=True)
    f_small = _font(14)

    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    y = 0

    # ---- the bar -------------------------------------------------------
    bar_h = PAD + f_title.size + 10 + f_sub.size + PAD
    if draw:
        draw.rectangle([0, 0, WIDTH, bar_h], fill=BRAND)
        draw.text((PAD, PAD), title, font=f_title, fill=PAPER)
        draw.text((PAD, PAD + f_title.size + 10), subtitle, font=f_sub,
                  fill=(219, 234, 254))
    y = bar_h

    # ---- the three counts ----------------------------------------------
    card_h = 104
    y += 20
    if draw and counts:
        gap = 14
        cw = (WIDTH - 2 * PAD - gap * (len(counts) - 1)) // len(counts)
        for i, (label, n) in enumerate(counts):
            x = PAD + i * (cw + gap)
            draw.rounded_rectangle([x, y, x + cw, y + card_h], radius=10,
                                   fill=PAPER, outline=LINE, width=1)
            draw.text((x + 18, y + 16), str(n), font=f_num,
                      fill=URGENT if n else MUTED)
            draw.text((x + 18, y + 16 + f_num.size + 10), label.upper(),
                      font=f_label, fill=MUTED)
    if counts:
        y += card_h
    y += 22

    # ---- the sections --------------------------------------------------
    last = COLS[-1]
    last.width = WIDTH - 2 * PAD - sum(c.width for c in COLS[:-1]) - 12 * 4

    for heading, rows in sections:
        if not rows:
            continue
        if draw:
            draw.text((PAD, y), heading.upper(), font=f_head, fill=BRAND)
        y += f_head.size + 8
        if draw:
            draw.line([PAD, y, WIDTH - PAD, y], fill=BRAND, width=2)
        y += 10

        # column headings
        x = PAD
        for col in COLS:
            if draw:
                tx = (x + col.width - measure.textlength(col.head, font=f_head)
                      if col.right else x)
                draw.text((tx, y), col.head, font=f_head, fill=MUTED)
            x += col.width + 12
        y += f_head.size + 8

        for n, (task, late) in enumerate(rows):
            title_lines = _wrap(measure, task.title, f_cell, last.width)
            row_h = max(f_cell.size + 8, len(title_lines) * (f_cell.size + 5))
            row_h += ROW_PAD * 2

            if draw:
                if n % 2:
                    draw.rectangle([PAD - 8, y, WIDTH - PAD + 8, y + row_h],
                                   fill=STRIPE)
                draw.line([PAD - 8, y + row_h, WIDTH - PAD + 8, y + row_h],
                          fill=LINE, width=1)
                x, ty = PAD, y + ROW_PAD
                draw.text((x, ty), str(task.ref_label or task.id),
                          font=f_mono, fill=BRAND)
                x += COLS[0].width + 12
                draw.text((x, ty), task.due_at.strftime("%d %b %Y"),
                          font=f_cell, fill=INK)
                draw.text((x, ty + f_cell.size + 4),
                          task.due_at.strftime("%I:%M %p"), font=f_small,
                          fill=MUTED)
                x += COLS[1].width + 12
                bg, fg = PILL.get(task.priority.value, PILL["low"])
                _pill(draw, x, ty - 2, task.priority.value.upper(), f_small,
                      bg, (fg, f_small))
                x += COLS[2].width + 12
                days = f"{late} day" + ("s" if late != 1 else "")
                draw.text((x + COLS[3].width
                           - measure.textlength(days, font=f_mono), ty), days,
                          font=f_mono, fill=URGENT if late >= 3 else INK)
                x += COLS[3].width + 12
                for i, line in enumerate(title_lines):
                    draw.text((x, ty + i * (f_cell.size + 5)), line,
                              font=f_cell, fill=INK)
            y += row_h
        y += 22

    # ---- the note under it ---------------------------------------------
    if note:
        lines = _wrap(measure, note, f_small, WIDTH - 2 * PAD)
        if draw:
            for i, line in enumerate(lines):
                draw.text((PAD, y + i * (f_small.size + 5)), line,
                          font=f_small, fill=MUTED)
        y += len(lines) * (f_small.size + 5)
    y += PAD

    return int(y)


# ------------------------------------------------- putting one on the web --
# How long a published picture stays reachable. Long enough that somebody
# who opens the message a few days later still sees the list, short enough
# that a link copied out of a chat stops working before the information in
# it is interesting to anybody.
KEEP_DAYS = 7


def publish(db, org_id: int, png: bytes, about: str = "",
            base_url: str = "") -> tuple[str, str]:
    """Store a picture and give back (its path, its full address).

    The full address is what the WhatsApp provider is handed: it fetches
    the picture from its own servers, so a path alone is no use to it and
    a site address that has not been learned yet means no picture at all —
    which the caller is told about by getting "" back rather than by the
    provider failing an hour later.
    """
    import secrets

    from ..models import PublicImage

    row = PublicImage(org_id=org_id, token=secrets.token_hex(16), data=png,
                      about=(about or "")[:160])
    db.add(row)
    db.commit()
    site = (base_url or "").strip().rstrip("/")
    return row.path, (site + row.path if site else "")


def sweep(db, org_id: int | None = None) -> int:
    """Delete pictures older than KEEP_DAYS. Returns how many went."""
    from datetime import timedelta

    from sqlalchemy import select

    from .. import clock
    from ..models import PublicImage

    cutoff = clock.now() - timedelta(days=KEEP_DAYS)
    q = select(PublicImage).where(PublicImage.created_at < cutoff)
    if org_id is not None:
        q = q.where(PublicImage.org_id == org_id)
    gone = 0
    for row in db.scalars(q).all():
        db.delete(row)
        gone += 1
    if gone:
        db.commit()
    return gone
