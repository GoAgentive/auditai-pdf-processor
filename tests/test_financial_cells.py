"""Check the shipped Markdown and citation geometry against known, drawn cells."""
from collections import Counter
import re

import pymupdf

from extraction import extract_markdown_parallel, extract_words


def test_year_columns_and_citation_boxes_on_two_statements(tmp_path):
    pages = [
        [("Cash", "12,450", "12,450"), ("Expenses", "(1,350)", "0"), ("Other", "", "1,350")],
        [("Cash", "8,299", "12,450"), ("Expenses", "0", "(1,350)"), ("Other", "1,350", "")],
    ]
    path = tmp_path / "comparatives.pdf"
    columns = [72, 245, 356, 490]
    with pymupdf.open() as pdf:
        for rows in pages:
            page = pdf.new_page(width=595, height=842)
            for n in range(5):
                page.draw_line((72, 110 + 32 * n), (490, 110 + 32 * n))
            for x in columns:
                page.draw_line((x, 110), (x, 238))
            for r, row in enumerate([("Account", "2025", "2024"), *rows]):
                for c, text in enumerate(row):
                    page.insert_text((columns[c] + 8, 131 + 32 * r), text, fontsize=11)
        pdf.save(path)

    chunks = extract_markdown_parallel(str(path), len(pages))
    with pymupdf.open(path) as pdf:
        words = extract_words(pdf)

    assert len(chunks) == len(pages)
    for number, (rows, chunk) in enumerate(zip(pages, chunks), 1):
        content = chunk["text"]
        grid = []
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("|") and not re.fullmatch(r"[| :\-]+", line):
                grid.append(tuple(s.strip().strip("*") for s in line[1:-1].split("|")))
        assert grid == [("Account", "2025", "2024"), *rows]

        for r, row in enumerate(rows, 1):
            for c in (1, 2):
                # Each word must lie inside the original cell on the original page.
                # Checking the full rect catches offsets and swapped columns even for repeats.
                cell = pymupdf.Rect(columns[c], 110 + 32 * r, columns[c + 1], 142 + 32 * r)
                found = []
                for word in words:
                    if word.page != number:
                        continue
                    b = word.absolute_bbox
                    if cell.contains(pymupdf.Rect(b["x0"], b["y0"], b["x1"], b["y1"])):
                        found.append(word.text)
                assert Counter(found) == Counter([row[c]] if row[c] else [])
