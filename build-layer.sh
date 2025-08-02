#!/bin/bash

# Build script for Lambda Layer with heavy dependencies
# Creates a Lambda layer containing PyMuPDF and service-specific boto3 (S3 + Secrets Manager only)
# This significantly reduces package size compared to full boto3 installation
set -e

echo "Building Lambda Layer for PDF processor dependencies..."

# Clean up any existing build artifacts
rm -rf layer-build/
mkdir -p layer-build/python/

# Use existing requirements.txt file for layer dependencies

# Use Docker to build in Lambda-compatible environment (x86_64)
echo "Building layer dependencies using Docker (Lambda Python 3.11 x86_64 environment)..."
echo "Checking Docker availability..."
docker --version || { echo "ERROR: Docker not available!"; exit 1; }

echo "Starting Docker build..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker run --rm \
  --platform linux/amd64 \
  -v "$SCRIPT_DIR":/var/task \
  -w /var/task \
  --entrypoint /bin/bash \
  public.ecr.aws/lambda/python:3.11 \
  -c "
    yum install -y gcc gcc-c++ make zip
    echo '=== Docker Debug Info ==='
    pwd
    ls -la
    echo '=== Checking for requirements.txt ==='
    ls -la requirements.txt || echo 'requirements.txt not found!'
    echo '=== Installing dependencies ==='
    pip install -r requirements.txt -t layer-build/python/ --no-cache-dir
    
    # Remove unnecessary files to reduce layer size
    cd layer-build/python/
    
    # Remove test files and documentation
    find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name 'tests' -exec rm -rf {} + 2>/dev/null || true
    find . -type d -name 'test' -exec rm -rf {} + 2>/dev/null || true
    find . -name '*.pyc' -delete 2>/dev/null || true
    find . -name '*.pyo' -delete 2>/dev/null || true
    find . -name '*.dist-info' -type d -exec rm -rf {} + 2>/dev/null || true
    
    # Remove specific large unnecessary files from PyMuPDF
    find . -name '*.md' -delete 2>/dev/null || true
    find . -name '*.txt' -delete 2>/dev/null || true
    find . -name 'COPYING' -delete 2>/dev/null || true
    find . -name 'README*' -delete 2>/dev/null || true
    
    # Remove development headers and documentation from pymupdf
    rm -rf pymupdf/mupdf-devel/include/ 2>/dev/null || true
    rm -rf pymupdf/docs/ 2>/dev/null || true
    
    # Remove duplicate/old PyMuPDF installations
    rm -rf fitz_old/ 2>/dev/null || true
    rm -rf PyMuPDF-1.24.14.dist-info/ 2>/dev/null || true
    rm -rf PyMuPDFb-1.24.14.dist-info/ 2>/dev/null || true
    
    # More aggressive cleanup for botocore (remove unused service data)
    # Keep only essential services: s3, secretsmanager, lambda
    if [ -d botocore/data/ ]; then
      cd botocore/data/
      for service in */; do
        if [[ ! \$service =~ ^(s3|secretsmanager|lambda|sts)/\$ ]]; then
          rm -rf \"\$service\" 2>/dev/null || true
        fi
      done
      cd ../../
      
      # Remove examples and documentation from remaining services
      find botocore/data/ -name 'examples-*.json' -delete 2>/dev/null || true
      find botocore/data/ -name 'waiters-*.json' -delete 2>/dev/null || true
    fi
  " || { echo "ERROR: Docker build failed!"; exit 1; }

# Create layer deployment package
if [ ! -d "layer-build" ]; then
    echo "ERROR: layer-build directory not found! Docker build likely failed."
    exit 1
fi

cd layer-build
zip -r ../dependencies-layer.zip . || { echo "ERROR: Failed to create zip file!"; exit 1; }
cd ..

if [ ! -f "dependencies-layer.zip" ]; then
    echo "ERROR: dependencies-layer.zip was not created!"
    exit 1
fi

echo "Lambda layer package created: dependencies-layer.zip"
echo "Size: $(ls -lh dependencies-layer.zip | awk '{print $5}')"

# Clean up build directory
rm -rf layer-build/

echo "Layer build complete!"