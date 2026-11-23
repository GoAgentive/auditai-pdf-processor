"""Exercise the deployed entry point, substituting only object storage."""
import json
from pathlib import Path
import shutil
from uuid import UUID

import fitz
import pytest

from quality_check import run_early_quality_check


def make_mixed_pdf(path):
    fixture = Path(__file__).parent / "fixtures" / "skia_delivery_note_2.pdf"
    with fitz.open() as document, fitz.open(fixture) as digital:
        for text in ("Invoice SILVER LANTERN 7418\nTotal USD 16,825.40",
                     "Invoice RED MAPLE 9137\nTotal USD 82,107.31"):
            document.insert_pdf(digital, from_page=0, to_page=0)
            with fitz.open() as receipt:
                paper = receipt.new_page()
                # Printed lines across the sheet, as on a scanned statement.
                paper.insert_text((55, 80), text, fontsize=18)
                for row in range(12):
                    paper.insert_text((55, 160 + row * 45), f"Item {row + 1} purchase processed", fontsize=15)
                page = document.new_page(width=paper.rect.width, height=paper.rect.height)
                page.insert_image(page.rect, pixmap=paper.get_pixmap())
        document.save(path)


@pytest.mark.parametrize("s3_output", [False, True])
def test_only_scanned_pages_in_provider_object(tmp_path, monkeypatch, s3_output):
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    import index
    import s3_output as storage

    source = tmp_path / "source.pdf"
    subset = tmp_path / "subset.pdf"
    make_mixed_pdf(source)
    # Keep the two real delivery notes. Give the table assertion its own known
    # rows and columns so flattened prose cannot pass simply by containing "|".
    with fitz.open(source) as document:
        page = document.new_page()
        for x in (55, 230, 340):
            page.draw_line((x, 100), (x, 175))
        for y in (100, 125, 150, 175):
            page.draw_line((55, y), (340, y))
        for row, (label, amount) in enumerate([("Account", "Amount"), ("Rent", "4,321"), ("Supplies", "6,789")]):
            page.insert_text((60, 117 + 25 * row), label)
            page.insert_text((235, 117 + 25 * row), amount)
        document.saveIncr()
    keys = []
    objects = {}

    class Storage:
        def download_file(self, bucket, key, destination):
            shutil.copyfile(source, destination)

        def upload_file(self, path, bucket, key, ExtraArgs):
            assert ExtraArgs == {"ContentType": "application/pdf"}
            prefix, unique_id, name = key.split("/")
            assert (bucket, prefix, name) == ("out", "_lambda_results", "scans.pdf")
            assert UUID(unique_id).version == 4
            keys.append(key)
            shutil.copyfile(path, subset)

        def put_object(self, **kwargs):
            objects[kwargs["Key"]] = json.loads(kwargs["Body"])

    monkeypatch.setattr(index, "s3_client", Storage())
    if s3_output:
        monkeypatch.setattr(storage, "S3_OUTPUT_THRESHOLD_PAGES", 0)
    assert not run_early_quality_check(str(source))[0]
    result = index.process_pdf_from_s3("in", "synthetic-selective.pdf", "none", "out", "job", 500000, True)
    assert result["success"], result
    assert keys == [result["ocr_subset_key"]]
    assert result["ocr_pages"] == [2, 4]
    assert result["document_info"]["page_count"] == 5
    if s3_output:
        native = next(v for k, v in objects.items() if k.endswith("page_3.json"))
        assert "FOB Destination" in str(native)
        table = next(v for k, v in objects.items() if k.endswith("page_5.json"))["structured_data"]["text"]
    else:
        assert "FOB Destination" in result["structured_data"][2]["text"]
        table = result["structured_data"][4]["text"]
    cells = [[c.strip().strip("*") for c in line.strip("|").split("|")] for line in table.splitlines() if line.startswith("|")]
    assert ["Rent", "4,321"] in cells
    assert ["Supplies", "6,789"] in cells
    with fitz.open(source) as original, fitz.open(subset) as sent:
        assert len(sent) == 2
        for i, source_number in enumerate(result["ocr_pages"]):
            assert sent[i].get_pixmap().samples == original[source_number - 1].get_pixmap().samples
            assert not sent[i].get_text().strip()


def test_selective_mode_preserves_native_scan_and_word_limit_verdicts(tmp_path):
    source = tmp_path / "mixed.pdf"
    make_mixed_pdf(source)
    for indices, expected in (([0, 2], True), ([1, 3], False)):
        path = tmp_path / f"{indices[0]}.pdf"
        with fitz.open(source) as doc:
            doc.select(indices)
            doc.save(path)
        passed, stats = run_early_quality_check(str(path), page_specific=True)
        assert passed == expected
        assert "ocr_pages" not in stats
    passed, stats = run_early_quality_check(str(source), max_words=2, page_specific=True)
    assert not passed and stats["error_code"] == "TOO_MANY_WORDS"


def test_subset_storage_failure_preserves_only_trusted_native_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    import index

    source = tmp_path / "source.pdf"
    make_mixed_pdf(source)
    monkeypatch.setattr(index.s3_client, "download_file", lambda bucket, key, dest: shutil.copyfile(source, dest))

    def unavailable(*args, **kwargs):
        raise OSError("storage unavailable")

    monkeypatch.setattr(index.s3_client, "upload_file", unavailable)
    result = index.process_pdf_from_s3("in", "failure-proof.pdf", "none", "out", "job", 500000, True)
    assert result["success"]
    assert result["ocr_pages"] == [2, 4]
    assert result["ocr_subset_error"] == "preparation_failed"
    assert "ocr_subset_key" not in result
    assert "FOB Destination" in result["structured_data"][2]["text"]
