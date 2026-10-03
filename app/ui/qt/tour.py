"""In-app guided tour: a dimming "shadow" layer with a spotlight and arrows.

The tour never fakes a UI: it highlights real widgets of the live dashboard
(`MainWindow` hands out the targets) and dims everything else with a
semi-transparent layer, so what the user learns is exactly what they will see
afterwards. Nothing here talks to the agent — it is pure presentation.

Layout is computed by hand because the overlay is painted on top of the
window, and it mirrors itself under a right-to-left layout (Arabic): the
bubble swaps sides and the arrow is drawn on the opposite edge, so the tour
stays correct in both languages.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPainterPath,
                           QPen, QPolygonF)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.i18n import is_rtl, tr
from app.ui.qt.theme import PALETTE, color


@dataclass(frozen=True)
class TourStep:
    """One stop of the tour.

    `target` is the *attribute name* on the window (resolved at show time, so
    a step whose widget does not exist yet is skipped instead of crashing),
    or "" for a centred step with no spotlight.
    """

    key: str                      # catalog key prefix: f"tour.{key}.title/body"
    target: str = ""
    placement: str = "auto"       # auto | above | below | left | right | center


#: The tour itself. Ordered top-down through the dashboard: what it is, what
#: you can do, then the two panels that need explaining (extension, games).
DEFAULT_STEPS: tuple[TourStep, ...] = (
    TourStep("welcome", placement="center"),
    TourStep("rules", target="scroll"),
    TourStep("new_rule", target="new_rule_button"),
    TourStep("pause", target="pause_button"),
    TourStep("extension", target="browser_panel"),
    TourStep("detectors", target="detector_panel"),
    TourStep("timeline", target="timeline_panel"),
    TourStep("language", target="language_button"),
    TourStep("finish", placement="center"),
)

_SPOT_MARGIN = 6          # px of padding around the highlighted widget
_BUBBLE_WIDTH = 330
_BUBBLE_GAP = 30          # gap between spotlight and bubble (arrow room)


class TourOverlay(QWidget):
    """Full-window overlay: dims the app, spotlights one widget at a time.

    Signals:
        finished(bool) — True when the user reached the last step, False when
        they skipped (Esc / Skip). The caller uses it to stop offering the
        tour on first run.
    """

    finished = Signal(bool)

    def __init__(self, window: QWidget, steps: tuple[TourStep, ...] = DEFAULT_STEPS) -> None:
        super().__init__(window)
        self.setObjectName("TourOverlay")
        self._window = window
        self._steps = tuple(steps)
        self._index = 0
        self._hole = QRect()          # spotlight rect in overlay coordinates
        self._steps = tuple(s for s in self._steps)  # kept for tail-call clarity
        self._skipped_unavailable = 0

        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.StrongFocus)

        # ------------------------------------------------------------- bubble
        self.bubble = QFrame(self)
        self.bubble.setObjectName("TourBubble")
        self.bubble.setStyleSheet(
            f"QFrame#TourBubble {{ background: {PALETTE['panel_alt']};"
            f" border: 1px solid {color('info')}; border-radius: 12px; }}"
        )
        bubble_layout = QVBoxLayout(self.bubble)
        bubble_layout.setContentsMargins(16, 14, 16, 12)
        bubble_layout.setSpacing(8)
        # The bubble is exactly as tall as its (word-wrapped, translated)
        # content demands: Arabic bodies are longer than the English ones and
        # a fixed height clipped the last line ("الآن." was cut off).
        bubble_layout.setSizeConstraint(QLayout.SetFixedSize)

        self.counter_label = QLabel()
        self.counter_label.setObjectName("CardSub")
        bubble_layout.addWidget(self.counter_label)

        self.title_label = QLabel()
        title_font = QFont(self.title_label.font())
        title_font.setPointSizeF(title_font.pointSizeF() + 2.5)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setWordWrap(True)
        bubble_layout.addWidget(self.title_label)

        self.body_label = QLabel()
        self.body_label.setObjectName("CardSub")
        self.body_label.setWordWrap(True)
        bubble_layout.addWidget(self.body_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.skip_button = QPushButton()
        self.skip_button.setObjectName("Ghost")
        self.skip_button.clicked.connect(self.skip)
        self.back_button = QPushButton()
        self.back_button.setObjectName("Ghost")
        self.back_button.clicked.connect(self.back)
        self.next_button = QPushButton()
        self.next_button.clicked.connect(self.next)
        buttons.addWidget(self.skip_button)
        buttons.addStretch(1)
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)
        bubble_layout.addLayout(buttons)

    # ------------------------------------------------------------------ flow
    def start(self) -> None:
        self._index = self._first_available_index(0)
        self._resize_to_window()
        self.show()
        self.raise_()
        self.setFocus(Qt.OtherFocusReason)
        self._render_step()

    def next(self) -> None:
        candidate = self._first_available_index(self._index + 1)
        if candidate is None:            # past the end: the tour is done
            self._finish(completed=True)
            return
        self._index = candidate
        self._render_step()

    def back(self) -> None:
        candidate = self._last_available_index(self._index - 1)
        if candidate is None:
            return
        self._index = candidate
        self._render_step()

    def skip(self) -> None:
        self._finish(completed=False)

    def _finish(self, *, completed: bool) -> None:
        self.hide()
        self.deleteLater()
        self.finished.emit(completed)

    def retranslate(self) -> None:
        """Re-render the current step (called on a live language switch)."""
        self._render_step()

    # --------------------------------------------------------------- targets
    def _target_widget(self, step: TourStep) -> QWidget | None:
        if not step.target:
            return None
        widget = getattr(self._window, step.target, None)
        if not isinstance(widget, QWidget) or not widget.isVisible():
            return None
        return widget

    def _first_available_index(self, start: int) -> int | None:
        for index in range(start, len(self._steps)):
            step = self._steps[index]
            if not step.target or self._target_widget(step) is not None:
                return index
        return None

    def _last_available_index(self, start: int) -> int | None:
        for index in range(start, -1, -1):
            step = self._steps[index]
            if not step.target or self._target_widget(step) is not None:
                return index
        return None

    # ----------------------------------------------------------------- paint
    def _resize_to_window(self) -> None:
        self.setGeometry(self._window.rect())

    def _hole_for(self, widget: QWidget | None) -> QRect:
        if widget is None:
            return QRect()
        top_left = widget.mapTo(self._window, QPoint(0, 0))
        rect = QRect(top_left, widget.size()).adjusted(
            -_SPOT_MARGIN, -_SPOT_MARGIN, _SPOT_MARGIN, _SPOT_MARGIN
        )
        return rect.intersected(self.rect())

    def _render_step(self) -> None:
        step = self._steps[self._index]
        self._resize_to_window()
        widget = self._target_widget(step)
        self._hole = self._hole_for(widget)

        self.counter_label.setText(
            tr("tour.step_counter", current=self._index + 1, total=len(self._steps))
        )
        self.title_label.setText(tr(f"tour.{step.key}.title"))
        self.body_label.setText(tr(f"tour.{step.key}.body"))
        self.skip_button.setText(tr("tour.skip"))
        self.back_button.setText(tr("tour.back"))
        self.next_button.setText(
            tr("tour.done") if self._first_available_index(self._index + 1) is None
            else tr("tour.next")
        )
        self.back_button.setVisible(self._last_available_index(self._index - 1) is not None)

        self._place_bubble(step)
        self.update()

    def _fit_label(self, widget: QLabel, width: int) -> None:
        """Give a word-wrapped label exactly the height its text needs.

        Measured with QFontMetrics instead of trusting `sizeHint()`: a stale
        label cache reported one line for a three-line Arabic paragraph and
        clipped the last line off the bubble.
        """
        metrics = QFontMetrics(widget.font())
        rect = metrics.boundingRect(
            QRect(0, 0, width, 10_000), Qt.TextWordWrap, widget.text()
        )
        widget.setFixedSize(width, rect.height())

    def _place_bubble(self, step: TourStep) -> None:
        """Put the bubble next to the spotlight, mirrored for RTL."""
        self.bubble.setFixedWidth(_BUBBLE_WIDTH)
        inner = _BUBBLE_WIDTH - 32            # bubble left+right margins
        self._fit_label(self.title_label, inner)
        self._fit_label(self.body_label, inner)
        self.bubble.layout().activate()
        self.bubble.adjustSize()
        size = self.bubble.size()

        if self._hole.isNull():
            center = self.rect().center()
            pos = QPoint(center.x() - size.width() // 2,
                         max(12, center.y() - size.height() - 40))
            self.bubble.move(pos)
            self.bubble.show()
            return

        rtl = is_rtl()
        placement = step.placement
        if placement == "auto":
            # Prefer the side with room; under RTL the "leading" side is right.
            if rtl:
                placement = "right" if self._hole.right() - size.width() - _BUBBLE_GAP > 0 \
                    else "left"
            else:
                placement = "right" if self._hole.right() + size.width() + _BUBBLE_GAP < self.width() \
                    else "left"

        if placement == "right" and self._hole.right() + size.width() + _BUBBLE_GAP >= self.width():
            placement = "below"
        if placement == "left" and self._hole.left() - size.width() - _BUBBLE_GAP < 0:
            placement = "below"
        if placement == "below" and self._hole.bottom() + size.height() + _BUBBLE_GAP >= self.height():
            placement = "above"
        if placement == "above" and self._hole.top() - size.height() - _BUBBLE_GAP < 0:
            placement = "below"

        if placement == "right":
            pos = QPoint(self._hole.right() + _BUBBLE_GAP,
                         self._hole.center().y() - size.height() // 2)
        elif placement == "left":
            pos = QPoint(self._hole.left() - size.width() - _BUBBLE_GAP,
                         self._hole.center().y() - size.height() // 2)
        elif placement == "above":
            pos = QPoint(self._hole.center().x() - size.width() // 2,
                         self._hole.top() - size.height() - _BUBBLE_GAP)
        else:  # below
            pos = QPoint(self._hole.center().x() - size.width() // 2,
                         self._hole.bottom() + _BUBBLE_GAP)

        pos.setX(max(10, min(pos.x(), self.width() - size.width() - 10)))
        pos.setY(max(10, min(pos.y(), self.height() - size.height() - 10)))
        self.bubble.move(pos)
        self.bubble.resize(size)
        self.bubble.show()
        self._bubble_placement = placement

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # ---------------------------------------------- the shadow (dimming)
        path = QPainterPath()
        path.addRect(QRectF(self.rect()))
        if not self._hole.isNull():
            hole = QPainterPath()
            hole.addRoundedRect(QRectF(self._hole), 12, 12)
            path = path.subtracted(hole)
        painter.fillPath(path, QColor(8, 10, 20, 190))

        # ------------------------------------------------ spotlight outline
        if not self._hole.isNull():
            painter.setPen(QPen(QColor(color("info")), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(self._hole), 12, 12)
            self._draw_arrow(painter)

    def _draw_arrow(self, painter: QPainter) -> None:
        """A line + head from the bubble to the spotlight (mirrored for RTL)."""
        placement = getattr(self, "_bubble_placement", "below")
        bubble = self.bubble.geometry()
        accent = QColor(color("info"))

        # The arrow always travels from the bubble *into* the spotlight: it
        # starts on the bubble's near edge and ends just inside the hole, so
        # the head (drawn another few pixels past `end`) lands on the widget.
        if placement in ("right", "left"):
            hole_edge = self._hole.right() if placement == "right" else self._hole.left()
            start = QPoint(bubble.left() if placement == "right" else bubble.right(),
                           bubble.center().y())
            end = QPoint(hole_edge - (2 if placement == "right" else -2),
                         self._hole.center().y())
        else:
            hole_edge = self._hole.bottom() if placement == "below" else self._hole.top()
            start = QPoint(bubble.center().x(),
                           bubble.bottom() if placement == "below" else bubble.top())
            end = QPoint(self._hole.center().x(),
                         hole_edge - (2 if placement == "below" else -2))

        painter.setPen(QPen(accent, 2.4))
        painter.drawLine(start, end)

        # arrow head: points at the spotlight, sized in the travel direction
        direction = QPoint(end.x() - start.x(), end.y() - start.y())
        length = max(1.0, (direction.x() ** 2 + direction.y() ** 2) ** 0.5)
        ux, uy = direction.x() / length, direction.y() / length
        head, half = 11.0, 6.0
        tip = QPoint(int(end.x() + ux * head), int(end.y() + uy * head))
        left = QPoint(int(end.x() - uy * half), int(end.y() + ux * half))
        right = QPoint(int(end.x() + uy * half), int(end.y() - ux * half))
        painter.setBrush(accent)
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(QPolygonF([tip, left, right]))

    # ------------------------------------------------------------- key input
    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        if key == Qt.Key_Escape:
            self.skip()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.next()
            return
        # Arrow keys follow the reading direction, like every other RTL UI.
        forward_keys = (Qt.Key_Left, Qt.Key_Right) if is_rtl() else (Qt.Key_Right,)
        backward_keys = (Qt.Key_Right, Qt.Key_Left) if is_rtl() else (Qt.Key_Left,)
        if key in forward_keys:
            self.next()
            return
        if key in backward_keys:
            self.back()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Clicking the dimmed area advances — the spotlight itself stays
        click-through-able for the eye, but the tour owns the click."""
        if not self._hole.contains(event.position().toPoint()):
            self.next()
        else:
            super().mousePressEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._render_step()
        super().resizeEvent(event)
