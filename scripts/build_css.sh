#!/usr/bin/env bash
# Build the Tailwind CSS bundle for local development.
#
# Usage:
#   ./scripts/build_css.sh           # one-shot build → static/tailwind.css
#   ./scripts/build_css.sh --watch   # watch mode, rebuilds on template changes
#
# The standalone Tailwind binary must be on your PATH as 'tailwindcss'.
# Install it (no Node required):
#   curl -fsSL https://github.com/tailwindlabs/tailwindcss/releases/download/v3.4.17/tailwindcss-linux-x64 \
#     -o ~/.local/bin/tailwindcss && chmod +x ~/.local/bin/tailwindcss
#
# Run this in a separate terminal alongside uvicorn during development.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p static

exec tailwindcss \
  -c tailwind.config.js \
  -i ./assets/tailwind.css \
  -o ./static/tailwind.css \
  "$@"
