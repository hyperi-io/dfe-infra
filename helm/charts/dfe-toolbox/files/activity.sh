# Sourced by an interactive sh through $ENV (bash gets the same hook from PROMPT_COMMAND).
# Writes the epoch to the activity file at shell start and at every prompt, which is where a finished command lands.
# Each write goes to a temporary file renamed over it, so the probe never reads a half-written one.
date +%s 2>/dev/null >/tmp/.dfe-toolbox-activity.$$ && mv -f /tmp/.dfe-toolbox-activity.$$ /tmp/.dfe-toolbox-activity
PS1='$(date +%s 2>/dev/null >/tmp/.dfe-toolbox-activity.$$ && mv -f /tmp/.dfe-toolbox-activity.$$ /tmp/.dfe-toolbox-activity)'"${PS1}"
