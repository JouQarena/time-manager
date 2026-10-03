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
from app.ui.qt.language import apply_saved_language, language_manager
from app.ui.qt.main_window import MainWindow
from app.ui.qt.rule_editor import RuleDialog
from app.ui.qt.extension_help import ExtensionHelpDialog
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


def wants_first_run_tour(service: AgentService) -> bool:
    """True until the user has seen the guided tour through to the end.

    Stored as a plain setting so it survives reinstalls of the app folder but
    not a fresh profile — a new user gets the tour, everyone else never sees
    it unless they ask for it (Guide button / tray menu).
    """
    try:
        if service.db is None:
            return False
        return not bool(service.db.get_setting("tour_seen", False))
    except Exception:  # noqa: BLE001 - a missing settings row must not block startup
        return False


def run_gui(db_path: str | None = None, *, notifier_out: list | None = None) -> int:
    """Start the agent + tray + dashboard. Returns the process exit code."""
    app = create_app()
    service = AgentService(db_path)
    # Before any widget is built: the first paint must already be in the
    # user's language and text direction.
    apply_saved_language(service.settings)
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

    window = MainWindow(service, tray=None,
                        autostart_tour=wants_first_run_tour(service))
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

    # The guided tour, rendered by the shipping overlay (shadow + arrow).
    overlay = window.start_tour()
    app.processEvents()
    overlay.next()          # step 2 spotlights the rule list
    app.processEvents()
    target = out / "tour.png"
    window.grab().save(str(target))
    written.append(target)
    overlay.skip()
    app.processEvents()

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

    # The extension Help dialog behind the Browser extension panel's Help
    # button: the folder and the token are masked here (the token is a
    # machine secret — a screenshot must never carry a usable one).
    help_dialog = ExtensionHelpDialog(service, parent=window)
    help_dialog.resize(620, 560)
    help_dialog.show()
    app.processEvents()
    target = out / "extension_help.png"
    help_dialog.grab().save(str(target))
    written.append(target)
    help_dialog.close()

    # Arabic: the same dashboard, switched language + RTL layout. Rendered
    # from the live window so the docs cannot drift from the shipped strings.
    from app.i18n import current_language

    active = current_language()
    try:
        language_manager().apply("ar")
        app.processEvents()
        window.refresh()
        window.refresh_timeline()
        app.processEvents()
        target = out / "dashboard-ar.png"
        window.grab().save(str(target))
        written.append(target)
    finally:
        # Never leave the process (or the test suite) in a switched state.
        language_manager().apply(active, notify=False)
        window.refresh()
        app.processEvents()

    window.hide()
    if owns_service:
        service.stop(backup=False)
    return written
