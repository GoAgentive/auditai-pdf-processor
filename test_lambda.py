#!/usr/bin/env python3
"""
Test script for PDF processor Lambda function with authentication
"""

import json
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from index import lambda_handler, verify_auth_token

def test_auth_verification():
    """Test the authentication verification"""
    print("Testing authentication verification...")
    
    # Test with valid auth (would need actual secret in AWS for real test)
    test_event_valid = {
        "s3_path": "s3://test-bucket/sample.pdf",
        "authorization": "Bearer test-token-12345"
    }
    
    # Test with invalid auth
    test_event_invalid = {
        "s3_path": "s3://test-bucket/sample.pdf",
        "authorization": "Bearer wrong-token"
    }
    
    # Test with missing auth
    test_event_no_auth = {
        "s3_path": "s3://test-bucket/sample.pdf"
    }
    
    print("Note: These tests require AWS credentials and actual secrets to work")
    print("Events prepared for auth testing - would need real AWS environment to execute")

def test_lambda_with_sample_event():
    """Test the Lambda function with a sample event"""
    
    # Sample event with authentication - replace with actual S3 path and valid token for testing
    test_event = {
        "s3_path": "s3://your-test-bucket/sample-document.pdf",
        "authorization": "Bearer your-test-token-here"
    }
    
    # Mock context object (not used in our function)
    class MockContext:
        def __init__(self):
            self.function_name = "test-pdf-processor"
            self.memory_limit_in_mb = 1024
            self.invoked_function_arn = "arn:aws:lambda:us-west-2:123456789012:function:test-pdf-processor"
            self.aws_request_id = "test-request-id"
    
    context = MockContext()
    
    try:
        # Invoke the Lambda handler
        result = lambda_handler(test_event, context)
        
        print("Lambda execution successful!")
        print(f"Status Code: {result['statusCode']}")
        print(f"Headers: {result['headers']}")
        
        # Parse the response body
        body = json.loads(result['body'])
        
        if body['success']:
            print(f"Document processed successfully!")
            print(f"Pages: {body['document_info']['page_count']}")
            print(f"Words: {body['word_count']}")
            print(f"File size: {body['document_info']['file_size']} bytes")
            
            # Show first few words as sample
            if body['word_bounding_boxes']:
                print("\nFirst 5 words:")
                for i, word in enumerate(body['word_bounding_boxes'][:5]):
                    print(f"  {i+1}. '{word['text']}' on page {word['page']}")
            
            # Show markdown preview
            if body['markdown_text']:
                print("\nMarkdown preview (first 200 chars):")
                print(body['markdown_text'][:200] + "...")
        else:
            print(f"Processing failed: {body['error']}")
            
    except Exception as e:
        print(f"Test failed: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_auth_verification()
    print("\n" + "="*50 + "\n")
    test_lambda_with_sample_event()