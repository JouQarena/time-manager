"""Small reusable widgets: status chip, rule card, section panels."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.i18n import tr
from app.ui.qt.theme import PALETTE, color
from app.ui.viewmodel import RuleRow


def label(text: str = "", object_name: str = "", *, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if object_name:
        widget.setObjectName(object_name)
    widget.setWordWrap(wrap)
    return widget


class Chip(QLabel):
    """A pill-shaped status chip whose text/colour can be updated live."""

    def __init__(self, text: str = "", role: str = "muted") -> None:
        super().__init__(text)
        self.setObjectName("Chip")
        self.set_role(role)

    def set_role(self, role: str) -> None:
        self.setStyleSheet(
            f"QLabel#Chip {{ background: {PALETTE['panel_alt']};"
            f" border: 1px solid {color(role)}; color: {color(role)};"
            f" border-radius: 10px; padding: 3px 10px; font-size: 12px; }}"
        )

    def update_text(self, text: str, role: str) -> None:
        self.setText(text)
        self.set_role(role)


class RuleCard(QFrame):
    """One rule: title, subtitle, usage numbers, progress bar, quick actions."""

    toggle_requested = Signal(int, bool)
    edit_requested = Signal(int)

    def __init__(self, row: RuleRow, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        self.rule_id = row.rule_id
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(10)
        self.title = label(row.name, "CardTitle")
        header.addWidget(self.title)
        self.state_chip = Chip(row.state_text, row.state_role)
        header.addWidget(self.state_chip)
        header.addStretch(1)
        self._enabled = row.enabled
        self.edit_button = QPushButton(tr("card.edit"))
        self.edit_button.setObjectName("Ghost")
        self.toggle_button = QPushButton(
            tr("card.disable") if row.enabled else tr("card.enable")
        )
        self.toggle_button.setObjectName("Ghost")
        header.addWidget(self.edit_button)
        header.addWidget(self.toggle_button)
        outer.addLayout(header)

        self.subtitle = label(row.subtitle, "CardSub")
        outer.addWidget(self.subtitle)

        usage = QHBoxLayout()
        self.used = label(f"{row.used_text} / {row.limit_text}", "CardUsed")
        usage.addWidget(self.used)
        usage.addStretch(1)
        self.remaining = label(row.remaining_text, "CardSub")
        usage.addWidget(self.remaining)
        outer.addLayout(usage)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        outer.addWidget(self.progress)

        self.footnote = label(row.session_text, "CardSub")
        outer.addWidget(self.footnote)

        self.edit_button.clicked.connect(lambda: self.edit_requested.emit(self.rule_id))
        # The label is translated, so the *state* decides the next action —
        # comparing button text to "Disable" would break in any other language.
        self.toggle_button.clicked.connect(
            lambda: self.toggle_requested.emit(self.rule_id, not self._enabled)
        )
        self.update_row(row)

    def update_row(self, row: RuleRow) -> None:
        self.title.setText(row.name)
        self.subtitle.setText(row.subtitle)
        self.state_chip.update_text(row.state_text, row.state_role)
        self.used.setText(f"{row.used_text} / {row.limit_text}")
        self.remaining.setText(row.remaining_text)
        self.footnote.setText(
            row.session_text + (tr("card.paused_suffix") if row.paused else "")
        )
        percent = int(round(row.progress * 100))
        self.progress.setValue(percent)
        chunk = color("bad" if percent >= 100 else "warn" if percent >= 80 else "info")
        self.progress.setStyleSheet(
            f"QProgressBar {{ background: #232842; border: 0; border-radius: 4px; }}"
            f"QProgressBar::chunk {{ background: {chunk}; border-radius: 4px; }}"
        )
        self._enabled = row.enabled
        self.edit_button.setText(tr("card.edit"))
        self.toggle_button.setText(
            tr("card.disable") if row.enabled else tr("card.enable")
        )


class Panel(QFrame):
    """Titled panel with a vertical body layout; `body` is for content."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        # Title row: the section title, plus room for one small action button
        # ("Help" on the browser panel) so panels can explain themselves.
        header = QHBoxLayout()
        header.setSpacing(8)
        self.title_label = label(title.upper(), "SectionTitle")
        header.addWidget(self.title_label)
        header.addStretch(1)
        outer.addLayout(header)
        self._header = header
        self.body = QVBoxLayout()
        self.body.setSpacing(6)
        self.body.addStretch(1)  # keeps content at the top of the panel
        outer.addLayout(self.body)

    def add_action(self, text: str, slot) -> QPushButton:
        """A small ghost button on the title row; returned for re-labelling."""
        button = QPushButton(text)
        button.setObjectName("Ghost")
        button.setCursor(Qt.PointingHandCursor)
        # Tighter than the standard button so the title row keeps its height.
        button.setStyleSheet(
            "QPushButton#Ghost { padding: 2px 10px; font-size: 11.5px; }"
        )
        button.clicked.connect(slot)
        self._header.addWidget(button)
        return button

    def set_title(self, title: str) -> None:
        """Re-label the panel (language switch, no rebuild)."""
        self.title_label.setText(title.upper())

    def clear(self) -> None:
        while self.body.count():
            item = self.body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Hide *now*, delete later. `deleteLater()` alone leaves the
                # old label painted until the event loop gets around to it, so
                # a repaint in between (the language switch re-labels a panel
                # and grabs a frame, the dashboard refreshes once a second)
                # shows the outgoing and the incoming text overlapping.
                widget.hide()
                widget.deleteLater()
        self.body.addStretch(1)

    def add_line(self, text: str, *, object_name: str = "CardSub", mono: bool = False) -> QLabel:
        widget = label(text, "Mono" if mono else object_name, wrap=True)
        widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.insertWidget(self.body.count() - 1, widget)  # before the stretch
        return widget

    def add_widget(self, widget: QWidget) -> None:
        self.body.insertWidget(self.body.count() - 1, widget)

    def lines(self) -> list[str]:
        """The text of every label in the panel, top to bottom."""
        out: list[str] = []
        for index in range(self.body.count()):
            widget = self.body.itemAt(index).widget()
            if isinstance(widget, QLabel):
                out.append(widget.text())
        return out


class SecurityBanner(QFrame):
    """Phase 7: stacked anti-bypass notices. Hidden entirely when all clear.

    The dashboard must make bypass findings impossible to miss: an unclean
    stop, erased usage, a silent extension or a rolled-back clock each render
    here in their severity colour until the situation is resolved.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SecurityBanner")
        # One f-string on purpose: a plain "..." literal would keep its `}}`
        # doubled and the whole stylesheet would fail to parse (Qt then falls
        # back to the light default background).
        self.setStyleSheet(
            f"QFrame#SecurityBanner {{ background: {PALETTE['panel_alt']};"
            f" border-radius: 8px; }}"
        )
        self._stack = QVBoxLayout(self)
        self._stack.setContentsMargins(14, 10, 14, 10)
        self._stack.setSpacing(4)

    def set_notices(self, notices: list[tuple[str, str]]) -> None:
        while self._stack.count():
            item = self._stack.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()          # see Panel.clear() for the why
                widget.deleteLater()
        for role, text in notices:
            line = QLabel("\u26A0  " + text)
            line.setWordWrap(True)
            line.setTextInteractionFlags(Qt.TextSelectableByMouse)
            line.setStyleSheet(f"color: {color(role)}; font-size: 13px;")
            self._stack.addWidget(line)
        self.setVisible(bool(notices))

    def notices(self) -> list[str]:
        out: list[str] = []
        for index in range(self._stack.count()):
            widget = self._stack.itemAt(index).widget()
            if isinstance(widget, QLabel):
                out.append(widget.text())
        return out


class EmptyState(QFrame):
    def __init__(self, text: str, action_text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 24, 20, 24)
        layout.setSpacing(10)
        self.body_label = label(text, "CardSub", wrap=True)
        self.body_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.body_label)
        self.action_button = QPushButton(action_text or "—")
        self.action_button.setVisible(bool(action_text))
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.action_button)
        row.addStretch(1)
        layout.addLayout(row)

    def set_texts(self, text: str, action_text: str) -> None:
        """Re-apply the wording after a language switch."""
        self.body_label.setText(text)
        self.action_button.setText(action_text or "—")
        self.action_button.setVisible(bool(action_text))
