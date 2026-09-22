"""Settings dialog: pairing token, monitoring behaviour, startup, backup."""

from __future__ import annotations

from PySide6.QtWidgets import (    QScrollArea,

    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config.settings import save_settings
from app.service import AgentService
from app.ui.qt.theme import PALETTE


class SettingsDialog(QDialog):
    def __init__(self, service: AgentService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Time Manager settings")
        self.setMinimumSize(480, 320)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(12)

        # Scroll on short screens instead of pushing Save off-screen (same
        # treatment as the rule editor).
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        form_column = QVBoxLayout(content)
        form_column.setContentsMargins(0, 0, 0, 0)
        form_column.setSpacing(12)
        scroll.setWidget(content)

        # ------------------------------------------------------ monitoring
        monitor_box = QGroupBox("Monitoring")
        form = QFormLayout(monitor_box)
        self.interval = QSpinBox()
        self.interval.setRange(1, 60)
        self.interval.setSuffix(" s")
        self.interval.setValue(int(service.settings.monitoring_interval))
        form.addRow("Check every", self.interval)

        self.foreground_only = QCheckBox("Count an app only while it is in the foreground")
        self.foreground_only.setChecked(bool(self._setting("enforce_foreground_only", True)))
        form.addRow("", self.foreground_only)

        self.grace = QSpinBox()
        self.grace.setRange(0, 60)
        self.grace.setSuffix(" s")
        self.grace.setValue(int(self._setting("background_grace_seconds", 5) or 5))
        form.addRow("Grace after losing focus", self.grace)

        self.idle = QSpinBox()
        self.idle.setRange(0, 3600)
        self.idle.setSuffix(" s")
        self.idle.setSpecialValueText("off")
        self.idle.setValue(int(self._setting("idle_grace_seconds", 0) or 0))
        form.addRow("Ignore idle time after", self.idle)

        self.browser_foreground = QCheckBox(
            "Website time requires a browser in the foreground"
        )
        self.browser_foreground.setChecked(bool(self._setting("require_browser_foreground", True)))
        form.addRow("", self.browser_foreground)

        self.close_timeout = QSpinBox()
        self.close_timeout.setRange(0, 60)
        self.close_timeout.setSuffix(" s")
        self.close_timeout.setValue(int(self._setting("graceful_close_timeout_seconds", 6) or 6))
        form.addRow("Graceful close timeout", self.close_timeout)
        form_column.addWidget(monitor_box)

        # ------------------------------------------------------- browser link
        browser_box = QGroupBox("Browser extension")
        browser_layout = QFormLayout(browser_box)

        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(self._setting("ipc_port", 17846) or 17846))
        browser_layout.addRow("Agent port", self.port)

        token_row = QHBoxLayout()
        self.token = QLineEdit(service.token)
        self.token.setReadOnly(True)
        self.token.setEchoMode(QLineEdit.Password)
        token_row.addWidget(self.token, 1)
        self.reveal = QPushButton("Show")
        self.reveal.setObjectName("Ghost")
        self.reveal.setCheckable(True)
        self.reveal.toggled.connect(self._toggle_token)
        token_row.addWidget(self.reveal)
        self.copy_button = QPushButton("Copy")
        self.copy_button.setObjectName("Ghost")
        self.copy_button.clicked.connect(self._copy_token)
        token_row.addWidget(self.copy_button)
        holder = QWidget()
        holder.setLayout(token_row)
        browser_layout.addRow("Pairing token", holder)

        regenerate = QPushButton("Regenerate token")
        regenerate.setObjectName("Ghost")
        regenerate.clicked.connect(self._regenerate)
        browser_layout.addRow("", regenerate)

        hint = QLabel(
            "Paste this token into the extension's Options page. Regenerating "
            "invalidates the old one and restarts the browser link — you must "
            "re-pair every browser."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {PALETTE['text_dim']}; font-size: 11.5px;")
        browser_layout.addRow("", hint)
        form_column.addWidget(browser_box)

        # ------------------------------------------------------------ general
        general_box = QGroupBox("General")
        general_form = QFormLayout(general_box)
        self.startup = QCheckBox("Start Time Manager with Windows")
        self.startup.setChecked(bool(service.settings.launch_at_startup))
        general_form.addRow("", self.startup)

        self.notifications = QCheckBox("Show desktop notifications for warnings and limits")
        self.notifications.setChecked(bool(self._setting("notifications_enabled", True)))
        general_form.addRow("", self.notifications)

        backup_row = QHBoxLayout()
        backup = QPushButton("Back up now")
        backup.setObjectName("Ghost")
        backup.clicked.connect(self._backup)
        backup_row.addWidget(backup)
        open_profile = QLineEdit(str(service.db_path.parent))
        open_profile.setReadOnly(True)
        backup_row.addWidget(open_profile, 1)
        backup_holder = QWidget()
        backup_holder.setLayout(backup_row)
        general_form.addRow("Data", backup_holder)
        form_column.addWidget(general_box)

        # ------------------------------------------------- strict protection
        protect_box = QGroupBox("Strict-mode protection")
        protect_form = QFormLayout(protect_box)
        self.watchdog = QCheckBox(
            "Restart the agent if it stops (Task Scheduler watchdog, checks every minute)"
        )
        self.watchdog.setChecked(bool(service.settings.strict_watchdog))
        self.watchdog.setEnabled(service.watchdog.available)
        protect_form.addRow("", self.watchdog)
        protect_hint = QLabel(
            "Everything Time Manager does against bypassing is visible in the "
            "audit trail: STRICT rules keep their Windows startup entry (it is "
            "re-added if removed), usage cannot be reset by editing the "
            "database, and an agent that was killed reports the gap at the next "
            "start. The watchdog registers a task named \u201cTime Manager "
            "Watchdog\u201d that you can inspect or delete in Task Scheduler."
            + ("" if service.watchdog.available else
               " (The watchdog needs Windows and is unavailable on this OS.)")
        )
        protect_hint.setWordWrap(True)
        protect_hint.setStyleSheet(f"color: {PALETTE['text_dim']}; font-size: 11.5px;")
        protect_form.addRow("", protect_hint)
        form_column.addWidget(protect_box)

        self.message = QLabel()
        self.message.setWordWrap(True)
        form_column.addWidget(self.message)
        form_column.addStretch(1)

        outer.addWidget(scroll)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ------------------------------------------------------------------ helpers
    def _setting(self, key: str, default=None):
        assert self.service.db is not None
        return self.service.db.get_setting(key, default)

    def _set_setting(self, key: str, value) -> None:
        assert self.service.db is not None
        self.service.db.set_setting(key, value)

    def _toggle_token(self, revealed: bool) -> None:
        self.token.setEchoMode(QLineEdit.Normal if revealed else QLineEdit.Password)
        self.reveal.setText("Hide" if revealed else "Show")

    def _copy_token(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.service.token)
        self.message.setText("Token copied to the clipboard.")
        self.message.setStyleSheet(f"color: {PALETTE['text_dim']};")

    def _regenerate(self) -> None:
        answer = QMessageBox.question(
            self, "Regenerate token",
            "Browsers connected with the current token will be disconnected and "
            "must be re-paired.\n\nContinue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        token = self.service.regenerate_token()
        self.token.setText(token)
        self.message.setText("New token issued; the browser link restarted. "
                             "Re-paste it in the extension options.")
        self.message.setStyleSheet("color: #f0b429;")

    def _backup(self) -> None:
        try:
            target = self.service.backup()
        except Exception as exc:  # noqa: BLE001 - report, never crash the dialog
            self.message.setText(f"Backup failed: {exc}")
            self.message.setStyleSheet("color: #e05252;")
            return
        self.message.setText(f"Backup written to {target}" if target else "Nothing to back up yet.")
        self.message.setStyleSheet(f"color: {PALETTE['text_dim']};")

    def _save(self) -> None:
        settings = self.service.settings
        settings.monitoring_interval = float(self.interval.value())
        settings.launch_at_startup = self.startup.isChecked()
        save_settings(settings)

        self._set_setting("enforce_foreground_only", self.foreground_only.isChecked())
        self._set_setting("background_grace_seconds", self.grace.value())
        self._set_setting("idle_grace_seconds", self.idle.value())
        self._set_setting("require_browser_foreground", self.browser_foreground.isChecked())
        self._set_setting("graceful_close_timeout_seconds", self.close_timeout.value())
        self._set_setting("notifications_enabled", self.notifications.isChecked())
        self._set_setting("ipc_port", self.port.value())

        # Autostart is a Windows registry entry; report what actually happened.
        startup_note = ""
        try:
            from app.windows import startup as startup_mod

            ok = startup_mod.sync_with_setting(self.startup.isChecked())
            if self.startup.isChecked() and not ok:
                startup_note = " (autostart is only available on Windows)"
        except Exception as exc:  # noqa: BLE001
            startup_note = f" (autostart not configured: {exc})"

        # Watchdog: install/remove the scheduled task immediately and say so.
        watchdog_note = ""
        if self.watchdog.isEnabled():
            result = self.service.set_watchdog_enabled(self.watchdog.isChecked())
            if not result.ok and result.action not in ("NOT_PRESENT",):
                watchdog_note = f" (watchdog: {result.action.lower()} — {result.detail})"

        self.message.setText(
            "Saved. Monitoring changes apply on the next agent start."
            " Restart the agent for the port change to take effect."
            + startup_note + watchdog_note
        )
        self.message.setStyleSheet(f"color: {PALETTE['text_dim']};")
