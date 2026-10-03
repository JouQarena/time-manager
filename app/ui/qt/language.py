"""Runtime language switching for the Qt layer.

`app/i18n.py` holds the strings; this module is the Qt-side switch: it applies
the language to `QApplication` (layout direction, font fallback, number
locale) and emits a signal so every open window can re-label itself in place.

Arabic is right-to-left, so switching must also flip the layout direction —
`QApplication.setLayoutDirection(Qt.RightToLeft)` does that for the whole app,
including dialogs that are created later. Layouts built with `addLayout` get
mirrored automatically; the one thing that does *not* mirror is the custom
painted tour overlay, which asks `is_rtl()` for its own arrow placement.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from app.i18n import (
    available_languages,
    current_language,
    is_rtl,
    language_name,
    normalize_language,
    set_language,
)

__all__ = [
    "LanguageManager",
    "apply_language",
    "apply_saved_language",
    "language_choices",
    "language_manager",
    "language_name",
]

log = logging.getLogger(__name__)

#: Font families tried, in order, when the UI is Arabic. Every candidate has
#: real Arabic coverage on at least one target OS, and Qt falls through the
#: list per character — so a missing family degrades instead of drawing boxes.
_ARABIC_FONTS = ["Segoe UI", "Noto Sans Arabic", "Noto Naskh Arabic", "Tahoma",
                 "Arial", "DejaVu Sans"]
_LATIN_FONTS = ["Segoe UI", "system-ui", "sans-serif"]


def language_choices() -> list[tuple[str, str]]:
    """(code, native name) pairs for combo boxes and menus."""
    return [(lang.code, lang.native_name) for lang in available_languages()]


class LanguageManager(QObject):
    """Owns the active language and tells the UI when it changes."""

    changed = Signal(str)  # new language code

    def __init__(self, app: QApplication | None = None) -> None:
        super().__init__()
        self._app = app or QApplication.instance()
        self._base_font: QFont | None = None

    # ------------------------------------------------------------------ apply
    def apply(self, code: str, *, notify: bool = True) -> str:
        """Switch language; returns the code actually applied."""
        resolved = set_language(code)
        self._apply_qt(resolved)
        if notify:
            self.changed.emit(resolved)
        return resolved

    def _apply_qt(self, code: str) -> None:
        # Prefer the live instance: a manager built before the QApplication
        # existed (imports during collection, headless helpers) must still
        # apply the switch to the app that is actually running.
        app = QApplication.instance() or self._app
        if app is None:  # headless unit-test path: no QApplication yet
            return
        app.setLayoutDirection(Qt.RightToLeft if is_rtl(code) else Qt.LeftToRight)
        # Western digits in both languages: limits and audit timestamps are
        # technical values that must match `--status` and the database, so the
        # catalog only translates unit *letters* (see app/i18n.py).
        app.setProperty("tmLanguage", code)
        self._apply_font(app, code)

    def _apply_font(self, app: QApplication, code: str) -> None:
        if self._base_font is None:
            self._base_font = QFont(app.font())
        families = _ARABIC_FONTS if is_rtl(code) else _LATIN_FONTS
        font = QFont(self._base_font)
        font.setFamilies(families)
        app.setFont(font)


_manager: LanguageManager | None = None


def language_manager() -> LanguageManager:
    """Process-wide manager (created on first use)."""
    global _manager
    if _manager is None:
        _manager = LanguageManager()
    return _manager


def apply_saved_language(settings=None) -> str:
    """Apply the language stored in the config; falls back to English.

    Called once during start-up, before any window is built, so the very
    first paint is already in the right language and direction.
    """
    code = getattr(settings, "language", None)
    return language_manager().apply(normalize_language(code), notify=False)


def apply_language(code: str) -> str:
    """Switch the running UI to `code` and return what was applied."""
    return language_manager().apply(code)


def apply_language_for(service, code: str) -> str:
    """Switch the UI *and* remember the choice in the user's config.

    Used by every entry point (header globe, tray submenu, settings dialog) so
    they cannot drift: one call changes the language, updates the service's
    settings object and writes `config.json`.
    """
    from app.config.settings import save_settings

    applied = apply_language(code)
    try:
        service.settings.language = applied
        save_settings(service.settings)
    except Exception:  # noqa: BLE001 - a read-only profile must not break the switch
        log.warning("Could not persist the language choice.", exc_info=True)
    return applied


def current() -> str:  # convenience re-export for the UI layer
    return current_language()


def rtl() -> bool:
    return is_rtl(current_language())
