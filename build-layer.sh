#!/bin/bash

# Build script for Lambda Layer with heavy dependencies
# Creates a Lambda layer containing PyMuPDF and service-specific boto3 (S3 + Secrets Manager only)
# This significantly reduces package size compared to full boto3 installation
set -e

echo "Building Lambda Layer for PDF processor dependencies..."

# Clean up any existing build artifacts
rm -rf layer-build/
mkdir -p layer-build/python/

# Dependencies are hardcoded below to avoid Docker volume mounting issues

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
    echo '=== Installing hardcoded dependencies ==='
    echo 'Current working directory:' \$(pwd)
    echo 'Creating layer-build/python directory...'
    mkdir -p layer-build/python/
    
    pip install \
        PyMuPDF==1.24.14 \
        pymupdf4llm>=0.0.5 \
        boto3==1.34.0 \
        -t layer-build/python/ --no-cache-dir
    
    echo '=== Checking installation results ==='
    ls -la layer-build/
    ls -la layer-build/python/ | head -20
    echo 'Directory size:'
    du -sh layer-build/python/
    
    # Basic cleanup - skip aggressive optimizations that cause permission issues
    cd layer-build/python/
    
    # Only do essential cleanup that typically works
    find . -name '*.pyc' -delete 2>/dev/null || true
    find . -name '*.pyo' -delete 2>/dev/null || true
    
    # Skip other cleanup operations to avoid permission issues in CI
    echo 'Skipping detailed cleanup to avoid permission issues in Docker/CI environment'
    
    echo '=== Final directory check ==='
    cd /var/task
    ls -la layer-build/python/ | head -10
    echo 'Final directory size:'
    du -sh layer-build/python/
  " || { echo "ERROR: Docker build failed!"; exit 1; }

# Create layer deployment package
if [ ! -d "layer-build" ]; then
    echo "ERROR: layer-build directory not found! Docker build likely failed."
    exit 1
fi

echo "=== Pre-zip directory check ==="
echo "layer-build contents:"
ls -la layer-build/
echo "layer-build/python contents:"
ls -la layer-build/python/ | head -10
echo "layer-build/python size:"
du -sh layer-build/python/

# Note: Permission fix for Docker-created files handled by caller

cd layer-build
echo "=== Creating zip from directory: $(pwd) ==="
zip -r ../dependencies-layer.zip . || { echo "ERROR: Failed to create zip file!"; exit 1; }
cd ..

if [ ! -f "dependencies-layer.zip" ]; then
    echo "ERROR: dependencies-layer.zip was not created!"
    exit 1
fi

echo "Lambda layer package created: dependencies-layer.zip"
echo "Size: $(ls -lh dependencies-layer.zip | awk '{print $5}')"
echo "Size in bytes: $(stat -c%s dependencies-layer.zip 2>/dev/null || stat -f%z dependencies-layer.zip)"

echo "=== Verifying zip contents ==="
unzip -l dependencies-layer.zip | head -20

# Note: Build directory cleanup should be handled by caller to avoid permission issues
# when running in Docker environments (Docker creates files as root)

echo "Layer build complete!"
echo "Note: layer-build/ directory left for caller to clean up if needed"