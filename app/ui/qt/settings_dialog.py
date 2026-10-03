"""Settings dialog: language, pairing token, monitoring, startup, backup."""

from __future__ import annotations

from PySide6.QtWidgets import (    QScrollArea,

    QCheckBox,
    QComboBox,
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
from app.i18n import current_language, tr
from app.service import AgentService
from app.ui.qt.language import (apply_language_for, language_choices,
                                language_manager)
from app.ui.qt.theme import PALETTE


def _hint(text: str) -> QLabel:
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setStyleSheet(f"color: {PALETTE['text_dim']}; font-size: 11.5px;")
    return widget


class SettingsDialog(QDialog):
    def __init__(self, service: AgentService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.setMinimumSize(480, 320)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(12)

        # Scroll on short screens instead of pushing Save off-screen (same
        # treatment as the rule editor).
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._content = QWidget()
        self._form_column = QVBoxLayout(self._content)
        self._form_column.setContentsMargins(0, 0, 0, 0)
        self._form_column.setSpacing(12)
        scroll.setWidget(self._content)

        # ----------------------------------------------------------- language
        self.language_box = QGroupBox()
        language_form = QFormLayout(self.language_box)
        self.language_combo = QComboBox()
        for code, name in language_choices():
            self.language_combo.addItem(name, code)
        index = self.language_combo.findData(current_language())
        if index >= 0:
            self.language_combo.setCurrentIndex(index)
        # Live switch: the dialog (and the whole app) re-labels immediately,
        # so the user sees exactly what they are choosing.
        self.language_combo.currentIndexChanged.connect(self._language_chosen)
        self.language_label = QLabel()
        language_form.addRow(self.language_label, self.language_combo)
        self.language_hint = _hint(tr("set.language_hint"))
        language_form.addRow("", self.language_hint)
        self._form_column.addWidget(self.language_box)

        # ------------------------------------------------------ monitoring
        self.monitor_box = QGroupBox()
        form = QFormLayout(self.monitor_box)
        self.interval = QSpinBox()
        self.interval.setRange(1, 60)
        self.interval.setSuffix(" s")
        self.interval.setValue(int(service.settings.monitoring_interval))
        self._interval_label = QLabel()
        form.addRow(self._interval_label, self.interval)

        self.foreground_only = QCheckBox()
        self.foreground_only.setChecked(bool(self._setting("enforce_foreground_only", True)))
        form.addRow("", self.foreground_only)

        self.grace = QSpinBox()
        self.grace.setRange(0, 60)
        self.grace.setSuffix(" s")
        self.grace.setValue(int(self._setting("background_grace_seconds", 5) or 5))
        self._grace_label = QLabel()
        form.addRow(self._grace_label, self.grace)

        self.idle = QSpinBox()
        self.idle.setRange(0, 3600)
        self.idle.setSuffix(" s")
        self.idle.setValue(int(self._setting("idle_grace_seconds", 0) or 0))
        self._idle_label = QLabel()
        form.addRow(self._idle_label, self.idle)

        self.browser_foreground = QCheckBox()
        self.browser_foreground.setChecked(bool(self._setting("require_browser_foreground", True)))
        form.addRow("", self.browser_foreground)

        self.close_timeout = QSpinBox()
        self.close_timeout.setRange(0, 60)
        self.close_timeout.setSuffix(" s")
        self.close_timeout.setValue(int(self._setting("graceful_close_timeout_seconds", 6) or 6))
        self._close_label = QLabel()
        form.addRow(self._close_label, self.close_timeout)
        self._form_column.addWidget(self.monitor_box)

        # ------------------------------------------------------- browser link
        self.browser_box = QGroupBox()
        browser_layout = QFormLayout(self.browser_box)

        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(self._setting("ipc_port", 17846) or 17846))
        self._port_label = QLabel()
        browser_layout.addRow(self._port_label, self.port)

        token_row = QHBoxLayout()
        self.token = QLineEdit(service.token)
        self.token.setReadOnly(True)
        self.token.setEchoMode(QLineEdit.Password)
        token_row.addWidget(self.token, 1)
        self.reveal = QPushButton()
        self.reveal.setObjectName("Ghost")
        self.reveal.setCheckable(True)
        self.reveal.toggled.connect(self._toggle_token)
        token_row.addWidget(self.reveal)
        self.copy_button = QPushButton()
        self.copy_button.setObjectName("Ghost")
        self.copy_button.clicked.connect(self._copy_token)
        token_row.addWidget(self.copy_button)
        holder = QWidget()
        holder.setLayout(token_row)
        self._token_label = QLabel()
        browser_layout.addRow(self._token_label, holder)

        self.regenerate = QPushButton()
        self.regenerate.setObjectName("Ghost")
        self.regenerate.clicked.connect(self._regenerate)
        browser_layout.addRow("", self.regenerate)

        self.browser_hint = _hint(tr("set.hint"))
        browser_layout.addRow("", self.browser_hint)
        self._form_column.addWidget(self.browser_box)

        # ------------------------------------------------------------ general
        self.general_box = QGroupBox()
        general_form = QFormLayout(self.general_box)
        self.startup = QCheckBox()
        self.startup.setChecked(bool(service.settings.launch_at_startup))
        general_form.addRow("", self.startup)

        self.notifications = QCheckBox()
        self.notifications.setChecked(bool(self._setting("notifications_enabled", True)))
        general_form.addRow("", self.notifications)

        backup_row = QHBoxLayout()
        self.backup_button = QPushButton()
        self.backup_button.setObjectName("Ghost")
        self.backup_button.clicked.connect(self._backup)
        backup_row.addWidget(self.backup_button)
        open_profile = QLineEdit(str(service.db_path.parent))
        open_profile.setReadOnly(True)
        backup_row.addWidget(open_profile, 1)
        backup_holder = QWidget()
        backup_holder.setLayout(backup_row)
        self._data_label = QLabel()
        general_form.addRow(self._data_label, backup_holder)
        self._form_column.addWidget(self.general_box)

        # ------------------------------------------------- strict protection
        self.protect_box = QGroupBox()
        protect_form = QFormLayout(self.protect_box)
        self.watchdog = QCheckBox()
        self.watchdog.setChecked(bool(service.settings.strict_watchdog))
        self.watchdog.setEnabled(service.watchdog.available)
        protect_form.addRow("", self.watchdog)
        self.protect_hint = _hint("")
        protect_form.addRow("", self.protect_hint)
        self._form_column.addWidget(self.protect_box)

        self.message = QLabel()
        self.message.setWordWrap(True)
        self._form_column.addWidget(self.message)
        self._form_column.addStretch(1)

        outer.addWidget(scroll)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

        # Re-label when the language changes from anywhere (this dialog, the
        # header globe or the tray menu) while the dialog is open.
        language_manager().changed.connect(self._on_language_changed)
        self.retranslate_ui()

    # ------------------------------------------------------- language switch
    def _language_chosen(self) -> None:
        code = self.language_combo.currentData()
        if code and code != current_language():
            apply_language_for(self.service, code)

    def _on_language_changed(self, code: str) -> None:
        index = self.language_combo.findData(code)
        if index >= 0 and index != self.language_combo.currentIndex():
            self.language_combo.blockSignals(True)
            self.language_combo.setCurrentIndex(index)
            self.language_combo.blockSignals(False)
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self.setWindowTitle(tr("set.title"))
        self.language_box.setTitle(tr("set.group_language"))
        self.language_label.setText(tr("set.language"))
        self.language_hint.setText(tr("set.language_hint"))

        self.monitor_box.setTitle(tr("set.group_monitoring"))
        self._interval_label.setText(tr("set.check_every"))
        self.foreground_only.setText(tr("set.foreground_only"))
        self._grace_label.setText(tr("set.grace"))
        self._idle_label.setText(tr("set.idle"))
        self.idle.setSpecialValueText(tr("set.idle_off"))
        self.browser_foreground.setText(tr("set.browser_foreground"))
        self._close_label.setText(tr("set.close_timeout"))

        self.browser_box.setTitle(tr("set.group_browser"))
        self._port_label.setText(tr("set.agent_port"))
        self._token_label.setText(tr("set.token"))
        self.reveal.setText(tr("set.hide") if self.reveal.isChecked() else tr("set.show"))
        self.copy_button.setText(tr("set.copy"))
        self.regenerate.setText(tr("set.regen"))
        self.browser_hint.setText(tr("set.hint"))

        self.general_box.setTitle(tr("set.group_general"))
        self.startup.setText(tr("set.startup"))
        self.notifications.setText(tr("set.notifications"))
        self.backup_button.setText(tr("set.backup_now"))
        self._data_label.setText(tr("set.data"))

        self.protect_box.setTitle(tr("set.group_strict"))
        self.watchdog.setText(tr("set.watchdog"))
        hint = tr("set.protect_hint")
        if not self.service.watchdog.available:
            hint += tr("set.protect_hint_nonwindows")
        self.protect_hint.setText(hint)

        if not self.message.text():
            self.message.setStyleSheet(f"color: {PALETTE['text_dim']};")

    # ------------------------------------------------------------------ helpers
    def _setting(self, key: str, default=None):
        assert self.service.db is not None
        return self.service.db.get_setting(key, default)

    def _set_setting(self, key: str, value) -> None:
        assert self.service.db is not None
        self.service.db.set_setting(key, value)

    def _toggle_token(self, revealed: bool) -> None:
        self.token.setEchoMode(QLineEdit.Normal if revealed else QLineEdit.Password)
        self.reveal.setText(tr("set.hide") if revealed else tr("set.show"))

    def _copy_token(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.service.token)
        self.message.setText(tr("set.token_copied"))
        self.message.setStyleSheet(f"color: {PALETTE['text_dim']};")

    def _regenerate(self) -> None:
        answer = QMessageBox.question(
            self, tr("set.regen_title"), tr("set.regen_body"),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        token = self.service.regenerate_token()
        self.token.setText(token)
        self.message.setText(tr("set.new_token"))
        self.message.setStyleSheet("color: #f0b429;")

    def _backup(self) -> None:
        try:
            target = self.service.backup()
        except Exception as exc:  # noqa: BLE001 - report, never crash the dialog
            self.message.setText(tr("set.backup_failed", error=exc))
            self.message.setStyleSheet("color: #e05252;")
            return
        self.message.setText(
            tr("set.backup_written", target=target) if target
            else tr("set.nothing_to_backup")
        )
        self.message.setStyleSheet(f"color: {PALETTE['text_dim']};")

    def _save(self) -> None:
        settings = self.service.settings
        settings.monitoring_interval = float(self.interval.value())
        settings.launch_at_startup = self.startup.isChecked()
        settings.language = current_language()
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
                startup_note = tr("set.autostart_windows_only")
        except Exception as exc:  # noqa: BLE001
            startup_note = tr("set.autostart_failed", error=exc)

        # Watchdog: install/remove the scheduled task immediately and say so.
        watchdog_note = ""
        if self.watchdog.isEnabled():
            result = self.service.set_watchdog_enabled(self.watchdog.isChecked())
            if not result.ok and result.action not in ("NOT_PRESENT",):
                watchdog_note = tr("set.watchdog_note", action=result.action.lower(),
                                   detail=result.detail)

        self.message.setText(tr("set.saved") + startup_note + watchdog_note)
        self.message.setStyleSheet(f"color: {PALETTE['text_dim']};")
