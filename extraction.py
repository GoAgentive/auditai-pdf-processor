"""PDF extraction using PyMuPDF with subprocess-based parallelism.

Uses multiprocessing.Process with /tmp file IPC to avoid Lambda's
missing /dev/shm. Each worker opens its own fitz.Document and writes
results to a temp JSON file.
"""

import json
import logging
import multiprocessing
import os
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


def _worker_extract_markdown(pdf_path: str, page_indices: list, output_file: str):
    """Worker process: extract markdown for a subset of pages, write to temp file."""
    doc = fitz.open(pdf_path)
    try:
        result = pymupdf4llm.to_markdown(doc, pages=page_indices, page_chunks=True)
        _sanitize_chunks(result)
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
            return _sanitize_chunks(pymupdf4llm.to_markdown(doc, page_chunks=True))
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
                    graphics_primitives.append(
                        {
                            "type": "line",
                            "p1": {
                                "x": float(p1.x),
                                "y": float(p1.y),
                                "x_norm": float(p1.x) / page_width,
                                "y_norm": float(p1.y) / page_height,
                            },
                            "p2": {
                                "x": float(p2.x),
                                "y": float(p2.y),
                                "x_norm": float(p2.x) / page_width,
                                "y_norm": float(p2.y) / page_height,
                            },
                            "stroke_width": stroke_width,
                            "stroke_color": list(color) if color else None,
                            "fill_color": list(fill) if fill else None,
                        }
                    )
                elif op == "c":
                    p1, p2, p3, p4 = item[1], item[2], item[3], item[4]
                    graphics_primitives.append(
                        {
                            "type": "curve",
                            "p1": {
                                "x": float(p1.x),
                                "y": float(p1.y),
                                "x_norm": float(p1.x) / page_width,
                                "y_norm": float(p1.y) / page_height,
                            },
                            "p2": {
                                "x": float(p2.x),
                                "y": float(p2.y),
                                "x_norm": float(p2.x) / page_width,
                                "y_norm": float(p2.y) / page_height,
                            },
                            "p3": {
                                "x": float(p3.x),
                                "y": float(p3.y),
                                "x_norm": float(p3.x) / page_width,
                                "y_norm": float(p3.y) / page_height,
                            },
                            "p4": {
                                "x": float(p4.x),
                                "y": float(p4.y),
                                "x_norm": float(p4.x) / page_width,
                                "y_norm": float(p4.y) / page_height,
                            },
                            "stroke_width": stroke_width,
                            "stroke_color": list(color) if color else None,
                            "fill_color": list(fill) if fill else None,
                        }
                    )
                elif op == "re":
                    rect = item[1]
                    graphics_primitives.append(
                        {
                            "type": "rect",
                            "bbox": {
                                "x0": float(rect.x0),
                                "y0": float(rect.y0),
                                "x1": float(rect.x1),
                                "y1": float(rect.y1),
                                "x0_norm": float(rect.x0) / page_width,
                                "y0_norm": float(rect.y0) / page_height,
                                "x1_norm": float(rect.x1) / page_width,
                                "y1_norm": float(rect.y1) / page_height,
                            },
                            "stroke_width": stroke_width,
                            "stroke_color": list(color) if color else None,
                            "fill_color": list(fill) if fill else None,
                        }
                    )
                elif op == "qu":
                    quad = item[1]
                    graphics_primitives.append(
                        {
                            "type": "quad",
                            "points": [
                                {
                                    "x": float(quad.ul.x),
                                    "y": float(quad.ul.y),
                                    "x_norm": float(quad.ul.x) / page_width,
                                    "y_norm": float(quad.ul.y) / page_height,
                                },
                                {
                                    "x": float(quad.ur.x),
                                    "y": float(quad.ur.y),
                                    "x_norm": float(quad.ur.x) / page_width,
                                    "y_norm": float(quad.ur.y) / page_height,
                                },
                                {
                                    "x": float(quad.ll.x),
                                    "y": float(quad.ll.y),
                                    "x_norm": float(quad.ll.x) / page_width,
                                    "y_norm": float(quad.ll.y) / page_height,
                                },
                                {
                                    "x": float(quad.lr.x),
                                    "y": float(quad.lr.y),
                                    "x_norm": float(quad.lr.x) / page_width,
                                    "y_norm": float(quad.lr.y) / page_height,
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
