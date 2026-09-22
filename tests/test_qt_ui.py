"""Qt layer tests, offscreen.

pytest-qt is not a dependency, so these tests own their QApplication. Every
test drives the real widgets through the real AgentService (fake clock, fake
process table) — no Qt calls hit a display, network or the OS.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="PySide6 is optional (GUI extra)")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.core.rules.models import Rule  # noqa: E402
from app.core.types import Action, Mode, RuleType  # noqa: E402
from app.ui import viewmodel  # noqa: E402
from app.ui.qt import theme, widgets  # noqa: E402
from app.ui.qt.app import create_app, render_screenshots  # noqa: E402
from app.ui.qt.demo import demo_service  # noqa: E402
from app.ui.qt.main_window import MainWindow  # noqa: E402
from app.ui.qt.rule_editor import RuleDialog  # noqa: E402
from app.ui.qt.settings_dialog import SettingsDialog  # noqa: E402
from app.ui.qt.tray import QtNotifier, TrayController  # noqa: E402


@pytest.fixture()
def service(qapp):
    svc = demo_service()
    svc.start()
    yield svc
    svc.stop(backup=False)


@pytest.fixture()
def window(qapp, service):
    win = MainWindow(service, tray=None)
    win.show()  # offscreen: makes isVisible() meaningful for children
    yield win
    win.close()


# ------------------------------------------------------------------ the app
def test_create_app_is_singleton_and_styled(qapp):
    assert QApplication.instance() is qapp
    assert create_app([]) is qapp
    assert "QFrame#Card" in qapp.styleSheet()
    assert qapp.quitOnLastWindowClosed() is False  # the tray owns the lifetime


def test_theme_stylesheet_has_no_external_resources():
    css = theme.STYLESHEET
    for banned in ("http://", "https://", "url(", "@import"):
        assert banned not in css, f"the UI must not reach the network ({banned})"


# --------------------------------------------------------------- dashboard
def test_window_shows_all_rules_from_the_service(window, service):
    window.refresh()
    rows = viewmodel.rule_rows(service.snapshot())
    assert len(window.cards) == len(rows)
    assert set(window.cards) == {row.rule_id for row in rows}
    # The engine's own state text is what the card displays.
    card = window.cards[rows[0].rule_id]
    assert rows[0].name in card.title.text()
    assert card.used.text().startswith(rows[0].used_text)


def test_header_reflects_the_service(window, service):
    assert window.health_chip.text() == "Monitoring"
    assert service.db_path.name in window.path_label.text()

    service.pause(30)
    window.refresh()
    assert "Paused" in window.health_chip.text()
    assert "30m 00s" in window.banner.text()
    assert window.banner.isVisible()
    assert window.pause_button.text().startswith("Paused · ")
    service.resume()
    window.refresh()
    assert window.health_chip.text() == "Monitoring"
    assert not window.banner.isVisible()  # the banner goes away again


def test_timeline_shows_the_demo_audit_rows(window, service):
    window.refresh_timeline()
    lines = window.timeline_panel.lines()
    assert len(lines) >= 3  # the demo database ships three audit rows
    assert any("Close app" in line or "Block site" in line for line in lines)
    assert any("EXECUTED" in line for line in lines)
    assert any("SKIPPED" in line for line in lines)


def test_link_panel_shows_the_real_port(window, service):
    window.refresh()
    first = window.link_panel.lines()[0]
    assert str(service.ipc.bound_port) in first
    assert "127.0.0.1" in first  # loopback only, never a public interface
    assert "No browser connected" in window.browser_panel.lines()[0]


def test_detector_panel_shows_the_registered_detector(window, service):
    window.refresh()
    lines = window.detector_panel.lines()
    assert any("League of Legends" in line for line in lines)
    assert any("budget" in line.lower() for line in lines)


def test_detector_panel_flags_a_quarantined_detector(window, service):
    """A quarantined detector must be visible, not silent."""
    managed = service.detectors.managed("league_of_legends")
    managed.stats.quarantined = True
    managed.stats.errors = 5
    try:
        window.refresh()
        text = " ".join(window.detector_panel.lines())
        assert "matches will not be closed" in text
        assert "1 quarantined" in text
    finally:
        managed.stats.quarantined = False
        managed.stats.errors = 0
    window.refresh()
    assert "0 quarantined" in " ".join(window.detector_panel.lines())
    assert "matches will not be closed" not in " ".join(window.detector_panel.lines())


def test_rule_editor_offers_known_games_from_the_registry(qapp):
    dialog = RuleDialog(None)
    dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(RuleType.GAME))
    labels = [dialog.game_combo.itemText(i) for i in range(dialog.game_combo.count())]
    assert "League of Legends" in labels
    dialog.game_combo.setCurrentIndex(labels.index("League of Legends"))
    assert dialog.target.text() == "leagueclientux.exe"   # the client, not the match exe
    assert "league of legends.exe" in dialog.extra.text()  # the match process is an extra
    assert dialog.name.text() == "League of Legends"
    dialog.daily_enabled.setChecked(True)   # a rule needs at least one limit
    dialog.daily_minutes.setValue(120)
    rule = dialog.rule()
    assert rule.type is RuleType.GAME
    assert rule.executables == ("leagueclientux.exe", "leagueclient.exe",
                                "riotclientservices.exe", "league of legends.exe")
    dialog.close()


def test_rule_editor_hides_known_games_for_websites(qapp):
    dialog = RuleDialog(None)
    dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(RuleType.WEBSITE))
    assert not dialog.game_combo.isVisible()
    dialog.show()
    assert not dialog.game_combo.isVisible()
    dialog.close()


def test_pause_and_resume_from_the_menu(window, service):
    window._pause(15)
    assert service.pause_ctl.is_paused()
    window.refresh()
    assert window.pause_button.text().startswith("Paused")
    window._resume()
    assert not service.pause_ctl.is_paused()
    window.refresh()
    assert window.pause_button.text() == "Pause"


def test_disable_and_enable_a_rule_from_the_card(window, service):
    rule_id = service.db.list_rules()[0].id
    window._toggle_rule(rule_id, False)
    assert service.db.get_rule(rule_id).enabled is False
    window.refresh()
    assert window.cards[rule_id].toggle_button.text() == "Enable"  # card follows the db
    window._toggle_rule(rule_id, True)
    assert service.db.get_rule(rule_id).enabled is True


def test_refresh_is_timer_driven_and_idempotent(window):
    assert window.refresh_timer.interval() == 1000
    assert window.timeline_timer.interval() == 5000
    first = window.health_chip.text()
    for _ in range(3):
        window.refresh()
    assert window.health_chip.text() == first  # repeated refreshes do not drift


def test_closing_the_window_hides_to_tray_instead_of_quitting(qapp, service):
    win = MainWindow(service, tray=None)
    win.tray = TrayController(win, service)  # a tray is available
    win.show()
    win.close()
    assert win.isHidden()  # hidden, not destroyed
    assert QApplication.instance() is qapp


def test_window_title_uses_the_viewmodel(window, service):
    window.refresh()
    assert window.windowTitle() == viewmodel.window_title(service.snapshot())


# ------------------------------------------------------------- rule editor
def test_rule_editor_builds_a_valid_core_rule(qapp):
    dialog = RuleDialog(None)
    dialog.name.setText("YouTube")
    dialog.type_combo.setCurrentIndex(2)  # Website
    dialog.target.setText("youtube.com")
    dialog.daily_enabled.setChecked(True)
    dialog.daily_minutes.setValue(30)
    dialog.warnings.setText("5, 1")
    rule = dialog.rule()
    assert rule.name == "YouTube"
    assert rule.type is RuleType.WEBSITE
    assert rule.domain == "youtube.com"
    assert rule.daily_limit_seconds == 30 * 60
    assert rule.warning_seconds == (300, 60)
    dialog.close()


def test_rule_editor_rejects_invalid_input_through_the_core_model(qapp):
    dialog = RuleDialog(None)
    dialog.name.setText("")  # no name
    dialog.target.setText("youtube.com")
    with pytest.raises(ValueError):
        dialog.rule()
    dialog.name.setText("YouTube")
    dialog.target.setText("")  # no target
    with pytest.raises(ValueError):
        dialog.rule()
    dialog.target.setText("youtube.com")
    dialog.daily_enabled.setChecked(False)  # no limit at all
    with pytest.raises(ValueError):
        dialog.rule()
    dialog.close()


def test_rule_editor_round_trips_an_existing_rule(qapp):
    rule = Rule(name="Discord", type=RuleType.APPLICATION, target="discord.exe",
                executable="discord.exe", daily_limit_seconds=600,
                session_limit_seconds=300, warning_seconds=(120,),
                action=Action.CLOSE, mode=Mode.STRICT)
    dialog = RuleDialog(rule)
    assert dialog.name.text() == "Discord"
    assert dialog.daily_minutes.value() == 10
    assert dialog.mode_combo.currentText().startswith("Strict")
    rebuilt = dialog.rule()
    assert rebuilt.daily_limit_seconds == 600
    assert rebuilt.session_limit_seconds == 300
    assert rebuilt.mode is Mode.STRICT
    dialog.close()


def test_rule_editor_hides_day_pickers_until_needed(qapp):
    dialog = RuleDialog(None)
    assert dialog.always.isChecked()
    assert not dialog.days_combo.isEnabled()  # nothing to pick: no schedule
    assert not dialog.custom_days.isVisible()
    dialog.show()
    dialog.always.setChecked(False)
    assert dialog.days_combo.isEnabled()
    dialog.days_combo.setCurrentText("Custom…")
    assert dialog.custom_days.isVisible()  # day boxes appear only now
    dialog.close()


def test_saving_from_the_editor_reaches_the_database(qapp, service):
    dialog = RuleDialog(None, parent=None)
    dialog.name.setText("Steam")
    dialog.type_combo.setCurrentIndex(0)  # Application
    dialog.target.setText("steam.exe")
    dialog.daily_enabled.setChecked(True)
    dialog.daily_minutes.setValue(45)
    rule = dialog.rule()  # the dialog only builds; the caller stores
    rule_id = service.save_rule(rule)
    stored = service.db.get_rule(rule_id)
    assert stored.name == "Steam" and stored.daily_limit_seconds == 45 * 60
    dialog.close()


# --------------------------------------------------------------- settings
def test_settings_dialog_saves_config_and_database_settings(qapp, service):
    dialog = SettingsDialog(service, parent=None)
    assert dialog.service is service
    dialog.interval.setValue(2)
    dialog.grace.setValue(10)
    dialog.foreground_only.setChecked(False)
    dialog.browser_foreground.setChecked(False)
    dialog.port.setValue(19001)
    dialog.notifications.setChecked(False)
    dialog._save()  # the Save button's handler, minus the modal message box
    assert service.settings.monitoring_interval == 2.0
    # Settings are stored typed (int/bool), not as strings.
    assert service.db.get_setting("background_grace_seconds") == 10
    assert service.db.get_setting("require_browser_foreground") is False
    assert service.db.get_setting("ipc_port") == 19001
    assert service.db.get_setting("notifications_enabled") is False
    assert "Saved" in dialog.message.text()
    dialog.close()


def test_settings_dialog_masks_the_token_until_asked(qapp, service):
    dialog = SettingsDialog(service, parent=None)
    assert dialog.token.echoMode() == dialog.token.EchoMode.Password
    dialog._toggle_token(True)
    assert dialog.token.echoMode() == dialog.token.EchoMode.Normal
    assert dialog.token.text() == service.token
    dialog._toggle_token(False)
    assert dialog.token.echoMode() == dialog.token.EchoMode.Password
    # The token itself is never edited here: the field stays read-only.
    assert dialog.token.isReadOnly()
    dialog.close()


def test_settings_dialog_backs_up_on_demand(qapp, service, monkeypatch, tmp_path):
    dialog = SettingsDialog(service, parent=None)
    monkeypatch.setattr("app.service.profile_dir", lambda: tmp_path / "profile")
    dialog._backup()
    backups = list((tmp_path / "profile" / "backups").glob("*.db"))
    assert backups, "the Back up now button must write a real backup file"
    assert "Backup" in dialog.message.text()
    dialog.close()


# -------------------------------------------------------------------- tray
def test_tray_menu_offers_pause_resume_and_quit(qapp, service):
    win = MainWindow(service, tray=None)
    tray = TrayController(win, service)
    actions = [a.text() for a in tray.menu.actions() if not a.isSeparator()]
    assert "Open dashboard" in actions
    assert "Quit Time Manager" in actions
    pause_menu = next(a.menu() for a in tray.menu.actions() if a.menu())
    assert [a.text() for a in pause_menu.actions()] == [
        "15 minutes", "30 minutes", "60 minutes", "Resume"]


def test_tray_status_follows_the_service(qapp, service):
    win = MainWindow(service, tray=None)
    tray = TrayController(win, service)
    tray.update_status(service.snapshot())
    assert tray.status_action.text() == "Monitoring"
    assert "Time Manager" in tray.icon.toolTip()
    service.pause(5)
    tray.update_status(service.snapshot())
    assert tray.status_action.text().startswith("Paused")
    assert "5m" in tray.status_action.text()  # minutes left, right in the menu
    assert tray.resume_action.isEnabled()
    service.resume()
    tray.update_status(service.snapshot())
    assert not tray.resume_action.isEnabled()
    win.close()


def test_notifier_records_and_respects_the_toggle(qapp):
    notifier = QtNotifier()  # no tray: history only, nothing is shown
    notifier.notify("Discord", "5 minutes left")
    assert notifier.history[-1][0] == "Discord"
    notifier.set_enabled(False)
    notifier.notify("League", "limit reached")
    assert len(notifier.history) == 2  # recorded, but the user asked for quiet
    notifier.set_enabled(True)


def test_notifier_is_wired_to_the_enforcement_executor(qapp, service):
    notifier = QtNotifier()
    service.set_notifier(notifier)  # what run_gui() does at startup
    assert service.executor.notifier is notifier


def test_playground_widgets_render(qapp):
    chip = widgets.Chip("Ready", viewmodel.ROLE_OK)
    assert chip.text() == "Ready"
    chip.update_text("Warning", viewmodel.ROLE_WARN)
    assert theme.color(viewmodel.ROLE_WARN) in chip.styleSheet()
    panel = widgets.Panel("Title")
    panel.add_line("first")
    panel.add_line("second")
    panel.clear()
    assert panel.body.count() == 1  # only the trailing stretch survives


# ---------------------------------------------------------------- capture
def test_gui_runs_end_to_end_and_stops_cleanly(qapp, tmp_path):
    """`run_gui` for real: event loop, tray, live agent — then a clean exit."""
    from PySide6.QtCore import QTimer

    from app.ui.qt import app as qt_app

    db_path = tmp_path / "gui.db"
    QTimer.singleShot(1600, qapp.quit)  # let the agent take a few ticks
    code = qt_app.run_gui(str(db_path))
    assert code == 0
    assert db_path.exists()

    # The agent really ran (and was stopped by run_gui's aboutToQuit hook).
    from app.config.lockfile import InstanceLock

    assert not InstanceLock(db_path.with_suffix(".lock")).path.exists()
    from app.database.db import Database

    db = Database(db_path).connect()
    assert db.get_setting("ipc_port") is not None  # the profile was initialised
    db.close()


def test_render_screenshots_produces_real_pngs(qapp, tmp_path, service):
    written = render_screenshots(tmp_path, service=service)
    names = {path.name for path in written}
    assert names == {"dashboard.png", "rule_editor.png", "settings.png"}
    for path in written:
        assert path.exists() and path.stat().st_size > 5000  # real pixels, not blank
    # PNG magic bytes: these are images, not empty files.
    assert written[0].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_screenshots_creates_demo_data_when_asked(qapp, tmp_path):
    written = render_screenshots(tmp_path)  # no service: uses the demo
    assert len(written) == 3
    assert all(path.exists() for path in written)


def test_grab_is_not_blank_white(qapp, window, service):
    window.refresh()
    image = window.grab().toImage()
    assert not image.isNull()
    samples = [image.pixelColor(x, y).name()
               for x in range(0, image.width(), 37)
               for y in range(0, image.height(), 29)]
    assert len(set(samples)) > 5  # a themed dashboard, not a blank canvas
    # The dark theme, not a blank white grab: a handful of pure-white pixels
    # can be text anti-aliasing (Windows ClearType clips glyph cores to
    # #ffffff), but a blank white window is almost entirely white.
    white_ratio = samples.count("#ffffff") / len(samples)
    assert white_ratio < 0.02, f"grab is {white_ratio:.0%} white"


# ----------------------------------------------------------------- Phase 7
def test_security_banner_hidden_when_all_clear(window, service):
    window.refresh()
    assert window.security_banner.isHidden()
    assert viewmodel.security_notices(service.snapshot()) == []


def test_security_banner_shows_findings(window, service):
    service._unclean_stop = {"last_start_at": "2026-09-20T22:00:00+00:00",
                             "local": "2026-09-21T00:00:00+02:00"}
    window.refresh()
    assert not window.security_banner.isHidden()
    lines = window.security_banner.notices()
    assert any("stopped unexpectedly" in line for line in lines)

    service._unclean_stop = None  # resolved -> the banner disappears again
    window.refresh()
    assert window.security_banner.isHidden()


def test_settings_dialog_toggles_the_watchdog_setting(qapp, service, monkeypatch):
    # _save() persists via the global save_settings -> the *real*
    # %APPDATA% config.json; a pytest run on a real machine used to flip the
    # user's actual strict_watchdog setting (field-observed).
    monkeypatch.setattr("app.ui.qt.settings_dialog.save_settings", lambda s: None)
    dialog = SettingsDialog(service, parent=None)
    assert service.settings.strict_watchdog is False
    # The widget is disabled where the watchdog is unavailable (non-Windows);
    # forcing it on exercises the persist path that Windows users get.
    dialog.watchdog.setEnabled(True)
    dialog.watchdog.setChecked(True)
    dialog._save()
    assert service.settings.strict_watchdog is True  # persisted on the config
    dialog.watchdog.setChecked(False)
    dialog._save()
    assert service.settings.strict_watchdog is False
    dialog.close()


def test_rule_editor_shows_honest_game_caveats(qapp):
    from app.ui.qt.rule_editor import PRESET_CAVEATS, _preset_caveat

    assert "once the game is closed" in _preset_caveat("valorant")
    assert "once the game is closed" in _preset_caveat("repo")
    assert "never closes the wrong game" in _preset_caveat("teamfight_tactics")
    assert _preset_caveat("league_of_legends") == ""
    assert PRESET_CAVEATS["valorant"].startswith(" VALORANT")

def test_rule_dialog_fits_small_screens(qapp):
    """Field report: on a 768px-high laptop (125% scaling = ~610 logical
    pixels) the rule dialog opened taller than the screen with Save below
    the fold and no way to shrink it. The form must live in a scroll area,
    the dialog must shrink freely, and it must open inside the screen."""
    from PySide6.QtWidgets import QScrollArea

    from app.ui.qt.rule_editor import RuleDialog

    dialog = RuleDialog()
    try:
        assert dialog.findChild(QScrollArea) is not None
        # shrunken freely: the layout minimum must stay small
        assert dialog.minimumSizeHint().height() <= 480
        dialog.resize(520, 440)
        qapp.processEvents()
        assert dialog.size().height() <= 460
        # and its default opens inside a small screen
        assert dialog.height() <= qapp.primaryScreen().availableGeometry().height()
    finally:
        dialog.deleteLater()


def test_settings_dialog_fits_small_screens(qapp, service):
    from PySide6.QtWidgets import QScrollArea

    dialog = SettingsDialog(service)
    try:
        assert dialog.findChild(QScrollArea) is not None
        assert dialog.minimumSizeHint().height() <= 480
    finally:
        dialog.deleteLater()

def test_edit_website_rule_shows_the_normalized_domain(qapp):
    """Field report: a user pasted a full URL as the website target; saving
    normalizes it, but re-opening the editor then showed the raw URL again,
    teaching the wrong shape. Edit mode must show the domain that matches
    (reddit.com covers www and every other subdomain)."""
    from app.core.rules.models import Rule
    from app.core.types import Action, Mode, RuleType

    rule = Rule(
        name="reddit", type=RuleType.WEBSITE, target="https://www.reddit.com/",
        domain="https://www.reddit.com/", daily_limit_seconds=900,
        warning_seconds=(300, 60), action=Action.WARN_ONLY, mode=Mode.NORMAL,
    )
    rule.validate()
    assert rule.domain == "www.reddit.com"
    dialog = RuleDialog(rule)
    try:
        assert dialog.target.text() == "www.reddit.com"
    finally:
        dialog.deleteLater()
