#!/bin/bash
# SessionStart: make a cloud session match CI (.github/workflows/ci.yml) so the
# gate can run there: wxPython from Ubuntu, a .venv with requirements-build.txt,
# the pre-commit and pre-push hooks, and rtk. Cloud only; on your own computer
# this does nothing. Safe to run again: finished steps are skipped.
[ "$CLAUDE_CODE_REMOTE" = "true" ] || exit 0

ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$ROOT" || exit 0
LOG="$HOME/.cache/blindpilot-cloud-setup.log"
mkdir -p "$(dirname "$LOG")"
say() { echo "cloud-setup: $*" >&2; }

# Python deps change with requirements files, so key the stamp on them.
STAMP=".venv/.setup-$(cat requirements.txt requirements-build.txt | sha256sum | cut -c1-12)"

if [ ! -f "$STAMP" ]; then
  {
    SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
    # Same packages as CI: GTK runtime for the wx wheel, SDL2, a virtual display.
    if ! dpkg -s python3-wxgtk4.0 xvfb libsdl2-2.0-0 >/dev/null 2>&1; then
      $SUDO apt-get update -qq &&
        DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq --no-install-recommends \
          python3-wxgtk4.0 xvfb libsdl2-2.0-0
    fi
    # Python 3.12 is what the pinned wx wheel and CI use.
    [ -x .venv/bin/python ] || python3.12 -m venv --system-site-packages .venv
    .venv/bin/python -m pip install -q --upgrade pip &&
      .venv/bin/python -m pip install -q --only-binary=wxPython -r requirements-build.txt &&
      .venv/bin/python -c "import wx" &&
      touch "$STAMP"
  } >>"$LOG" 2>&1 || say "Python setup failed; see $LOG. The gate may not run here."
fi

# Commit and push hooks, same as a fresh clone on your computer.
if [ -x .venv/bin/pre-commit ] && [ ! -f .git/hooks/pre-push.pre-commit ]; then
  .venv/bin/pre-commit install -t pre-commit -t pre-push >>"$LOG" 2>&1 &&
    mv .git/hooks/pre-push .git/hooks/pre-push.pre-commit &&
    # The push hook runs the whole suite, and wx tests crash their worker
    # without a display. Give the hook a virtual one, as CI's xvfb-run does.
    # stdin (the refs being pushed) passes straight through.
    cat >.git/hooks/pre-push <<'HOOK' && chmod +x .git/hooks/pre-push
#!/bin/sh
HOOK_DIR=$(dirname "$0")
if [ -z "$DISPLAY" ] && command -v Xvfb >/dev/null 2>&1; then
  FD_FILE=$(mktemp)
  Xvfb -displayfd 3 -screen 0 1280x1024x24 3>"$FD_FILE" >/dev/null 2>&1 &
  XPID=$!
  for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s "$FD_FILE" ] && break; sleep 0.5; done
  DISPLAY=":$(head -n1 "$FD_FILE")"; export DISPLAY
  "$HOOK_DIR/pre-push.pre-commit" "$@"; RC=$?
  kill "$XPID" 2>/dev/null; rm -f "$FD_FILE"
  exit $RC
fi
exec "$HOOK_DIR/pre-push.pre-commit" "$@"
HOOK
fi

# rtk (https://github.com/rtk-ai/rtk) compacts command output.
RTK_VERSION=v1.2.0
BIN="$HOME/.local/bin"
if ! command -v rtk >/dev/null 2>&1 && [ ! -x "$BIN/rtk" ]; then
  curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh |
    RTK_VERSION="$RTK_VERSION" RTK_INSTALL_DIR="$BIN" sh >>"$LOG" 2>&1 ||
    say "rtk install failed; commands will run without it."
fi

# Every later Bash command: the venv's python first, then rtk.
if [ -n "$CLAUDE_ENV_FILE" ]; then
  {
    echo "export PATH=\"$ROOT/.venv/bin:$BIN:\$PATH\""
    echo "export VIRTUAL_ENV=\"$ROOT/.venv\""
  } >>"$CLAUDE_ENV_FILE"
fi
exit 0
