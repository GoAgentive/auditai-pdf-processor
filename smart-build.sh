#!/bin/bash

# Smart build script that only rebuilds when Lambda source changes
# 
# Usage:
#   ./smart-build.sh  - Only builds if source files changed (recommended for CI/CD)
#   ./build.sh        - Always builds (useful for force rebuild or debugging)
#
set -e

LAMBDA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HASH_FILE="$LAMBDA_DIR/.build-hash"
ZIP_FILE="$LAMBDA_DIR/pdf-processor-lambda.zip"

# Calculate hash of all source files (including build scripts to catch runtime version changes)
calculate_source_hash() {
    find "$LAMBDA_DIR" -maxdepth 1 \( -name "*.py" -o -name "requirements.txt" -o -name "Dockerfile" -o -name "build*.sh" \) | \
    sort | \
    xargs cat | \
    shasum -a 256 | \
    cut -d' ' -f1
}

# Get current source hash
CURRENT_HASH=$(calculate_source_hash)

# Check if we need to rebuild
NEEDS_BUILD=false

if [ ! -f "$HASH_FILE" ]; then
    echo "No previous build hash found. Building..."
    NEEDS_BUILD=true
elif [ ! -f "$ZIP_FILE" ]; then
    echo "Lambda zip file missing. Building..."
    NEEDS_BUILD=true
else
    PREVIOUS_HASH=$(cat "$HASH_FILE" 2>/dev/null || echo "")
    if [ "$CURRENT_HASH" != "$PREVIOUS_HASH" ]; then
        echo "Source files changed. Building..."
        echo "Previous hash: $PREVIOUS_HASH"
        echo "Current hash:  $CURRENT_HASH"
        NEEDS_BUILD=true
    else
        echo "No changes detected. Skipping build."
        echo "Current hash: $CURRENT_HASH"
    fi
fi

if [ "$NEEDS_BUILD" = true ]; then
    echo "Building Lambda package..."
    ./build.sh
    
    # Save the current hash
    echo "$CURRENT_HASH" > "$HASH_FILE"
    echo "Build complete. Hash saved: $CURRENT_HASH"
else
    echo "Lambda package is up to date."
fi