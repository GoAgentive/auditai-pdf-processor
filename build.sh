#!/bin/bash

# Build script for PDF processor Lambda
# This is the core build logic - called by smart-build.sh for automated deployments
# or can be run directly for force rebuilds
set -e

echo "Building PDF processor Lambda..."

# Clean up any existing build artifacts
rm -rf build/
mkdir -p build/

# Use Docker to build in Lambda-compatible environment (x86_64)
echo "Building dependencies using Docker (Lambda Python 3.11 x86_64 environment)..."
docker run --rm \
  --platform linux/amd64 \
  -v "$PWD":/var/task \
  -w /var/task \
  --entrypoint /bin/bash \
  public.ecr.aws/lambda/python:3.11 \
  -c "
    pip install -r requirements.txt -t build/
    cp index.py build/
  "

# Create deployment package
cd build
zip -r ../pdf-processor-lambda.zip .
cd ..

echo "Lambda deployment package created: pdf-processor-lambda.zip"
echo "Size: $(ls -lh pdf-processor-lambda.zip | awk '{print $5}')"

# Clean up build directory
rm -rf build/

echo "Build complete!"