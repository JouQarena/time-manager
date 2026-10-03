"""Step-by-step help for installing and pairing the browser extension.

Reached from the *Help* button on the dashboard's **Browser extension** panel.
It says the same three things `docs/INSTALL-EXTENSION.md` says, but with the two
values a user cannot guess: **this** install's extension folder and **this**
machine's pairing token — both copyable, so the whole job can be done without a
terminal or a screenshot.

The wording is deliberately short: three steps, nothing to scroll past on a
768-pixel-high laptop screen.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.i18n import tr
from app.resources import extension_dir
from app.service import AgentService
from app.ui.qt.language import language_manager
from app.ui.qt.theme import color
from app.ui.qt.widgets import label


def _wrap(text: str, object_name: str = "CardSub") -> QLabel:
    widget = label(text, object_name, wrap=True)
    return widget


class ExtensionHelpDialog(QDialog):
    """How to load the extension in Chrome/Edge and link it to the agent."""

    def __init__(self, service: AgentService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.folder = extension_dir()
        self.setMinimumSize(520, 340)
        self.resize(620, 560)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 12)
        outer.setSpacing(10)

        # Same treatment as the settings dialog: the steps scroll on a short
        # screen (a 768-pixel-high laptop) instead of clipping their own text,
        # and the buttons below stay reachable.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._content = QWidget()
        self._body = QVBoxLayout(self._content)
        self._body.setContentsMargins(0, 0, 6, 0)
        self._body.setSpacing(8)
        scroll.setWidget(self._content)
        outer.addWidget(scroll, 1)

        body = self._body
        self.heading = label(tr("help.title"), "CardTitle")
        body.addWidget(self.heading)
        self.intro = _wrap(tr("help.intro"))
        body.addWidget(self.intro)

        # ------------------------------------------------------------- step 1
        self.step1_text = _wrap(tr("help.step1"))
        body.addWidget(self._numbered(1, self.step1_text))

        # ------------------------------------------------------------- step 2
        self.step2_text = _wrap(tr("help.step2"))
        body.addWidget(self._numbered(2, self.step2_text))
        self.path_field = QLineEdit(str(self.folder))
        self.path_field.setReadOnly(True)
        self.path_field.setObjectName("Mono")
        self.copy_path = QPushButton(tr("help.copy_path"))
        self.copy_path.setObjectName("Ghost")
        self.copy_path.clicked.connect(self._copy_path)
        body.addLayout(self._row(self.path_field, self.copy_path))
        self.path_note = _wrap(tr("help.path_note"))
        body.addWidget(self.path_note)
        self.path_warning = _wrap(tr("help.path_missing"))
        self.path_warning.setStyleSheet(f"color: {color('warn')}; font-size: 11.5px;")
        self.path_warning.setVisible(not (self.folder / "manifest.json").is_file())
        body.addWidget(self.path_warning)

        # ------------------------------------------------------------- step 3
        self.step3_text = _wrap(tr("help.step3"))
        body.addWidget(self._numbered(3, self.step3_text))
        self.token = QLineEdit(self.service.token)
        self.token.setReadOnly(True)
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setObjectName("Mono")
        self.reveal = QPushButton(tr("set.show"))
        self.reveal.setObjectName("Ghost")
        self.reveal.setCheckable(True)
        self.reveal.toggled.connect(self._toggle_token)
        self.copy_token = QPushButton(tr("help.copy_token"))
        self.copy_token.setObjectName("Ghost")
        self.copy_token.clicked.connect(self._copy_token)
        body.addLayout(self._row(self.token, self.reveal, self.copy_token))
        self.port = self._port()
        self.token_note = _wrap(tr("help.token_note", port=self.port))
        body.addWidget(self.token_note)

        # -------------------------------------------------------------- footer
        self.footer = _wrap(tr("help.footer"))
        body.addWidget(self.footer)

        self.status = _wrap("")
        self.status.setStyleSheet(f"color: {color('ok')}; font-size: 11.5px;")
        body.addWidget(self.status)
        body.addStretch(1)

        buttons = QHBoxLayout()
        self.open_folder = QPushButton(tr("help.open_folder"))
        self.open_folder.setObjectName("Ghost")
        self.open_folder.clicked.connect(self._open_folder)
        buttons.addWidget(self.open_folder)
        buttons.addStretch(1)
        self.close_button = QPushButton(tr("help.close"))
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        outer.addLayout(buttons)

        # Re-label in place when the language is switched from another window.
        language_manager().changed.connect(self._on_language_changed)

    # ------------------------------------------------------------------ build
    def _numbered(self, number: int, text_label: QLabel) -> QWidget:
        """A numbered row: accent badge on the left, wrapped text beside it."""
        badge = label(str(number), "Chip")
        badge.setAlignment(Qt.AlignCenter)
        badge.setFixedWidth(28)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(badge, 0, Qt.AlignTop | Qt.AlignLeft)
        row.addWidget(text_label, 1)
        holder = QWidget()
        holder.setLayout(row)
        return holder

    @staticmethod
    def _row(*widgets) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        for index, widget in enumerate(widgets):
            row.addWidget(widget, 1 if index == 0 else 0)
        return row

    def _port(self) -> int:
        """The port the extension must be told: what the agent really bound."""
        ipc = getattr(self.service, "ipc", None)
        for attribute in ("bound_port", "port"):
            value = getattr(ipc, attribute, 0)
            if value:
                return int(value)
        return int(self.service.settings.ipc_port)

    # ---------------------------------------------------------------- actions
    def _copy_path(self) -> None:
        QApplication.clipboard().setText(str(self.folder))
        self._say(tr("help.path_copied"))

    def _copy_token(self) -> None:
        QApplication.clipboard().setText(self.service.token)
        self._say(tr("help.token_copied"))

    def _say(self, text: str, *, ok: bool = True) -> None:
        self.status.setStyleSheet(
            f"color: {color('ok') if ok else color('warn')}; font-size: 11.5px;"
        )
        self.status.setText(text)

    def _toggle_token(self, revealed: bool) -> None:
        self.token.setEchoMode(QLineEdit.Normal if revealed else QLineEdit.Password)
        self.reveal.setText(tr("set.hide") if revealed else tr("set.show"))

    def _open_folder(self) -> None:
        """Hand the folder to the file manager; never fatal if there is none."""
        if not self.folder.is_dir():
            self._say(tr("help.path_missing"), ok=False)
            return
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.folder)))
        except Exception:  # noqa: BLE001 - never break the dialog over this
            self._say(tr("help.path_missing"), ok=False)

    # --------------------------------------------------------------- language
    def _on_language_changed(self, _code: str) -> None:
        self.retranslate_ui()

    def retranslate_ui(self) -> None:
        self.heading.setText(tr("help.title"))
        self.intro.setText(tr("help.intro"))
        self.step1_text.setText(tr("help.step1"))
        self.step2_text.setText(tr("help.step2"))
        self.step3_text.setText(tr("help.step3"))
        self.path_note.setText(tr("help.path_note"))
        self.path_warning.setText(tr("help.path_missing"))
        self.token_note.setText(tr("help.token_note", port=self.port))
        self.footer.setText(tr("help.footer"))
        self.copy_path.setText(tr("help.copy_path"))
        self.copy_token.setText(tr("help.copy_token"))
        self.reveal.setText(tr("set.hide") if self.reveal.isChecked() else tr("set.show"))
        self.open_folder.setText(tr("help.open_folder"))
        self.close_button.setText(tr("help.close"))
