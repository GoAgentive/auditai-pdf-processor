#!/bin/bash

# Fallback Lambda Layer build script (no Docker required)
# WARNING: This creates a layer that may not be fully compatible with Lambda runtime
# Use only when Docker is not available
set -e

echo "Building Lambda Layer WITHOUT Docker (fallback method)..."
echo "WARNING: This may not be fully compatible with Lambda's Python 3.11 runtime"

# Clean up any existing build artifacts
rm -rf layer-build/
mkdir -p layer-build/python/

echo "Installing dependencies locally..."
pip3 install \
    PyMuPDF==1.24.14 \
    pymupdf4llm>=0.0.5 \
    boto3==1.34.0 \
    -t layer-build/python/ --no-cache-dir || { 
        echo "ERROR: pip install failed!"
        echo "Trying with --user and manual copy..."
        pip3 install --user \
            PyMuPDF==1.24.14 \
            pymupdf4llm>=0.0.5 \
            boto3==1.34.0 \
            --no-cache-dir
        
        # Find user site-packages and copy
        USER_SITE=$(python3 -c "import site; print(site.USER_SITE)")
        if [ -d "$USER_SITE" ]; then
            echo "Copying from user site-packages: $USER_SITE"
            cp -r "$USER_SITE"/* layer-build/python/ 2>/dev/null || true
        fi
    }

# Basic cleanup
cd layer-build/python/
find . -name '*.pyc' -delete 2>/dev/null || true
find . -name '*.pyo' -delete 2>/dev/null || true
find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

cd ../..

# Create layer deployment package
if [ ! -d "layer-build" ]; then
    echo "ERROR: layer-build directory not found!"
    exit 1
fi

cd layer-build
zip -r ../dependencies-layer.zip . || { echo "ERROR: Failed to create zip file!"; exit 1; }
cd ..

if [ ! -f "dependencies-layer.zip" ]; then
    echo "ERROR: dependencies-layer.zip was not created!"
    exit 1
fi

echo "Fallback Lambda layer package created: dependencies-layer.zip"
echo "Size: $(ls -lh dependencies-layer.zip | awk '{print $5}')"
echo "WARNING: This layer was built without Docker and may not be fully compatible with Lambda runtime"

# Cleanup
rm -rf layer-build/

echo "Fallback layer build complete!"