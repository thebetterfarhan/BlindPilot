#!/bin/sh
# PostToolUse (Edit|Write): format and lint the one Python file just changed.
# Fixable issues are fixed in place. Anything left is sent back to Claude
# (exit 2) so it is fixed now, not at commit time. Silent if ruff is missing.
PY=python
command -v "$PY" >/dev/null 2>&1 || PY=python3
"$PY" -m ruff --version >/dev/null 2>&1 || exit 0

FILE=$("$PY" -c 'import sys, json; print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))' 2>/dev/null)
case "$FILE" in
  *.py) [ -f "$FILE" ] || exit 0 ;;
  *) exit 0 ;;
esac

"$PY" -m ruff format --quiet "$FILE" >/dev/null 2>&1
OUT=$("$PY" -m ruff check --fix --quiet "$FILE" 2>&1) || {
  echo "ruff check failed on $FILE:" >&2
  echo "$OUT" | tail -n 40 >&2
  exit 2
}
exit 0
