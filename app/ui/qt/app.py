"""GUI entry point: builds the QApplication, starts the agent, shows the window.

Also hosts `render_screenshots()` — used by `--gui-shot` and the Qt test suite
to render the real widgets offscreen into PNGs, so the dashboards in the docs
are produced by the shipping code rather than by hand.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from app.service import AgentAlreadyRunning, AgentService
from app.ui.qt.main_window import MainWindow
from app.ui.qt.rule_editor import RuleDialog
from app.ui.qt.settings_dialog import SettingsDialog
from app.ui.qt.theme import STYLESHEET
from app.ui.qt.tray import QtNotifier, TrayController

log = logging.getLogger(__name__)


def create_app(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName("Time Manager")
    app.setApplicationVersion(__import__("app").__version__)
    app.setQuitOnLastWindowClosed(False)  # the tray keeps the agent alive
    app.setStyleSheet(STYLESHEET)
    return app


def run_gui(db_path: str | None = None, *, notifier_out: list | None = None) -> int:
    """Start the agent + tray + dashboard. Returns the process exit code."""
    app = create_app()
    service = AgentService(db_path)
    notifier = QtNotifier()
    service.set_notifier(notifier)
    notifier.set_enabled(service.settings.notifications_enabled)

    try:
        service.start_background()
    except AgentAlreadyRunning as exc:
        QMessageBox.warning(None, "Time Manager", str(exc))
        return 3
    except Exception as exc:  # noqa: BLE001 - show it instead of dying silently
        QMessageBox.critical(None, "Time Manager", f"Could not start:\n\n{exc}")
        return 1

    window = MainWindow(service, tray=None)
    tray = TrayController(window, service)
    notifier.tray = tray
    window.tray = tray
    if notifier_out is not None:
        notifier_out.append(notifier)

    if not service.settings.start_minimized:
        window.show()

    app.aboutToQuit.connect(lambda: service.stop())
    return app.exec()


# --------------------------------------------------------------------- capture
def render_screenshots(out_dir: str | Path, service: AgentService | None = None) -> list[Path]:
    """Render dashboard/editor/settings/tray offscreen; returns the file paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    app = create_app()
    written: list[Path] = []

    owns_service = service is None
    if service is None:
        # Demo data on a fake clock: the screenshots show a real engine run.
        from app.ui.qt.demo import demo_service

        service = demo_service()
        service.start()

    window = MainWindow(service, tray=None)
    window.resize(1000, 760)
    window.show()
    app.processEvents()
    window.refresh()
    window.refresh_timeline()
    app.processEvents()

    target = out / "dashboard.png"
    window.grab().save(str(target))
    written.append(target)

    editor = RuleDialog(None, parent=window)
    editor.name.setText("Steam")
    editor.target.setText("steam.exe")
    editor.daily_enabled.setChecked(True)
    editor.daily_minutes.setValue(90)
    editor.session_enabled.setChecked(True)
    editor.session_minutes.setValue(45)
    editor.warnings.setText("15, 5")
    editor.always.setChecked(True)
    editor.resize(600, 740)
    editor.show()
    app.processEvents()
    target = out / "rule_editor.png"
    editor.grab().save(str(target))
    written.append(target)
    editor.close()

    settings = SettingsDialog(service, parent=window)
    settings.resize(620, 640)
    settings.show()
    app.processEvents()
    target = out / "settings.png"
    settings.grab().save(str(target))
    written.append(target)
    settings.close()

    window.hide()
    if owns_service:
        service.stop(backup=False)
    return written
