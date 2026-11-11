#!/bin/sh
# PreToolUse (Bash): let rtk rewrite the command so its output is compacted
# before it reaches the model. If rtk isn't installed, the command runs as is.
if command -v rtk >/dev/null 2>&1; then
  exec rtk hook claude
elif [ -x "$HOME/.local/bin/rtk" ]; then
  exec "$HOME/.local/bin/rtk" hook claude
fi
exit 0
