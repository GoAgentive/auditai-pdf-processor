#!/bin/bash

# Build script for PDF processor Lambda
set -e

echo "Building PDF processor Lambda..."

# Clean up any existing build artifacts
rm -rf build/
mkdir -p build/

# Create a temporary directory for pip install
mkdir -p build/python/lib/python3.11/site-packages/

# Install dependencies to the build directory
pip install -r requirements.txt --target build/python/lib/python3.11/site-packages/

# Copy the Lambda function code
cp index.py build/

# Create deployment package
cd build
zip -r ../pdf-processor-lambda.zip .
cd ..

echo "Lambda deployment package created: pdf-processor-lambda.zip"
echo "Size: $(ls -lh pdf-processor-lambda.zip | awk '{print $5}')"

# Clean up build directory
rm -rf build/

echo "Build complete!"