#!/usr/bin/env bash
# Cross-platform smoke build: proves the PyInstaller spec bundles every
# dependency and that the frozen executables actually run. On Windows this is
# a preview of TimeManager.exe; on Linux/macOS it builds the same spec with
# the platform-specific pieces (icon resource, pywin32) skipped.
set -euo pipefail
cd "$(dirname "$0")/.."

python -m pip install -q -r requirements.txt -r requirements-build.txt
python -m PyInstaller time-manager.spec --noconfirm --clean

echo
echo "== frozen smoke checks =="
./dist/TimeManager/TimeManager --version
./dist/TimeManager/TimeManager --show-extension-path
./dist/TimeManager/TimeManager --init-db --db /tmp/tm-smoke.db
./dist/TimeManager/TimeManager --status --db /tmp/tm-smoke.db
./dist/TimeManager/TimeManager --security --db /tmp/tm-smoke.db
./dist/TimeManager/TimeManager --simulate 5 --tick 5
./dist/TimeManager/TimeManager --detector-selftest | tail -1
rm -f /tmp/tm-smoke.db*
echo "smoke build OK: dist/TimeManager/"
