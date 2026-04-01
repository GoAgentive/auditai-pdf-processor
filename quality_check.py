"""Early quality checks using fast word extraction (no pymupdf4llm dependency).

Runs in ~0.3s for 130 pages vs ~14s for pymupdf4llm. Allows fast rejection
of scanned/image PDFs before committing to expensive markdown extraction.
"""

import logging
from typing import Dict, Any, Tuple

import fitz

logger = logging.getLogger(__name__)

# Minimum thresholds for machine-readable PDFs
MIN_WORDS_PER_PAGE = 75
MIN_TOTAL_WORDS = 10
MAX_WORD_LENGTH = 200  # Detect binary/corrupted content


def run_early_quality_check(pdf_path: str) -> Tuple[bool, Dict[str, Any]]:
    """
    Fast quality check using only word extraction (no pymupdf4llm).

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

        for i in range(page_count):
            words = doc[i].get_text("words")
            word_count = len(words)
            total_words += word_count

            if word_count < MIN_WORDS_PER_PAGE:
                pages_with_few_words += 1

            # Check for abnormally long words (binary content indicator)
            for w in words:
                if len(str(w[4])) > MAX_WORD_LENGTH:
                    has_long_words = True
                    break

        words_per_page = total_words / page_count if page_count > 0 else 0

        stats = {
            "word_count": total_words,
            "page_count": page_count,
            "words_per_page": round(words_per_page, 1),
        }

        # Check failure conditions
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

        return True, stats

    finally:
        doc.close()
