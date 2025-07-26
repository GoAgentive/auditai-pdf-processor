#!/bin/bash

# Build script for PDF processor Lambda
set -e

echo "Building PDF processor Lambda..."

# Clean up any existing build artifacts
rm -rf build/
mkdir -p build/

# Create Lambda function package
echo "Creating Lambda function package..."
mkdir -p build/lambda/
cp index.py build/lambda/

# Create deployment package
cd build/lambda
zip -r ../../pdf-processor-lambda.zip .
cd ../..

echo "Lambda deployment package created: pdf-processor-lambda.zip"
echo "Size: $(ls -lh pdf-processor-lambda.zip | awk '{print $5}')"

# Create Lambda layer for dependencies
echo "Creating Lambda layer for dependencies..."
mkdir -p build/layer/python/lib/python3.11/site-packages/

# Install dependencies to the layer directory
pip install -r requirements.txt --target build/layer/python/lib/python3.11/site-packages/

# Create layer package
cd build/layer
zip -r ../../pdf-processor-layer.zip .
cd ../..

echo "Lambda layer package created: pdf-processor-layer.zip"
echo "Layer size: $(ls -lh pdf-processor-layer.zip | awk '{print $5}')"

# Clean up build directory
rm -rf build/

echo "Build complete! Package built for x86_64 architecture."