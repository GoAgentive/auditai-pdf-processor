#!/bin/bash

# Deploy script for PDF processor Lambda
set -e

# Default to dev environment
ENVIRONMENT=${1:-dev}

# Validate environment
case $ENVIRONMENT in
  dev|staging|prod)
    echo "Deploying to $ENVIRONMENT environment..."
    ;;
  *)
    echo "Error: Invalid environment '$ENVIRONMENT'. Use: dev, staging, or prod"
    exit 1
    ;;
esac

FUNCTION_NAME="pdf-processor-lambda-$ENVIRONMENT"

# Build the Lambda package
echo "Building Lambda package..."
./build.sh

# Check if Lambda function exists
if aws lambda get-function --function-name "$FUNCTION_NAME" >/dev/null 2>&1; then
    echo "Updating existing Lambda function: $FUNCTION_NAME"
    aws lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file fileb://pdf-processor-lambda.zip
else
    echo "Creating new Lambda function: $FUNCTION_NAME"
    aws lambda create-function \
        --function-name "$FUNCTION_NAME" \
        --runtime python3.11 \
        --role "arn:aws:iam::540215402531:role/lambda-execution-role" \
        --handler index.lambda_handler \
        --zip-file fileb://pdf-processor-lambda.zip \
        --timeout 300 \
        --memory-size 1024 \
        --description "PDF processor Lambda for $ENVIRONMENT environment"
fi

# Clean up
rm pdf-processor-lambda.zip

echo "Deployment to $ENVIRONMENT complete!"
echo "Function name: $FUNCTION_NAME"