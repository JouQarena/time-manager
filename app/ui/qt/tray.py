"""System tray icon, menu, and the notifier that plugs into enforcement."""

from __future__ import annotations

import logging

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from app.ui import viewmodel
from app.ui.qt.icons import app_icon

log = logging.getLogger(__name__)


class TrayController:
    """Owns the tray icon. `available` is False on systems without a tray.

    The window and menu are injected so this class stays free of app wiring;
    `QtNotifier` (below) is what the enforcement engine calls.
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
        if self.available:
            self.icon.show()

    def _build_menu(self) -> None:
        open_action = QAction("Open dashboard", self.menu)
        open_action.triggered.connect(self.show_window)
        self.menu.addAction(open_action)
        self.menu.addSeparator()

        self.status_action = QAction("Monitoring", self.menu)
        self.status_action.setEnabled(False)
        self.menu.addAction(self.status_action)

        pause_menu = self.menu.addMenu("Pause tracking")
        for minutes in (15, 30, 60):
            action = QAction(f"{minutes} minutes", self.menu)
            action.triggered.connect(lambda _=False, m=minutes: self._pause(m))
            pause_menu.addAction(action)
        self.resume_action = QAction("Resume", self.menu)
        self.resume_action.triggered.connect(self._resume)
        pause_menu.addAction(self.resume_action)

        rules_action = QAction("Rules and usage…", self.menu)
        rules_action.triggered.connect(self.show_window)
        self.menu.addAction(rules_action)

        self.menu.addSeparator()
        quit_action = QAction("Quit Time Manager", self.menu)
        quit_action.triggered.connect(self._quit)
        self.menu.addAction(quit_action)

    # ------------------------------------------------------------------ slots
    def _activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_window()

    def show_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def _pause(self, minutes: int) -> None:
        self.service.pause(minutes)
        self.update_status(self.service.snapshot())

    def _resume(self) -> None:
        self.service.resume()
        self.update_status(self.service.snapshot())

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
        self.icon.setToolTip(f"Time Manager — {text}\n{line}")
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
