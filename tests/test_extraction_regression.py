"""Regression tests for text extraction on Chrome/Skia-generated PDFs.

pymupdf4llm 0.0.18 through 0.2.9 silently dropped text that sits inside
vector-graphics clusters (styled header rects) or on top of raster images
(Skia renders CSS box-shadows as DeviceGray images). Chrome print-to-PDF
output ("Dynamic ... Template" docs, producer "Skia/PDF") triggers both,
so entire header blocks vanished from the markdown while raw fitz text
extraction still saw every word. Fixed upstream in pymupdf4llm 1.27.2.x.
(1.28.0 reintroduces content loss — it drops table cells, including dollar
amounts, on Skia-generated invoices — so the pin stays on 1.27.2.3.)

The fixtures are HeadlessChrome/Skia-generated delivery notes that
reproduce the loss. These tests run the same code paths the Lambda uses
(extraction.extract_markdown_parallel -> pymupdf4llm.to_markdown with
page_chunks=True, plus the quality gates from quality_check.py).
"""

import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import fitz
import pytest

from extraction import extract_markdown_parallel, extract_words
from quality_check import run_early_quality_check, run_markdown_quality_check

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SKIA_PDFS = sorted(FIXTURES.glob("skia_delivery_note_*.pdf"))

# Text that lives inside the vector-drawn header / shadow-image region of
# fixture 2 — exactly the block that versions 0.0.18-0.2.9 dropped.
HEADER_TERMS = [
    "DELIVERY NOTE",
    "Global Distributors",
    "Order Number",
    "Ship Date",
    "Delivery Date",
    "Shipping Terms",
    "FOB Destination",
]


def _markdown_chunks(pdf_path):
    doc = fitz.open(str(pdf_path))
    page_count = len(doc)
    doc.close()
    return extract_markdown_parallel(str(pdf_path), page_count)


def _normalize_markdown(md: str) -> str:
    """Strip markdown syntax so raw-text tokens can be matched as substrings."""
    return re.sub(r"[|*#\s]+", " ", md)


def _ligature_variants(token: str) -> list[str]:
    """Known-acceptable forms of a token when table extraction collapses ligatures.

    pymupdf4llm's table path decodes ligature glyphs one character short
    (e.g. the ffi in "Office" becomes "ff" -> "Offce"). Cosmetic, single-glyph,
    and present in every 1.27.x/1.28.x release — tolerated, unlike dropped text.
    """
    return [
        token,
        token.replace("ffi", "ff"),
        token.replace("ffl", "fl"),
        token.replace("fi", "f"),
        token.replace("fl", "f"),
    ]


def test_fixtures_present():
    assert len(SKIA_PDFS) >= 2, "expected the Skia delivery-note fixtures"


def test_header_block_survives_markdown_extraction():
    """The vector-header / shadow-image text must appear in the markdown."""
    chunks = _markdown_chunks(FIXTURES / "skia_delivery_note_2.pdf")
    md = "\n".join(c["text"] for c in chunks)
    missing = [t for t in HEADER_TERMS if t not in md]
    assert not missing, f"markdown extraction dropped header text: {missing}"


@pytest.mark.parametrize("pdf_path", SKIA_PDFS, ids=lambda p: p.name)
def test_markdown_covers_all_raw_tokens(pdf_path):
    """Every distinct token raw fitz extracts must survive into the markdown.

    This is the generic invariant behind the header regression: markdown
    output may reformat text, but it must not lose content.
    """
    doc = fitz.open(str(pdf_path))
    raw = " ".join(page.get_text() for page in doc)
    doc.close()

    chunks = _markdown_chunks(pdf_path)
    md_norm = _normalize_markdown("\n".join(c["text"] for c in chunks))

    raw_tokens = set(re.findall(r"[A-Za-z0-9$.,\-]{3,}", raw))
    missing = sorted(
        t for t in raw_tokens
        if not any(v in md_norm for v in _ligature_variants(t))
    )
    assert not missing, f"{len(missing)} raw tokens missing from markdown: {missing[:10]}"


@pytest.mark.parametrize("pdf_path", SKIA_PDFS, ids=lambda p: p.name)
def test_sanitized_chunks_are_json_serializable(pdf_path):
    """The Lambda IPCs chunks through /tmp JSON files; fitz objects must be gone."""
    chunks = _markdown_chunks(pdf_path)
    json.dumps(chunks)


def test_quality_gates_pass_with_full_extraction():
    """With the header intact, the markdown/word-mismatch gate passes cleanly.

    Under 0.2.9 this fixture scored word_ratio 0.79 — above the 0.75
    fallback threshold — so the mutilated extraction leaked to production.
    """
    pdf_path = str(FIXTURES / "skia_delivery_note_2.pdf")
    passed, stats = run_early_quality_check(pdf_path)
    assert passed, f"early quality check failed: {stats}"

    chunks = _markdown_chunks(pdf_path)
    md_passed, md_stats = run_markdown_quality_check(
        chunks, early_word_count=stats["word_count"]
    )
    assert md_passed, f"markdown quality check failed: {md_stats}"
    assert md_stats["word_ratio"] >= 0.9, (
        f"markdown lost words vs raw extraction: {md_stats}"
    )


@pytest.mark.parametrize("pdf_path", SKIA_PDFS, ids=lambda p: p.name)
def test_word_bounding_boxes_extracted(pdf_path):
    doc = fitz.open(str(pdf_path))
    try:
        words = extract_words(doc)
    finally:
        doc.close()
    assert len(words) > 100
    assert all(0.0 <= w.bbox["x0"] <= 1.0 for w in words)


@pytest.mark.parametrize("kind", ["scan", "header", "blank", "blank-image", "logo", "dark-cover", "border", "tiled", "rotated-scan"])
def test_mixed_pdf_keeps_scanned_content_on_the_ocr_path(tmp_path, kind):
    # Two digital notes make the document-wide word ratio pass even if the
    # following printed page would be completely missing from the output.
    target = tmp_path / "mixed.pdf"
    with fitz.open(str(SKIA_PDFS[1])) as note, fitz.open() as doc:
        doc.insert_pdf(note)
        doc.insert_pdf(note)
        page = doc.new_page(width=note[0].rect.width, height=note[0].rect.height)
        if kind == "tiled":
            for i in range(4):
                rect = fitz.Rect(0, page.rect.height * i / 4, page.rect.width, page.rect.height * (i + 1) / 4)
                page.insert_image(rect, pixmap=note[0].get_pixmap(clip=rect))
        elif kind != "blank":
            image = note[0].get_pixmap()
            if kind == "blank-image":
                image.clear_with(255)
            if kind == "border":
                image.clear_with(255)
                image.set_rect(fitz.IRect(0, 0, 3, image.height), (60, 60, 60))
                image.set_rect(fitz.IRect(0, 0, image.width, 2), (60, 60, 60))
            if kind == "dark-cover":
                image.clear_with(70)
            rect = fitz.Rect(20, 20, 50, 50) if kind == "logo" else page.rect
            page.insert_image(rect, pixmap=image)
        if kind in ("header", "blank-image"):
            page.insert_text((30, 30), "Delivery summary")
        if kind == "rotated-scan":
            page.set_rotation(90)
        doc.save(target)

    passed, stats = run_early_quality_check(str(target))
    if kind in ("scan", "header", "rotated-scan", "tiled"):
        assert not passed
        assert "Page 3" in stats["failure_reason"]
        assert "error_code" not in stats  # ordinary quality failure routes to OCR
    else:
        assert passed, stats
