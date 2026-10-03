"""The dashboard window: live status, per-rule usage, audit trail."""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from app import __version__
from app.core.pause import MAX_PAUSE_MINUTES
from app.i18n import tr
from app.service import AgentService
from app.ui import viewmodel
from app.ui.qt.icons import app_icon
from app.ui.qt.language import (apply_language_for, language_choices,
                                language_manager, language_name)
from app.ui.qt.widgets import Chip, EmptyState, Panel, RuleCard, SecurityBanner, label
from app.ui.qt.rule_editor import RuleDialog
from app.ui.qt.settings_dialog import SettingsDialog
from app.ui.qt.extension_help import ExtensionHelpDialog
from app.ui.qt.tour import DEFAULT_STEPS, TourOverlay

log = logging.getLogger(__name__)

REFRESH_MS = 1000
TIMELINE_MS = 5000


class MainWindow(QMainWindow):
    """Thin view over `AgentService.snapshot()`.

    The window never mutates agent state directly: every action goes through
    the service, and the next refresh paints the result. That keeps the UI
    honest (it shows what the agent really did) and testable.

    Widgets that carry *static* text keep their handles so `retranslate_ui()`
    can re-label the whole window in place when the language changes — no
    restart, no window rebuild. Anything computed per tick comes from
    `app.ui.viewmodel`, which reads the catalog itself.
    """

    hide_to_tray = Signal()

    def __init__(self, service: AgentService, *, tray=None,
                 autostart_tour: bool = False) -> None:
        super().__init__()
        self.service = service
        self.tray = tray
        self.tour: TourOverlay | None = None
        self.setWindowTitle(f"Time Manager {__version__}")
        self.setWindowIcon(app_icon(64))
        # Fit the screen we are actually on (768-high laptops at 125%
        # scaling have ~610 logical pixels of desktop height).
        screen = self.screen() or QGuiApplication.primaryScreen()
        available = screen.availableGeometry()
        self.resize(min(980, available.width() - 24),
                    min(720, available.height() - 64))

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 16, 18, 12)
        outer.setSpacing(12)

        # ------------------------------------------------------------- header
        header = QHBoxLayout()
        header.setSpacing(10)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.title_label = label(tr("mw.title"), "Title")
        title_box.addWidget(self.title_label)
        self.subtitle = label(tr("mw.starting"), "Subtitle")
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box)
        header.addStretch(1)

        self.health_chip = Chip(tr("mw.chip_starting"), "muted")
        header.addWidget(self.health_chip)

        self.pause_button = QPushButton(tr("mw.pause"))
        self.pause_button.setObjectName("Ghost")
        self.pause_menu = QMenu(self)
        self._pause_actions: list[tuple[int, QAction]] = []
        for minutes in (15, 30, 60, 120):
            action = QAction(tr("mw.pause_for", minutes=minutes), self)
            action.triggered.connect(lambda _=False, m=minutes: self._pause(m))
            self.pause_menu.addAction(action)
            self._pause_actions.append((minutes, action))
        self._pause_all_action = QAction(
            tr("mw.pause_until_tomorrow", hours=MAX_PAUSE_MINUTES // 60), self
        )
        self._pause_all_action.triggered.connect(lambda: self._pause(MAX_PAUSE_MINUTES))
        self.pause_menu.addAction(self._pause_all_action)
        self.pause_menu.addSeparator()
        self.resume_action = QAction(tr("mw.resume"), self)
        self.resume_action.triggered.connect(self._resume)
        self.pause_menu.addAction(self.resume_action)
        self.pause_button.setMenu(self.pause_menu)
        header.addWidget(self.pause_button)

        self.new_rule_button = QPushButton(tr("mw.new_rule"))
        self.new_rule_button.clicked.connect(self._new_rule)
        header.addWidget(self.new_rule_button)

        # Language switch, right in the header: the button shows the language
        # you would switch *to*, so one click always does what it says.
        self.language_button = QPushButton()
        self.language_button.setObjectName("Ghost")
        self.language_button.setToolTip(tr("tray.language"))
        self.language_menu = QMenu(self)
        self._language_group = QActionGroup(self)
        self._language_group.setExclusive(True)
        self._language_actions: dict[str, QAction] = {}
        for code, name in language_choices():
            action = QAction(name, self)
            action.setCheckable(True)
            action.triggered.connect(lambda _=False, c=code: self._switch_language(c))
            self._language_group.addAction(action)
            self.language_menu.addAction(action)
            self._language_actions[code] = action
        self.language_button.setMenu(self.language_menu)
        header.addWidget(self.language_button)

        self.guide_button = QPushButton(tr("mw.guide"))
        self.guide_button.setObjectName("Ghost")
        self.guide_button.clicked.connect(self.start_tour)
        header.addWidget(self.guide_button)

        self.settings_button = QPushButton(tr("mw.settings"))
        self.settings_button.setObjectName("Ghost")
        self.settings_button.clicked.connect(self._open_settings)
        header.addWidget(self.settings_button)
        outer.addLayout(header)

        self.banner = label("", "Banner", wrap=True)
        self.banner.setVisible(False)
        outer.addWidget(self.banner)

        # Phase 7: anti-bypass findings, shown above the rules when non-empty.
        self.security_banner = SecurityBanner()
        self.security_banner.setVisible(False)
        outer.addWidget(self.security_banner)

        # ------------------------------------------------------------- content
        content = QHBoxLayout()
        content.setSpacing(12)

        rules_column = QVBoxLayout()
        rules_column.setSpacing(8)
        rules_header = QHBoxLayout()
        self.rules_title = label(tr("mw.rules_title"), "SectionTitle")
        rules_header.addWidget(self.rules_title)
        rules_header.addStretch(1)
        self.rules_meta = label("", "CardSub")
        rules_header.addWidget(self.rules_meta)
        rules_column.addLayout(rules_header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.rules_container = QWidget()
        self.rules_layout = QVBoxLayout(self.rules_container)
        self.rules_layout.setContentsMargins(0, 0, 6, 0)
        self.rules_layout.setSpacing(10)
        self.rules_layout.addStretch(1)
        self.scroll.setWidget(self.rules_container)
        rules_column.addWidget(self.scroll, 1)
        content.addLayout(rules_column, 3)

        side = QVBoxLayout()
        side.setSpacing(12)
        self.browser_panel = Panel(tr("mw.panel_browser"))
        # The one panel whose empty state is a puzzle ("what browser
        # extension?") gets a Help button that answers it with this install's
        # folder and token: install steps + pairing, no terminal needed.
        self.help_button = self.browser_panel.add_action(
            tr("mw.panel_help"), self._open_extension_help
        )
        side.addWidget(self.browser_panel)
        self.link_panel = Panel(tr("mw.panel_link"))
        side.addWidget(self.link_panel)
        self.detector_panel = Panel(tr("mw.panel_detectors"))
        side.addWidget(self.detector_panel)
        self.timeline_panel = Panel(tr("mw.panel_timeline"))
        side.addWidget(self.timeline_panel, 1)
        content.addLayout(side, 2)
        outer.addLayout(content, 1)

        # ------------------------------------------------------------ statusbar
        self.setStatusBar(QStatusBar())
        self.status_label = QLabel("")
        self.statusBar().addWidget(self.status_label)
        self.path_label = QLabel("")
        self.statusBar().addPermanentWidget(self.path_label)

        self._cards: dict[int, RuleCard] = {}
        self._empty: EmptyState | None = None

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start(REFRESH_MS)
        self.timeline_timer = QTimer(self)
        self.timeline_timer.timeout.connect(self.refresh_timeline)
        self.timeline_timer.start(TIMELINE_MS)

        # Re-label in place when another window switches the language
        # (the tray menu and the settings dialog both can).
        language_manager().changed.connect(self._on_language_changed)

        self.retranslate_ui()
        self.refresh()
        self.refresh_timeline()

        if autostart_tour:
            # Let the first paint settle before dimming the window.
            QTimer.singleShot(900, self.start_tour)

    # ------------------------------------------------------- language switch
    def _switch_language(self, code: str) -> None:
        applied = apply_language_for(self.service, code)
        log.info("UI language switched to %s", applied)

    def _on_language_changed(self, _code: str) -> None:
        self.retranslate_ui()
        self.refresh()
        self.refresh_timeline()

    def retranslate_ui(self) -> None:
        """Re-apply every static string from the catalog (live, no restart)."""
        from app.i18n import current_language

        self.title_label.setText(tr("mw.title"))
        self.subtitle.setText(tr("mw.starting"))
        self.health_chip.update_text(tr("mw.chip_starting"), "muted")
        for minutes, action in self._pause_actions:
            action.setText(tr("mw.pause_for", minutes=minutes))
        self._pause_all_action.setText(
            tr("mw.pause_until_tomorrow", hours=MAX_PAUSE_MINUTES // 60)
        )
        self.resume_action.setText(tr("mw.resume"))
        self.new_rule_button.setText(tr("mw.new_rule"))
        self.guide_button.setText(tr("mw.guide"))
        self.settings_button.setText(tr("mw.settings"))
        self.rules_title.setText(tr("mw.rules_title"))
        self.browser_panel.set_title(tr("mw.panel_browser"))
        self.help_button.setText(tr("mw.panel_help"))
        self.link_panel.set_title(tr("mw.panel_link"))
        self.detector_panel.set_title(tr("mw.panel_detectors"))
        self.timeline_panel.set_title(tr("mw.panel_timeline"))

        active = current_language()
        for code, action in self._language_actions.items():
            action.setChecked(code == active)
        # The header button offers the *other* language, by its own name.
        other = "en" if active == "ar" else "ar"
        self.language_button.setText(language_name(other))
        self.language_button.setToolTip(tr("tray.language"))

        if self._empty is not None:
            self._empty.set_texts(tr("mw.empty"), tr("mw.empty_action"))
        if self.tour is not None:
            self.tour.retranslate()

    # ------------------------------------------------------------------ paint
    def refresh(self) -> None:
        snapshot = self.service.snapshot()
        self.setWindowTitle(viewmodel.window_title(snapshot))

        role, text = viewmodel.health(snapshot)
        self.health_chip.update_text(text, role)
        # Use the agent's clock: the dashboard must agree with the accounting
        # (and the demo/screenshot runs on a fake clock).
        self.subtitle.setText(
            f"{snapshot.day} · {viewmodel.format_reset(self.service.clock.local_now())}"
            f" · v{snapshot.agent_version}"
        )
        self.status_label.setText(viewmodel.status_line(snapshot))
        self.path_label.setText(snapshot.db_path)
        self.path_label.setToolTip(tr("mw.tooltip_profile", path=snapshot.profile_dir))

        banner = viewmodel.pause_banner(snapshot)
        self.banner.setText(banner or "")
        self.banner.setVisible(bool(banner))
        self.security_banner.set_notices(viewmodel.security_notices(snapshot))
        self.pause_button.setText(
            tr("mw.paused_chip",
               duration=viewmodel.format_duration(snapshot.pause_remaining_seconds))
            if snapshot.paused else tr("mw.pause")
        )
        self.resume_action.setEnabled(snapshot.paused)

        rows = viewmodel.rule_rows(snapshot)
        self._sync_cards(rows)
        self.rules_meta.setText(
            tr("mw.rules_meta", count=len(rows),
               active=sum(1 for r in rows if r.enabled))
        )

        self.browser_panel.clear()
        for line in viewmodel.browser_lines(snapshot):
            self.browser_panel.add_line(line)
        self.link_panel.clear()
        for line in viewmodel.link_lines(snapshot):
            self.link_panel.add_line(line, mono=True)
        self.detector_panel.clear()
        for line in viewmodel.detector_lines(snapshot):
            self.detector_panel.add_line(line)
        if self.tray is not None:
            self.tray.update_status(snapshot)

    @property
    def cards(self) -> dict[int, RuleCard]:
        """Rule cards by rule id — a copy, so callers cannot corrupt the view."""
        return dict(self._cards)

    def _sync_cards(self, rows) -> None:
        if not rows:
            for card in self._cards.values():
                card.setParent(None)
                card.deleteLater()
            self._cards.clear()
            if self._empty is None:
                self._empty = EmptyState(tr("mw.empty"), tr("mw.empty_action"))
                self._empty.action_button.clicked.connect(self._new_rule)
                self.rules_layout.insertWidget(0, self._empty)
            return
        if self._empty is not None:
            self._empty.setParent(None)
            self._empty.deleteLater()
            self._empty = None

        seen: set[int] = set()
        for index, row in enumerate(rows):
            card = self._cards.get(row.rule_id)
            if card is None:
                card = RuleCard(row)
                card.toggle_requested.connect(self._toggle_rule)
                card.edit_requested.connect(self._edit_rule)
                self._cards[row.rule_id] = card
                self.rules_layout.insertWidget(index, card)
            else:
                card.update_row(row)
                # keep the display order stable while numbers change
                current = self.rules_layout.indexOf(card)
                if current != index:
                    self.rules_layout.removeWidget(card)
                    self.rules_layout.insertWidget(index, card)
            seen.add(row.rule_id)
        for rule_id in list(self._cards):
            if rule_id not in seen:
                card = self._cards.pop(rule_id)
                card.setParent(None)
                card.deleteLater()

    def refresh_timeline(self) -> None:
        rows = self.service.enforcement_history(limit=40)
        self.timeline_panel.clear()
        if not rows:
            self.timeline_panel.add_line(tr("mw.nothing_enforced"))
            return
        for line in viewmodel.timeline(rows, limit=8):
            self.timeline_panel.add_line(line, mono=True)

    # ------------------------------------------------------------ guided tour
    def start_tour(self, steps=None) -> TourOverlay:
        """Highlight the dashboard step by step (shadow + arrows)."""
        if self.tour is not None:
            self.tour.skip()
            self.tour = None
        overlay = TourOverlay(self, tuple(steps) if steps else DEFAULT_STEPS)
        overlay.finished.connect(self._tour_finished)
        self.tour = overlay
        overlay.start()
        return overlay

    def _tour_finished(self, completed: bool) -> None:
        self.tour = None
        if completed:
            # Remember it so the first-run tour does not reappear every launch.
            try:
                if self.service.db is not None:
                    self.service.db.set_setting("tour_seen", True)
            except Exception:  # noqa: BLE001 - never break the UI over a flag
                log.debug("Could not store the tour flag.", exc_info=True)

    # ---------------------------------------------------------------- actions
    def _pause(self, minutes: int) -> None:
        self.service.pause(minutes)
        self.refresh()

    def _resume(self) -> None:
        self.service.resume()
        self.refresh()

    # -------------------------------------------------------------- rule edits
    def _new_rule(self) -> None:
        dialog = RuleDialog(None, parent=self)
        if dialog.exec():
            self.service.save_rule(dialog.rule())
            self.refresh()
            self.refresh_timeline()

    def _edit_rule(self, rule_id: int) -> None:
        rule = next((r for r in self.service.list_rules() if r.id == rule_id), None)
        if rule is None:
            return
        dialog = RuleDialog(rule, parent=self)
        if dialog.exec():
            self.service.save_rule(dialog.rule())
            self.refresh()

    def _toggle_rule(self, rule_id: int, enable: bool) -> None:
        self.service.set_rule_enabled(rule_id, enable)
        self.refresh()

    def _open_extension_help(self) -> None:
        """Install/pair instructions for the browser extension (Help button)."""
        ExtensionHelpDialog(self.service, parent=self).exec()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.service, parent=self)
        dialog.exec()
        self.refresh()

    # ------------------------------------------------------------------ window
    def closeEvent(self, event: QCloseEvent) -> None:
        """Closing hides to the tray; only Quit really exits (see tray menu)."""
        if self.tray is not None and self.tray.available:
            event.ignore()
            self.hide()
            self.hide_to_tray.emit()
            return
        super().closeEvent(event)

    def confirm_quit(self) -> bool:
        answer = QMessageBox.question(
            self, tr("mw.quit_title"), tr("mw.quit_body"),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return answer == QMessageBox.Yes
