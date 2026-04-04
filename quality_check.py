"""Early quality checks using fast word extraction (no pymupdf4llm dependency).

Runs in ~0.3s for 130 pages vs ~14s for pymupdf4llm. Allows fast rejection
of scanned/image PDFs before committing to expensive markdown extraction.

This is the single source of truth for OCR quality checks — the Elixir-side
quality checks (ocr_quality_check.ex) defer to the Lambda for all content
quality decisions.
"""

import logging
import re
from typing import Dict, Any, Tuple

import fitz

logger = logging.getLogger(__name__)

# Minimum thresholds for machine-readable PDFs
MIN_WORDS_PER_PAGE = 75
MIN_TOTAL_WORDS = 10
MAX_WORD_LENGTH = 200  # Detect binary/corrupted content
MIN_CONTENT_LENGTH = 50  # Minimum concatenated text length


def run_early_quality_check(pdf_path: str) -> Tuple[bool, Dict[str, Any]]:
    """
    Fast quality check using only word extraction (no pymupdf4llm).

    Checks (in order):
    1. Page count > 0
    2. Minimum total words
    3. Minimum words per page ratio
    4. Abnormally long words (binary content)
    5. All pages insufficient content
    6. Content too short
    7. Excessive special characters (>40%)
    8. Encoding corruption patterns
    9. Fragmented text patterns
    10. Repeated character sequences

    Returns:
        (passed, stats) where stats contains word_count, page_count,
        words_per_page, and failure_reason (if failed).
    """
    doc = fitz.open(pdf_path)
    try:
        page_count = len(doc)
        total_words = 0
        pages_with_few_words = 0
        has_long_words = False
        all_word_texts = []

        for i in range(page_count):
            words = doc[i].get_text("words")
            word_count = len(words)
            total_words += word_count

            if word_count < MIN_WORDS_PER_PAGE:
                pages_with_few_words += 1

            for w in words:
                text = str(w[4])
                all_word_texts.append(text)
                if len(text) > MAX_WORD_LENGTH:
                    has_long_words = True

        words_per_page = total_words / page_count if page_count > 0 else 0
        concatenated_text = " ".join(all_word_texts)

        stats = {
            "word_count": total_words,
            "page_count": page_count,
            "words_per_page": round(words_per_page, 1),
        }

        # Basic extraction checks
        if page_count == 0:
            stats["failure_reason"] = "No pages found"
            return False, stats

        if total_words < MIN_TOTAL_WORDS:
            stats["failure_reason"] = f"Too few words ({total_words})"
            return False, stats

        if words_per_page < MIN_WORDS_PER_PAGE:
            stats["failure_reason"] = (
                f"Too few words per page ({words_per_page:.1f}, minimum {MIN_WORDS_PER_PAGE})"
            )
            return False, stats

        if has_long_words:
            stats["failure_reason"] = "Detected abnormally long words (binary content)"
            return False, stats

        if pages_with_few_words == page_count:
            stats["failure_reason"] = "All pages have insufficient content"
            return False, stats

        # Content quality checks on concatenated word text
        if len(concatenated_text) < MIN_CONTENT_LENGTH:
            stats["failure_reason"] = (
                f"Content too short ({len(concatenated_text)} chars)"
            )
            return False, stats

        gibberish_reason = _check_gibberish(concatenated_text)
        if gibberish_reason:
            stats["failure_reason"] = gibberish_reason
            return False, stats

        return True, stats

    finally:
        doc.close()


def run_markdown_quality_check(
    page_chunks: list,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Post-extraction quality check on pymupdf4llm markdown output.

    Catches the gap where word extraction passes (PDF has selectable text)
    but pymupdf4llm produces empty/trivial markdown. This happens with
    certain PDF structures (Type3 fonts, Chrome-generated PDFs with
    full-page background images, XFA forms).

    Returns:
        (passed, stats) where stats contains page-level markdown metrics
        and failure_reason (if failed).
    """
    if not page_chunks:
        return False, {
            "failure_reason": "No markdown chunks produced",
            "pages_with_content": 0,
            "total_md_chars": 0,
        }

    total_pages = len(page_chunks)
    pages_with_content = 0
    total_md_chars = 0
    empty_pages = []

    for i, chunk in enumerate(page_chunks):
        text = (chunk.get("text") or "").strip()
        char_count = len(text)
        total_md_chars += char_count
        if char_count > 0:
            pages_with_content += 1
        else:
            empty_pages.append(i + 1)  # 1-indexed

    md_chars_per_page = round(total_md_chars / total_pages, 1)

    stats = {
        "pages_with_content": pages_with_content,
        "total_pages": total_pages,
        "total_md_chars": total_md_chars,
        "md_chars_per_page": md_chars_per_page,
    }

    # All pages produced empty markdown
    if pages_with_content == 0:
        stats["failure_reason"] = "All pages produced empty markdown"
        stats["empty_pages"] = empty_pages
        return False, stats

    # Less than 20% of pages have content (for docs with 5+ pages)
    if total_pages >= 5 and pages_with_content / total_pages < 0.2:
        stats["failure_reason"] = (
            f"Too few pages with markdown content "
            f"({pages_with_content}/{total_pages})"
        )
        stats["empty_pages"] = empty_pages
        return False, stats

    # Total content is trivially short (less than 20 chars per page average)
    if md_chars_per_page < 20:
        stats["failure_reason"] = (
            f"Markdown content too sparse ({md_chars_per_page} chars/page avg)"
        )
        return False, stats

    if empty_pages:
        logger.info(
            "Markdown quality check: %d empty pages: %s",
            len(empty_pages),
            empty_pages[:20],
        )

    return True, stats


def _check_gibberish(text: str) -> str | None:
    """
    Check concatenated word text for gibberish/corruption patterns.

    Returns failure reason string if gibberish detected, None if clean.
    """
    if len(text) < MIN_CONTENT_LENGTH:
        return None

    # Repeated character sequences (e.g. "aaaaaaaaaaaaaaaaaaaaaa")
    if re.search(r"(.)\1{20,}", text):
        return "Detected repeated character sequences"

    # Excessive special characters (>40% of content)
    if _has_excessive_special_chars(text):
        return "Excessive special characters (>40%)"

    # Encoding corruption
    if _has_encoding_corruption(text):
        return "Detected encoding corruption patterns"

    # Fragmented text
    if _has_fragmented_text(text):
        return "Detected fragmented text patterns"

    # Mixed encoding issues
    if _has_mixed_encoding_issues(text):
        return "Detected mixed encoding issues"

    return None


def _has_excessive_special_chars(text: str) -> bool:
    """Check if >40% of characters are non-standard."""
    if not text:
        return False
    normal_pattern = re.compile(
        r"[a-zA-Z0-9\s.,!?;:()\-'\"/%$€£¥@#&*+=<>\[\]{}|\\~`^]"
    )
    special_count = sum(1 for ch in text if not normal_pattern.match(ch))
    return special_count / len(text) > 0.4


def _has_encoding_corruption(text: str) -> bool:
    """Check for replacement characters and encoding error patterns."""
    if "�" in text:
        return True
    # Long sequences of non-ASCII
    if re.search(r"[^\x00-\x7f]{10,}", text):
        return True
    # Multiple question marks (encoding failures)
    if re.search(r"\?\?\?+", text):
        return True
    return False


def _has_fragmented_text(text: str) -> bool:
    """Check for high ratio of single-char or fragment words."""
    words = text.split()
    if len(words) <= 10:
        return False

    fragment_count = 0
    for word in words:
        wlen = len(word)
        # Single characters excluding common single letters
        if wlen == 1 and word not in ("a", "A", "i", "I"):
            fragment_count += 1
        # Very short words with special characters
        elif wlen <= 3 and re.search(r"[^\w]", word):
            fragment_count += 1

    return fragment_count / len(words) > 0.6


def _has_mixed_encoding_issues(text: str) -> bool:
    """Check for lines with high symbol density."""
    lines = [line for line in text.split("\n") if len(line) >= 5]
    if len(lines) <= 3:
        return False

    problematic = 0
    for line in lines:
        symbol_count = sum(1 for ch in line if re.match(r"[^\w\s]", ch))
        if symbol_count / len(line) > 0.5:
            problematic += 1

    return problematic / len(lines) > 0.4
