#!/usr/bin/env bash
set -euo pipefail

WW_PYTHON="${WW_PYTHON:-python3}"

"$WW_PYTHON" -m compileall -q src tests spike
# compileall only proves the code parses on THIS interpreter. The serving host
# runs 3.10, where three f-string forms that 3.12 accepts are SyntaxErrors.
"$WW_PYTHON" spike/check_py310_fstrings.py
"$WW_PYTHON" spike/check_fixture_privacy.py
"$WW_PYTHON" -m pytest -q
