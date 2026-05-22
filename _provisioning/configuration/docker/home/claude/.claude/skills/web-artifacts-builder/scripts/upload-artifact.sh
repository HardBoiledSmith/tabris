#!/bin/bash
# Upload bundle.html to S3 and print the public URL.
# Usage: bash scripts/upload-artifact.sh [bundle.html path]
set -e

BUCKET="hbsmith-tabris-artifacts"
BASE_URL="https://tabris-artifacts.hbsmith.io"
BUNDLE_FILE="${1:-bundle.html}"
RUN_ID="$(date +%s)"
USER_ID="${SLACK_USER_ID:-anonymous}"

if [ ! -f "$BUNDLE_FILE" ]; then
  echo "Error: $BUNDLE_FILE not found." >&2
  exit 1
fi

S3_KEY="${USER_ID}/${RUN_ID}/bundle.html"
aws s3 cp "$BUNDLE_FILE" "s3://${BUCKET}/${S3_KEY}" \
  --content-type "text/html" \
  --cache-control "no-cache"

PUBLIC_URL="${BASE_URL}/${S3_KEY}"
echo ""
echo "Artifact published: ${PUBLIC_URL}"
