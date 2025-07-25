import json
import boto3
import fitz  # PyMuPDF
import io
from typing import Dict, List, Any
import logging
import os

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

s3_client = boto3.client('s3')
secrets_client = boto3.client('secretsmanager')

def verify_auth_token(event) -> bool:
    """
    Verify the authorization token from the request.
    
    Returns:
        bool: True if authentication is valid, False otherwise
    """
    try:
        # Get authorization header from API Gateway event or direct invocation
        auth_header = None
        
        if 'headers' in event:
            # API Gateway event
            headers = event.get('headers', {})
            auth_header = headers.get('Authorization') or headers.get('authorization')
        elif 'authorization' in event:
            # Direct invocation with auth field
            auth_header = event.get('authorization')
        
        if not auth_header:
            logger.error("No authorization header found")
            return False
        
        # Extract token from "Bearer <token>" format
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]  # Remove "Bearer " prefix
        else:
            token = auth_header
        
        # Get the expected token from AWS Secrets Manager
        secret_id = os.environ.get('AUTH_SECRET_ID')
        if not secret_id:
            logger.error("AUTH_SECRET_ID environment variable not set")
            return False
        
        try:
            response = secrets_client.get_secret_value(SecretId=secret_id)
            secret_data = json.loads(response['SecretString'])
            expected_token = secret_data.get('accessKey')
            
            if not expected_token:
                logger.error("No accessKey found in secret")
                return False
            
            # Compare tokens
            if token == expected_token:
                logger.info("Authentication successful")
                return True
            else:
                logger.error("Token mismatch")
                return False
                
        except Exception as e:
            logger.error(f"Failed to retrieve or validate secret: {str(e)}")
            return False
            
    except Exception as e:
        logger.error(f"Authentication verification failed: {str(e)}")
        return False

def extract_text_with_bounding_boxes(pdf_document: fitz.Document) -> tuple[str, List[Dict[str, Any]]]:
    """
    Extract text as markdown and word-level bounding boxes from PDF.
    
    Returns:
        tuple: (markdown_text, word_bounding_boxes)
    """
    markdown_text = ""
    word_bounding_boxes = []
    
    for page_num in range(len(pdf_document)):
        page = pdf_document[page_num]
        
        # Get page dimensions
        page_rect = page.rect
        page_width = page_rect.width
        page_height = page_rect.height
        
        # Extract text blocks for markdown
        blocks = page.get_text("dict")
        page_markdown = f"\n\n## Page {page_num + 1}\n\n"
        
        for block in blocks["blocks"]:
            if "lines" in block:
                for line in block["lines"]:
                    line_text = ""
                    for span in line["spans"]:
                        text = span["text"].strip()
                        if text:
                            line_text += text + " "
                    
                    if line_text.strip():
                        page_markdown += line_text.strip() + "\n"
        
        markdown_text += page_markdown
        
        # Extract word-level bounding boxes
        words = page.get_text("words")
        for word_info in words:
            x0, y0, x1, y1, word_text, block_no, line_no, word_no = word_info
            
            # Normalize coordinates to 0-1 range
            normalized_bbox = {
                "x0": x0 / page_width,
                "y0": y0 / page_height,
                "x1": x1 / page_width,
                "y1": y1 / page_height
            }
            
            word_bounding_boxes.append({
                "page": page_num + 1,
                "text": word_text,
                "bbox": normalized_bbox,
                "absolute_bbox": {
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1
                },
                "page_dimensions": {
                    "width": page_width,
                    "height": page_height
                },
                "block_no": block_no,
                "line_no": line_no,
                "word_no": word_no
            })
    
    return markdown_text, word_bounding_boxes

def process_pdf_from_s3(bucket: str, key: str) -> Dict[str, Any]:
    """
    Download PDF from S3, process it, and return results.
    
    Args:
        bucket: S3 bucket name
        key: S3 object key
        
    Returns:
        dict: Processing results with markdown and bounding boxes
    """
    try:
        # Download PDF from S3
        logger.info(f"Downloading PDF from s3://{bucket}/{key}")
        response = s3_client.get_object(Bucket=bucket, Key=key)
        pdf_data = response['Body'].read()
        
        # Get file size for memory management
        file_size = len(pdf_data)
        logger.info(f"PDF size: {file_size} bytes")
        
        # Process PDF with PyMuPDF
        pdf_document = fitz.open(stream=pdf_data, filetype="pdf")
        
        try:
            # Extract text and bounding boxes
            markdown_text, word_bounding_boxes = extract_text_with_bounding_boxes(pdf_document)
            
            # Get document metadata
            metadata = pdf_document.metadata
            page_count = len(pdf_document)
            
            result = {
                "success": True,
                "document_info": {
                    "page_count": page_count,
                    "file_size": file_size,
                    "title": metadata.get("title", ""),
                    "author": metadata.get("author", ""),
                    "subject": metadata.get("subject", ""),
                    "creator": metadata.get("creator", "")
                },
                "markdown_text": markdown_text,
                "word_bounding_boxes": word_bounding_boxes,
                "word_count": len(word_bounding_boxes)
            }
            
            logger.info(f"Successfully processed PDF: {page_count} pages, {len(word_bounding_boxes)} words")
            return result
            
        finally:
            pdf_document.close()
            
    except Exception as e:
        logger.error(f"Error processing PDF: {str(e)}")
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__
        }

def lambda_handler(event, context):
    """
    AWS Lambda handler for PDF processing.
    
    Expected event format:
    {
        "s3_path": "s3://bucket-name/path/to/file.pdf",
        "authorization": "Bearer <token>"  // Optional: can be in headers instead
    }
    """
    try:
        # Verify authentication first
        if not verify_auth_token(event):
            logger.error("Authentication failed")
            return {
                'statusCode': 403,
                'headers': {
                    'Content-Type': 'application/json',
                },
                'body': json.dumps({
                    'success': False,
                    'error': 'Authentication failed',
                    'error_type': 'AuthenticationError'
                })
            }
        # Parse S3 path from event
        if 'body' in event:
            # API Gateway event
            body = json.loads(event['body'])
        else:
            # Direct invocation
            body = event
            
        s3_path = body.get('s3_path')
        if not s3_path:
            return {
                'statusCode': 400,
                'body': json.dumps({
                    'error': 'Missing s3_path parameter',
                    'expected_format': 's3://bucket-name/path/to/file.pdf'
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
        result = process_pdf_from_s3(bucket, key)
        
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
        logger.error(f"Lambda handler error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({
                'success': False,
                'error': str(e),
                'error_type': type(e).__name__
            })
        }