#!/usr/bin/env bash
# bump-version.sh — set the saebooks-web version, keeping the two literals that
# must agree in lockstep so the version can never silently go stale or drift
# between surfaces.
#
# Adapted from the engine's scripts/bump-version.sh (same interface on purpose,
# so muscle memory carries between the two repos).
#
# The canonical version is  saebooks_web/__init__.py  __version__ . It feeds:
#   * OpenAPI  /api/docs  info.version        (main.py: version=__version__)
#   * the sidebar version badge               (base.html: {{ app_version() }})
# pyproject.toml [project] version must equal it, because that is what gets
# baked into the installed package metadata.
# tests/test_version_display.py asserts the runtime surfaces agree; this script
# + `--check` keep the two source literals from diverging in the first place.
#
# Usage:
#   scripts/bump-version.sh 0.6.2         set version in __init__ + pyproject
#   scripts/bump-version.sh 0.6.2 --tag   ...and create annotated git tag v0.6.2
#   scripts/bump-version.sh --check       assert the two match (drift guard); exit 1 on mismatch
#   scripts/bump-version.sh --show        print the current version
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INIT="$REPO_ROOT/saebooks_web/__init__.py"
PYPROJECT="$REPO_ROOT/pyproject.toml"

init_version()      { sed -nE 's/^__version__[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$INIT" | head -1; }
pyproject_version() { sed -nE 's/^version[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' "$PYPROJECT" | head -1; }

check_sync() {
  local a b; a="$(init_version)"; b="$(pyproject_version)"
  if [[ "$a" != "$b" ]]; then
    echo "VERSION DRIFT: saebooks_web/__init__.py=${a:-<none>}  pyproject.toml=${b:-<none>}" >&2
    return 1
  fi
  echo "version in sync: $a"
}

case "${1:-}" in
  --check)        check_sync; exit $? ;;
  --show)         init_version; exit 0 ;;
  ""|-h|--help)   sed -nE 's/^# ?//p' "${BASH_SOURCE[0]}" | sed '/^!/d'; exit 0 ;;
esac

NEW="$1"
# Matches the existing scheme: 0.1, 0.1.2, 0.3 (1-3 dotted numeric segments).
if ! [[ "$NEW" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]]; then
  echo "error: version must look like 0.3 or 0.3.1 (got: '$NEW')" >&2; exit 2
fi

OLD="$(init_version)"
sed -i -E "s/^(__version__[[:space:]]*=[[:space:]]*)\"[^\"]+\"/\1\"$NEW\"/" "$INIT"
# Only the FIRST top-level `version =` (the [project] version), never any other.
sed -i -E "0,/^version[[:space:]]*=/ s/^(version[[:space:]]*=[[:space:]]*)\"[^\"]+\"/\1\"$NEW\"/" "$PYPROJECT"

check_sync >/dev/null
echo "bumped ${OLD:-<none>} -> $NEW  (saebooks_web/__init__.py + pyproject.toml)"

if [[ "${2:-}" == "--tag" ]]; then
  git -C "$REPO_ROOT" tag -a "v$NEW" -m "saebooks-web v$NEW"
  echo "created annotated tag v$NEW (push with: git push origin v$NEW)"
fi
