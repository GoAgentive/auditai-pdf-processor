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

# Try Docker build first, but with better volume handling
docker run --rm \
  --platform linux/amd64 \
  -v "$SCRIPT_DIR":/host \
  -w /tmp/build \
  --entrypoint /bin/bash \
  public.ecr.aws/lambda/python:3.11 \
  -c "
    yum install -y gcc gcc-c++ make zip
    echo '=== Installing hardcoded dependencies ==='
    echo 'Current working directory:' \$(pwd)
    echo 'Creating build directory...'
    mkdir -p /tmp/build/python/
    
    pip install \
        PyMuPDF==1.24.14 \
        pymupdf4llm>=0.0.5 \
        boto3==1.34.0 \
        -t /tmp/build/python/ --no-cache-dir
    
    echo '=== Checking installation results ==='
    ls -la /tmp/build/
    ls -la /tmp/build/python/ | head -20
    echo 'Directory size:'
    du -sh /tmp/build/python/
    
    # Basic cleanup
    cd /tmp/build/python/
    find . -name '*.pyc' -delete 2>/dev/null || true
    find . -name '*.pyo' -delete 2>/dev/null || true
    
    echo '=== Copying to host mount ==='
    cd /tmp/build
    # Create the host directory
    mkdir -p /host/layer-build/
    # Copy with proper permissions
    cp -r python/ /host/layer-build/
    # Fix permissions for the copied files
    chmod -R 755 /host/layer-build/python/ 2>/dev/null || true
    
    echo '=== Verifying host copy ==='
    ls -la /host/layer-build/
    ls -la /host/layer-build/python/ | head -10
    du -sh /host/layer-build/python/
  " || { 
    echo "ERROR: Docker build failed! Trying fallback method..."
    chmod +x build-layer-no-docker.sh
    ./build-layer-no-docker.sh
    exit $?
  }

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