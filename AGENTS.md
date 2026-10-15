# AGENTS.md

## Environment

- `run_terminal_command` fails on this machine: "Bash is required but was not found on this Windows system" (Git for Windows not installed). Until bash.exe exists or `CODEBUFF_GIT_BASH_PATH` points to it, no shell = no pytest/mypy/git — plan verification accordingly and don't burn turns retrying commands.
