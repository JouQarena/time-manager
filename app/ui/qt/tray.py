"""System tray icon, menu, and the notifier that plugs into enforcement."""

from __future__ import annotations

import logging

from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from app.i18n import tr
from app.ui import viewmodel
from app.ui.qt.icons import app_icon
from app.ui.qt.language import apply_language_for, language_choices, language_manager

log = logging.getLogger(__name__)


class TrayController:
    """Owns the tray icon. `available` is False on systems without a tray.

    The window and menu are injected so this class stays free of app wiring;
    `QtNotifier` (below) is what the enforcement engine calls. The menu is
    rebuilt from the catalog on a language switch, so the tray follows the
    dashboard instead of staying in the language it was created with.
    """

    def __init__(self, window, service) -> None:
        self.window = window
        self.service = service
        self.icon = QSystemTrayIcon(app_icon(64), window)
        self.icon.setToolTip("Time Manager")
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self.menu = QMenu()
        self._build_menu()
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        language_manager().changed.connect(self._on_language_changed)
        if self.available:
            self.icon.show()

    def _build_menu(self) -> None:
        self.open_action = QAction(tr("tray.open_dashboard"), self.menu)
        self.open_action.triggered.connect(self.show_window)
        self.menu.addAction(self.open_action)
        self.menu.addSeparator()

        self.status_action = QAction(tr("health.monitoring"), self.menu)
        self.status_action.setEnabled(False)
        self.menu.addAction(self.status_action)

        self.pause_menu = self.menu.addMenu(tr("tray.pause_tracking"))
        self._pause_actions: list[tuple[int, QAction]] = []
        for minutes in (15, 30, 60):
            action = QAction(tr("tray.minutes", minutes=minutes), self.menu)
            action.triggered.connect(lambda _=False, m=minutes: self._pause(m))
            self.pause_menu.addAction(action)
            self._pause_actions.append((minutes, action))
        self.resume_action = QAction(tr("tray.resume"), self.menu)
        self.resume_action.triggered.connect(self._resume)
        self.pause_menu.addAction(self.resume_action)

        self.rules_action = QAction(tr("tray.rules"), self.menu)
        self.rules_action.triggered.connect(self.show_window)
        self.menu.addAction(self.rules_action)

        self.guide_action = QAction(tr("tray.show_guide"), self.menu)
        self.guide_action.triggered.connect(self.show_guide)
        self.menu.addAction(self.guide_action)

        # Language submenu: same switch as the header globe, always reachable
        # even when the dashboard window is closed to the tray.
        self.language_menu = self.menu.addMenu(tr("tray.language"))
        self._language_group = QActionGroup(self.menu)
        self._language_group.setExclusive(True)
        self._language_actions: dict[str, QAction] = {}
        for code, name in language_choices():
            action = QAction(name, self.menu)
            action.setCheckable(True)
            action.triggered.connect(lambda _=False, c=code: self._set_language(c))
            self._language_group.addAction(action)
            self.language_menu.addAction(action)
            self._language_actions[code] = action
        self._sync_language_checks()

        self.menu.addSeparator()
        self.quit_action = QAction(tr("tray.quit"), self.menu)
        self.quit_action.triggered.connect(self._quit)
        self.menu.addAction(self.quit_action)

    # ------------------------------------------------------------------ slots
    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window()

    def show_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def show_guide(self) -> None:
        """Run the guided tour (opening the dashboard first — the tour dims it)."""
        self.show_window()
        self.window.start_tour()

    def _pause(self, minutes: int) -> None:
        self.service.pause(minutes)
        self.update_status(self.service.snapshot())

    def _resume(self) -> None:
        self.service.resume()
        self.update_status(self.service.snapshot())

    def _set_language(self, code: str) -> None:
        apply_language_for(self.service, code)

    def _on_language_changed(self, _code: str) -> None:
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self.open_action.setText(tr("tray.open_dashboard"))
        for minutes, action in self._pause_actions:
            action.setText(tr("tray.minutes", minutes=minutes))
        self.pause_menu.setTitle(tr("tray.pause_tracking"))
        self.resume_action.setText(tr("tray.resume"))
        self.rules_action.setText(tr("tray.rules"))
        self.guide_action.setText(tr("tray.show_guide"))
        self.language_menu.setTitle(tr("tray.language"))
        self.quit_action.setText(tr("tray.quit"))
        self._sync_language_checks()
        self.update_status(self.service.snapshot())

    def _sync_language_checks(self) -> None:
        from app.i18n import current_language

        active = current_language()
        for code, action in self._language_actions.items():
            action.setChecked(code == active)

    def _quit(self) -> None:
        from PySide6.QtWidgets import QApplication

        if self.window.confirm_quit():
            QApplication.quit()

    # ------------------------------------------------------------------ update
    def update_status(self, snapshot) -> None:
        role, text = viewmodel.health(snapshot)
        line = viewmodel.status_line(snapshot)
        if snapshot.paused:
            text += f" · {viewmodel.format_duration(snapshot.pause_remaining_seconds)}"
        self.status_action.setText(text)
        self.resume_action.setEnabled(snapshot.paused)
        self.icon.setToolTip(tr("tray.tooltip", status=text, line=line))
        role_for_icon = role if role != "info" else "ok"
        self.icon.setIcon(app_icon(64, role=role_for_icon))

    def notify(self, title: str, message: str, *, urgent: bool = False) -> None:
        self.icon.showMessage(
            title, message,
            QSystemTrayIcon.Critical if urgent else QSystemTrayIcon.Information,
            8000,
        )


class QtNotifier:
    """`Notifier` protocol implementation handed to `EnforcementExecutor`.

    Failures are swallowed: a missing tray must never stop enforcement.
    """

    def __init__(self, tray: TrayController | None = None) -> None:
        self.tray = tray
        self.history: list[tuple[str, str, bool]] = []
        self._enabled = True

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def notify(self, title: str, message: str, *, urgent: bool = False) -> None:
        self.history.append((title, message, urgent))
        if not self._enabled or self.tray is None or not self.tray.available:
            return
        try:
            self.tray.notify(title, message, urgent=urgent)
        except Exception:  # noqa: BLE001
            log.debug("Tray notification failed.", exc_info=True)
