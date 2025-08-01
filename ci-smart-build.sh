#!/bin/bash

# CI-optimized smart build script that uses git to detect changes
# This version is more efficient for CI environments where file artifacts don't persist
set -e

LAMBDA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HASH_FILE="$LAMBDA_DIR/.build-hash"

# Calculate hash of all source files (same as smart-build.sh)
calculate_source_hash() {
    find "$LAMBDA_DIR" -name "*.py" -o -name "requirements.txt" -o -name "Dockerfile" | \
    sort | \
    xargs cat | \
    shasum -a 256 | \
    cut -d' ' -f1
}

# Check if Lambda files changed in recent commits (CI-specific optimization)
check_git_changes() {
    # Check if any Lambda files changed in the last 2 commits
    # This catches both the current commit and potential merge commits
    if git diff --name-only HEAD~2..HEAD | grep -q "^lambda/pdf-processor/"; then
        echo "Lambda files changed in recent commits. Building..."
        return 0  # Need to build
    else
        echo "No Lambda file changes detected in recent commits."
        return 1  # Skip build
    fi
}

# Get current source hash
CURRENT_HASH=$(calculate_source_hash)

# Check if we need to rebuild
NEEDS_BUILD=false

# First check: Do we have cached artifacts and they match current hash?
if [ -f "$HASH_FILE" ] && [ -f "dependencies-layer.zip" ] && [ -f "function-code.zip" ]; then
    PREVIOUS_HASH=$(cat "$HASH_FILE" 2>/dev/null || echo "")
    if [ "$CURRENT_HASH" = "$PREVIOUS_HASH" ]; then
        echo "Cached build artifacts found and hash matches. Skipping build."
        echo "Current hash: $CURRENT_HASH"
        exit 0
    else
        echo "Hash changed. Building..."
        echo "Previous hash: $PREVIOUS_HASH"
        echo "Current hash:  $CURRENT_HASH"
        NEEDS_BUILD=true
    fi
else
    # Second check: Are we in CI and no Lambda changes detected?
    if [ "$CI" = "true" ] && [ "$GITHUB_ACTIONS" = "true" ]; then
        if check_git_changes; then
            NEEDS_BUILD=true
        else
            echo "CI environment: No Lambda changes detected, but no cached artifacts available."
            echo "This might be the first build or cache miss. Building to be safe..."
            NEEDS_BUILD=true
        fi
    else
        echo "No cached artifacts found. Building..."
        NEEDS_BUILD=true
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