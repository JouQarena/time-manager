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
from app.ui.qt.theme import PALETTE

DAY_CHOICES = {
    "Every day": "EVERYDAY",
    "Weekdays (Mon–Fri)": "WEEKDAYS",
    "Weekends (Sat–Sun)": "WEEKENDS",
    "Custom…": "CUSTOM",
}
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
PRESET_CAVEATS = {
    "valorant": " VALORANT's menu and match share one process, so 'wait' "
                "means the limit applies once the game is closed. Pick Close "
                "or Block instead if you want the limit immediately.",
    "repo": " R.E.P.O. has no local menu/match signal, so 'wait' means the "
            "limit applies once the game is closed. Pick Close or Block "
            "instead if you want the limit immediately.",
    "teamfight_tactics": " TFT shares its match process with League of "
                         "Legends, so the limit waits while any League match "
                         "is running (it never closes the wrong game).",
}


def _preset_caveat(detector_id: str) -> str:
    return PRESET_CAVEATS.get(detector_id, "")


class RuleDialog(QDialog):
    def __init__(self, rule: Rule | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.existing = rule
        self.setWindowTitle("Edit rule" if rule else "New rule")
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
        basics = QGroupBox("What to limit")
        form = QFormLayout(basics)
        form.setSpacing(8)

        self.name = QLineEdit(rule.name if rule else "")
        self.name.setPlaceholderText("e.g. Discord, League of Legends, YouTube")
        form.addRow("Name", self.name)

        self.type_combo = QComboBox()
        for rule_type, text in (
            (RuleType.APPLICATION, "Application (exe)"),
            (RuleType.GAME, "Game"),
            (RuleType.WEBSITE, "Website (domain)"),
        ):
            self.type_combo.addItem(text, rule_type)
        self.type_combo.currentIndexChanged.connect(self._type_changed)
        form.addRow("Type", self.type_combo)

        # Known games come from the detector registry, so users never have to
        # guess process names (and a plugin can add its own to this list).
        known_games = presets()
        self.game_combo = QComboBox()
        self.game_combo.addItem("Choose a known game…", None)
        for preset in known_games:
            self.game_combo.addItem(preset.get("name") or preset.get("id"), preset)
        self.game_combo.currentIndexChanged.connect(self._game_preset_chosen)
        self.game_combo.setVisible(bool(known_games))
        form.addRow("Known game", self.game_combo)

        target_row = QHBoxLayout()
        self.target = QLineEdit()
        self.target.setPlaceholderText("discord.exe  •  league of legends.exe  •  youtube.com")
        target_row.addWidget(self.target, 1)
        self.browse = QPushButton("Browse…")
        self.browse.setObjectName("Ghost")
        self.browse.clicked.connect(self._browse)
        target_row.addWidget(self.browse)
        holder = QWidget()
        holder.setLayout(target_row)
        form.addRow("Target", holder)

        self.extra = QLineEdit()
        self.extra.setPlaceholderText("optional: extra exes, comma-separated "
                                      "(launcher, helper processes)")
        form.addRow("Also match", self.extra)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {PALETTE['text_dim']}; font-size: 11.5px;")
        form.addRow("", self.hint)
        form_column.addWidget(basics)

        # ------------------------------------------------------------- limits
        limits = QGroupBox("Limits")
        form2 = QFormLayout(limits)
        form2.setSpacing(8)

        self.daily_enabled = QCheckBox("Daily limit")
        self.daily_minutes = QSpinBox()
        self.daily_minutes.setRange(1, 24 * 60)
        self.daily_minutes.setSuffix(" min")
        daily_row = QHBoxLayout()
        daily_row.addWidget(self.daily_enabled)
        daily_row.addWidget(self.daily_minutes, 1)
        daily_holder = QWidget()
        daily_holder.setLayout(daily_row)
        form2.addRow("", daily_holder)

        self.session_enabled = QCheckBox("Per-session limit")
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
        self.warnings.setPlaceholderText("minutes before the limit, comma-separated")
        form2.addRow("Warn me", self.warnings)

        self.action_combo = QComboBox()
        form2.addRow("When the limit is reached", self.action_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Normal — instances already running may finish", Mode.NORMAL)
        self.mode_combo.addItem("Strict — close/block immediately and on relaunch", Mode.STRICT)
        form2.addRow("Mode", self.mode_combo)
        form_column.addWidget(limits)

        # ----------------------------------------------------------- schedule
        schedule_box = QGroupBox("Schedule")
        form3 = QFormLayout(schedule_box)
        self.always = QCheckBox("Always active")
        self.always.setChecked(True)
        self.always.toggled.connect(self._schedule_toggled)
        form3.addRow("", self.always)

        self.days_combo = QComboBox()
        self.days_combo.addItems(DAY_CHOICES)
        self.days_combo.currentIndexChanged.connect(self._schedule_toggled)
        form3.addRow("Days", self.days_combo)

        self.custom_days = QWidget()
        custom_row = QHBoxLayout(self.custom_days)
        custom_row.setContentsMargins(0, 0, 0, 0)
        self.day_boxes: dict[str, QCheckBox] = {}
        for token in DAY_TOKENS:
            box = QCheckBox(token.title())
            self.day_boxes[token] = box
            custom_row.addWidget(box)
        form3.addRow("", self.custom_days)

        window_row = QHBoxLayout()
        self.start_time = QTimeEdit(QTime(18, 0))
        self.start_time.setDisplayFormat("HH:mm")
        self.end_time = QTimeEdit(QTime(22, 0))
        self.end_time.setDisplayFormat("HH:mm")
        self.window_enabled = QCheckBox("Only between")
        window_row.addWidget(self.window_enabled)
        window_row.addWidget(self.start_time)
        window_row.addWidget(QLabel("and"))
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
                self.days_combo.setCurrentText("Every day")
            elif days == frozenset({"MON", "TUE", "WED", "THU", "FRI"}):
                self.days_combo.setCurrentText("Weekdays (Mon–Fri)")
            elif days == frozenset({"SAT", "SUN"}):
                self.days_combo.setCurrentText("Weekends (Sat–Sun)")
            else:
                self.days_combo.setCurrentText("Custom…")
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
        if self.name.text().strip() in ("", "New rule"):
            self.name.setText(str(preset.get("name") or preset.get("id")))
        self.hint.setText(
            f"{preset.get('name')} is watched by the '{preset.get('id')}' game detector: "
            "the limit can wait for a match to end instead of interrupting it."
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
        self.hint.setText({
            RuleType.WEBSITE: "Domains only, e.g. youtube.com — subdomains count too. "
                              "Blocks are enforced by the browser extension.",
            RuleType.GAME: "The game's main executable. Limits can wait for a match to "
                           "finish when the game detector knows the session state.",
            RuleType.APPLICATION: "The process name as Task Manager shows it, e.g. "
                                  "discord.exe. Closing is graceful first (WM_CLOSE).",
        }[rule_type])

        # actions valid for this type
        current = self.action_combo.currentData()
        self.action_combo.clear()
        if is_site:
            self.action_combo.addItem("Block the site in the browser", Action.BLOCK)
            self.action_combo.addItem("Only warn me", Action.WARN_ONLY)
        else:
            if rule_type == RuleType.GAME:
                self.action_combo.addItem("Wait for the match to end, then close",
                                          Action.WAIT_FOR_SESSION_END)
            self.action_combo.addItem("Close the app", Action.CLOSE)
            self.action_combo.addItem("Prevent launching it", Action.BLOCK)
            self.action_combo.addItem("Only warn me", Action.WARN_ONLY)
        index = self.action_combo.findData(current) if current else -1
        if index >= 0:
            self.action_combo.setCurrentIndex(index)

    def _schedule_toggled(self) -> None:
        enabled = not self.always.isChecked()
        for widget in (self.days_combo, self.window_enabled, self.start_time,
                       self.end_time, self.custom_days):
            widget.setEnabled(enabled)
        self.custom_days.setVisible(enabled and self.days_combo.currentText() == "Custom…")

    def _browse(self) -> None:
        start = os.environ.get("WINDIR", "") or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "Pick the application", start, "Executables (*.exe);;All files (*)"
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
                raise ValueError("The window start and end must differ.")
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
            raise ValueError("Give the rule a target (exe name or domain).")
        if not self.name.text().strip():
            raise ValueError("Give the rule a name.")

        daily = self.daily_minutes.value() * 60 if self.daily_enabled.isChecked() else None
        session = (self.session_minutes.value() * 60
                   if self.session_enabled.isChecked() else None)
        if daily is None and session is None:
            raise ValueError("Set a daily limit, a session limit, or both.")

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
        QMessageBox.information(
            self, "Rule saved",
            f"{rule.name}\n\n"
            f"{rule.type.value.title()} · {rule.target}\n"
            f"Daily: {self._minutes_text(rule.daily_limit_seconds)} · "
            f"Session: {self._minutes_text(rule.session_limit_seconds)}\n"
            f"Action: {rule.action.value} · Mode: {rule.mode.value}",
        )
        self.accept()

    @staticmethod
    def _minutes_text(seconds: int | None) -> str:
        return "none" if seconds is None else f"{seconds // 60} min"
