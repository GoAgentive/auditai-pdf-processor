#!/bin/bash

# Build script for Lambda Layer with heavy dependencies
# Creates a Lambda layer containing PyMuPDF and service-specific boto3 (S3 + Secrets Manager only)
# This significantly reduces package size compared to full boto3 installation
set -e

echo "Building Lambda Layer for PDF processor dependencies..."

# Clean up any existing build artifacts
rm -rf layer-build/

# Dependencies are hardcoded below to avoid Docker volume mounting issues

# Use Docker to build in Lambda-compatible environment (x86_64)
echo "Building layer dependencies using Docker (Lambda Python 3.12 x86_64 environment)..."
echo "Checking Docker availability..."
docker --version || { echo "ERROR: Docker not available!"; exit 1; }

echo "Starting Docker build..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Try Docker build first, with correct Lambda layer structure
# Using Python 3.12 (Amazon Linux 2023) which has glibc 2.34, compatible with PyMuPDF 1.26.x wheels
docker run --rm \
  --platform linux/amd64 \
  -v "$SCRIPT_DIR":/host \
  -w /tmp/build \
  --entrypoint /bin/bash \
  public.ecr.aws/lambda/python:3.12 \
  -c "
    dnf install -y gcc gcc-c++ make zip
    echo '=== Installing dependencies ==='
    echo 'Current working directory:' \$(pwd)
    echo 'Creating Lambda layer directory structure...'
    mkdir -p /tmp/build/python/lib/python3.12/site-packages/

    # Python 3.12 on Amazon Linux 2023 has glibc 2.34
    # PyMuPDF 1.26.x wheels require glibc 2.28+ (manylinux_2_28)
    # This combination allows pre-built wheels to work
    pip install \
        PyMuPDF==1.26.6 \
        pymupdf4llm==0.2.9 \
        boto3==1.34.0 \
        -t /tmp/build/python/lib/python3.12/site-packages/ --no-cache-dir
    
    echo '=== Checking installation results ==='
    ls -la /tmp/build/
    ls -la /tmp/build/python/lib/python3.12/site-packages/ | head -20
    echo 'Directory size:'
    du -sh /tmp/build/python/
    
    # Basic cleanup
    cd /tmp/build/python/lib/python3.12/site-packages/
    find . -name '*.pyc' -delete 2>/dev/null || true
    find . -name '*.pyo' -delete 2>/dev/null || true
    
    echo '=== Creating host layer-build directory ==='
    cd /tmp/build
    # Create the zip directly and copy to host
    zip -r dependencies-layer.zip python/
    
    echo '=== Copying zip to host ==='
    mkdir -p /host/layer-build/
    cp dependencies-layer.zip /host/layer-build/
    # Also copy the python directory for inspection
    cp -r python/ /host/layer-build/
    chmod -R 755 /host/layer-build/ 2>/dev/null || true
    
    echo '=== Verifying host copy ==='
    ls -la /host/layer-build/
    ls -la /host/layer-build/dependencies-layer.zip
    echo 'Zip file size:' \$(stat -c%s /host/layer-build/dependencies-layer.zip 2>/dev/null || stat -f%z /host/layer-build/dependencies-layer.zip)
    du -sh /host/layer-build/python/
  " || { 
    echo "ERROR: Docker build failed! Trying fallback method..."
    chmod +x build-layer-no-docker.sh
    ./build-layer-no-docker.sh
    exit $?
  }

# Verify Docker created the zip file correctly
if [ ! -f "layer-build/dependencies-layer.zip" ]; then
    echo "ERROR: Docker did not create dependencies-layer.zip! Build likely failed."
    exit 1
fi

echo "=== Verifying Docker-created zip ==="
echo "layer-build contents:"
ls -la layer-build/
echo "dependencies-layer.zip size:"
ls -lh layer-build/dependencies-layer.zip
echo "Size in bytes: $(stat -c%s layer-build/dependencies-layer.zip 2>/dev/null || stat -f%z layer-build/dependencies-layer.zip)"

echo "=== Verifying zip contents ==="
unzip -l layer-build/dependencies-layer.zip | head -20

# Copy the zip to the expected location for backward compatibility
cp layer-build/dependencies-layer.zip ./dependencies-layer.zip

echo "Lambda layer package ready: dependencies-layer.zip"
echo "Size: $(ls -lh dependencies-layer.zip | awk '{print $5}')"

# Copy to Pulumi build directory (following Pulumi best practices)
echo "=== Copying to Pulumi build directory ==="
PULUMI_BUILD_DIR="../../.pulumi-config/build"
mkdir -p "$PULUMI_BUILD_DIR"
cp dependencies-layer.zip "$PULUMI_BUILD_DIR/"
cp function-code.zip "$PULUMI_BUILD_DIR/" 2>/dev/null || echo "function-code.zip not found yet"

echo "Files copied to Pulumi build directory:"
ls -la "$PULUMI_BUILD_DIR/"

# Note: Build directory cleanup should be handled by caller to avoid permission issues
# when running in Docker environments (Docker creates files as root)

echo "Layer build complete!"
echo "Note: layer-build/ directory left for caller to clean up if needed"