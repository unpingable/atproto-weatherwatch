#!/usr/bin/env bash
set -euo pipefail

WW_PYTHON="${WW_PYTHON:-python3}"

"$WW_PYTHON" -m compileall -q src tests spike
"$WW_PYTHON" spike/check_fixture_privacy.py
"$WW_PYTHON" -m pytest -q
