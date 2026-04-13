#!/bin/bash
# Block commits that include sensitive files
if git diff --cached --name-only | grep -qE '\.(env|key|pem)$|creds\.md|secrets\.'; then
  echo "BLOCKED: Attempting to commit sensitive files"
  exit 1
fi

# Block commits directly to main
BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" = "main" ]; then
  echo "BLOCKED: Direct commits to main are not allowed. Use a feature branch."
  exit 1
fi

echo "Pre-commit checks passed"
