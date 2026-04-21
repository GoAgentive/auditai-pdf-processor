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
# Markdown must retain at least this fraction of early-check words.
# pymupdf4llm legitimately reduces word count by ~10-20% (formatting,
# deduplication), but anything below this threshold indicates dropped
# text content which is unacceptable for audit evidence. Prefer
# falling back to Azure OCR over accepting partial text.
MIN_MARKDOWN_WORD_RATIO = 0.75
# Fraction of total characters that must belong to 21+ char runs of a single
# repeated character for the document to be rejected as gibberish. Incidental
# runs (leader dots in a ToC, "====" banners, signature lines) legitimately
# hit the 21-char threshold; only reject when they dominate the content.
MAX_REPEATED_CHAR_RATIO = 0.3


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
    10. Repeated character sequences (when they dominate the document)

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
    early_word_count: int,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Post-extraction quality check on pymupdf4llm markdown output.

    Catches the gap where word extraction passes (PDF has selectable text)
    but pymupdf4llm produces empty/trivial markdown. This happens with
    certain PDF structures (Type3 fonts, Chrome-generated PDFs with
    full-page background images, XFA forms), where pymupdf4llm's layout
    engine misclassifies text inside vector graphic clusters and drops it.

    Args:
        page_chunks: List of page chunk dicts from pymupdf4llm.
        early_word_count: Word count from the early quality check
            (fitz get_text("words")). Used to detect markdown/word mismatch
            where raw extraction finds content but markdown drops it.

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

    # Markdown/word mismatch: pymupdf4llm dropped most content.
    # This catches cases where vector graphic clusters (e.g. invoice grid
    # lines) cause the layout engine to skip text blocks that fall inside
    # the cluster bounding box, even though raw word extraction finds them.
    if early_word_count > 0:
        md_text = " ".join(
            (chunk.get("text") or "").strip() for chunk in page_chunks
        )
        md_word_count = len(md_text.split())
        word_ratio = md_word_count / early_word_count
        stats["early_word_count"] = early_word_count
        stats["md_word_count"] = md_word_count
        stats["word_ratio"] = round(word_ratio, 3)

        if word_ratio < MIN_MARKDOWN_WORD_RATIO:
            stats["failure_reason"] = (
                f"Markdown/word mismatch: markdown produced {md_word_count} words "
                f"but raw extraction found {early_word_count} "
                f"(ratio {word_ratio:.1%}, threshold {MIN_MARKDOWN_WORD_RATIO:.0%})"
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

    # Repeated character sequences (e.g. "aaaaaaaaaaaaaaaaaaaaaa") — only
    # rejects when these runs dominate the document. Avoids false positives
    # on ToC leader dots, "====" banners, and "____" signature lines.
    repeat_chars = sum(
        len(m.group(0)) for m in re.finditer(r"(.)\1{20,}", text)
    )
    if repeat_chars / len(text) > MAX_REPEATED_CHAR_RATIO:
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
