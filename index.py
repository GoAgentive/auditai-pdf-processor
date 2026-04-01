"""AWS Lambda handler for PDF processing with PyMuPDF.

Thin entry point that orchestrates:
1. Early quality check (fast word extraction, ~0.3s)
2. Parallel markdown extraction (multiprocessing across 6 vCPUs)
3. Parallel S3 output (ThreadPoolExecutor for per-page writes)
"""

import json
import boto3
import fitz
import logging
import os
from typing import Dict, Any

from models import DocumentInfo, PDFProcessingResponse
from quality_check import run_early_quality_check
from extraction import (
    extract_markdown_parallel,
    extract_words,
    extract_graphics,
    build_structured_data,
    build_graphics_only_data,
)
from s3_output import should_use_s3_output, write_results_to_s3

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Log library versions at module load time
import pymupdf4llm

print(f"PyMuPDF (fitz) version: {fitz.version}")
print(f"pymupdf4llm version: {pymupdf4llm.__version__}")

s3_client = boto3.client("s3")


def process_pdf_from_s3(
    bucket: str,
    key: str,
    graphics_mode: str = "none",
    output_bucket: str = None,
    request_id: str = None,
) -> Dict[str, Any]:
    """
    Download PDF from S3 to /tmp, process it, and return results.

    Pipeline:
    1. Download to /tmp
    2. Early quality check (word extraction only — ~0.5s on Lambda)
    3. If quality fails: return early with failure
    4. Parallel markdown extraction (multiprocessing)
    5. Word bounding box extraction
    6. Optional graphics extraction
    7. S3 output for large docs, inline for small
    """
    local_path = f"/tmp/{os.path.basename(key)}"
    try:
        # Step 1: Download PDF to /tmp
        s3_client.download_file(bucket, key, local_path)
        file_size = os.path.getsize(local_path)
        logger.info("Downloaded %s (%d bytes) to %s", key, file_size, local_path)

        # Step 2: Early quality check (fast — no pymupdf4llm)
        if graphics_mode != "graphics_only":
            passed, stats = run_early_quality_check(local_path)
            logger.info("Quality check: passed=%s, stats=%s", passed, stats)

            if not passed:
                return {
                    "success": False,
                    "error": stats.get("failure_reason", "Quality check failed"),
                    "error_type": "QualityCheckFailed",
                    "word_count": stats.get("word_count", 0),
                    "page_count": stats.get("page_count", 0),
                }

        # Open document for processing
        pdf_document = fitz.open(local_path)
        try:
            page_count = len(pdf_document)
            metadata = pdf_document.metadata

            document_info = DocumentInfo(
                page_count=int(page_count),
                file_size=int(file_size),
                title=str(metadata.get("title", "") or ""),
                author=str(metadata.get("author", "") or ""),
                subject=str(metadata.get("subject", "") or ""),
                creator=str(metadata.get("creator", "") or ""),
            )

            # Graphics-only mode: skip text extraction entirely
            if graphics_mode == "graphics_only":
                per_page_graphics = extract_graphics(pdf_document)
                structured_data = build_graphics_only_data(per_page_graphics)
                word_bounding_boxes = []
            else:
                # Step 3: Parallel markdown extraction
                page_chunks = extract_markdown_parallel(local_path, page_count)

                # Step 4: Word bounding boxes (fast, ~0.3s)
                word_bounding_boxes = extract_words(pdf_document)

                # Step 5: Optional graphics
                per_page_graphics = None
                if graphics_mode == "full":
                    per_page_graphics = extract_graphics(pdf_document)

                structured_data = build_structured_data(
                    page_chunks, graphics_mode, per_page_graphics
                )

            # Step 6: Output
            if should_use_s3_output(page_count, output_bucket, request_id):
                manifest_key = write_results_to_s3(
                    s3_client,
                    output_bucket,
                    request_id,
                    key,
                    word_bounding_boxes,
                    structured_data,
                    document_info.to_dict(),
                )
                return {
                    "success": True,
                    "output_mode": "s3",
                    "manifest_key": manifest_key,
                    "output_bucket": output_bucket,
                    "document_info": document_info.to_dict(),
                    "page_count": int(page_count),
                    "word_count": int(len(word_bounding_boxes)),
                }

            # Inline response for small documents
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
        logger.error("Error processing PDF: %s", str(e), exc_info=True)
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
        }
    finally:
        if os.path.exists(local_path):
            os.remove(local_path)


def lambda_handler(event, context):
    """
    AWS Lambda handler for PDF processing.

    Expected event format:
    {
        "s3_path": "s3://bucket-name/path/to/file.pdf",
        "graphics_mode": "none" | "full" | "graphics_only"  (optional, default: "none"),
        "output_bucket": "bucket-name"  (optional, enables per-page S3 output for large docs),
        "request_id": "unique-id"  (optional, used for S3 output key prefix)
    }

    Graphics modes:
    - "none": Extract text/markdown only, no graphics (default)
    - "full": Extract text/markdown + graphics
    - "graphics_only": Extract only graphics, skip text/markdown/bboxes
    """
    try:
        if "body" in event:
            body = json.loads(event["body"])
        else:
            body = event

        s3_path = body.get("s3_path")
        graphics_mode = body.get("graphics_mode", "none")
        output_bucket = body.get("output_bucket")
        request_id = body.get("request_id")
        logger.info(
            "Received s3_path: %s, graphics_mode: %s, output_bucket: %s",
            s3_path, graphics_mode, output_bucket,
        )

        if not s3_path:
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "error": "Missing s3_path parameter",
                    "expected_format": "s3://bucket-name/path/to/file.pdf",
                }),
            }

        if graphics_mode not in ["none", "full", "graphics_only"]:
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "error": "Invalid graphics_mode parameter",
                    "valid_values": ["none", "full", "graphics_only"],
                }),
            }

        if not s3_path.startswith("s3://"):
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "error": "Invalid S3 path format",
                    "expected_format": "s3://bucket-name/path/to/file.pdf",
                }),
            }

        path_parts = s3_path[5:].split("/", 1)
        if len(path_parts) != 2:
            return {
                "statusCode": 400,
                "body": json.dumps({
                    "error": "Invalid S3 path format",
                    "expected_format": "s3://bucket-name/path/to/file.pdf",
                }),
            }

        bucket, key = path_parts

        result = process_pdf_from_s3(bucket, key, graphics_mode, output_bucket, request_id)

        status_code = 200 if result.get("success") else 500
        return {
            "statusCode": status_code,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
            },
            "body": json.dumps(result),
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
            }),
        }
