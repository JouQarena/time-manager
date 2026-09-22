"""Resource locations that work from source *and* from a PyInstaller bundle.

PyInstaller (one-folder mode) extracts data files under a `_internal`
directory next to the executable and exposes it as `sys._MEIPASS` at runtime.
Everything that ships as data (the browser-extension folder, the app icon)
must therefore be resolved through this module instead of relative paths,
which only work when running from a source checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running from a PyInstaller-built executable."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """The folder the bundled data files live in (read-only)."""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    # app/resources.py -> project root
    return Path(__file__).resolve().parents[1]


def executable_dir() -> Path:
    """Folder holding the running executable (source: the project root)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def extension_dir() -> Path:
    """The unpacked browser extension (Chrome/Edge -> 'Load unpacked')."""
    return bundle_dir() / "browser-extension"


def app_icon_path() -> Path:
    """Windows executable icon asset (may not exist; the spec handles that)."""
    return bundle_dir() / "assets" / "app.ico"
