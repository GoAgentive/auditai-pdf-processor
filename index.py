import json
import boto3
import fitz  # PyMuPDF
import pymupdf4llm
import io
from typing import Dict, List, Any, Optional, Union
import logging
import os

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Log library versions at module load time
print(f"PyMuPDF (fitz) version: {fitz.version}")
print(f"pymupdf4llm version: {pymupdf4llm.__version__}")

# Response type definitions for PDF processing
class BoundingBox:
    def __init__(self, x0: float, y0: float, x1: float, y1: float):
        self.x0 = x0
        self.y0 = y0
        self.x1 = x1
        self.y1 = y1
    
    def to_dict(self) -> Dict[str, float]:
        return {
            "x0": float(self.x0),
            "y0": float(self.y0),
            "x1": float(self.x1),
            "y1": float(self.y1)
        }

class WordBoundingBox:
    def __init__(self, page: int, text: str, bbox: Dict[str, float], 
                 absolute_bbox: Dict[str, float], page_dimensions: Dict[str, float],
                 block_no: int, line_no: int, word_no: int):
        self.page = page
        self.text = text
        self.bbox = bbox
        self.absolute_bbox = absolute_bbox
        self.page_dimensions = page_dimensions
        self.block_no = block_no
        self.line_no = line_no
        self.word_no = word_no
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "page": int(self.page),
            "text": str(self.text),
            "bbox": self.bbox,
            "absolute_bbox": self.absolute_bbox,
            "page_dimensions": self.page_dimensions,
            "block_no": int(self.block_no),
            "line_no": int(self.line_no),
            "word_no": int(self.word_no)
        }

class ImageData:
    def __init__(self, number: int, bbox: Dict[str, float], transform: List[float],
                 width: int, height: int, colorspace: int, cs_name: str,
                 xres: int, yres: int, bpc: int, size: int):
        self.number = number
        self.bbox = bbox
        self.transform = transform
        self.width = width
        self.height = height
        self.colorspace = colorspace
        self.cs_name = cs_name
        self.xres = xres
        self.yres = yres
        self.bpc = bpc
        self.size = size
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "number": int(self.number),
            "bbox": self.bbox,
            "transform": [float(x) for x in self.transform],
            "width": int(self.width),
            "height": int(self.height),
            "colorspace": int(self.colorspace),
            "cs_name": str(self.cs_name),
            "xres": int(self.xres),
            "yres": int(self.yres),
            "bpc": int(self.bpc),
            "size": int(self.size)
        }

class DocumentInfo:
    def __init__(self, page_count: int, file_size: int, title: str, author: str, 
                 subject: str, creator: str):
        self.page_count = page_count
        self.file_size = file_size
        self.title = title
        self.author = author
        self.subject = subject
        self.creator = creator
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_count": int(self.page_count),
            "file_size": int(self.file_size),
            "title": str(self.title or ""),
            "author": str(self.author or ""),
            "subject": str(self.subject or ""),
            "creator": str(self.creator or "")
        }

class PageData:
    def __init__(self, metadata: Dict[str, Any], toc_items: List[Any], 
                 tables: List[Any], images: List[ImageData], graphics: List[Any],
                 text: str, words: List[Any]):
        self.metadata = metadata
        self.toc_items = toc_items
        self.tables = tables
        self.images = images
        self.graphics = graphics
        self.text = text
        self.words = words
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "metadata": self.metadata,
            "toc_items": self.toc_items,
            "tables": self.tables,
            "images": [img.to_dict() for img in self.images],
            "graphics": self.graphics,
            "text": str(self.text),
            "words": self.words
        }

class PDFProcessingResponse:
    def __init__(self, success: bool, document_info: DocumentInfo, 
                 word_bounding_boxes: List[WordBoundingBox],
                 word_count: int, structured_data: List[PageData],
                 error: Optional[str] = None, error_type: Optional[str] = None):
        self.success = success
        self.document_info = document_info
        self.word_bounding_boxes = word_bounding_boxes
        self.word_count = word_count
        self.structured_data = structured_data
        self.error = error
        self.error_type = error_type
    
    def to_dict(self) -> Dict[str, Any]:
        result = {
            "success": bool(self.success),
            "document_info": self.document_info.to_dict(),
            "word_bounding_boxes": [wb.to_dict() for wb in self.word_bounding_boxes],
            "word_count": int(self.word_count),
            "structured_data": [pd.to_dict() for pd in self.structured_data]
        }
        
        if self.error:
            result["error"] = str(self.error)
        if self.error_type:
            result["error_type"] = str(self.error_type)
            
        return result

s3_client = boto3.client('s3')


def extract_all_graphics(
    page: fitz.Page,
    page_width: float,
    page_height: float,
) -> List[Dict[str, Any]]:
    """
    Extract all vector graphics primitives from the page using get_drawings().

    Returns a list of normalized graphic primitives with both absolute and normalized coordinates.
    Supports: lines, curves (Bezier), rectangles, and quadrilaterals.

    Each primitive includes:
    - type: "line", "curve", "rect", or "quad"
    - Coordinate data (absolute and normalized 0-1)
    - Stroke properties (width, color)
    - Fill properties (fill color)
    """
    graphics_primitives = []
    drawings = page.get_cdrawings()

    for d in drawings:
        stroke_width = float(d.get("width", 0.0) or 0.0)
        color = d.get("color")  # Stroke color (RGB tuple or None)
        fill = d.get("fill")    # Fill color (RGB tuple or None)
        items = d.get("items", [])

        for item in items:
            op = item[0]

            # Line segments: ("l", p1, p2)
            if op == "l":
                p1, p2 = item[1], item[2]
                graphics_primitives.append({
                    "type": "line",
                    "p1": {
                        "x": float(p1.x),
                        "y": float(p1.y),
                        "x_norm": float(p1.x) / page_width,
                        "y_norm": float(p1.y) / page_height,
                    },
                    "p2": {
                        "x": float(p2.x),
                        "y": float(p2.y),
                        "x_norm": float(p2.x) / page_width,
                        "y_norm": float(p2.y) / page_height,
                    },
                    "stroke_width": stroke_width,
                    "stroke_color": list(color) if color else None,
                    "fill_color": list(fill) if fill else None,
                })

            # Cubic Bezier curves: ("c", p1, p2, p3, p4)
            elif op == "c":
                p1, p2, p3, p4 = item[1], item[2], item[3], item[4]
                graphics_primitives.append({
                    "type": "curve",
                    "p1": {
                        "x": float(p1.x),
                        "y": float(p1.y),
                        "x_norm": float(p1.x) / page_width,
                        "y_norm": float(p1.y) / page_height,
                    },
                    "p2": {
                        "x": float(p2.x),
                        "y": float(p2.y),
                        "x_norm": float(p2.x) / page_width,
                        "y_norm": float(p2.y) / page_height,
                    },
                    "p3": {
                        "x": float(p3.x),
                        "y": float(p3.y),
                        "x_norm": float(p3.x) / page_width,
                        "y_norm": float(p3.y) / page_height,
                    },
                    "p4": {
                        "x": float(p4.x),
                        "y": float(p4.y),
                        "x_norm": float(p4.x) / page_width,
                        "y_norm": float(p4.y) / page_height,
                    },
                    "stroke_width": stroke_width,
                    "stroke_color": list(color) if color else None,
                    "fill_color": list(fill) if fill else None,
                })

            # Rectangles: ("re", rect)
            elif op == "re":
                rect = item[1]
                graphics_primitives.append({
                    "type": "rect",
                    "bbox": {
                        "x0": float(rect.x0),
                        "y0": float(rect.y0),
                        "x1": float(rect.x1),
                        "y1": float(rect.y1),
                        "x0_norm": float(rect.x0) / page_width,
                        "y0_norm": float(rect.y0) / page_height,
                        "x1_norm": float(rect.x1) / page_width,
                        "y1_norm": float(rect.y1) / page_height,
                    },
                    "stroke_width": stroke_width,
                    "stroke_color": list(color) if color else None,
                    "fill_color": list(fill) if fill else None,
                })

            # Quadrilaterals: ("qu", quad)
            elif op == "qu":
                quad = item[1]
                graphics_primitives.append({
                    "type": "quad",
                    "points": [
                        {
                            "x": float(quad.ul.x),
                            "y": float(quad.ul.y),
                            "x_norm": float(quad.ul.x) / page_width,
                            "y_norm": float(quad.ul.y) / page_height,
                        },
                        {
                            "x": float(quad.ur.x),
                            "y": float(quad.ur.y),
                            "x_norm": float(quad.ur.x) / page_width,
                            "y_norm": float(quad.ur.y) / page_height,
                        },
                        {
                            "x": float(quad.ll.x),
                            "y": float(quad.ll.y),
                            "x_norm": float(quad.ll.x) / page_width,
                            "y_norm": float(quad.ll.y) / page_height,
                        },
                        {
                            "x": float(quad.lr.x),
                            "y": float(quad.lr.y),
                            "x_norm": float(quad.lr.x) / page_width,
                            "y_norm": float(quad.lr.y) / page_height,
                        },
                    ],
                    "stroke_width": stroke_width,
                    "stroke_color": list(color) if color else None,
                    "fill_color": list(fill) if fill else None,
                })

    return graphics_primitives


def extract_text_with_bounding_boxes(pdf_document: fitz.Document, pdf_data: bytes, graphics_mode: str = 'full') -> tuple[List[WordBoundingBox], List[PageData]]:
    """
    Extract page-level markdown and word-level bounding boxes from PDF.
    Uses pymupdf4llm with page_chunks=True for proper page-level markdown generation.

    Args:
        pdf_document: PyMuPDF document object
        pdf_data: Raw PDF bytes
        graphics_mode: Processing mode - 'none', 'full', or 'graphics_only'

    Returns:
        tuple: (word_bounding_boxes, structured_page_data)
    """
    # Initialize structured page data
    structured_page_data = []
    word_bounding_boxes = []

    # Process based on graphics_mode
    if graphics_mode != 'graphics_only':
        # Extract text/markdown/bboxes for 'none' and 'full' modes
        try:
            # Generate page-level markdown using page_chunks=True
            page_chunks = pymupdf4llm.to_markdown(pdf_document, page_chunks=True)

            for page_chunk in page_chunks:
                # Each page_chunk is a dict with 'text' and metadata
                page_markdown = page_chunk.get('text', '')
                page_metadata = page_chunk.get('metadata', {})
                page_tables = page_chunk.get('tables', [])
                page_images = page_chunk.get('images', [])

                # Create page data with markdown content
                page_data_obj = PageData(
                    metadata=page_metadata,
                    toc_items=[],
                    tables=page_tables,
                    images=[],  # Will be processed separately for bounding boxes
                    graphics=[],
                    text=page_markdown.strip(),  # Page-specific markdown
                    words=[]   # Word extraction handled separately
                )
                structured_page_data.append(page_data_obj)

        except Exception as e:
            print(f"ERROR: PDF extraction failed: {str(e)}")
            raise e

        # Extract word-level bounding boxes
        for page_num in range(len(pdf_document)):
            page = pdf_document[page_num]

            # Get page dimensions
            page_rect = page.rect
            page_width = float(page_rect.width)
            page_height = float(page_rect.height)

            # Extract word-level bounding boxes
            words = page.get_text("words")
            for word_info in words:
                x0, y0, x1, y1, word_text, block_no, line_no, word_no = word_info

                # Normalize coordinates to 0-1 range (ensure JSON serializable)
                normalized_bbox = {
                    "x0": float(x0 / page_width),
                    "y0": float(y0 / page_height),
                    "x1": float(x1 / page_width),
                    "y1": float(y1 / page_height)
                }

                absolute_bbox = {
                    "x0": float(x0),
                    "y0": float(y0),
                    "x1": float(x1),
                    "y1": float(y1)
                }

                page_dimensions = {
                    "width": float(page_width),
                    "height": float(page_height)
                }

                word_bbox = WordBoundingBox(
                    page=int(page_num + 1),
                    text=str(word_text),
                    bbox=normalized_bbox,
                    absolute_bbox=absolute_bbox,
                    page_dimensions=page_dimensions,
                    block_no=int(block_no),
                    line_no=int(line_no),
                    word_no=int(word_no)
                )
                word_bounding_boxes.append(word_bbox)

    # Extract graphics for 'full' and 'graphics_only' modes
    if graphics_mode in ['full', 'graphics_only']:
        for page_num in range(len(pdf_document)):
            page = pdf_document[page_num]

            # Get page dimensions
            page_rect = page.rect
            page_width = float(page_rect.width)
            page_height = float(page_rect.height)

            # Extract all graphics primitives
            graphics_primitives = extract_all_graphics(
                page, page_width, page_height
            )

            # For graphics_only mode, create minimal page data structures
            if graphics_mode == 'graphics_only':
                page_data_obj = PageData(
                    metadata={},
                    toc_items=[],
                    tables=[],
                    images=[],
                    graphics=graphics_primitives,
                    text="",
                    words=[]
                )
                structured_page_data.append(page_data_obj)
            else:
                # Attach to existing structured_data for 'full' mode
                if page_num < len(structured_page_data):
                    structured_page_data[page_num].graphics = graphics_primitives

    return word_bounding_boxes, structured_page_data

S3_OUTPUT_THRESHOLD_PAGES = 20


def write_results_to_s3(output_bucket: str, request_id: str, file_key: str,
                         word_bounding_boxes: list, structured_data: list,
                         document_info: 'DocumentInfo') -> str:
    """
    Write per-page results to S3 and return the manifest key prefix.

    Each page gets its own JSON file for lazy fetching by the consumer.
    """
    import hashlib
    file_key_hash = hashlib.md5(file_key.encode()).hexdigest()[:12]
    prefix = f"_lambda_results/{request_id}/{file_key_hash}"

    # Group word bounding boxes by page
    words_by_page: Dict[int, list] = {}
    for wb in word_bounding_boxes:
        page_num = wb.page if hasattr(wb, 'page') else wb.get('page', 1)
        words_by_page.setdefault(page_num, []).append(
            wb.to_dict() if hasattr(wb, 'to_dict') else wb
        )

    page_count = len(structured_data)
    page_keys = []

    for idx, page_data in enumerate(structured_data):
        page_num = idx + 1
        page_obj = {
            "structured_data": page_data.to_dict() if hasattr(page_data, 'to_dict') else page_data,
            "word_bounding_boxes": words_by_page.get(page_num, []),
        }
        page_key = f"{prefix}/page_{page_num}.json"
        s3_client.put_object(
            Bucket=output_bucket,
            Key=page_key,
            Body=json.dumps(page_obj),
            ContentType="application/json",
        )
        page_keys.append(page_key)

    # Write manifest
    manifest = {
        "document_info": document_info.to_dict(),
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

    return f"s3://{output_bucket}/{manifest_key}"


def process_pdf_from_s3(bucket: str, key: str, graphics_mode: str = 'none',
                         output_bucket: str = None, request_id: str = None) -> Dict[str, Any]:
    """
    Download PDF from S3 to /tmp, process it, and return results.

    For large documents (> S3_OUTPUT_THRESHOLD_PAGES pages) with an output_bucket,
    writes per-page results to S3 and returns a lightweight manifest.

    Args:
        bucket: S3 bucket name
        key: S3 object key
        graphics_mode: Processing mode - 'none', 'full', or 'graphics_only'
        output_bucket: Optional S3 bucket for per-page output (enables manifest mode)
        request_id: Request ID for S3 output key prefix

    Returns:
        dict: Processing results (inline or manifest)
    """
    local_path = f"/tmp/{os.path.basename(key)}"
    try:
        # Download PDF to /tmp instead of RAM
        s3_client.download_file(bucket, key, local_path)
        file_size = os.path.getsize(local_path)

        # Open from file path — PyMuPDF doesn't need the whole file in memory
        pdf_document = fitz.open(local_path)

        try:
            # Extract text and bounding boxes based on graphics_mode
            word_bounding_boxes, structured_data = extract_text_with_bounding_boxes(
                pdf_document, None, graphics_mode
            )

            # Get document metadata
            metadata = pdf_document.metadata
            page_count = len(pdf_document)

            # Create structured document info
            document_info = DocumentInfo(
                page_count=int(page_count),
                file_size=int(file_size),
                title=str(metadata.get("title", "") or ""),
                author=str(metadata.get("author", "") or ""),
                subject=str(metadata.get("subject", "") or ""),
                creator=str(metadata.get("creator", "") or "")
            )

            # For large documents with output_bucket, write per-page to S3
            if output_bucket and page_count > S3_OUTPUT_THRESHOLD_PAGES and request_id:
                manifest_key = write_results_to_s3(
                    output_bucket, request_id, key,
                    word_bounding_boxes, structured_data, document_info
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

            # For small documents, return inline as before
            response = PDFProcessingResponse(
                success=True,
                document_info=document_info,
                word_bounding_boxes=word_bounding_boxes,
                word_count=int(len(word_bounding_boxes)),
                structured_data=structured_data
            )

            return response.to_dict()

        finally:
            pdf_document.close()

    except Exception as e:
        print(f"ERROR: Error processing PDF: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__
        }
    finally:
        # Clean up /tmp
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
        # Parse S3 path from event
        if 'body' in event:
            # API Gateway event
            body = json.loads(event['body'])
        else:
            # Direct invocation
            body = event

        s3_path = body.get('s3_path')
        graphics_mode = body.get('graphics_mode', 'none')
        output_bucket = body.get('output_bucket')
        request_id = body.get('request_id')
        logger.info("Received s3_path: %s, graphics_mode: %s, output_bucket: %s", s3_path, graphics_mode, output_bucket)

        if not s3_path:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': 'Missing s3_path parameter',
                    'expected_format': 's3://bucket-name/path/to/file.pdf'
                })
            }

        # Validate graphics_mode
        if graphics_mode not in ['none', 'full', 'graphics_only']:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': 'Invalid graphics_mode parameter',
                    'valid_values': ['none', 'full', 'graphics_only']
                })
            }

        # Parse S3 path
        if not s3_path.startswith('s3://'):
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': 'Invalid S3 path format',
                    'expected_format': 's3://bucket-name/path/to/file.pdf'
                })
            }

        # Extract bucket and key from S3 path
        path_parts = s3_path[5:].split('/', 1)  # Remove 's3://' prefix
        if len(path_parts) != 2:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': 'Invalid S3 path format',
                    'expected_format': 's3://bucket-name/path/to/file.pdf'
                })
            }

        bucket, key = path_parts

        # Process the PDF
        result = process_pdf_from_s3(bucket, key, graphics_mode, output_bucket, request_id)
        
        # Return response
        status_code = 200 if result['success'] else 500
        return {
            'statusCode': status_code,
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            },
            'body': json.dumps(result)
        }
        
    except Exception as e:
        return {
            'statusCode': 500,
            'body': json.dumps({
                'success': False,
                'error': str(e),
                'error_type': type(e).__name__
            })
}
