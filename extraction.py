"""PDF extraction using PyMuPDF with subprocess-based parallelism.

Uses multiprocessing.Process with /tmp file IPC to avoid Lambda's
missing /dev/shm. Each worker opens its own fitz.Document and writes
results to a temp JSON file.
"""

from bisect import bisect_right
import json
import logging
import multiprocessing
import os
import re
import tempfile
from typing import Dict, List, Any, Tuple

import fitz
import pymupdf4llm

from models import WordBoundingBox, PageData

logger = logging.getLogger(__name__)

# Number of workers for multiprocessing (Lambda at 10GB = 6 vCPUs)
DEFAULT_WORKERS = int(os.environ.get("PYMUPDF_WORKERS", "6"))
# Minimum pages to justify multiprocessing overhead
MULTIPROCESSING_THRESHOLD = 10


def _sanitize_chunks(chunks: list) -> list:
    """Strip non-serializable fitz objects from pymupdf4llm output.

    pymupdf4llm embeds fitz.Rect in images[].bbox — we don't use image
    metadata downstream, so drop the images list entirely. The text,
    tables, metadata, and graphics fields are already plain Python types.
    """
    for chunk in chunks:
        chunk["images"] = []
    return chunks


_NUMBER = r"-?\d[\d,]*(?:\.\d+)?"
_AMOUNT = re.compile(rf"(?:-|\$?(?:{_NUMBER}|\({_NUMBER}\)))")


def _cell_text(markdown):
    """Compare the text across layout line breaks and numeric superscripts."""
    return re.sub(r"<br>|</?sup>|\*\*|\s+", "", markdown)


def _printed_rows(words):
    """Group words crossing the same horizontal midline, then read left to right."""
    rows = []
    midline = None
    for word in sorted(words, key=lambda w: w[1] + w[3]):
        if midline is None or not word[1] <= midline <= word[3]:
            rows.append([])
            midline = (word[1] + word[3]) / 2
        rows[-1].append(word)
    return [sorted(row, key=lambda w: w[0]) for row in rows]


def _split_period_table(doc, page_index, bbox, markdown):
    """Restore the label column only when the PDF corroborates the entire table."""
    lines = markdown.rstrip().splitlines()
    if len(lines) < 4 or not lines[0].startswith("|") or "<br>" not in lines[0]:
        return markdown
    if not all(line.startswith("|") and line.endswith("|") for line in lines):
        return markdown
    header = lines[0][1:-1].split("|")
    # Each native header cell must contain a piece of the caption and a year.
    header_parts = [[part.strip() for part in cell.split("<br>") if part.strip()]
                    for cell in header]
    if len(header) < 2 or any(len(parts) != 2 for parts in header_parts):
        return markdown
    captions, year_cells = zip(*header_parts)
    years = [_cell_text(cell) for cell in year_cells]
    if not all(re.fullmatch(r"(?:19|20)\d{2}", year) for year in years):
        return markdown
    if len(set(years)) != len(years) or lines[1] != "|" + "---|" * len(years):
        return markdown

    # A period caption spans the printed columns. The next physical line has
    # only years. Use that independent text ordering to rejoin the caption.
    try:
        words = doc[page_index].get_text("words", clip=bbox)
    except (RuntimeError, ValueError) as error:
        logger.warning("Skipping table correction on page %d: %s", page_index + 1, error)
        return markdown
    printed = _printed_rows(words)
    if len(printed) != len(lines) or [w[4] for w in printed[1]] != years:
        return markdown
    centers = [(w[0] + w[2]) / 2 for w in printed[1]]
    if any(right <= left for left, right in zip(centers, centers[1:])):
        return markdown
    # The midpoint between neighboring year headings separates their amounts.
    # Extend the first interval leftward to separate labels from the first year.
    boundaries = [centers[0] - (centers[1] - centers[0]) / 2]
    boundaries.extend((left + right) / 2 for left, right in zip(centers, centers[1:]))
    caption = " ".join(w[4] for w in printed[0])
    if not re.fullmatch(r"[\w ,./()–—-]+", caption):
        return markdown
    if _cell_text("".join(captions)) != _cell_text(caption):
        return markdown

    if all(part.startswith("**") and part.endswith("**") for part in captions):
        caption = f"**{caption}**"
    output = [caption, "", "||" + "|".join(year_cells) + "|", "|" + "---|" * (1 + len(years))]
    split_rows = 0
    for row, source_row in zip(lines[2:], printed[2:]):
        cells = row[1:-1].split("|")
        if len(cells) != len(years):
            return markdown
        columns = [[] for _ in range(len(years) + 1)]
        for x0, _, x1, _, word, *_ in source_row:
            column = bisect_right(boundaries, (x0 + x1) / 2)
            columns[column].append(word)
        label = " ".join(columns[0])
        if not any(ch.isalpha() for ch in label):
            return markdown
        amounts = ["".join(column) for column in columns[1:]]
        if label.endswith(":") and not any(amounts) and not any(map(_cell_text, cells[1:])):
            if _cell_text(cells[0]) != _cell_text(label):
                return markdown
            output.append(row + "|")
            continue
        # Two number tokens in one year column are ambiguous, even if their
        # concatenation happens to spell the value in the native Markdown.
        if any(len(tokens) > 1 and tokens[:-1] != ["$"] for tokens in columns[1:]):
            return markdown
        if not all(_AMOUNT.fullmatch(value) for value in amounts):
            return markdown
        if [_cell_text(cell) for cell in cells[1:]] != amounts[1:]:
            return markdown
        # Retain the original Markdown styling in both halves of the cell.
        parts = cells[0].split("<br>")
        for cut in range(1, len(parts)):
            left, right = "<br>".join(parts[:cut]), "<br>".join(parts[cut:])
            if _cell_text(left) == _cell_text(label) and _cell_text(right) == amounts[0]:
                if left.count("**") % 2 or right.count("**") % 2:
                    return markdown
                output.append("|" + "|".join([left, right, *cells[1:]]) + "|")
                split_rows += 1
                break
        else:
            return markdown

    if split_rows < 2:
        return markdown
    return "\n".join(output) + markdown[len(markdown.rstrip()):]


def _markdown_chunks(doc, pages=None):
    chunks = pymupdf4llm.to_markdown(doc, pages=pages, page_chunks=True)
    for index, chunk in zip(range(len(doc)) if pages is None else pages, chunks):
        if chunk.get("tables"):
            continue
        text = chunk["text"]
        offset = 0
        for box in chunk.get("page_boxes", []):
            start, end = (position + offset for position in box["pos"])
            if box["class"] == "table":
                original = text[start:end]
                corrected = _split_period_table(doc, index, box["bbox"], original)
                if corrected != original:
                    text = text[:start] + corrected + text[end:]
                    offset += len(corrected) - len(original)
                    end = start + len(corrected)
            box["pos"] = (start, end)
        chunk["text"] = text
    return _sanitize_chunks(chunks)


def _worker_extract_markdown(pdf_path: str, page_indices: list, output_file: str):
    """Worker process: extract markdown for a subset of pages, write to temp file."""
    doc = fitz.open(pdf_path)
    try:
        result = _markdown_chunks(doc, pages=page_indices)
        with open(output_file, "w") as f:
            json.dump(result, f)
    finally:
        doc.close()


def extract_markdown_parallel(
    pdf_path: str, page_count: int, n_workers: int = DEFAULT_WORKERS
) -> List[dict]:
    """
    Extract markdown from all pages using multiprocessing.Process.

    Uses /tmp files for IPC instead of shared memory (Lambda has no /dev/shm).
    For small documents, runs sequentially to avoid process spawn overhead.
    """
    if page_count < MULTIPROCESSING_THRESHOLD or n_workers <= 1:
        doc = fitz.open(pdf_path)
        try:
            return _markdown_chunks(doc)
        finally:
            doc.close()

    # Split pages across workers
    chunk_size = page_count // n_workers
    worker_args = []
    temp_files = []

    for i in range(n_workers):
        start = i * chunk_size
        end = start + chunk_size if i < n_workers - 1 else page_count
        page_indices = list(range(start, end))

        tf = tempfile.NamedTemporaryFile(
            dir="/tmp", suffix=".json", delete=False, prefix=f"pymupdf_w{i}_"
        )
        tf.close()
        temp_files.append(tf.name)
        worker_args.append((pdf_path, page_indices, tf.name))

    # Spawn worker processes
    processes = []
    for args in worker_args:
        p = multiprocessing.Process(target=_worker_extract_markdown, args=args)
        p.start()
        processes.append(p)

    # Wait for all workers
    for p in processes:
        p.join(timeout=240)  # 4 min max per worker

    # Collect results from temp files
    all_chunks = []
    for tf_path in temp_files:
        try:
            with open(tf_path, "r") as f:
                chunks = json.load(f)
            all_chunks.extend(chunks)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error("Worker output missing or corrupt: %s (%s)", tf_path, e)
        finally:
            if os.path.exists(tf_path):
                os.remove(tf_path)

    return all_chunks


def extract_words(pdf_document: fitz.Document) -> List[WordBoundingBox]:
    """Extract word-level bounding boxes from all pages."""
    word_bounding_boxes = []

    for page_num in range(len(pdf_document)):
        page = pdf_document[page_num]
        page_rect = page.rect
        page_width = float(page_rect.width)
        page_height = float(page_rect.height)

        words = page.get_text("words")
        for word_info in words:
            x0, y0, x1, y1, word_text, block_no, line_no, word_no = word_info

            normalized_bbox = {
                "x0": float(x0 / page_width),
                "y0": float(y0 / page_height),
                "x1": float(x1 / page_width),
                "y1": float(y1 / page_height),
            }
            absolute_bbox = {
                "x0": float(x0),
                "y0": float(y0),
                "x1": float(x1),
                "y1": float(y1),
            }
            page_dimensions = {"width": page_width, "height": page_height}

            word_bounding_boxes.append(
                WordBoundingBox(
                    page=int(page_num + 1),
                    text=str(word_text),
                    bbox=normalized_bbox,
                    absolute_bbox=absolute_bbox,
                    page_dimensions=page_dimensions,
                    block_no=int(block_no),
                    line_no=int(line_no),
                    word_no=int(word_no),
                )
            )

    return word_bounding_boxes


def _px(p, idx):
    """Extract coordinate from a point that may be a fitz.Point or a plain tuple."""
    try:
        return float(p[idx])
    except (TypeError, KeyError):
        return float(p.x if idx == 0 else p.y)


def extract_graphics(pdf_document: fitz.Document) -> List[List[Dict[str, Any]]]:
    """Extract vector graphics from all pages. Returns list of per-page graphics lists."""
    all_graphics = []

    for page_num in range(len(pdf_document)):
        page = pdf_document[page_num]
        page_rect = page.rect
        page_width = float(page_rect.width)
        page_height = float(page_rect.height)

        graphics_primitives = []
        drawings = page.get_cdrawings()

        for d in drawings:
            stroke_width = float(d.get("width", 0.0) or 0.0)
            color = d.get("color")
            fill = d.get("fill")
            items = d.get("items", [])

            for item in items:
                op = item[0]

                if op == "l":
                    p1, p2 = item[1], item[2]
                    p1x, p1y = _px(p1, 0), _px(p1, 1)
                    p2x, p2y = _px(p2, 0), _px(p2, 1)
                    graphics_primitives.append(
                        {
                            "type": "line",
                            "p1": {
                                "x": p1x,
                                "y": p1y,
                                "x_norm": p1x / page_width,
                                "y_norm": p1y / page_height,
                            },
                            "p2": {
                                "x": p2x,
                                "y": p2y,
                                "x_norm": p2x / page_width,
                                "y_norm": p2y / page_height,
                            },
                            "stroke_width": stroke_width,
                            "stroke_color": list(color) if color else None,
                            "fill_color": list(fill) if fill else None,
                        }
                    )
                elif op == "c":
                    p1, p2, p3, p4 = item[1], item[2], item[3], item[4]
                    p1x, p1y = _px(p1, 0), _px(p1, 1)
                    p2x, p2y = _px(p2, 0), _px(p2, 1)
                    p3x, p3y = _px(p3, 0), _px(p3, 1)
                    p4x, p4y = _px(p4, 0), _px(p4, 1)
                    graphics_primitives.append(
                        {
                            "type": "curve",
                            "p1": {
                                "x": p1x,
                                "y": p1y,
                                "x_norm": p1x / page_width,
                                "y_norm": p1y / page_height,
                            },
                            "p2": {
                                "x": p2x,
                                "y": p2y,
                                "x_norm": p2x / page_width,
                                "y_norm": p2y / page_height,
                            },
                            "p3": {
                                "x": p3x,
                                "y": p3y,
                                "x_norm": p3x / page_width,
                                "y_norm": p3y / page_height,
                            },
                            "p4": {
                                "x": p4x,
                                "y": p4y,
                                "x_norm": p4x / page_width,
                                "y_norm": p4y / page_height,
                            },
                            "stroke_width": stroke_width,
                            "stroke_color": list(color) if color else None,
                            "fill_color": list(fill) if fill else None,
                        }
                    )
                elif op == "re":
                    rect = item[1]
                    rx0, ry0 = float(rect[0]), float(rect[1])
                    rx1, ry1 = float(rect[2]), float(rect[3])
                    graphics_primitives.append(
                        {
                            "type": "rect",
                            "bbox": {
                                "x0": rx0,
                                "y0": ry0,
                                "x1": rx1,
                                "y1": ry1,
                                "x0_norm": rx0 / page_width,
                                "y0_norm": ry0 / page_height,
                                "x1_norm": rx1 / page_width,
                                "y1_norm": ry1 / page_height,
                            },
                            "stroke_width": stroke_width,
                            "stroke_color": list(color) if color else None,
                            "fill_color": list(fill) if fill else None,
                        }
                    )
                elif op == "qu":
                    quad = item[1]
                    ul = quad[0] if isinstance(quad, tuple) else quad.ul
                    ur = quad[1] if isinstance(quad, tuple) else quad.ur
                    ll = quad[2] if isinstance(quad, tuple) else quad.ll
                    lr = quad[3] if isinstance(quad, tuple) else quad.lr
                    graphics_primitives.append(
                        {
                            "type": "quad",
                            "points": [
                                {
                                    "x": _px(ul, 0),
                                    "y": _px(ul, 1),
                                    "x_norm": _px(ul, 0) / page_width,
                                    "y_norm": _px(ul, 1) / page_height,
                                },
                                {
                                    "x": _px(ur, 0),
                                    "y": _px(ur, 1),
                                    "x_norm": _px(ur, 0) / page_width,
                                    "y_norm": _px(ur, 1) / page_height,
                                },
                                {
                                    "x": _px(ll, 0),
                                    "y": _px(ll, 1),
                                    "x_norm": _px(ll, 0) / page_width,
                                    "y_norm": _px(ll, 1) / page_height,
                                },
                                {
                                    "x": _px(lr, 0),
                                    "y": _px(lr, 1),
                                    "x_norm": _px(lr, 0) / page_width,
                                    "y_norm": _px(lr, 1) / page_height,
                                },
                            ],
                            "stroke_width": stroke_width,
                            "stroke_color": list(color) if color else None,
                            "fill_color": list(fill) if fill else None,
                        }
                    )

        all_graphics.append(graphics_primitives)

    return all_graphics


def build_structured_data(
    page_chunks: List[dict],
    graphics_mode: str,
    per_page_graphics: List[List[Dict[str, Any]]] = None,
) -> List[PageData]:
    """Build PageData list from markdown chunks and optional graphics."""
    structured_data = []

    for i, chunk in enumerate(page_chunks):
        page_graphics = []
        if per_page_graphics and i < len(per_page_graphics):
            page_graphics = per_page_graphics[i]

        structured_data.append(
            PageData(
                metadata=chunk.get("metadata", {}),
                toc_items=[],
                tables=chunk.get("tables", []),
                images=[],
                graphics=page_graphics,
                text=chunk.get("text", "").strip(),
                words=[],
            )
        )

    return structured_data


def build_graphics_only_data(
    per_page_graphics: List[List[Dict[str, Any]]],
) -> List[PageData]:
    """Build minimal PageData for graphics_only mode."""
    return [
        PageData(
            metadata={},
            toc_items=[],
            tables=[],
            images=[],
            graphics=page_graphics,
            text="",
            words=[],
        )
        for page_graphics in per_page_graphics
    ]
