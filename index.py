import json
import boto3
import fitz  # PyMuPDF
import io
import requests
from typing import Dict, List, Any
import logging
import os

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

s3_client = boto3.client('s3')

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

def parse_s3_path(s3_path: str) -> tuple[str, str]:
    """Parse S3 path into bucket and key components."""
    if not s3_path.startswith('s3://'):
        raise ValueError('Invalid S3 path format')
    
    path_parts = s3_path[5:].split('/', 1)  # Remove 's3://' prefix
    if len(path_parts) != 2:
        raise ValueError('Invalid S3 path format')
    
    return path_parts[0], path_parts[1]

def store_results_in_s3(bucket: str, job_id: str, result: Dict[str, Any]) -> str:
    """Store processing results in S3 and return the S3 path."""
    results_key = f"processed/{job_id}/results.json"
    
    s3_client.put_object(
        Bucket=bucket,
        Key=results_key,
        Body=json.dumps(result, indent=2),
        ContentType='application/json'
    )
    
    return f"s3://{bucket}/{results_key}"

def send_webhook_notification(callback_url: str, payload: Dict[str, Any], max_retries: int = 3):
    """Send webhook notification with retry logic."""
    for attempt in range(max_retries):
        try:
            response = requests.post(
                callback_url, 
                json=payload, 
                timeout=30,
                headers={'Content-Type': 'application/json'}
            )
            response.raise_for_status()
            logger.info(f"Successfully sent webhook notification to {callback_url}")
            return
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"Webhook attempt {attempt + 1} failed: {str(e)}")
            if attempt == max_retries - 1:
                logger.error(f"Failed to send webhook after {max_retries} attempts")
                raise

def send_completion_to_eventbridge(job_id: str, status: str, summary: Dict[str, Any]):
    """Send completion event to EventBridge for SQS-based workflows."""
    try:
        eventbridge = boto3.client('events')
        
        eventbridge.put_events(
            Entries=[
                {
                    'Source': 'pdf-processor',
                    'DetailType': 'Document Processing Completed',
                    'Detail': json.dumps({
                        'jobId': job_id,
                        'status': status,
                        'summary': summary,
                        'timestamp': f"{int(__import__('time').time())}"
                    })
                }
            ]
        )
        logger.info(f"Sent completion event to EventBridge for job {job_id}")
        
    except Exception as e:
        logger.error(f"Failed to send EventBridge event: {str(e)}")
        # Don't raise - this is not critical for processing

def is_sqs_event(event: Dict[str, Any]) -> bool:
    """Check if the event is from SQS."""
    return 'Records' in event and len(event['Records']) > 0 and event['Records'][0].get('eventSource') == 'aws:sqs'

def is_api_gateway_event(event: Dict[str, Any]) -> bool:
    """Check if the event is from API Gateway."""
    return 'body' in event and 'headers' in event

def parse_sqs_message(sqs_record: Dict[str, Any]) -> Dict[str, Any]:
    """Parse SQS message body and extract processing parameters."""
    try:
        # Parse the message body
        message_body = json.loads(sqs_record['body'])
        
        # Extract message attributes if present
        message_attributes = sqs_record.get('messageAttributes', {})
        
        # Build processing parameters
        params = {
            's3_path': message_body.get('s3_path'),
            'job_id': message_body.get('job_id'),
            'callback_url': message_body.get('callback_url'),
            'sqs_message_id': sqs_record['messageId'],
            'receipt_handle': sqs_record['receiptHandle']
        }
        
        # Add any additional attributes
        for attr_name, attr_data in message_attributes.items():
            if attr_data.get('dataType') == 'String':
                params[attr_name] = attr_data.get('stringValue')
        
        return params
        
    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Failed to parse SQS message: {str(e)}")
        raise ValueError(f"Invalid SQS message format: {str(e)}")

def process_single_document(params: Dict[str, Any]) -> Dict[str, Any]:
    """Process a single document with the given parameters."""
    s3_path = params.get('s3_path')
    job_id = params.get('job_id')
    callback_url = params.get('callback_url')
    
    if not s3_path:
        raise ValueError('Missing s3_path parameter')
    
    # Parse S3 path and process PDF
    bucket, key = parse_s3_path(s3_path)
    result = process_pdf_from_s3(bucket, key)
    
    # Handle async notification (webhook or EventBridge)
    if job_id:
        try:
            # Store results in S3
            results_s3_path = store_results_in_s3(bucket, job_id, result)
            
            # Prepare summary
            summary = {
                "page_count": result.get('document_info', {}).get('page_count'),
                "word_count": result.get('word_count'),
                "file_size": result.get('document_info', {}).get('file_size'),
                "success": result['success']
            }
            
            # Send webhook if callback_url provided
            if callback_url:
                callback_payload = {
                    "job_id": job_id,
                    "status": "completed" if result['success'] else "failed",
                    "results_s3_path": results_s3_path,
                    "summary": summary
                }
                
                if not result['success']:
                    callback_payload["error"] = result.get('error')
                    callback_payload["error_type"] = result.get('error_type')
                
                send_webhook_notification(callback_url, callback_payload)
            
            # Always send EventBridge event for SQS-based workflows
            send_completion_to_eventbridge(
                job_id, 
                "completed" if result['success'] else "failed", 
                summary
            )
            
        except Exception as e:
            logger.error(f"Error in async processing for job {job_id}: {str(e)}")
            # Send failure notification
            try:
                if callback_url:
                    failure_payload = {
                        "job_id": job_id,
                        "status": "failed",
                        "error": f"Async processing failed: {str(e)}"
                    }
                    send_webhook_notification(callback_url, failure_payload)
                
                send_completion_to_eventbridge(job_id, "failed", {"error": str(e)})
            except:
                logger.error("Failed to send failure notifications")
            
            # Re-raise for SQS error handling
            raise
    
    return result

def lambda_handler(event, context):
    """
    AWS Lambda handler for PDF processing.
    
    Supports multiple invocation modes:
    1. Direct invocation (backward compatibility)
    2. API Gateway
    3. SQS with DLQ support
    
    Event formats:
    
    Direct/API Gateway:
    {
        "s3_path": "s3://bucket-name/path/to/file.pdf",
        "job_id": "optional-job-id",
        "callback_url": "optional-callback-url"
    }
    
    SQS:
    {
        "Records": [
            {
                "body": "{\"s3_path\": \"s3://bucket/file.pdf\", \"job_id\": \"123\"}",
                "messageAttributes": {...}
            }
        ]
    }
    """
    try:
        # Determine event source and parse accordingly
        if is_sqs_event(event):
            # SQS batch processing
            return handle_sqs_batch(event, context)
        
        elif is_api_gateway_event(event):
            # API Gateway event
            body = json.loads(event['body'])
            result = process_single_document(body)
            
            status_code = 200 if result['success'] else 500
            return {
                'statusCode': status_code,
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                },
                'body': json.dumps(result)
            }
        
        else:
            # Direct invocation
            result = process_single_document(event)
            
            # For async mode, return minimal response
            if event.get('job_id'):
                return {
                    'statusCode': 200,
                    'body': json.dumps({
                        'success': True,
                        'message': 'Processing completed',
                        'job_id': event.get('job_id')
                    })
                }
            else:
                # Sync mode - return full results
                return {
                    'statusCode': 200 if result['success'] else 500,
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*'
                    },
                    'body': json.dumps(result)
                }
        
    except Exception as e:
        logger.error(f"Lambda handler error: {str(e)}")
        
        # Return appropriate error response based on invocation type
        if is_sqs_event(event):
            # For SQS, we need to raise the exception to trigger DLQ
            raise
        else:
            # For direct/API Gateway, return error response
            return {
                'statusCode': 500,
                'body': json.dumps({
                    'success': False,
                    'error': str(e),
                    'error_type': type(e).__name__
                })
            }

def handle_sqs_batch(event: Dict[str, Any], context) -> Dict[str, Any]:
    """
    Handle SQS batch processing with proper error handling for DLQ.
    
    Returns batch item failures for partial batch failure handling.
    """
    batch_item_failures = []
    
    for record in event['Records']:
        try:
            # Parse SQS message
            params = parse_sqs_message(record)
            job_id = params.get('job_id', 'unknown')
            
            logger.info(f"Processing SQS message for job {job_id}")
            
            # Process the document
            result = process_single_document(params)
            
            logger.info(f"Successfully processed SQS message for job {job_id}")
            
        except Exception as e:
            logger.error(f"Failed to process SQS message {record['messageId']}: {str(e)}")
            
            # Add to batch item failures - this will cause the message to be retried
            # After max retries, it will go to DLQ
            batch_item_failures.append({
                'itemIdentifier': record['messageId']
            })
    
    # Return batch results
    # If all messages processed successfully, batch_item_failures will be empty
    # If some failed, only the failed messages will be retried/sent to DLQ
    return {
        'batchItemFailures': batch_item_failures
    }