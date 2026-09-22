#!/usr/bin/env bash
# Time Manager — one-file setup (Linux/macOS; the Windows equivalent is setup.bat).
#
#   ./setup.sh              install into .venv + run the test suite
#   ./setup.sh --fresh      rebuild .venv from scratch
#   ./setup.sh --build      also install the PyInstaller build tools
#   ./setup.sh --no-test    skip the test-suite verification
set -euo pipefail
cd "$(dirname "$0")"

echo
echo " =========================================="
echo "  Time Manager - one-file setup"
echo " =========================================="
echo

# 1. find Python 3.12+ --------------------------------------------------------
PY=""
for candidate in python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
            PY="$candidate"
            break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "[X] No Python 3.12+ found. Install it (e.g. from python.org or your" >&2
    echo "    package manager) and re-run this script." >&2
    exit 1
fi
echo "[1/4] Using Python: $PY ($("$PY" --version))"

# 2. the virtual environment --------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --fresh) [ -d .venv ] && { echo "[2/4] Removing the old .venv (--fresh)"; rm -rf .venv; } ;;
    esac
done
if [ -x .venv/bin/python ]; then
    echo "[2/4] Reusing the existing .venv (pass --fresh to rebuild)"
else
    echo "[2/4] Creating the virtual environment .venv ..."
    "$PY" -m venv .venv
fi
VPY="$(pwd)/.venv/bin/python"

# 3. install dependencies ------------------------------------------------------
echo "[3/4] Installing dependencies from requirements.txt ..."
"$VPY" -m pip install --upgrade pip --quiet
"$VPY" -m pip install -r requirements.txt
for arg in "$@"; do
    case "$arg" in
        --build)
            echo "      Installing build tools from requirements-build.txt ..."
            "$VPY" -m pip install -r requirements-build.txt
            ;;
    esac
done

# 4. verify --------------------------------------------------------------------
SKIP_TEST=0
for arg in "$@"; do
    case "$arg" in --no-test) SKIP_TEST=1 ;; esac
done
if [ "$SKIP_TEST" -eq 0 ]; then
    echo "[4/4] Running the test suite ..."
    "$VPY" -m pytest -q
fi
"$VPY" -c "import PySide6, psutil, websockets; import app.main"

echo
echo " =========================================="
echo "  Setup complete - everything is installed."
echo " =========================================="
echo
echo "  Start the app:      .venv/bin/python -m app.main --gui"
echo "  Head-less agent:    .venv/bin/python -m app.main --monitor"
echo "  CLI status:         .venv/bin/python -m app.main --status"
echo "  Cross-plat build:   ./scripts/build_smoke.sh"
echo
