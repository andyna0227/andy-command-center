#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd -- "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

python3 refresh-data.py

git add data.json
if ! git diff --cached --quiet; then
  git commit -m "chore: refresh dashboard data"
  git push
else
  echo "No changes to commit"
fi
