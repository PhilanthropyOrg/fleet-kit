#!/usr/bin/env bash
# test_verified_test_greenfield.sh — verify that verified_test.sh handles greenfield repos
set -euo pipefail

echo "Testing verified_test.sh greenfield detection..."

SCRIPTS_DIR="$(dirname "$(readlink -f "$0")")"

# Create a temporary greenfield repo
TEMP_REPO=$(mktemp -d)
trap "rm -rf '$TEMP_REPO'" EXIT

echo "Setting up test greenfield repo at $TEMP_REPO"
cd "$TEMP_REPO"

# Initialize git repo with minimal content
git init
git config user.email "test@example.com"
git config user.name "Test User"
touch README.md
git add README.md
git commit -m "initial commit"

# Run verified_test.sh from the greenfield repo
echo ""
echo "Running verified_test.sh in greenfield repo..."
output=$("$SCRIPTS_DIR/verified_test.sh" 2>&1 || true)

echo "$output"

# Check that it detects greenfield
if echo "$output" | grep -q "greenfield"; then
    echo ""
    echo "✓ PASS: verified_test.sh correctly detected greenfield repo"
    exit 0
else
    echo ""
    echo "✗ FAIL: verified_test.sh did not detect greenfield repo"
    echo "Output was: $output"
    exit 1
fi
