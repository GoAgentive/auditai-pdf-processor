#!/bin/bash

# Build script for Lambda Function code (without heavy dependencies)
# Creates a lightweight package containing only the application code
set -e

echo "Building Lambda Function code package..."

# Clean up any existing build artifacts
rm -rf function-build/
mkdir -p function-build/

# Copy only the function code (no dependencies - they're in the layer)
cp index.py function-build/

# Create function deployment package
cd function-build
zip -r ../function-code.zip .
cd ..

echo "Lambda function package created: function-code.zip"
echo "Size: $(ls -lh function-code.zip | awk '{print $5}')"

# Clean up build directory
rm -rf function-build/

echo "Function build complete!"