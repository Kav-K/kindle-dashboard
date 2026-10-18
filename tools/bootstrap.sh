#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -e "$ROOT/.venv" ]; then
    printf '%s\n' 'The .venv directory already exists; activate it instead. Nothing was overwritten.' >&2
    exit 1
fi
PYTHON=${PYTHON:-python3}
"$PYTHON" -m venv "$ROOT/.venv"
if [ "${1:-}" = '--bambu' ]; then
    "$ROOT/.venv/bin/python" -m pip install "$ROOT[bambu]"
else
    "$ROOT/.venv/bin/python" -m pip install "$ROOT"
fi
printf '%s\n' "Installed. Run: $ROOT/.venv/bin/inkboard init --directory ~/.config/inkboard/home --wizard"
