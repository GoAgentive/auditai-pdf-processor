"""Check the shipped Markdown and citation geometry against known, drawn cells."""
from collections import Counter
from pathlib import Path
import re

import pymupdf
import pymupdf4llm
import pytest

from extraction import _split_period_table, extract_markdown_parallel, extract_words


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


def test_unruled_statement_keeps_labels_and_years_in_distinct_cells():
    # Hooker Furnishings FY2025 Form 10-K, consolidated statements of operations, F-6.
    # https://www.sec.gov/Archives/edgar/data/1077688/000118518525000321/R5.htm
    path = Path(__file__).parent / "fixtures" / "hoft_fy2025_operations.pdf"
    chunk, = extract_markdown_parallel(str(path), 1)
    text = chunk["text"]
    grid = []
    for line in text.splitlines():
        if line.startswith("|"):
            cells = re.sub(r"<br>|\*\*", "", line[1:-1]).split("|")
            grid.append([re.sub(r"\s+", " ", cell).strip().replace("$ ", "$") for cell in cells])
    assert grid[0] == ["", "2025", "2024", "2023"]
    assert ["Net sales", "$397,465", "$433,226", "$583,102"] in grid
    assert ["Gross profit", "88,648", "108,692", "93,294"] in grid
    assert ["Weighted average shares outstanding:", "", "", ""] in grid
    assert ["Trade name impairment charges", "2,831", "-", "13"] in grid
    assert ["Cash dividends declared per share", "$0.92", "$0.89", "$0.82"] in grid
    for box in chunk["page_boxes"]:
        start, end = box["pos"]
        if box["class"] == "table":
            assert text[start:end].startswith("**For the 53 Week Period")
            assert "Cash dividends declared per share" in text[start:end]
        if box["class"] == "page-footer":
            assert text[start:end].strip() == "F-6"


def test_conflicting_amount_keeps_the_entire_original_table():
    path = Path(__file__).parent / "fixtures" / "hoft_fy2025_operations.pdf"
    with pymupdf.open(path) as doc:
        chunk, = pymupdf4llm.to_markdown(doc, page_chunks=True)
        box = next(box for box in chunk["page_boxes"] if box["class"] == "table")
        start, end = box["pos"]
        table = chunk["text"][start:end]
        # The final amount disagrees, although every earlier row can be split.
        modified = table.replace("0.82", "0.28")
        assert modified != table
        assert _split_period_table(doc, 0, box["bbox"], modified) == modified


@pytest.mark.parametrize(("note", "prior"), [("5", ""), ("(2)", "7")])
def test_number_beside_label_is_never_promoted_to_an_empty_year(note, prior):
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=200)
        for y, row in [
            (20, [(20, "Year Ended")]),
            (40, [(210, "2025"), (300, "2024")]),
            (60, [(20, "Sales"), (210, "100"), (300, "90")]),
            (80, [(20, "Cost"), (210, "40"), (300, "30")]),
            (100, [(20, "Other"), (52, note), (300, prior)]),
        ]:
            for x, word in row:
                if word:
                    page.insert_text((x, y), word, fontsize=10)
        table = (
            "|Year<br>2025|Ended<br>2024|\n|---|---|\n"
            "|Sales<br>100|90|\n|Cost<br>40|30|\n"
            f"|Other<br>{note}|{prior}|\n"
        )
        assert _split_period_table(doc, 0, page.rect, table) == table
