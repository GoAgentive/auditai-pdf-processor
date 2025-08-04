#!/bin/bash

# Main build script for Lambda layers architecture
# This script builds both the dependency layer and function code separately
set -e

echo "Building PDF processor Lambda with layers architecture..."

# Build the dependencies layer first
echo "Step 1: Building Lambda dependencies layer..."
if ! ./build-layer.sh; then
    echo "WARNING: Docker-based build failed, trying fallback method..."
    chmod +x build-layer-no-docker.sh
    ./build-layer-no-docker.sh
fi

# Build the function code package
echo "Step 2: Building Lambda function code..."
./build-function.sh

echo ""
echo "=========================================="
echo "Lambda packages built successfully!"
echo "=========================================="
echo "Dependencies layer: dependencies-layer.zip ($(ls -lh dependencies-layer.zip | awk '{print $5}'))"
echo "Function code: function-code.zip ($(ls -lh function-code.zip | awk '{print $5}'))"
echo ""