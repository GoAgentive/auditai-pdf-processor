"""S3 output for per-page results with parallel writes."""

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Any

logger = logging.getLogger(__name__)

S3_OUTPUT_THRESHOLD_PAGES = 20
S3_WRITE_WORKERS = 10  # Parallel S3 put_object calls


def should_use_s3_output(page_count: int, output_bucket: str, request_id: str) -> bool:
    """Determine if results should be written to S3 vs returned inline."""
    return bool(output_bucket) and page_count > S3_OUTPUT_THRESHOLD_PAGES and bool(request_id)


def write_results_to_s3(
    s3_client,
    output_bucket: str,
    request_id: str,
    file_key: str,
    word_bounding_boxes: list,
    structured_data: list,
    document_info_dict: dict,
) -> str:
    """
    Write per-page results to S3 in parallel and return the manifest key.

    Each page gets its own JSON file for lazy fetching by the consumer.
    S3 writes are parallelized with ThreadPoolExecutor for ~5-10x speedup
    over sequential writes on large documents.
    """
    file_key_hash = hashlib.md5(file_key.encode()).hexdigest()[:12]
    prefix = f"_lambda_results/{request_id}/{file_key_hash}"

    # Group word bounding boxes by page
    words_by_page: Dict[int, list] = {}
    for wb in word_bounding_boxes:
        page_num = wb.page if hasattr(wb, "page") else wb.get("page", 1)
        words_by_page.setdefault(page_num, []).append(
            wb.to_dict() if hasattr(wb, "to_dict") else wb
        )

    page_count = len(structured_data)

    # Build all page payloads
    page_uploads = []
    for idx, page_data in enumerate(structured_data):
        page_num = idx + 1
        page_obj = {
            "structured_data": page_data.to_dict() if hasattr(page_data, "to_dict") else page_data,
            "word_bounding_boxes": words_by_page.get(page_num, []),
        }
        page_key = f"{prefix}/page_{page_num}.json"
        page_uploads.append((page_key, json.dumps(page_obj)))

    # Write pages in parallel
    def upload_page(args):
        key, body = args
        s3_client.put_object(
            Bucket=output_bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
        return key

    n_workers = min(S3_WRITE_WORKERS, page_count)
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        page_keys = list(executor.map(upload_page, page_uploads))

    # Write manifest (single write, after all pages are done)
    manifest = {
        "document_info": document_info_dict,
        "page_count": page_count,
        "word_count": len(word_bounding_boxes),
        "page_keys": page_keys,
        "prefix": prefix,
    }
    manifest_key = f"{prefix}/manifest.json"
    s3_client.put_object(
        Bucket=output_bucket,
        Key=manifest_key,
        Body=json.dumps(manifest),
        ContentType="application/json",
    )

    logger.info(
        "Wrote %d page results to s3://%s/%s (parallel=%d)",
        page_count, output_bucket, prefix, n_workers,
    )

    return f"s3://{output_bucket}/{manifest_key}"
