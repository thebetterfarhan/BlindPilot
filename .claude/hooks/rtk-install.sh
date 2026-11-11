#!/bin/sh
# SessionStart: install rtk (https://github.com/rtk-ai/rtk) in cloud sessions,
# which start from a clean machine. On your own computer, install rtk yourself;
# this script does nothing there.
[ "$CLAUDE_CODE_REMOTE" = "true" ] || exit 0

RTK_VERSION=v1.2.0
BIN="$HOME/.local/bin"

if ! command -v rtk >/dev/null 2>&1 && [ ! -x "$BIN/rtk" ]; then
  curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh \
    | RTK_VERSION="$RTK_VERSION" RTK_INSTALL_DIR="$BIN" sh >/dev/null 2>&1 \
    || echo "rtk install failed; commands will run without it." >&2
fi

# Put rtk on PATH for the rest of the session's Bash commands.
if [ -n "$CLAUDE_ENV_FILE" ] && [ -x "$BIN/rtk" ]; then
  echo "export PATH=\"$BIN:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi
exit 0
