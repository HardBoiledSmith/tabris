#!/bin/bash
# Upload bundle.html to S3 and print the public URL.
# Usage: bash scripts/upload-artifact.sh [bundle.html path]
set -e

BUCKET="$ARTIFACTS_S3_BUCKET"
BASE_URL="$ARTIFACTS_BASE_URL"
BUNDLE_FILE="${1:-bundle.html}"
RUN_ID="$(date +%s)"
USER_ID="${SLACK_USER_ID:-anonymous}"
# 128-bit random token makes the URL unguessable. Bucket grants ListBucket only
# to the EC2 role (not CloudFront/anonymous), so artifacts cannot be enumerated —
# the full key, including this token, is required to read the object.
TOKEN="$(openssl rand -hex 16)"

if [ ! -f "$BUNDLE_FILE" ]; then
  echo "Error: $BUNDLE_FILE not found." >&2
  exit 1
fi

S3_KEY="${USER_ID}/${RUN_ID}-${TOKEN}/bundle.html"
aws s3 cp "$BUNDLE_FILE" "s3://${BUCKET}/${S3_KEY}" \
  --content-type "text/html" \
  --cache-control "no-cache"

PUBLIC_URL="${BASE_URL}/${S3_KEY}"
echo ""
echo "Artifact published: ${PUBLIC_URL}"
