#!/usr/bin/env python3
"""Extraction sanity sweep — run this whenever bumping PyMuPDF/pymupdf4llm.

For each PDF it compares raw PyMuPDF text extraction (ground truth for
"what text exists") against pymupdf4llm markdown output (what the Lambda
ships), and reports any raw token that failed to survive into the markdown.

This is the check that caught both known upstream regressions:
- pymupdf4llm 0.0.18-0.2.9 dropped text inside vector-graphics clusters /
  Skia shadow images (whole header blocks on Chrome-generated PDFs)
- pymupdf4llm 1.28.0 drops table cells (incl. dollar amounts) on
  Skia-generated invoices

Ligature collapse in the table path (e.g. "Office" -> "Offce", present in
all 1.27.x/1.28.x releases) is tolerated and reported separately — it is
cosmetic, unlike dropped content.

Usage (inside a venv built from the candidate requirements.txt):

    python scripts/compare_extraction_versions.py                # tests/fixtures
    python scripts/compare_extraction_versions.py extra1.pdf ... # custom corpus

To evaluate a version bump, build one venv per candidate version and run
this script (plus `pytest tests/`) in each; diff the output. Exits non-zero
if any PDF lost non-ligature tokens, so it can gate CI.
"""

import argparse
import pathlib
import re
import sys

import fitz
import pymupdf4llm

TOKEN_RE = re.compile(r"[A-Za-z0-9$.,\-]{3,}")
DEFAULT_CORPUS = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def ligature_variants(token: str) -> list[str]:
    """Acceptable collapsed forms when table extraction decodes ligatures short."""
    return [
        token,
        token.replace("ffi", "ff"),
        token.replace("ffl", "fl"),
        token.replace("fi", "f"),
        token.replace("fl", "f"),
    ]


def sweep_pdf(path: pathlib.Path) -> dict:
    doc = fitz.open(str(path))
    raw = " ".join(page.get_text() for page in doc)
    raw_words = sum(len(page.get_text("words")) for page in doc)
    doc.close()

    chunks = pymupdf4llm.to_markdown(fitz.open(str(path)), page_chunks=True)
    md = "\n".join(c["text"] for c in chunks)
    md_norm = re.sub(r"[|*#\s]+", " ", md)

    raw_tokens = set(TOKEN_RE.findall(raw))
    hard_missing = []
    ligature_only = []
    for token in sorted(raw_tokens):
        if token in md_norm:
            continue
        if any(v in md_norm for v in ligature_variants(token)):
            ligature_only.append(token)
        else:
            hard_missing.append(token)

    return {
        "pages": len(chunks),
        "tables": sum(len(c.get("tables", [])) for c in chunks),
        "md_chars": len(md),
        "raw_words": raw_words,
        "raw_tokens": len(raw_tokens),
        "hard_missing": hard_missing,
        "ligature_only": ligature_only,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "pdfs",
        nargs="*",
        type=pathlib.Path,
        help=f"PDFs to sweep (default: {DEFAULT_CORPUS}/*.pdf)",
    )
    args = parser.parse_args()

    pdfs = args.pdfs or sorted(DEFAULT_CORPUS.glob("*.pdf"))
    if not pdfs:
        print("no PDFs to check", file=sys.stderr)
        return 2

    print(f"PyMuPDF {fitz.version[0]} | pymupdf4llm {pymupdf4llm.__version__}\n")
    failed = False
    for path in pdfs:
        r = sweep_pdf(path)
        status = "OK  " if not r["hard_missing"] else "LOSS"
        print(
            f"{status} {path.name:45s} pages={r['pages']:3d} tables={r['tables']:2d} "
            f"md_chars={r['md_chars']:6d} raw_tokens={r['raw_tokens']:5d} "
            f"missing={len(r['hard_missing'])} ligature-only={len(r['ligature_only'])}"
        )
        if r["hard_missing"]:
            failed = True
            print(f"       lost tokens: {r['hard_missing'][:15]}")
        if r["ligature_only"]:
            print(f"       ligature-collapsed (cosmetic): {r['ligature_only'][:5]}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
