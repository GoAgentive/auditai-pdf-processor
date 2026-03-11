# PDF Processor Lambda

This Lambda function processes PDF documents from S3 using PyMuPDF and returns both markdown-formatted text and word-level bounding boxes.

## Features

- Downloads PDF documents from S3
- Extracts text in markdown format
- Provides word-level bounding boxes with normalized coordinates
- Handles memory-efficient processing for large documents
- Returns structured JSON response

## API Usage

### Request Format

```json
{
  "s3_path": "s3://bucket-name/path/to/document.pdf"
}
```

### Response Format

```json
{
  "success": true,
  "document_info": {
    "page_count": 5,
    "file_size": 245760,
    "title": "Document Title",
    "author": "Author Name",
    "subject": "Subject",
    "creator": "PDF Creator"
  },
  "markdown_text": "## Page 1\n\nDocument content here...",
  "word_bounding_boxes": [
    {
      "page": 1,
      "text": "Document",
      "bbox": {
        "x0": 0.1,
        "y0": 0.2,
        "x1": 0.25,
        "y1": 0.23
      },
      "absolute_bbox": {
        "x0": 72.0,
        "y0": 144.0,
        "x1": 180.0,
        "y1": 165.6
      },
      "page_dimensions": {
        "width": 720.0,
        "height": 720.0
      },
      "block_no": 0,
      "line_no": 0,
      "word_no": 0
    }
  ],
  "word_count": 1234
}
```

## Building and Deployment

### Local Development

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Test locally:
```bash
python test_lambda.py
```

### Building for AWS Lambda

1. Build the deployment package:
```bash
./build.sh
```

2. Deploy using Pulumi:
```bash
pulumi up
```

### Using Docker (Alternative)

1. Build the Docker image:
```bash
docker build -t pdf-processor .
```

2. Test with Docker:
```bash
docker run -p 9000:8080 pdf-processor
```

## Configuration

The Lambda function is configured through Pulumi with the following settings:

- **Runtime**: Python 3.11
- **Memory**: 1024 MB
- **Timeout**: 300 seconds (5 minutes)
- **Permissions**: S3 read access, CloudWatch Logs

## Error Handling

Error responses are backward-compatible and structured:

- Legacy fields: `success`, `error`, `error_type`
- Structured fields: `error_code`, `error_category`, `error_summary`, `error_origin`, `is_timeout`, `processing_stage`
- Route fields: `was_sent_to_ocr`, `ocr_service`, `processing_provider`, `processed_with_pymupdf`, `external_ocr_used`, `route_outcome`, `fallback_to_external_ocr_recommended`
- Optional fields: `is_retryable`, `error_reference`, `error_detail`

`error` always includes a bracketed code prefix (example: `[OCR_LAMBDA_INVALID_S3_PATH] ...`) so upstream services can classify failures consistently.

### Error Code Matrix

| Code | Stage | Meaning | Retryable | Fallback to external OCR |
|---|---|---|---|---|
| `OCR_LAMBDA_MISSING_S3_PATH` | `request_validation` | Required `s3_path` missing | No | No |
| `OCR_LAMBDA_INVALID_GRAPHICS_MODE` | `request_validation` | `graphics_mode` invalid | No | No |
| `OCR_LAMBDA_INVALID_S3_PATH` | `request_validation` | S3 URI format invalid | No | No |
| `OCR_LAMBDA_S3_OBJECT_NOT_FOUND` | `s3_download` | Object key missing in S3 | No | No |
| `OCR_LAMBDA_S3_BUCKET_NOT_FOUND` | `s3_download` | Bucket missing in S3 | No | No |
| `OCR_LAMBDA_S3_ACCESS_DENIED` | `s3_download` | IAM/S3 permission denied | No | No |
| `OCR_LAMBDA_S3_TIMEOUT` | `s3_download` | S3 read timed out | Yes | Yes |
| `OCR_LAMBDA_S3_DOWNLOAD_FAILED` | `s3_download` | Other S3 download failure | Yes | No |
| `OCR_LAMBDA_PDF_EMPTY` | `open_pdf` | Empty PDF payload | No | No |
| `OCR_LAMBDA_PDF_ENCRYPTED` | `open_pdf` | Encrypted PDF not processable | No | No |
| `OCR_LAMBDA_PDF_CORRUPT` | `open_pdf` | Corrupt/unreadable PDF bytes | No | No |
| `OCR_LAMBDA_PDF_PARSE_FAILED` | `open_pdf` | Other PDF parse/open failure | No | No |
| `OCR_LAMBDA_PYMUPDF_TIMEOUT` | `open_pdf`/`extract_pdf` | PyMuPDF operation timed out | Yes | Yes |
| `OCR_LAMBDA_PYMUPDF_MEMORY_LIMIT` | `open_pdf`/`extract_pdf` | Memory pressure/OOM | Yes | Yes |
| `OCR_LAMBDA_PYMUPDF_EXTRACTION_FAILED` | `extract_pdf` | Other PyMuPDF extraction failure | Yes | Yes |
| `OCR_LAMBDA_UNHANDLED_EXCEPTION` | `request_processing` | Unhandled handler failure | Yes | No |

### Route Semantics

- `was_sent_to_ocr=true` means the request reached the OCR Lambda boundary.
- `processed_with_pymupdf=true` means extraction executed in PyMuPDF.
- `processed_with_pymupdf=false` means rejection/failure happened before extraction.
- `external_ocr_used=false` in this Lambda (this component does not call Azure/OpenAI OCR directly).
- `fallback_to_external_ocr_recommended=true` flags failures where downstream can consider Azure/OpenAI OCR fallback.

## Bounding Box Coordinates

The function returns both normalized (0-1) and absolute pixel coordinates:

- **Normalized coordinates**: `bbox` field with values between 0 and 1
- **Absolute coordinates**: `absolute_bbox` field with pixel values
- **Page dimensions**: Original page width and height in pixels

## Performance Considerations

- Large PDFs are processed page by page to manage memory
- PyMuPDF is efficient for text extraction
- Consider using streaming for very large documents
- Monitor Lambda memory usage and adjust as needed

## Integration with Elixir Router

The Lambda is integrated with the Elixir application via the `PDFProcessorController`:

```elixir
# Route definition
post "/pdf/process", PDFProcessorController, :process_pdf
```

## Testing

Use the provided test script to verify functionality:

```bash
python test_lambda.py
```

Make sure to update the S3 path in the test script with an actual PDF file for testing.
