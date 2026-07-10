"""Burn audit review annotations into a PDF with PyMuPDF (fitz).

This is the server-side replacement for the client-side pdf-lib exporter
(ui/components/pdf-viewer/pdf-export-service.ts +
enhanced-pdf-annotations.ts). It is a faithful port of that drawing logic,
adapted to PyMuPDF's coordinate model.

Coordinate model
----------------
Incoming bboxes are ``{x1, y1, x2, y2}`` in TOP-LEFT origin, PDF points
(1/72"), in the page's DISPLAYED (rotated) orientation. ``page_number`` /
``page_no`` is 1-based.

PyMuPDF's page coordinate space is ALSO top-left origin. ``page.insert_text``
places the baseline at ``point.y`` (top-left origin), and ``add_text_annot``
places the icon rect in displayed space. So incoming coordinates are passed
straight through to fitz Point/Rect — NO Y-flip and NO manual rotation math
(unlike the pdf-lib source, which flips to the bottom-left MediaBox space via
``webToDrawCoords``).

Save strategy
-------------
* If MuPDF had to repair a malformed original (``doc.is_repaired``), an
  incremental save is impossible, so we do a full clean save
  (``garbage=3, clean=True, deflate=True``) which repairs it into a valid PDF
  while preserving native + our annotations.
* Otherwise we ``saveIncr()`` — appends only, so the original bytes remain an
  exact byte prefix of the output (native comments untouched).
"""

import base64
import json
import logging
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

import fitz

logger = logging.getLogger(__name__)

# ─── Fonts ────────────────────────────────────────────────────────────────
# IBM Plex Mono for badge codes; Noto Sans Symbols 2 for ✓/✗ glyphs that fall
# outside Plex's WinAnsi + Latin Extended range. Copied into the lambda
# package at build time (see fonts/) — no network in lambda.
_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
PLEX_FONT_PATH = os.path.join(_FONT_DIR, "IBMPlexMono-Regular.ttf")
NOTO_FONT_PATH = os.path.join(_FONT_DIR, "NotoSansSymbols2-Tickmarks.ttf")

# Fontnames used for on-page embedding via insert_text. Consistent names let
# PyMuPDF embed each TTF once per page and reuse it.
_PLEX_FONTNAME = "PlexMono"
_NOTO_FONTNAME = "NotoSym"

# IBM Plex Mono ships WinAnsi + Latin Extended; route anything past this
# codepoint to the Noto symbol fallback. Mirrors pickMonoFamily() in
# enhanced-pdf-annotations.ts.
_ASCII_AND_LATIN1_LIMIT = 0x017F

# Match CoverSheet.tsx COLOR.green / COLOR.red exactly.
VALIDATION_COLOR = (0x1E / 255, 0x6B / 255, 0x34 / 255)
EXCEPTION_COLOR = (0xA8 / 255, 0x20 / 255, 0x1A / 255)

# Combined-badge delimiter (middle dot) rendered in neutral gray.
_DELIMITER = "·"  # ·
_DELIMITER_COLOR = (0.4, 0.4, 0.4)

# Default stroke color for drawn proof marks (line / arrow / double_line) —
# matches overlay red (#dc2626).
_DEFAULT_LINE_COLOR = (0.86, 0.15, 0.15)

# Cover-sheet sentinel link scheme emitted by CoverSheet.tsx.
_URI_PREFIX = "agentive-goto:"
# /XYZ destinations scroll the target point to the top of the viewport; add
# breathing room above so the row isn't flush with the toolbar (matches
# rewrite-cover-links.ts TOP_MARGIN). In top-left space "above" = smaller y.
_TOP_MARGIN = 36

# Lazily-loaded fitz.Font objects used only for text width measurement
# (advance-width layout). Distinct from on-page embedding.
_measure_fonts: Dict[str, "fitz.Font"] = {}


def _measure_font(family: str) -> "fitz.Font":
    font = _measure_fonts.get(family)
    if font is None:
        path = NOTO_FONT_PATH if family == "noto" else PLEX_FONT_PATH
        font = fitz.Font(fontfile=path)
        _measure_fonts[family] = font
    return font


# ─── Small ports of the TS helpers ────────────────────────────────────────
def _pick_mono_family(text: str) -> str:
    """'noto' if any char is outside Plex's WinAnsi+Latin1 range, else 'plex'."""
    for ch in text:
        if ord(ch) > _ASCII_AND_LATIN1_LIMIT:
            return "noto"
    return "plex"


def _fontname_for_family(family: str) -> str:
    return _NOTO_FONTNAME if family == "noto" else _PLEX_FONTNAME


def _fontfile_for_family(family: str) -> str:
    return NOTO_FONT_PATH if family == "noto" else PLEX_FONT_PATH


def _tick_semantic_color(tick_mark: Dict[str, Any]) -> Tuple[float, float, float]:
    return (
        EXCEPTION_COLOR
        if tick_mark.get("acceptance_type") == "exception"
        else VALIDATION_COLOR
    )


def _tick_badge_text(tick_mark: Dict[str, Any]) -> str:
    key = tick_mark.get("key", "")
    file_index = tick_mark.get("fileIndex")
    return f"{key}-{file_index}" if file_index else f"{key}"


def _adaptive_font_size(bbox_height: float) -> float:
    """Scale tick-mark text down on compact financials (port of getAdaptiveFontSize)."""
    if bbox_height >= 14:
        return 8.0
    return float(max(5, round(bbox_height * 0.55)))


def _is_bottom_right(tick_mark: Dict[str, Any]) -> bool:
    tt = tick_mark.get("test_types") or []
    return "F/CF" in tt or "RX" in tt


# Order in which top-right ticks render within a combined badge.
_TOP_RIGHT_KEY_ORDER = {"PY": 0, "IC": 1}


def _top_right_sort_key(tm: Dict[str, Any]) -> Tuple[int, int]:
    key_rank = _TOP_RIGHT_KEY_ORDER.get(tm.get("key"), 99)
    exception_rank = 1 if tm.get("acceptance_type") == "exception" else 0
    return (key_rank, exception_rank)


def _sort_top_right(tick_marks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(tick_marks, key=_top_right_sort_key)


def _natural_key(s: str):
    """Natural-sort key so 'IC-2' < 'IC-10' (mirrors the Intl.Collator numeric mode)."""
    import re

    return [
        int(t) if t.isdigit() else t.lower()
        for t in re.split(r"(\d+)", s or "")
    ]


def _sort_bottom_right(tick_marks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # exception (0) before validation (1); tiebreak natural-sort on id.
    def rank(tm):
        return 0 if tm.get("acceptance_type") == "exception" else 1

    return sorted(
        tick_marks, key=lambda tm: (rank(tm), _natural_key(str(tm.get("id", ""))))
    )


def _hex_to_rgb(hex_str: Optional[str]) -> Optional[Tuple[float, float, float]]:
    """#RRGGBB -> normalized RGB triple, or None for malformed input."""
    if not hex_str or not isinstance(hex_str, str):
        return None
    import re

    m = re.match(r"^#?([0-9a-fA-F]{6})$", hex_str.strip())
    if not m:
        return None
    n = int(m.group(1), 16)
    return (((n >> 16) & 0xFF) / 255, ((n >> 8) & 0xFF) / 255, (n & 0xFF) / 255)


# Named colors fallback (normalized). Port of convertColorToRgbArray.
_NAMED_COLORS = {
    "red": (1, 0, 0),
    "green": (0, 1, 0),
    "blue": (0, 0, 1),
    "yellow": (1, 1, 0),
    "orange": (1, 0.5, 0),
    "purple": (0.5, 0, 0.5),
    "pink": (1, 0.75, 0.8),
    "gray": (0.5, 0.5, 0.5),
    "black": (0, 0, 0),
    "white": (1, 1, 1),
}


def _convert_color_to_rgb(color_string: Optional[str]) -> Tuple[float, float, float]:
    """Port of EnhancedPdfAnnotations.convertColorToRgbArray."""
    if isinstance(color_string, str) and color_string.startswith("#"):
        rgb = _hex_to_rgb(color_string)
        if rgb is not None:
            return rgb
    if isinstance(color_string, str):
        named = _NAMED_COLORS.get(color_string.lower())
        if named is not None:
            return named
    return (0, 0, 1)  # Default to blue


def _location_key(page_no: int, bbox: Dict[str, float]) -> str:
    return f"{page_no}|{bbox['x1']}|{bbox['y1']}|{bbox['x2']}|{bbox['y2']}"


# ─── Drawing primitives ───────────────────────────────────────────────────
def _to_raw(page: "fitz.Page", x: float, y: float) -> "fitz.Point":
    """Map a DISPLAYED (rotated) top-left point to the page's UNROTATED
    raw-MediaBox space for fitz content methods (insert_text / draw_line),
    which draw in raw space and do not auto-apply /Rotate.

    Identity when page.rotation == 0, so rotation-0 output is byte/pixel
    identical to passing the point straight through. (Native annotations via
    add_text_annot handle rotation themselves and are left untouched.)
    """
    return fitz.Point(x, y) * page.derotation_matrix


def _draw_tick_text(
    page: "fitz.Page",
    x: float,
    y: float,
    text: str,
    color: Tuple[float, float, float],
    font_size: float,
) -> None:
    """Draw a single tick-mark badge segment as page content at baseline (x, y)."""
    if not text:
        return
    family = _pick_mono_family(text)
    try:
        # Advance-width layout is computed in displayed space by the caller;
        # map the baseline point to raw space and counter-rotate the glyphs by
        # the page rotation so the badge reads upright once /Rotate is applied.
        page.insert_text(
            _to_raw(page, x, y),
            text,
            fontsize=font_size,
            fontname=_fontname_for_family(family),
            fontfile=_fontfile_for_family(family),
            color=color,
            rotate=page.rotation,
        )
    except Exception as err:  # pragma: no cover - defensive, mirrors TS warn+skip
        logger.warning("Failed to draw tick text %r (family=%s): %s", text, family, err)


def _draw_single_tick(
    page: "fitz.Page", x: float, y: float, tick_mark: Dict[str, Any], font_size: float
) -> None:
    _draw_tick_text(
        page, x, y, _tick_badge_text(tick_mark), _tick_semantic_color(tick_mark), font_size
    )


def _draw_combined_tick(
    page: "fitz.Page",
    x: float,
    y: float,
    tick_marks: List[Dict[str, Any]],
    font_size: float,
) -> float:
    """Render multiple ticks at one slot as a single horizontal badge.

    Returns total drawn width so the caller can position comment icons past
    the badge. Port of createCombinedTickMarkAnnotation.
    """
    if not tick_marks:
        return 0.0

    delimiter_width = _measure_font("plex").text_length(_DELIMITER, fontsize=font_size)

    cursor_x = x
    total_width = 0.0
    for i, tick_mark in enumerate(tick_marks):
        segment_text = _tick_badge_text(tick_mark)
        family = _pick_mono_family(segment_text)
        segment_width = _measure_font(family).text_length(segment_text, fontsize=font_size)

        _draw_tick_text(
            page, cursor_x, y, segment_text, _tick_semantic_color(tick_mark), font_size
        )
        cursor_x += segment_width
        total_width += segment_width

        if i < len(tick_marks) - 1:
            _draw_tick_text(page, cursor_x, y, _DELIMITER, _DELIMITER_COLOR, font_size)
            cursor_x += delimiter_width
            total_width += delimiter_width

    return total_width


def _draw_line(
    page: "fitz.Page",
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    color: Tuple[float, float, float],
    thickness: float = 2,
    with_arrow: bool = False,
) -> None:
    """Draw a stroke from (x1,y1)->(x2,y2), optional arrowhead. Port of
    createEditableLineAnnotation. Arrowhead trig is in top-left space; because
    an arrowhead is symmetric a vertical mirror vs the bottom-left source is
    visually identical."""
    import math

    # Endpoints (and arrowhead legs) are computed in displayed space, then each
    # mapped to raw space for draw_line (identity when page.rotation == 0).
    try:
        page.draw_line(
            _to_raw(page, x1, y1), _to_raw(page, x2, y2), color=color, width=thickness
        )
    except Exception as err:  # pragma: no cover
        logger.warning("Failed to draw line: %s", err)
        return

    if not with_arrow:
        return

    dx = x2 - x1
    dy = y2 - y1
    length = math.sqrt(dx * dx + dy * dy)
    if length <= 0:
        return
    ux = dx / length
    uy = dy / length
    head_len = 10
    head_angle = math.pi / 6
    cos_a = math.cos(head_angle)
    sin_a = math.sin(head_angle)
    left_x = x2 - head_len * (ux * cos_a + uy * sin_a)
    left_y = y2 - head_len * (uy * cos_a - ux * sin_a)
    right_x = x2 - head_len * (ux * cos_a - uy * sin_a)
    right_y = y2 - head_len * (uy * cos_a + ux * sin_a)
    try:
        page.draw_line(
            _to_raw(page, x2, y2), _to_raw(page, left_x, left_y),
            color=color, width=thickness,
        )
        page.draw_line(
            _to_raw(page, x2, y2), _to_raw(page, right_x, right_y),
            color=color, width=thickness,
        )
    except Exception as err:  # pragma: no cover
        logger.warning("Failed to draw arrowhead: %s", err)


def _draw_double_line(
    page: "fitz.Page",
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    color: Tuple[float, float, float],
    thickness: float = 2,
    gap: float = 4,
) -> None:
    """Two parallel strokes offset along the perpendicular. Port of
    createEditableDoubleLineAnnotation."""
    import math

    dx = x2 - x1
    dy = y2 - y1
    length = math.sqrt(dx * dx + dy * dy)
    if length <= 0:
        return
    # Perpendicular unit vector: rotate (dx, dy) 90° -> (-dy, dx).
    px = -dy / length
    py = dx / length
    half = gap / 2
    ox = px * half
    oy = py * half
    # Offsets computed in displayed space, then each endpoint mapped to raw
    # space for draw_line (identity when page.rotation == 0).
    try:
        page.draw_line(
            _to_raw(page, x1 + ox, y1 + oy), _to_raw(page, x2 + ox, y2 + oy),
            color=color, width=thickness,
        )
        page.draw_line(
            _to_raw(page, x1 - ox, y1 - oy), _to_raw(page, x2 - ox, y2 - oy),
            color=color, width=thickness,
        )
    except Exception as err:  # pragma: no cover
        logger.warning("Failed to draw double line: %s", err)


def _add_text_note(
    page: "fitz.Page",
    x: float,
    y: float,
    content: Optional[str],
    icon: str = "Comment",
    color: Optional[Tuple[float, float, float]] = None,
    author: Optional[str] = None,
    subject_override: Optional[str] = None,
) -> None:
    """Add a native sticky Text annotation. Port of
    createEditableCommentAnnotation.

    The pdf-lib source centers a 16x16 icon rect on (x, y); add_text_annot
    anchors the icon at its top-left corner, so we offset by (-8, -8) to match.
    """
    # Guard against null/empty content (port).
    if (
        content is None
        or (isinstance(content, str) and content.strip() == "")
        or content == "null"
        or content == "undefined"
    ):
        logger.warning("Skipping text annotation - empty or invalid content")
        return

    # Subject + default color follow the icon name (port). Standalone comments
    # and RC citations pass no color override -> yellow default.
    subject = "Comment"
    default_color: Tuple[float, float, float] = (1, 1, 0)  # Yellow
    if icon == "Key":
        subject, default_color = "Key Point", (1, 0.5, 0)
    elif icon == "Note":
        subject, default_color = "Note", (0, 0.78, 1)
    elif icon == "Help":
        subject, default_color = "Help", (1, 0, 1)
    elif icon == "Insert":
        subject, default_color = "Insert", (0, 1, 0)

    if subject_override is not None:
        subject = subject_override

    stroke = color if color is not None else default_color

    try:
        annot = page.add_text_annot(fitz.Point(x - 8, y - 8), content, icon=icon)
        annot.set_colors(stroke=stroke)
        annot.set_opacity(0.9)
        info_kwargs: Dict[str, str] = {"content": content, "subject": subject}
        if author and author.strip() and author not in ("null", "undefined"):
            info_kwargs["title"] = author
        annot.set_info(**info_kwargs)
        annot.set_open(False)
        # Print flag (F=4) so the note prints, matching the pdf-lib F:4.
        annot.set_flags(fitz.PDF_ANNOT_IS_PRINT)
        annot.update()
    except Exception as err:
        logger.error("Failed to add text annotation: %s", err)
        raise


# ─── Section handlers (mirror pdf-export-service.ts) ──────────────────────
def _add_manual_annotation_tick_marks(
    doc: "fitz.Document", manual_annotations: List[Dict[str, Any]]
) -> None:
    for annotation in manual_annotations:
        tick_mark = annotation.get("tick_mark")
        if not tick_mark:
            continue
        bbox = annotation.get("bbox")
        page_number = annotation.get("page_number")
        if not bbox or not page_number:
            continue
        page_index = page_number - 1
        if page_index < 0 or page_index >= doc.page_count:
            continue

        page = doc[page_index]
        bbox_height = bbox["y2"] - bbox["y1"]
        font_size = _adaptive_font_size(bbox_height)
        icon_size = 16

        if _is_bottom_right(tick_mark):
            x = bbox["x2"] + icon_size / 2
            y = bbox["y2"]
        else:
            x = bbox["x2"] + icon_size / 2
            y = max(bbox["y1"], bbox["y2"] - max(icon_size, font_size + 1))

        _draw_single_tick(page, x, y, tick_mark, font_size)


def _add_detected_number_tick_marks(
    doc: "fitz.Document", numbers_with_tick_marks: List[Dict[str, Any]]
) -> Dict[str, float]:
    """Returns badge widths keyed by location (max across both slots)."""
    badge_width_by_location: Dict[str, float] = {}

    for number in numbers_with_tick_marks:
        bbox = number.get("bbox")
        page_no = number.get("page_no")
        if not bbox or not page_no:
            continue
        page_index = page_no - 1
        if page_index < 0 or page_index >= doc.page_count:
            continue

        page = doc[page_index]
        bbox_height = bbox["y2"] - bbox["y1"]
        font_size = _adaptive_font_size(bbox_height)

        all_ticks = number.get("tickMarks") or []
        bottom_right = _sort_bottom_right([t for t in all_ticks if _is_bottom_right(t)])
        top_right = [t for t in all_ticks if not _is_bottom_right(t)]

        # F/CF + RX — bottom-right.
        if len(bottom_right) == 1:
            _draw_single_tick(page, bbox["x2"] + 2, bbox["y2"], bottom_right[0], font_size)
        elif len(bottom_right) >= 2:
            width = _draw_combined_tick(
                page, bbox["x2"] + 2, bbox["y2"], bottom_right, font_size
            )
            key = _location_key(page_no, bbox)
            badge_width_by_location[key] = max(
                badge_width_by_location.get(key, 0.0), width
            )

        # IC/PY — top-right.
        top_y = max(bbox["y1"], bbox["y2"] - (font_size + 1))
        if len(top_right) == 1:
            _draw_single_tick(page, bbox["x2"] + 2, top_y, top_right[0], font_size)
        elif len(top_right) >= 2:
            width = _draw_combined_tick(page, bbox["x2"] + 2, top_y, top_right, font_size)
            key = _location_key(page_no, bbox)
            badge_width_by_location[key] = max(
                badge_width_by_location.get(key, 0.0), width
            )

    return badge_width_by_location


def _add_standalone_comments(
    doc: "fitz.Document", comments: List[Dict[str, Any]]
) -> None:
    for comment in comments:
        bbox = comment.get("bbox")
        page_no = comment.get("page_no")
        if not bbox or not page_no:
            continue
        page_index = page_no - 1
        if page_index < 0 or page_index >= doc.page_count:
            continue

        page = doc[page_index]
        shape = comment.get("shape")

        if shape in ("line", "arrow", "double_line"):
            color = _hex_to_rgb(comment.get("color")) or _DEFAULT_LINE_COLOR
            if shape == "double_line":
                _draw_double_line(
                    page, bbox["x1"], bbox["y1"], bbox["x2"], bbox["y2"], color
                )
            else:
                _draw_line(
                    page,
                    bbox["x1"],
                    bbox["y1"],
                    bbox["x2"],
                    bbox["y2"],
                    color,
                    with_arrow=(shape == "arrow"),
                )
            continue

        # shape 'comment' / undefined -> sticky Text note at top-left of bbox.
        _add_text_note(
            page,
            bbox["x1"],
            bbox["y1"],
            comment.get("comment"),
            icon="Comment",
            color=None,  # No override -> yellow default
            author=comment.get("author"),
        )


def _add_rc_citation_comments(
    doc: "fitz.Document", rc_citations: List[Dict[str, Any]]
) -> None:
    for citation in rc_citations:
        bbox = citation.get("bbox")
        page_no = citation.get("page_no")
        if not bbox or not page_no:
            continue
        page_index = page_no - 1
        if page_index < 0 or page_index >= doc.page_count:
            continue

        page = doc[page_index]
        _add_text_note(
            page,
            bbox["x1"],
            bbox["y1"],
            citation.get("finding"),
            icon="Comment",
            color=None,
            author="Review Checklist",
        )


def _format_thread(thread: List[Dict[str, Any]]) -> str:
    """Format a comment thread as newline-delimited text. Port of the
    formattedThread logic (Author (timestamp):\\ncontent)."""
    from datetime import datetime

    parts = []
    for entry in thread:
        ts = entry.get("timestamp")
        formatted_date = ts
        try:
            # Parse ISO timestamp -> "Mon D, YYYY, HH:MM AM/PM" (approx toLocaleDateString).
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            formatted_date = dt.strftime("%b %-d, %Y, %I:%M %p")
        except Exception:
            pass
        parts.append(f"{entry.get('author')} ({formatted_date}):\n{entry.get('content')}")
    return "\n\n---\n\n".join(parts)


def _add_tick_mark_comments(
    doc: "fitz.Document",
    tick_mark_comments: List[Dict[str, Any]],
    badge_width_by_location: Dict[str, float],
) -> None:
    icon_stride_x = 14
    single_tick_fallback_width = 20
    gap_past_badge = 8
    icons_per_location: Dict[str, int] = {}

    for tm_comment in tick_mark_comments:
        bbox = tm_comment.get("bbox")
        page_no = tm_comment.get("page_no")
        if not bbox or not page_no:
            continue
        page_index = page_no - 1
        if page_index < 0 or page_index >= doc.page_count:
            continue
        thread = tm_comment.get("thread")
        if not thread:
            continue

        page = doc[page_index]
        loc_key = _location_key(page_no, bbox)
        badge_width = badge_width_by_location.get(loc_key, single_tick_fallback_width)
        index_at_location = icons_per_location.get(loc_key, 0)
        icons_per_location[loc_key] = index_at_location + 1

        x = bbox["x2"] + badge_width + gap_past_badge + index_at_location * icon_stride_x
        y = bbox["y2"] - 8

        formatted_thread = _format_thread(thread)
        color_rgb = _convert_color_to_rgb(tm_comment.get("tickMarkColor"))
        first_author = thread[0].get("author") if thread else None

        _add_text_note(
            page,
            x,
            y,
            formatted_thread,
            icon="Note",
            color=color_rgb,
            author=first_author,
        )


# ─── Cover post-processing (port of rewrite-cover-links.ts + page-labels.ts) ─
def _parse_agentive_goto_uri(uri: str) -> Optional[Dict[str, Any]]:
    if not uri.startswith(_URI_PREFIX):
        return None
    q = uri.find("?")
    if q < 0:
        return None
    from urllib.parse import parse_qs

    params = parse_qs(uri[q + 1 :])

    def _num(name):
        vals = params.get(name)
        if not vals:
            return None
        try:
            return float(vals[0])
        except (TypeError, ValueError):
            return None

    page = _num("page")
    if page is None or page <= 0:
        return None
    x = _num("x")
    y = _num("y")
    return {"page": int(page), "x": x, "y": y}


def _rewrite_cover_links(doc: "fitz.Document", cover_page_count: int) -> int:
    """Rewrite agentive-goto URI links on cover pages into internal GoTo links
    targeting the (now shifted) source pages. Returns count rewritten."""
    if cover_page_count <= 0:
        return 0
    total_pages = doc.page_count
    rewritten = 0
    for page_index in range(min(cover_page_count, total_pages)):
        page = doc[page_index]
        for link in list(page.get_links()):
            if link.get("kind") != fitz.LINK_URI:
                continue
            uri = link.get("uri", "")
            parsed = _parse_agentive_goto_uri(uri)
            if not parsed:
                continue
            target_index = (parsed["page"] - 1) + cover_page_count
            if target_index < 0 or target_index >= total_pages:
                continue

            page.delete_link(link)

            new_link: Dict[str, Any] = {
                "kind": fitz.LINK_GOTO,
                "page": target_index,
                "from": link["from"],
            }
            if parsed["x"] is not None and parsed["y"] is not None:
                # Add breathing room above the target (top-left: smaller y).
                dest_y = max(parsed["y"] - _TOP_MARGIN, 0)
                new_link["to"] = fitz.Point(parsed["x"], dest_y)
            else:
                new_link["to"] = fitz.Point(0, 0)
            page.insert_link(new_link)
            rewritten += 1
    return rewritten


def _apply_cover_page_labels(doc: "fitz.Document", cover_page_count: int) -> None:
    """Cover pages labeled 'Cover 1..'; source pages decimal restarting at 1.
    Port of page-labels.ts using doc.set_page_labels."""
    if cover_page_count <= 0:
        return
    labels = [
        {"startpage": 0, "prefix": "Cover ", "style": "D", "firstpagenum": 1},
    ]
    if doc.page_count > cover_page_count:
        labels.append(
            {"startpage": cover_page_count, "style": "D", "firstpagenum": 1}
        )
    doc.set_page_labels(labels)


# ─── Orchestration ────────────────────────────────────────────────────────
def burn_annotations(
    doc: "fitz.Document", annotation_data: Dict[str, Any]
) -> None:
    """Burn all annotations onto the document's (source) pages, in the same
    order as pdf-export-service.annotatePdfDocument. Call BEFORE inserting the
    cover so page_number-1 indices are 0-based and unshifted."""
    _add_manual_annotation_tick_marks(
        doc, annotation_data.get("manualAnnotations") or []
    )
    badge_width_by_location = _add_detected_number_tick_marks(
        doc, annotation_data.get("numbersWithTickMarks") or []
    )
    _add_standalone_comments(doc, annotation_data.get("comments") or [])
    _add_rc_citation_comments(doc, annotation_data.get("rcCitations") or [])
    _add_tick_mark_comments(
        doc,
        annotation_data.get("tickMarkComments") or [],
        badge_width_by_location,
    )


def build_annotated_pdf(
    original_path: str,
    out_path: str,
    annotation_data: Dict[str, Any],
    cover_bytes: Optional[bytes] = None,
) -> Tuple[str, int]:
    """Burn annotations, optionally prepend a cover, and save.

    Returns (mode, page_count). ``mode`` is 'incremental' or 'full-clean'.

    For the incremental path the doc MUST be opened at ``out_path`` (a copy of
    the original) because saveIncr writes back to the file it opened — so the
    original bytes remain an exact prefix of the output.
    """
    # Copy the original into out_path so an incremental save appends to it.
    if os.path.abspath(original_path) != os.path.abspath(out_path):
        shutil.copyfile(original_path, out_path)

    doc = fitz.open(out_path)
    try:
        repaired = doc.is_repaired

        # Burn onto source pages BEFORE inserting the cover (0-based indices).
        burn_annotations(doc, annotation_data)

        cover_page_count = 0
        if cover_bytes:
            cover = fitz.open(stream=cover_bytes, filetype="pdf")
            try:
                cover_page_count = cover.page_count
                doc.insert_pdf(cover, start_at=0)
            finally:
                cover.close()
            _rewrite_cover_links(doc, cover_page_count)
            _apply_cover_page_labels(doc, cover_page_count)

        page_count = doc.page_count

        if repaired:
            # A repaired original can't take an incremental write; full clean
            # save repairs it into a valid PDF preserving all annotations.
            tmp_path = out_path + ".full"
            doc.save(tmp_path, garbage=3, clean=True, deflate=True)
            doc.close()
            os.replace(tmp_path, out_path)
            return "full-clean", page_count

        # Incremental append: original bytes stay an exact prefix.
        doc.saveIncr()
        doc.close()
        return "incremental", page_count
    except Exception:
        try:
            doc.close()
        except Exception:
            pass
        raise


# ─── Lambda handler entry point ───────────────────────────────────────────
_CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
}


def _parse_s3_path(s3_path: str) -> Tuple[str, str]:
    path_parts = s3_path[5:].split("/", 1)
    if len(path_parts) != 2:
        raise ValueError("Invalid S3 path format")
    return path_parts[0], path_parts[1]


def handle_annotate(body: Dict[str, Any], s3_client) -> Dict[str, Any]:
    """Handle the 'annotate' operation: download original, burn annotations,
    optionally prepend cover, save (incremental or full-clean), upload result.

    Returns the API Gateway response envelope (statusCode/headers/body)."""
    s3_path = body.get("s3_path")
    output_bucket = body.get("output_bucket")
    output_key = body.get("output_key")
    # annotation_data + cover may be passed inline OR staged in S3. S3 is
    # preferred: the synchronous Lambda Invoke request payload is capped at 6 MB
    # (https://docs.aws.amazon.com/lambda/latest/api/API_Invoke.html), and a
    # font-embedding cover sheet plus a heavy annotation set can approach that.
    # The caller (Elixir) uploads both to S3 and passes keys; inline fields are
    # kept as a fallback for small payloads / tests.
    annotation_data = body.get("annotation_data") or {}
    annotation_s3_path = body.get("annotation_s3_path")
    cover_pdf_base64 = body.get("cover_pdf_base64")
    cover_s3_path = body.get("cover_s3_path")

    if not s3_path or not str(s3_path).startswith("s3://"):
        return {
            "statusCode": 400,
            "headers": _CORS_HEADERS,
            "body": json.dumps({"error": "Missing or invalid s3_path"}),
        }
    if not output_bucket or not output_key:
        return {
            "statusCode": 400,
            "headers": _CORS_HEADERS,
            "body": json.dumps({"error": "Missing output_bucket/output_key"}),
        }

    tmp_dir = tempfile.mkdtemp(prefix="annotate-")
    original_path = os.path.join(tmp_dir, "original.pdf")
    out_path = os.path.join(tmp_dir, "annotated.pdf")
    try:
        bucket, key = _parse_s3_path(s3_path)
        s3_client.download_file(bucket, key, original_path)

        # annotation_data: prefer the S3-staged JSON when provided.
        if annotation_s3_path:
            a_bucket, a_key = _parse_s3_path(annotation_s3_path)
            annotation_data = json.loads(
                s3_client.get_object(Bucket=a_bucket, Key=a_key)["Body"].read()
            )

        # cover: prefer the S3-staged PDF; else inline base64.
        cover_bytes = None
        if cover_s3_path:
            c_bucket, c_key = _parse_s3_path(cover_s3_path)
            cover_bytes = s3_client.get_object(Bucket=c_bucket, Key=c_key)["Body"].read()
        elif cover_pdf_base64:
            cover_bytes = base64.b64decode(cover_pdf_base64)

        mode, page_count = build_annotated_pdf(
            original_path, out_path, annotation_data, cover_bytes
        )

        with open(out_path, "rb") as fh:
            out_bytes = fh.read()
        s3_client.put_object(
            Bucket=output_bucket,
            Key=output_key,
            Body=out_bytes,
            ContentType="application/pdf",
        )

        logger.info(
            "annotate complete: output_key=%s mode=%s page_count=%d",
            output_key, mode, page_count,
        )
        return {
            "statusCode": 200,
            "headers": _CORS_HEADERS,
            "body": json.dumps(
                {"output_key": output_key, "mode": mode, "page_count": page_count}
            ),
        }
    except Exception as e:
        import traceback

        tb = traceback.format_exc()
        logger.error("annotate failed: %s\n%s", str(e), tb)
        return {
            "statusCode": 500,
            "headers": _CORS_HEADERS,
            "body": json.dumps(
                {
                    "success": False,
                    "error": f"{str(e)} | traceback: {tb[-500:]}",
                    "error_type": type(e).__name__,
                }
            ),
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
