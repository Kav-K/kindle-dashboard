#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "${1:-}" in
    '') [ "$#" -eq 0 ] || { printf '%s\n' 'Usage: sh tools/bootstrap.sh [--bambu]' >&2; exit 2; };;
    --bambu) [ "$#" -eq 1 ] || { printf '%s\n' 'Usage: sh tools/bootstrap.sh [--bambu]' >&2; exit 2; };;
    -h|--help) printf '%s\n' 'Usage: sh tools/bootstrap.sh [--bambu]'; exit 0;;
    *) printf '%s\n' 'Usage: sh tools/bootstrap.sh [--bambu]' >&2; exit 2;;
esac
if [ -e "$ROOT/.venv" ]; then
    printf '%s\n' 'The .venv directory already exists; activate it instead. Nothing was overwritten.' >&2
    exit 1
fi
PYTHON=${PYTHON:-python3}
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    printf '%s\n' 'Python 3.11 or newer is required. Install Python or set PYTHON to its executable path and retry. No environment was created.' >&2
    exit 1
fi
if ! "$PYTHON" -m venv "$ROOT/.venv"; then
    printf '%s\n' 'Virtual environment creation failed. Inspect the Python error before retrying.' >&2
    exit 1
fi
PACKAGE=$ROOT
if [ "${1:-}" = '--bambu' ]; then
    PACKAGE=$ROOT[bambu]
fi
if ! "$ROOT/.venv/bin/python" -m pip install "$PACKAGE"; then
    printf '%s\n' 'Dependency installation failed. Inspect the error; the environment is preserved.' >&2
    exit 1
fi
printf '%s\n' 'Installed. From this checkout, run:' '.venv/bin/inkboard init --directory ~/.config/inkboard/home --wizard'
