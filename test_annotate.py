"""Tests for the annotate operation (annotate.py).

Run with the spiked venv that has PyMuPDF 1.27.2.2:
    <venv>/bin/python -m pytest test_annotate.py -v
or directly:
    <venv>/bin/python test_annotate.py
"""

import base64
import os
import shutil
import tempfile

import fitz

import annotate

REAL_PDF = (
    "/Users/nathan/Repos/auditai/qa-tests/sandbox-files/"
    "fsr-current-year/REI-FY24-financial-statements.pdf"
)


def _sample_annotation_data():
    """AnnotationData exercising every branch."""
    bbox = lambda x1, y1, x2, y2: {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
    return {
        "manualAnnotations": [
            {
                "page_number": 1,
                "bbox": bbox(100, 100, 160, 112),
                "tick_mark": {
                    "key": "MANUALZZ",
                    "acceptance_type": "validation",
                    "test_types": ["IC"],
                    "fileIndex": 4,
                },
            },
            # No tick_mark -> skipped.
            {"page_number": 1, "bbox": bbox(10, 10, 20, 20)},
        ],
        "numbersWithTickMarks": [
            # Combined top-right IC + PY at one location.
            {
                "value": "1,234",
                "page_no": 1,
                "bbox": bbox(200, 200, 260, 212),
                "word_index": 0,
                "file_id": 1,
                "tickMarks": [
                    {"id": "a", "key": "IC", "acceptance_type": "validation",
                     "test_types": ["IC"], "fileIndex": 3},
                    {"id": "b", "key": "PYZZ", "acceptance_type": "exception",
                     "test_types": ["PY"], "fileIndex": 7},
                ],
            },
            # Single bottom-right F/CF with a symbol glyph (routes to Noto).
            {
                "value": "9,999",
                "page_no": 1,
                "bbox": bbox(300, 300, 360, 312),
                "word_index": 1,
                "file_id": 1,
                "tickMarks": [
                    {"id": "c", "key": "✓", "acceptance_type": "validation",
                     "test_types": ["F/CF"]},
                ],
            },
            # Out-of-range page -> skipped.
            {
                "value": "0",
                "page_no": 9999,
                "bbox": bbox(1, 1, 2, 2),
                "word_index": 2,
                "file_id": 1,
                "tickMarks": [{"id": "d", "key": "X", "test_types": ["IC"]}],
            },
        ],
        "comments": [
            {
                "page_no": 1,
                "bbox": bbox(120, 400, 140, 420),
                "comment": "STANDALONE_NOTE_ZZ",
                "author": "Auditor Jane",
                "shape": "comment",
            },
            {
                "page_no": 1,
                "bbox": bbox(50, 500, 250, 520),
                "comment": "",
                "shape": "arrow",
                "color": "#00ff00",
            },
            {
                "page_no": 1,
                "bbox": bbox(50, 540, 250, 540),
                "comment": "",
                "shape": "double_line",
                "color": "not-a-hex",  # -> default red
            },
            {
                "page_no": 1,
                "bbox": bbox(50, 560, 250, 580),
                "comment": "",
                "shape": "line",
            },
        ],
        "rcCitations": [
            {
                "page_no": 1,
                "bbox": bbox(400, 100, 420, 120),
                "finding": "RC_FINDING_ZZ",
                "text_content": "source text",
            }
        ],
        "tickMarkComments": [
            {
                "tickMarkApplicationId": "app-1",
                "page_no": 1,
                "bbox": bbox(200, 200, 260, 212),
                "tickMarkColor": "#a855f7",
                "tickMarkKey": "IC",
                "thread": [
                    {"content": "TICK_THREAD_ZZ", "author": "Reviewer Bob",
                     "timestamp": "2026-01-15T14:30:00Z"},
                    {"content": "reply here", "author": "Auditor Jane",
                     "timestamp": "2026-01-16T09:00:00Z"},
                ],
            }
        ],
    }


def _make_cover_pdf(num_pages=2):
    """Build a cover PDF that emits an agentive-goto sentinel link like
    react-pdf's CoverSheet does (target source page 2, at x=55,y=130)."""
    cover = fitz.open()
    for _ in range(num_pages):
        cover.new_page(width=612, height=792)
    cover[0].insert_link(
        {
            "kind": fitz.LINK_URI,
            "uri": "agentive-goto:?page=2&x=55&y=130",
            "from": fitz.Rect(50, 50, 300, 70),
        }
    )
    cover[0].insert_text(fitz.Point(72, 72), "COVER SHEET", fontsize=20)
    data = cover.tobytes()
    cover.close()
    return data


def _make_malformed_pdf(path):
    """Write a PDF whose startxref offset is wrong so MuPDF must repair it."""
    doc = fitz.open()
    p = doc.new_page(width=300, height=400)
    p.insert_text(fitz.Point(72, 72), "MALFORMED SOURCE PAGE", fontsize=14)
    doc.save(path)
    doc.close()
    with open(path, "rb") as fh:
        data = fh.read()
    idx = data.rfind(b"startxref")
    assert idx >= 0
    # Corrupt the byte offset that follows "startxref\n" so the xref can't be
    # located -> MuPDF rebuilds it and marks the doc repaired.
    corrupted = data[: idx + len(b"startxref\n")] + b"999999\n%%EOF\n"
    with open(path, "wb") as fh:
        fh.write(corrupted)


def test_incremental_byte_prefix_and_annotations():
    """Valid PDF -> incremental save; original bytes are an exact prefix and
    our annotations are present."""
    tmp = tempfile.mkdtemp()
    try:
        out = os.path.join(tmp, "out.pdf")
        mode, page_count = annotate.build_annotated_pdf(
            REAL_PDF, out, _sample_annotation_data(), cover_bytes=None
        )
        assert mode == "incremental", f"expected incremental, got {mode}"

        with open(REAL_PDF, "rb") as fh:
            original_bytes = fh.read()
        with open(out, "rb") as fh:
            out_bytes = fh.read()
        assert out_bytes.startswith(original_bytes), (
            "incremental output must have the original as an exact byte prefix"
        )
        assert len(out_bytes) > len(original_bytes), "expected appended bytes"

        # Reopen and verify annotations landed on page 1.
        doc = fitz.open(out)
        page = doc[0]
        text = page.get_text()
        # Tick badge codes (drawn as page content).
        assert "MANUALZZ" in text, "manual tick badge missing"
        assert "IC-3" in text, "combined IC segment missing"
        assert "PYZZ-7" in text, "combined PY segment missing"
        assert "·" in text, "combined-badge delimiter missing"
        # Native sticky-note annotations.
        contents = [a.info.get("content", "") for a in page.annots()]
        joined = "\n".join(contents)
        assert "STANDALONE_NOTE_ZZ" in joined, "standalone comment note missing"
        assert "RC_FINDING_ZZ" in joined, "RC citation note missing"
        assert "TICK_THREAD_ZZ" in joined, "tick-mark thread note missing"
        # Author (title) round-trips.
        authors = [a.info.get("title", "") for a in page.annots()]
        assert "Review Checklist" in authors, "RC citation author missing"
        # Line/arrow/double-line strokes exist as vector drawings.
        drawings = page.get_drawings()
        assert len(drawings) >= 1, "expected drawn line/arrow/double-line strokes"
        doc.close()
        print("[ok] incremental: byte-prefix + annotations present")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cover_prepended_and_links_rewritten():
    """Cover PDF is prepended; page count grows; agentive-goto link rewritten
    to an internal GoTo; page labels applied."""
    tmp = tempfile.mkdtemp()
    try:
        out = os.path.join(tmp, "out.pdf")
        base = fitz.open(REAL_PDF)
        source_pages = base.page_count
        base.close()

        cover_bytes = _make_cover_pdf(num_pages=2)
        mode, page_count = annotate.build_annotated_pdf(
            REAL_PDF, out, _sample_annotation_data(), cover_bytes=cover_bytes
        )
        assert page_count == source_pages + 2, (
            f"expected {source_pages + 2} pages, got {page_count}"
        )

        doc = fitz.open(out)
        assert doc.page_count == source_pages + 2

        # Cover link rewritten: source page 2 (1-based) -> index 1+2 = 3.
        links = doc[0].get_links()
        goto = [l for l in links if l.get("kind") == fitz.LINK_GOTO]
        assert goto, f"expected a GoTo link on cover page 0, got {links}"
        assert goto[0]["page"] == 3, f"expected target page index 3, got {goto[0]}"
        # No leftover agentive-goto URI links.
        assert not any(
            l.get("kind") == fitz.LINK_URI
            and l.get("uri", "").startswith("agentive-goto:")
            for l in links
        ), "sentinel URI link should have been replaced"

        # Page labels: cover pages "Cover 1/2", source restarts at "1".
        labels = [doc.get_page_labels()] if hasattr(doc, "get_page_labels") else []
        label0 = doc[0].get_label() if hasattr(doc[0], "get_label") else None
        label_src = doc[2].get_label() if hasattr(doc[2], "get_label") else None
        if label0 is not None:
            assert label0 == "Cover 1", f"cover label wrong: {label0!r}"
            assert label_src == "1", f"source label wrong: {label_src!r}"

        # Burned annotations moved with their source page (now index 2).
        assert "MANUALZZ" in doc[2].get_text(), "burned badge not on shifted source page"
        doc.close()
        print(f"[ok] cover: page_count {source_pages}->{page_count}, link+labels rewritten (mode={mode})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_repaired_fallback_full_clean():
    """Malformed original -> is_repaired -> full-clean save mode, annotations
    still preserved and the result is a valid readable PDF."""
    tmp = tempfile.mkdtemp()
    try:
        malformed = os.path.join(tmp, "malformed.pdf")
        _make_malformed_pdf(malformed)

        # Sanity: MuPDF reports this as repaired on open.
        probe = fitz.open(malformed)
        assert probe.is_repaired, "test fixture is not actually repaired by MuPDF"
        probe.close()

        data = {
            "manualAnnotations": [
                {
                    "page_number": 1,
                    "bbox": {"x1": 40, "y1": 120, "x2": 100, "y2": 132},
                    "tick_mark": {"key": "REPAIRZZ", "acceptance_type": "validation",
                                  "test_types": ["IC"], "fileIndex": 1},
                }
            ],
            "numbersWithTickMarks": [],
            "comments": [
                {
                    "page_no": 1,
                    "bbox": {"x1": 40, "y1": 160, "x2": 60, "y2": 180},
                    "comment": "REPAIR_NOTE_ZZ",
                    "shape": "comment",
                }
            ],
        }
        out = os.path.join(tmp, "out.pdf")
        mode, page_count = annotate.build_annotated_pdf(malformed, out, data)
        assert mode == "full-clean", f"expected full-clean, got {mode}"
        assert page_count == 1

        doc = fitz.open(out)
        assert not doc.is_repaired, "full-clean output should be a valid (non-repaired) PDF"
        page = doc[0]
        assert "REPAIRZZ-1" in page.get_text(), "burned badge missing after repair save"
        contents = "\n".join(a.info.get("content", "") for a in page.annots())
        assert "REPAIR_NOTE_ZZ" in contents, "comment note missing after repair save"
        doc.close()
        print("[ok] repaired-fallback: full-clean save preserved annotations")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_rotated_source_page_content_placement():
    """A source page with /Rotate=90: content-drawn badge + line strokes must
    land within the page's DISPLAYED rect (rotation handling via
    derotation_matrix + insert_text rotate=). Sticky notes are unaffected."""
    tmp = tempfile.mkdtemp()
    try:
        # Build a rotated source: rotate page index 1 to 90 degrees, save clean
        # (valid PDF -> incremental path preserved).
        rotated_src = os.path.join(tmp, "rotated.pdf")
        d = fitz.open(REAL_PDF)
        rot_index = 1
        d[rot_index].set_rotation(90)
        disp = d[rot_index].rect  # displayed rect after rotation (w/h swapped)
        d.save(rotated_src)
        d.close()

        # Known DISPLAYED-space targets on the rotated page.
        badge_bbox = {"x1": 100, "y1": 100, "x2": 160, "y2": 112}
        # badge draws at x = x2 + 8 = 168, y = max(y1, y2 - max(16, fs+1)) = 100
        line_p1 = (200, 250)
        line_p2 = (320, 250)
        data = {
            "manualAnnotations": [
                {
                    "page_number": rot_index + 1,
                    "bbox": badge_bbox,
                    "tick_mark": {"key": "ROTZZ", "acceptance_type": "validation",
                                  "test_types": ["IC"], "fileIndex": 1},
                }
            ],
            "numbersWithTickMarks": [],
            "comments": [
                {
                    "page_no": rot_index + 1,
                    "bbox": {"x1": line_p1[0], "y1": line_p1[1],
                             "x2": line_p2[0], "y2": line_p2[1]},
                    "comment": "",
                    "shape": "line",
                    "color": "#ff0000",
                }
            ],
        }

        out = os.path.join(tmp, "out.pdf")
        mode, _ = annotate.build_annotated_pdf(rotated_src, out, data)
        assert mode == "incremental", f"expected incremental, got {mode}"

        doc = fitz.open(out)
        page = doc[rot_index]
        assert page.rotation == 90
        pr = page.rect  # displayed rect: 792 x 612 for a rotated portrait page

        # Sanity: the badge text was actually drawn (present in the page).
        assert any(
            "ROTZZ-1" in w[4] for w in page.get_text("words")
        ), "badge text 'ROTZZ-1' not drawn on rotated page"

        # Placement is verified in DISPLAYED space via the rendered pixmap
        # (get_pixmap always renders in display orientation, 1px/pt at 72dpi).
        # get_text word coords are in the page's raw/unrotated space, so they
        # are not used for the displayed-placement assertion.
        pix = page.get_pixmap(alpha=False)
        assert pix.width == int(round(pr.width)) and pix.height == int(round(pr.height))

        def dark_pixels(x0, y0, x1, y1):
            n = 0
            for yy in range(max(0, y0), min(pix.height, y1)):
                for xx in range(max(0, x0), min(pix.width, x1)):
                    r, g, b = pix.pixel(xx, yy)
                    if r < 200 or g < 200 or b < 200:
                        n += 1
            return n

        def red_pixels(x0, y0, x1, y1):
            n = 0
            for yy in range(max(0, y0), min(pix.height, y1)):
                for xx in range(max(0, x0), min(pix.width, x1)):
                    r, g, b = pix.pixel(xx, yy)
                    if r > 150 and g < 100 and b < 100:
                        n += 1
            return n

        # 1) Badge lands in the expected DISPLAYED region. badge_bbox draws at
        #    displayed x = x2 + 8 = 168 (baseline y ~100), text extends right/up.
        badge_px = dark_pixels(158, 84, 220, 108)
        assert badge_px > 0, "badge glyphs not found in expected displayed region"

        # 2) Red line stroke (displayed (200,250)->(320,250)) lands along y~250.
        line_in = red_pixels(195, 240, 325, 262)
        assert line_in > 0, "red line stroke not found in expected displayed region"
        # Guard against the pre-fix behavior (stroke landing elsewhere): the
        # top strip of the displayed page should carry no red line pixels.
        line_outside = red_pixels(0, 0, int(pr.width), 120)
        assert line_outside == 0, (
            f"unexpected red pixels outside target region: {line_outside}"
        )
        doc.close()
        print(
            f"[ok] rotated page: displayed rect {pr.width:.0f}x{pr.height:.0f}, "
            f"badge px={badge_px}, red line in-region={line_in} outside={line_outside}"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _FakeS3:
    """Minimal S3 stub for handle_annotate: serves the local original and
    captures the uploaded output in memory."""

    def __init__(self, original_path):
        self._original_path = original_path
        self.put_calls = []

    def download_file(self, bucket, key, dest):
        shutil.copyfile(self._original_path, dest)

    def put_object(self, Bucket, Key, Body, ContentType):
        self.put_calls.append(
            {"Bucket": Bucket, "Key": Key, "Body": Body, "ContentType": ContentType}
        )


def test_handle_annotate_end_to_end():
    """Full handler envelope: download -> burn -> save -> upload."""
    import json as _json

    s3 = _FakeS3(REAL_PDF)
    cover_b64 = base64.b64encode(_make_cover_pdf(num_pages=1)).decode("ascii")
    body = {
        "operation": "annotate",
        "s3_path": "s3://in-bucket/path/to/original.pdf",
        "output_bucket": "out-bucket",
        "output_key": "exports/uuid.pdf",
        "annotation_data": _sample_annotation_data(),
        "cover_pdf_base64": cover_b64,
    }
    resp = annotate.handle_annotate(body, s3)
    assert resp["statusCode"] == 200, resp
    assert resp["headers"]["Access-Control-Allow-Origin"] == "*"
    payload = _json.loads(resp["body"])
    assert payload["output_key"] == "exports/uuid.pdf"
    assert payload["mode"] in ("incremental", "full-clean")
    assert payload["page_count"] >= 2

    assert len(s3.put_calls) == 1
    call = s3.put_calls[0]
    assert call["Bucket"] == "out-bucket"
    assert call["Key"] == "exports/uuid.pdf"
    assert call["ContentType"] == "application/pdf"
    assert call["Body"][:5] == b"%PDF-", "uploaded body is not a PDF"
    print(f"[ok] handler end-to-end: mode={payload['mode']} pages={payload['page_count']}")


if __name__ == "__main__":
    test_incremental_byte_prefix_and_annotations()
    test_cover_prepended_and_links_rewritten()
    test_repaired_fallback_full_clean()
    test_rotated_source_page_content_placement()
    test_handle_annotate_end_to_end()
    print("\nALL TESTS PASSED")
