"""The dashboard window: live status, per-rule usage, audit trail."""

from __future__ import annotations

import logging

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent
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
from app.service import AgentService
from app.ui import viewmodel
from app.ui.qt.icons import app_icon
from app.ui.qt.widgets import Chip, EmptyState, Panel, RuleCard, SecurityBanner, label
from app.ui.qt.rule_editor import RuleDialog
from app.ui.qt.settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

REFRESH_MS = 1000
TIMELINE_MS = 5000


class MainWindow(QMainWindow):
    """Thin view over `AgentService.snapshot()`.

    The window never mutates agent state directly: every action goes through
    the service, and the next refresh paints the result. That keeps the UI
    honest (it shows what the agent really did) and testable.
    """

    hide_to_tray = Signal()

    def __init__(self, service: AgentService, *, tray=None) -> None:
        super().__init__()
        self.service = service
        self.tray = tray
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
        title_box.addWidget(label("Time Manager", "Title"))
        self.subtitle = label("starting…", "Subtitle")
        title_box.addWidget(self.subtitle)
        header.addLayout(title_box)
        header.addStretch(1)

        self.health_chip = Chip("Starting", "muted")
        header.addWidget(self.health_chip)

        self.pause_button = QPushButton("Pause")
        self.pause_button.setObjectName("Ghost")
        pause_menu = QMenu(self)
        for minutes in (15, 30, 60, 120):
            action = QAction(f"Pause for {minutes} minutes", self)
            action.triggered.connect(lambda _=False, m=minutes: self._pause(m))
            pause_menu.addAction(action)
        action_all = QAction(f"Pause until tomorrow (max {MAX_PAUSE_MINUTES // 60}h)", self)
        action_all.triggered.connect(lambda: self._pause(MAX_PAUSE_MINUTES))
        pause_menu.addAction(action_all)
        pause_menu.addSeparator()
        self.resume_action = QAction("Resume tracking", self)
        self.resume_action.triggered.connect(self._resume)
        pause_menu.addAction(self.resume_action)
        self.pause_button.setMenu(pause_menu)
        header.addWidget(self.pause_button)

        new_rule = QPushButton("New rule")
        new_rule.clicked.connect(self._new_rule)
        header.addWidget(new_rule)

        settings_button = QPushButton("Settings")
        settings_button.setObjectName("Ghost")
        settings_button.clicked.connect(self._open_settings)
        header.addWidget(settings_button)
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
        rules_header.addWidget(label("Your rules", "SectionTitle"))
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
        self.browser_panel = Panel("Browser extension")
        side.addWidget(self.browser_panel)
        self.link_panel = Panel("Agent link")
        side.addWidget(self.link_panel)
        self.detector_panel = Panel("Game detectors")
        side.addWidget(self.detector_panel)
        self.timeline_panel = Panel("Recent enforcement")
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

        self.refresh()
        self.refresh_timeline()

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
        self.path_label.setToolTip(f"profile: {snapshot.profile_dir}")

        banner = viewmodel.pause_banner(snapshot)
        self.banner.setText(banner or "")
        self.banner.setVisible(bool(banner))
        self.security_banner.set_notices(viewmodel.security_notices(snapshot))
        self.pause_button.setText(
            f"Paused · {viewmodel.format_duration(snapshot.pause_remaining_seconds)}"
            if snapshot.paused else "Pause"
        )
        self.resume_action.setEnabled(snapshot.paused)

        rows = viewmodel.rule_rows(snapshot)
        self._sync_cards(rows)
        self.rules_meta.setText(
            f"{len(rows)} rule(s) · {sum(1 for r in rows if r.enabled)} active"
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
                self._empty = EmptyState(
                    "No rules yet.\n\nTrack an app, a game or a website to give it a "
                    "daily or per-session limit.",
                    "Create your first rule",
                )
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
            self.timeline_panel.add_line("Nothing enforced yet today.")
            return
        for line in viewmodel.timeline(rows, limit=8):
            self.timeline_panel.add_line(line, mono=True)

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
            self, "Quit Time Manager",
            "Quit the agent?\n\nMonitoring and enforcement stop until you start it again.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return answer == QMessageBox.Yes
