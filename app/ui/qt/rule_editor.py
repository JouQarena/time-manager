"""Rule editor dialog: create/edit an app, game or website rule.

The dialog only collects input; every constraint is validated by the core
`Rule` model (the same code the database and the engine use), so a rule that
cannot exist in the file cannot be created here either.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QTime
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QScrollArea,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.rules.models import Rule
from app.core.types import Action, Mode, RuleType
from app.i18n import tr
from app.ui.qt.theme import PALETTE

#: (catalog key, payload token). The combo shows `tr(key)` and stores the
#: token, so the logic never compares translated text — that comparison was
#: the first thing Arabic would have broken.
DAY_CHOICES: tuple[tuple[str, str], ...] = (
    ("schedule.every_day", "EVERYDAY"),
    ("schedule.weekdays", "WEEKDAYS"),
    ("schedule.weekends", "WEEKENDS"),
    ("schedule.custom", "CUSTOM"),
)
DAY_TOKENS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def presets() -> list[dict]:
    """Known games from the detector registry (built-in + plugins).

    Imported lazily and defensively: the rule editor must open even if the
    detector layer is unavailable for any reason.
    """
    try:
        from app.core.detection.loader import build_host

        host, _ = build_host()
        return host.presets()
    except Exception:  # noqa: BLE001 - never block rule editing on this
        return []


#: Honest per-game caveats for the "wait for the match to end" action, so the
#: user picks the right behaviour before saving (Phase 8, docs/PHASE8.md).
#: Honest per-game caveats for "wait for the match to end". They live in the
#: catalog (`re.caveat.<detector id>`) so they are translated like everything
#: else; an unknown detector simply has no caveat.
def _preset_caveat(detector_id: str) -> str:
    key = f"re.caveat.{detector_id}"
    text = tr(key)
    return f" {text}" if text != key else ""


class RuleDialog(QDialog):
    def __init__(self, rule: Rule | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.existing = rule
        self.setWindowTitle(tr("re.title_edit") if rule else tr("re.title_new"))
        # Short screens (e.g. 1366x768 at 125% scaling leave ~610 logical
        # pixels): the form scrolls instead of pushing Save off-screen, and
        # the dialog shrinks as far as the user wants.
        self.setMinimumSize(480, 320)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 12)
        outer.setSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        form_column = QVBoxLayout(content)
        form_column.setContentsMargins(0, 0, 0, 0)
        form_column.setSpacing(12)
        scroll.setWidget(content)

        # ------------------------------------------------------------- basics
        basics = QGroupBox(tr("re.group_basics"))
        form = QFormLayout(basics)
        form.setSpacing(8)

        self.name = QLineEdit(rule.name if rule else "")
        self.name.setPlaceholderText(tr("re.name_placeholder"))
        form.addRow(tr("re.name"), self.name)

        self.type_combo = QComboBox()
        for rule_type, key in (
            (RuleType.APPLICATION, "re.type_app"),
            (RuleType.GAME, "re.type_game"),
            (RuleType.WEBSITE, "re.type_website"),
        ):
            self.type_combo.addItem(tr(key), rule_type)
        self.type_combo.currentIndexChanged.connect(self._type_changed)
        form.addRow(tr("re.type"), self.type_combo)

        # Known games come from the detector registry, so users never have to
        # guess process names (and a plugin can add its own to this list).
        known_games = presets()
        self.game_combo = QComboBox()
        self.game_combo.addItem(tr("re.pick_game"), None)
        for preset in known_games:
            self.game_combo.addItem(preset.get("name") or preset.get("id"), preset)
        self.game_combo.currentIndexChanged.connect(self._game_preset_chosen)
        self.game_combo.setVisible(bool(known_games))
        form.addRow(tr("re.known_game"), self.game_combo)

        target_row = QHBoxLayout()
        self.target = QLineEdit()
        self.target.setPlaceholderText(tr("re.target_placeholder"))
        target_row.addWidget(self.target, 1)
        self.browse = QPushButton(tr("re.browse"))
        self.browse.setObjectName("Ghost")
        self.browse.clicked.connect(self._browse)
        target_row.addWidget(self.browse)
        holder = QWidget()
        holder.setLayout(target_row)
        form.addRow(tr("re.target"), holder)

        self.extra = QLineEdit()
        self.extra.setPlaceholderText(tr("re.also_match_placeholder"))
        form.addRow(tr("re.also_match"), self.extra)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {PALETTE['text_dim']}; font-size: 11.5px;")
        form.addRow("", self.hint)
        form_column.addWidget(basics)

        # ------------------------------------------------------------- limits
        limits = QGroupBox(tr("re.group_limits"))
        form2 = QFormLayout(limits)
        form2.setSpacing(8)

        self.daily_enabled = QCheckBox(tr("re.daily"))
        self.daily_minutes = QSpinBox()
        self.daily_minutes.setRange(1, 24 * 60)
        self.daily_minutes.setSuffix(" min")
        daily_row = QHBoxLayout()
        daily_row.addWidget(self.daily_enabled)
        daily_row.addWidget(self.daily_minutes, 1)
        daily_holder = QWidget()
        daily_holder.setLayout(daily_row)
        form2.addRow("", daily_holder)

        self.session_enabled = QCheckBox(tr("re.session"))
        self.session_minutes = QSpinBox()
        self.session_minutes.setRange(1, 24 * 60)
        self.session_minutes.setSuffix(" min")
        session_row = QHBoxLayout()
        session_row.addWidget(self.session_enabled)
        session_row.addWidget(self.session_minutes, 1)
        session_holder = QWidget()
        session_holder.setLayout(session_row)
        form2.addRow("", session_holder)

        self.warnings = QLineEdit("10, 5, 2")
        self.warnings.setPlaceholderText(tr("re.warn_placeholder"))
        form2.addRow(tr("re.warn_me"), self.warnings)

        self.action_combo = QComboBox()
        form2.addRow(tr("re.when_limit"), self.action_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem(tr("re.mode_normal"), Mode.NORMAL)
        self.mode_combo.addItem(tr("re.mode_strict"), Mode.STRICT)
        form2.addRow(tr("re.mode"), self.mode_combo)
        form_column.addWidget(limits)

        # ----------------------------------------------------------- schedule
        schedule_box = QGroupBox(tr("re.group_schedule"))
        form3 = QFormLayout(schedule_box)
        self.always = QCheckBox(tr("re.always"))
        self.always.setChecked(True)
        self.always.toggled.connect(self._schedule_toggled)
        form3.addRow("", self.always)

        self.days_combo = QComboBox()
        for key, token in DAY_CHOICES:
            self.days_combo.addItem(tr(key), token)
        self.days_combo.currentIndexChanged.connect(self._schedule_toggled)
        form3.addRow(tr("re.days"), self.days_combo)

        self.custom_days = QWidget()
        custom_row = QHBoxLayout(self.custom_days)
        custom_row.setContentsMargins(0, 0, 0, 0)
        self.day_boxes: dict[str, QCheckBox] = {}
        for token in DAY_TOKENS:
            box = QCheckBox(tr(f"day.{token}"))
            self.day_boxes[token] = box
            custom_row.addWidget(box)
        form3.addRow("", self.custom_days)

        window_row = QHBoxLayout()
        self.start_time = QTimeEdit(QTime(18, 0))
        self.start_time.setDisplayFormat("HH:mm")
        self.end_time = QTimeEdit(QTime(22, 0))
        self.end_time.setDisplayFormat("HH:mm")
        self.window_enabled = QCheckBox(tr("re.only_between"))
        window_row.addWidget(self.window_enabled)
        window_row.addWidget(self.start_time)
        window_row.addWidget(QLabel(tr("fmt.and")))
        window_row.addWidget(self.end_time)
        window_row.addStretch(1)
        window_holder = QWidget()
        window_holder.setLayout(window_row)
        form3.addRow("", window_holder)
        form_column.addWidget(schedule_box)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #e05252; font-size: 12px;")
        self.error.setVisible(False)
        form_column.addWidget(self.error)
        form_column.addStretch(1)

        outer.addWidget(scroll)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        # Open comfortably inside whatever screen we are given.
        screen = self.screen() or QGuiApplication.primaryScreen()
        available = screen.availableGeometry()
        self.resize(min(620, available.width() - 32),
                    min(760, available.height() - 64))

        if rule:
            self._load(rule)
        self._type_changed()
        self._schedule_toggled()  # always: day boxes start hidden, fields disabled

    # ------------------------------------------------------------------ setup
    def _load(self, rule: Rule) -> None:
        index = self.type_combo.findData(rule.type)
        if index >= 0:
            self.type_combo.setCurrentIndex(index)
        # Website rules: show the normalized domain, not whatever was typed
        # (a pasted URL would teach the wrong shape; the domain is what
        # actually matches — and the shortest form, e.g. reddit.com, is the
        # one that covers every subdomain).
        self.target.setText(rule.domain or rule.target)
        self.extra.setText(", ".join(rule.extra_executables))
        if rule.daily_limit_seconds:
            self.daily_enabled.setChecked(True)
            self.daily_minutes.setValue(max(1, rule.daily_limit_seconds // 60))
        if rule.session_limit_seconds:
            self.session_enabled.setChecked(True)
            self.session_minutes.setValue(max(1, rule.session_limit_seconds // 60))
        self.warnings.setText(", ".join(str(w // 60) for w in rule.warning_seconds))
        index = self.action_combo.findData(rule.action)
        if index >= 0:
            self.action_combo.setCurrentIndex(index)
        self.mode_combo.setCurrentIndex(0 if rule.mode == Mode.NORMAL else 1)
        if rule.schedule is not None:
            self.always.setChecked(False)
            days = rule.schedule.days
            if days is None or len(days) == 7:
                self._select_days("EVERYDAY")
            elif days == frozenset({"MON", "TUE", "WED", "THU", "FRI"}):
                self._select_days("WEEKDAYS")
            elif days == frozenset({"SAT", "SUN"}):
                self._select_days("WEEKENDS")
            else:
                self._select_days("CUSTOM")
                for token in days:
                    if token in self.day_boxes:
                        self.day_boxes[token].setChecked(True)
            if rule.schedule.windows:
                start, end = rule.schedule.windows[0]
                self.window_enabled.setChecked(True)
                self.start_time.setTime(QTime(start.hour, start.minute))
                self.end_time.setTime(QTime(end.hour, end.minute))
        self._schedule_toggled()

    def _game_preset_chosen(self, index: int) -> None:
        preset = self.game_combo.itemData(index)
        if not isinstance(preset, dict):
            return
        executables = list(preset.get("executables") or ())
        if not executables:
            return
        self.type_combo.setCurrentIndex(self.type_combo.findData(RuleType.GAME))
        self.target.setText(str(executables[0]))
        self.extra.setText(", ".join(str(e) for e in executables[1:]))
        if self.name.text().strip() in ("", tr("re.title_new")):
            self.name.setText(str(preset.get("name") or preset.get("id")))
        self.hint.setText(
            tr("re.preset_hint", name=preset.get("name"), id=preset.get("id"))
            + _preset_caveat(str(preset.get("id")))
        )

    def _type_changed(self) -> None:
        rule_type: RuleType = self.type_combo.currentData()
        is_site = rule_type == RuleType.WEBSITE
        self.browse.setVisible(not is_site)
        self.browse.setEnabled(not is_site)
        self.extra.setEnabled(not is_site)
        self.game_combo.setVisible(not is_site and self.game_combo.count() > 1)
        self.target.setPlaceholderText(
            "youtube.com" if is_site else
            ("league of legends.exe" if rule_type == RuleType.GAME else "discord.exe")
        )
        self.hint.setText(tr({
            RuleType.WEBSITE: "re.hint_website",
            RuleType.GAME: "re.hint_game",
            RuleType.APPLICATION: "re.hint_app",
        }[rule_type]))

        # actions valid for this type
        current = self.action_combo.currentData()
        self.action_combo.clear()
        if is_site:
            self.action_combo.addItem(tr("re.action_block_site"), Action.BLOCK)
            self.action_combo.addItem(tr("re.action_warn"), Action.WARN_ONLY)
        else:
            if rule_type == RuleType.GAME:
                self.action_combo.addItem(tr("re.action_wait"),
                                          Action.WAIT_FOR_SESSION_END)
            self.action_combo.addItem(tr("re.action_close"), Action.CLOSE)
            self.action_combo.addItem(tr("re.action_prevent"), Action.BLOCK)
            self.action_combo.addItem(tr("re.action_warn"), Action.WARN_ONLY)
        index = self.action_combo.findData(current) if current else -1
        if index >= 0:
            self.action_combo.setCurrentIndex(index)

    def _select_days(self, token: str) -> None:
        """Select a days-combo entry by its payload token (never by text)."""
        index = self.days_combo.findData(token)
        if index >= 0:
            self.days_combo.setCurrentIndex(index)

    def _schedule_toggled(self) -> None:
        enabled = not self.always.isChecked()
        for widget in (self.days_combo, self.window_enabled, self.start_time,
                       self.end_time, self.custom_days):
            widget.setEnabled(enabled)
        self.custom_days.setVisible(enabled and self.days_combo.currentData() == "CUSTOM")

    def _browse(self) -> None:
        start = os.environ.get("WINDIR", "") or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, tr("re.pick_app_title"), start, tr("re.pick_app_filter")
        )
        if not path:
            return
        self.target.setText(os.path.basename(path).lower())
        if not self.name.text().strip():
            self.name.setText(os.path.splitext(os.path.basename(path))[0].title())

    # ------------------------------------------------------------------- save
    def _parse_warnings(self) -> tuple[int, ...]:
        raw = self.warnings.text().replace(";", ",")
        values: list[int] = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            values.append(int(float(chunk) * 60) if "." in chunk else int(chunk) * 60)
        return tuple(sorted({v for v in values if v > 0}, reverse=True))

    def _schedule_payload(self) -> dict | None:
        if self.always.isChecked():
            return None
        days_choice = self.days_combo.currentText()
        if days_choice == "Custom…":
            days = ",".join(t for t, box in self.day_boxes.items() if box.isChecked())
            if not days:
                raise ValueError("Pick at least one day.")
        else:
            days = DAY_CHOICES[days_choice]
        payload: dict = {"days": days}
        if self.window_enabled.isChecked():
            if self.start_time.time() == self.end_time.time():
                raise ValueError(tr("re.err_window_order"))
            payload["windows"] = [[
                self.start_time.time().toString("HH:mm"),
                self.end_time.time().toString("HH:mm"),
            ]]
        return payload

    def rule(self) -> Rule:
        """Build the Rule from the form (raises ValueError when invalid)."""
        rule_type: RuleType = self.type_combo.currentData()
        target = self.target.text().strip()
        if not target:
            raise ValueError(tr("re.err_need_target"))
        if not self.name.text().strip():
            raise ValueError(tr("re.err_need_name"))

        daily = self.daily_minutes.value() * 60 if self.daily_enabled.isChecked() else None
        session = (self.session_minutes.value() * 60
                   if self.session_enabled.isChecked() else None)
        if daily is None and session is None:
            raise ValueError(tr("re.err_need_limits"))

        extra = tuple(
            part.strip() for part in self.extra.text().split(",") if part.strip()
        )
        kwargs = dict(
            name=self.name.text().strip(),
            type=rule_type,
            target=target,
            action=self.action_combo.currentData(),
            mode=self.mode_combo.currentData(),
            daily_limit_seconds=daily,
            session_limit_seconds=session,
            warning_seconds=self._parse_warnings(),
            schedule=self._schedule_payload(),
            enabled=self.existing.enabled if self.existing else True,
            id=self.existing.id if self.existing else None,
        )
        if rule_type == RuleType.WEBSITE:
            kwargs["domain"] = target
        else:
            kwargs["executable"] = target
            kwargs["extra_executables"] = extra
        return Rule(**kwargs)  # validation happens here, on purpose

    def _save(self) -> None:
        try:
            rule = self.rule()
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.setVisible(True)
            return
        # A rule that passes validation is stored by the caller; show a summary
        # so the user sees exactly what was accepted.
        self.error.setVisible(False)
        from app.ui.viewmodel import action_label

        QMessageBox.information(
            self, tr("re.saved_title"),
            tr(
                "re.saved_body",
                name=rule.name,
                type=tr(f"type.{rule.type.value.lower()}"),
                target=rule.target,
                daily=self._minutes_text(rule.daily_limit_seconds),
                session=self._minutes_text(rule.session_limit_seconds),
                action=action_label(rule),
                mode=(tr("re.mode_strict_short") if rule.mode == Mode.STRICT
                      else tr("re.mode_normal_short")),
            ),
        )
        self.accept()

    @staticmethod
    def _minutes_text(seconds: int | None) -> str:
        if seconds is None:
            return tr("re.none")
        return tr("re.minutes", minutes=seconds // 60)
