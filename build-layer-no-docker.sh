#!/bin/bash

# Fallback Lambda Layer build script (no Docker required)
# WARNING: This creates a layer that may not be fully compatible with Lambda runtime
# Use only when Docker is not available
set -e

echo "Building Lambda Layer WITHOUT Docker (cross-targeted to the Lambda runtime ABI)..."

# Clean up any existing build artifacts
rm -rf layer-build/
mkdir -p layer-build/python/lib/python3.12/site-packages/

echo "Installing dependencies as Lambda-runtime (python3.12 / manylinux x86_64) wheels..."
# Cross-target the Lambda runtime ABI regardless of the build host's Python. The host
# running this no-Docker path (notably the Pulumi deploy runner) is frequently NOT
# python3.12 — it has shipped python3.13 — and a plain `pip install` there fetches
# cp313 wheels for numpy/onnxruntime. Those fail to import under the python3.12 Lambda
# runtime, so `import pymupdf.layout` is caught and pymupdf4llm silently drops to its
# non-layout path → truncated OCR on Skia / vector-graphics PDFs.
#
# --only-binary=:all: forbids any host-Python source build; --abi/--implementation/
# --python-version/--platform pin every wheel to the runtime ABI. There is intentionally
# NO host-Python fallback: fail the build rather than ship an ABI-mismatched layer.
pip3 install \
    --python-version 3.12 --implementation cp --abi cp312 \
    --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 \
    --only-binary=:all: --no-cache-dir \
    PyMuPDF==1.27.2.3 \
    pymupdf4llm==1.27.2.3 \
    -t layer-build/python/lib/python3.12/site-packages/

# Basic cleanup
cd layer-build/python/lib/python3.12/site-packages/
find . -name '*.pyc' -delete 2>/dev/null || true
find . -name '*.pyo' -delete 2>/dev/null || true
find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

cd ../../../../..

# Create layer deployment package
if [ ! -d "layer-build" ]; then
    echo "ERROR: layer-build directory not found!"
    exit 1
fi

cd layer-build
zip -r ../dependencies-layer.zip python/ || { echo "ERROR: Failed to create zip file!"; exit 1; }
cd ..

if [ ! -f "dependencies-layer.zip" ]; then
    echo "ERROR: dependencies-layer.zip was not created!"
    exit 1
fi

echo "Fallback Lambda layer package created: dependencies-layer.zip"
echo "Size: $(ls -lh dependencies-layer.zip | awk '{print $5}')"
echo "Layer built without Docker, cross-targeted to python3.12 / manylinux x86_64."

# Copy to Pulumi build directory (following Pulumi best practices)
echo "=== Copying to Pulumi build directory ==="
PULUMI_BUILD_DIR="../../.pulumi-config/build"
mkdir -p "$PULUMI_BUILD_DIR"
cp dependencies-layer.zip "$PULUMI_BUILD_DIR/"

echo "Files copied to Pulumi build directory:"
ls -la "$PULUMI_BUILD_DIR/"

# Cleanup
rm -rf layer-build/

echo "Fallback layer build complete!"