# Sourced by an interactive sh through $ENV (bash gets the same hook from PROMPT_COMMAND).
# Writes the epoch to the activity file at shell start and at every prompt, which is where a finished command lands.
date +%s 2>/dev/null >/tmp/.dfe-toolbox-activity
PS1='$(date +%s 2>/dev/null >/tmp/.dfe-toolbox-activity)'"${PS1}"
