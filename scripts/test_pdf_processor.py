#!/usr/bin/env python3
"""
Test script that mimics lambda/pdf-processor/index.py for local file processing.
Prints total document pages and processing time.

Now also:
- Detects horizontal lines (rules) on each page via PyMuPDF's get_drawings()
- Stores them in structured_data[*].graphics
- Builds a "visual" text representation where lines above / below numbers
  are rendered as "=========" markers in plain text.
"""

import time
import sys
import json
import fitz  # PyMuPDF
import pymupdf4llm
from typing import Dict, List, Any, Optional


class BoundingBox:
    def __init__(self, x0: float, y0: float, x1: float, y1: float):
        self.x0 = x0
        self.y0 = y0
        self.x1 = x1
        self.y1 = y1

    def to_dict(self) -> Dict[str, float]:
        return {
            "x0": float(self.x0),
            "y0": float(self.y0),
            "x1": float(self.x1),
            "y1": float(self.y1),
        }


class WordBoundingBox:
    def __init__(
        self,
        page: int,
        text: str,
        bbox: Dict[str, float],
        absolute_bbox: Dict[str, float],
        page_dimensions: Dict[str, float],
        block_no: int,
        line_no: int,
        word_no: int,
    ):
        self.page = page
        self.text = text
        self.bbox = bbox
        self.absolute_bbox = absolute_bbox
        self.page_dimensions = page_dimensions
        self.block_no = block_no
        self.line_no = line_no
        self.word_no = word_no

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page": int(self.page),
            "text": str(self.text),
            "bbox": self.bbox,
            "absolute_bbox": self.absolute_bbox,
            "page_dimensions": self.page_dimensions,
            "block_no": int(self.block_no),
            "line_no": int(self.line_no),
            "word_no": int(self.word_no),
        }


class ImageData:
    def __init__(
        self,
        number: int,
        bbox: Dict[str, float],
        transform: List[float],
        width: int,
        height: int,
        colorspace: int,
        cs_name: str,
        xres: int,
        yres: int,
        bpc: int,
        size: int,
    ):
        self.number = number
        self.bbox = bbox
        self.transform = transform
        self.width = width
        self.height = height
        self.colorspace = colorspace
        self.cs_name = cs_name
        self.xres = xres
        self.yres = yres
        self.bpc = bpc
        self.size = size

    def to_dict(self) -> Dict[str, Any]:
        return {
            "number": int(self.number),
            "bbox": self.bbox,
            "transform": [float(x) for x in self.transform],
            "width": int(self.width),
            "height": int(self.height),
            "colorspace": int(self.colorspace),
            "cs_name": str(self.cs_name),
            "xres": int(self.xres),
            "yres": int(self.yres),
            "bpc": int(self.bpc),
            "size": int(self.size),
        }


class DocumentInfo:
    def __init__(
        self,
        page_count: int,
        file_size: int,
        title: str,
        author: str,
        subject: str,
        creator: str,
    ):
        self.page_count = page_count
        self.file_size = file_size
        self.title = title
        self.author = author
        self.subject = subject
        self.creator = creator

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_count": int(self.page_count),
            "file_size": int(self.file_size),
            "title": str(self.title or ""),
            "author": str(self.author or ""),
            "subject": str(self.subject or ""),
            "creator": str(self.creator or ""),
        }


class PageData:
    def __init__(
        self,
        metadata: Dict[str, Any],
        toc_items: List[Any],
        tables: List[Any],
        images: List[ImageData],
        graphics: List[Any],
        text: str,
        words: List[Any],
        visual_text: str = "",
    ):
        self.metadata = metadata
        self.toc_items = toc_items
        self.tables = tables
        self.images = images
        self.graphics = graphics  # will hold horizontal line dicts
        self.text = text
        self.words = words
        self.visual_text = visual_text  # text with "=========" markers

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metadata": self.metadata,
            "toc_items": self.toc_items,
            "tables": self.tables,
            "images": [img.to_dict() for img in self.images],
            "graphics": self.graphics,
            "text": str(self.text),
            "words": self.words,
            "visual_text": str(self.visual_text),
        }


class PDFProcessingResponse:
    def __init__(
        self,
        success: bool,
        document_info: DocumentInfo,
        word_bounding_boxes: List[WordBoundingBox],
        word_count: int,
        structured_data: List[PageData],
        error: Optional[str] = None,
        error_type: Optional[str] = None,
    ):
        self.success = success
        self.document_info = document_info
        self.word_bounding_boxes = word_bounding_boxes
        self.word_count = word_count
        self.structured_data = structured_data
        self.error = error
        self.error_type = error_type

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "success": bool(self.success),
            "document_info": self.document_info.to_dict(),
            "word_bounding_boxes": [wb.to_dict() for wb in self.word_bounding_boxes],
            "word_count": int(self.word_count),
            "structured_data": [pd.to_dict() for pd in self.structured_data],
        }

        if self.error:
            result["error"] = str(self.error)
        if self.error_type:
            result["error_type"] = str(self.error_type)

        return result


def extract_horizontal_lines(
    page: fitz.Page,
    page_width: float,
    page_height: float,
    h_tol: float = 1.5,        # vertical tolerance
    min_len: float = 10.0,     # minimum line length in points
    debug: bool = False,
) -> List[Dict[str, Any]]:
    """
    Extract horizontal line-like graphics from a page using get_drawings().

    Returns a list of dicts with both absolute and normalized coordinates.
    """
    horizontal_lines: List[Dict[str, Any]] = []

    drawings = page.get_drawings()
    if debug:
        print(f"[DEBUG] Page {page.number + 1}: get_drawings() returned {len(drawings)} drawing objects")

    for d_idx, d in enumerate(drawings):
        width = float(d.get("width", 0.0) or 0.0)
        items = d.get("items", [])

        if debug:
            print(f"[DEBUG]   Drawing #{d_idx}: {len(items)} items, stroke width={width}")

        for item in items:
            op = item[0]

            # Straight line segments
            if op == "l":
                p1 = item[1]
                p2 = item[2]
                x0, y0 = float(p1[0]), float(p1[1])
                x1, y1 = float(p2[0]), float(p2[1])

                if abs(y0 - y1) <= h_tol:
                    length = abs(x1 - x0)
                    if length >= min_len:
                        y_mid = (y0 + y1) / 2.0
                        line_dict = {
                            "type": "line",
                            "x0": x0,
                            "x1": x1,
                            "y": y_mid,
                            "width": width,
                            "length": length,
                            "x0_norm": x0 / page_width,
                            "x1_norm": x1 / page_width,
                            "y_norm": y_mid / page_height,
                            "length_norm": length / page_width,
                        }
                        horizontal_lines.append(line_dict)

            # Thin rectangles used as rules
            elif op == "re":
                rect: fitz.Rect = item[1]
                x0, y0, x1, y1 = (
                    float(rect.x0),
                    float(rect.y0),
                    float(rect.x1),
                    float(rect.y1),
                )
                height = abs(y1 - y0)
                width_rect = abs(x1 - x0)

                if height <= h_tol and width_rect >= min_len:
                    y_mid = (y0 + y1) / 2.0
                    line_dict = {
                        "type": "rect",
                        "x0": x0,
                        "x1": x1,
                        "y": y_mid,
                        "width": width,
                        "length": width_rect,
                        "x0_norm": x0 / page_width,
                        "x1_norm": x1 / page_width,
                        "y_norm": y_mid / page_height,
                        "length_norm": width_rect / page_width,
                    }
                    horizontal_lines.append(line_dict)

    if debug:
        print(f"[DEBUG] Page {page.number + 1}: detected {len(horizontal_lines)} horizontal line candidates\n")

    return horizontal_lines


def build_visual_text_with_rules(
    page_words: List[Dict[str, Any]],
    horizontal_lines: List[Dict[str, Any]],
    v_gap_max: float = 10.0,
    h_overlap_min_ratio: float = 0.4,
) -> str:
    """
    Build a "visual" text representation from words and horizontal lines.

    - Groups words into visual lines based on y-position.
    - If a line has a horizontal rule just above any numeric word, inserts
      "=========" above that text line.
    - If a line has a horizontal rule just below any numeric word, inserts
      "=========" below that text line.
    """

    if not page_words:
        return ""

    # 1. Group words into lines by y (basic clustering)
    line_y_tol = 3.0  # points
    # Sort words top-to-bottom by y0
    sorted_words = sorted(page_words, key=lambda w: w["y0"])

    lines: List[Dict[str, Any]] = []

    for w in sorted_words:
        y_center = (w["y0"] + w["y1"]) / 2.0
        assigned = False
        for line in lines:
            if abs(y_center - line["y_center"]) <= line_y_tol:
                line["words"].append(w)
                # update y_min, y_max, y_center
                line["y_min"] = min(line["y_min"], w["y0"])
                line["y_max"] = max(line["y_max"], w["y1"])
                line["y_center"] = (line["y_min"] + line["y_max"]) / 2.0
                assigned = True
                break
        if not assigned:
            lines.append(
                {
                    "words": [w],
                    "y_min": w["y0"],
                    "y_max": w["y1"],
                    "y_center": y_center,
                }
            )

    # Sort lines top-to-bottom
    lines.sort(key=lambda ln: ln["y_center"])

    def has_rule_for_line(line: Dict[str, Any], *, above: bool) -> bool:
        """
        Check if this visual line has a horizontal rule above or below
        any numeric word in the line.
        """
        y_min = line["y_min"]
        y_max = line["y_max"]

        for word in line["words"]:
            text = word["text"]
            if not any(ch.isdigit() for ch in text):
                continue

            wx0, wy0, wx1, wy1 = word["x0"], word["y0"], word["x1"], word["y1"]

            for hl in horizontal_lines:
                ly = hl["y"]
                lx0 = hl["x0"]
                lx1 = hl["x1"]

                # vertical constraint
                if above:
                    # line above the word: ly < wy0, but not too far
                    if not (ly <= wy0 and (wy0 - ly) <= v_gap_max):
                        continue
                else:
                    # line below the word: ly > wy1, but not too far
                    if not (ly >= wy1 and (ly - wy1) <= v_gap_max):
                        continue

                # horizontal overlap between this word and the rule
                overlap = min(wx1, lx1) - max(wx0, lx0)
                if overlap <= 0:
                    continue

                word_width = max(wx1 - wx0, 1e-6)
                if (overlap / word_width) >= h_overlap_min_ratio:
                    return True

        return False

    # 3. Build string lines with "=========" markers
    out_lines: List[str] = []

    for line in lines:
        # sort words left-to-right
        line_words = sorted(line["words"], key=lambda w: w["x0"])
        text_line = " ".join(w["text"] for w in line_words).strip()
        if not text_line:
            continue

        rule_above = has_rule_for_line(line, above=True)
        rule_below = has_rule_for_line(line, above=False)

        if rule_above:
            out_lines.append("=========")

        out_lines.append(text_line)

        if rule_below:
            out_lines.append("=========")

    return "\n".join(out_lines)


def extract_text_with_bounding_boxes(
    pdf_document: fitz.Document, pdf_data: bytes
) -> tuple[List[WordBoundingBox], List[PageData]]:
    """
    Extract page-level markdown and word-level bounding boxes from PDF.
    Uses pymupdf4llm with page_chunks=True for proper page-level markdown generation.

    Additionally:
    - Detects horizontal lines via get_drawings()
    - Stores them in PageData.graphics
    - Builds a visual text representation (PageData.visual_text) with "========="
      above/below numbers where rules exist nearby.

    Returns:
        tuple: (word_bounding_boxes, structured_page_data)
    """
    try:
        # Generate page-level markdown using page_chunks=True
        page_chunks = pymupdf4llm.to_markdown(pdf_document, page_chunks=True)

        structured_page_data: List[PageData] = []

        for page_chunk in page_chunks:
            page_markdown = page_chunk.get("text", "")
            page_metadata = page_chunk.get("metadata", {})
            page_tables = page_chunk.get("tables", [])
            page_images = page_chunk.get("images", [])

            page_data_obj = PageData(
                metadata=page_metadata,
                toc_items=[],
                tables=page_tables,
                images=[],   # images handled separately if needed
                graphics=[], # we will populate with horizontal line info later
                text=page_markdown.strip(),
                words=[],    # we won't use this in detail here
                visual_text="",
            )
            structured_page_data.append(page_data_obj)

    except Exception as e:
        print(f"ERROR: PDF extraction failed: {str(e)}")
        raise e

    # Extract word-level bounding boxes and horizontal lines
    word_bounding_boxes: List[WordBoundingBox] = []
    page_count = len(pdf_document)

    for page_num in range(page_count):
        page = pdf_document[page_num]

        # Page dimensions
        page_rect = page.rect
        page_width = float(page_rect.width)
        page_height = float(page_rect.height)

        # Extract horizontal lines for this page
        horizontal_lines = extract_horizontal_lines(
            page,
            page_width,
            page_height,
            h_tol=1.5,
            min_len=10.0,
            debug=False,
        )

        # Per-page word list for visual text building
        page_words: List[Dict[str, Any]] = []

        # Extract word-level bounding boxes
        words = page.get_text("words")
        for word_info in words:
            x0, y0, x1, y1, word_text, block_no, line_no, word_no = word_info

            # For global word_bounding_boxes
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

            page_dimensions = {
                "width": page_width,
                "height": page_height,
            }

            word_bbox = WordBoundingBox(
                page=int(page_num + 1),
                text=str(word_text),
                bbox=normalized_bbox,
                absolute_bbox=absolute_bbox,
                page_dimensions=page_dimensions,
                block_no=int(block_no),
                line_no=int(line_no),
                word_no=int(word_no),
            )
            word_bounding_boxes.append(word_bbox)

            # For per-page visual lines
            page_words.append(
                {
                    "text": str(word_text),
                    "x0": float(x0),
                    "y0": float(y0),
                    "x1": float(x1),
                    "y1": float(y1),
                }
            )

        # Attach line data and visual text to PageData
        if page_num < len(structured_page_data):
            pd_obj = structured_page_data[page_num]
            pd_obj.graphics = horizontal_lines
            pd_obj.visual_text = build_visual_text_with_rules(
                page_words, horizontal_lines
            )

    return word_bounding_boxes, structured_page_data


def process_pdf_from_file(file_path: str) -> Dict[str, Any]:
    """
    Process PDF from local file path and return results.

    Args:
        file_path: Path to local PDF file

    Returns:
        dict: Processing results with markdown, bounding boxes, and line graphics
    """
    try:
        start_time = time.time()

        # Read PDF file
        with open(file_path, "rb") as f:
            pdf_data = f.read()

        file_size = len(pdf_data)

        # Open PDF
        pdf_document = fitz.open(stream=pdf_data, filetype="pdf")

        try:
            word_bounding_boxes, structured_data = extract_text_with_bounding_boxes(
                pdf_document, pdf_data
            )

            metadata = pdf_document.metadata
            page_count = len(pdf_document)

            processing_time = time.time() - start_time

            print("=== PDF Processing Complete ===")
            print(f"Total pages: {page_count}")
            print(f"Processing time: {processing_time:.2f} seconds")
            print(f"File size: {file_size:,} bytes")
            print(f"Word count: {len(word_bounding_boxes):,}")
            print(f"Pages processed: {len(structured_data)}")

            document_info = DocumentInfo(
                page_count=int(page_count),
                file_size=int(file_size),
                title=str(metadata.get("title", "") or ""),
                author=str(metadata.get("author", "") or ""),
                subject=str(metadata.get("subject", "") or ""),
                creator=str(metadata.get("creator", "") or ""),
            )

            response = PDFProcessingResponse(
                success=True,
                document_info=document_info,
                word_bounding_boxes=word_bounding_boxes,
                word_count=int(len(word_bounding_boxes)),
                structured_data=structured_data,
            )

            return response.to_dict()

        finally:
            pdf_document.close()

    except Exception as e:
        print(f"ERROR: Error processing PDF: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
        }


def main():
    if len(sys.argv) != 2:
        print("Usage: python test_pdf_processor.py <path_to_pdf_file>")
        sys.exit(1)

    pdf_file_path = sys.argv[1]

    print(f"Processing PDF: {pdf_file_path}")
    result = process_pdf_from_file(pdf_file_path)

    if not result["success"]:
        print(f"Processing failed: {result.get('error', 'Unknown error')}")
        sys.exit(1)

    print("Processing completed successfully!")

    for i, page in enumerate(result["structured_data"], start=1):
        print("\n==================== PAGE", i, "(pymupdf4llm markdown) ====================")
        print(page["text"])

        print("\n-------------------- PAGE", i, "(visual with rules) --------------------")
        print(page["visual_text"] or "[no visual text built]")


if __name__ == "__main__":
    main()
