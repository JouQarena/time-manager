"""Dark theme + role colours for the Qt UI.

Kept in one place so the viewmodel's colour *roles* (`ok`, `warn`, `bad`, …)
map to actual paint colours in exactly one spot.
"""

from __future__ import annotations

ROLE_COLORS = {
    "ok": "#43c59e",
    "warn": "#f0b429",
    "bad": "#e05252",
    "info": "#5a6ae8",
    "muted": "#98a0bd",
}

PALETTE = {
    "bg": "#12141f",
    "panel": "#191d2e",
    "panel_alt": "#1e2337",
    "line": "#2a3050",
    "text": "#eef1fb",
    "text_dim": "#98a0bd",
    "accent": "#5a6ae8",
}


def color(role: str) -> str:
    return ROLE_COLORS.get(role, PALETTE["text_dim"])


STYLESHEET = f"""
QWidget {{
    background: {PALETTE['bg']};
    color: {PALETTE['text']};
    font-family: "Segoe UI", system-ui, sans-serif;
    font-size: 13px;
}}
QMainWindow, QDialog {{ background: {PALETTE['bg']}; }}
/* Leaf widgets must stay transparent. The blanket `QWidget` rule above also
   matches QLabel, and in a squeezed layout (a narrow window, or a card whose
   text no longer fits) Qt then paints the *window* colour behind every label
   instead of letting the card's own colour show through: the text ends up on
   a #12141f band. Nothing that only draws text or is a scroll container
   should ever fill its rect. */
QLabel, QCheckBox, QRadioButton, QStatusBar, QScrollArea {{
    background: transparent;
}}


QLabel#Subtitle {{ color: {PALETTE['text_dim']}; font-size: 12px; }}
QLabel#SectionTitle {{
    color: {PALETTE['text_dim']}; font-size: 11px; font-weight: 600;
    letter-spacing: 1px; text-transform: uppercase;
}}
QLabel#Mono {{ font-family: Consolas, "DejaVu Sans Mono", monospace; font-size: 12px; }}
QLabel#Chip {{
    background: {PALETTE['panel_alt']}; border: 1px solid {PALETTE['line']};
    border-radius: 10px; padding: 3px 10px; font-size: 12px;
}}
QLabel#Banner {{
    background: #3a2f14; border: 1px solid #6b5a1e; border-radius: 10px;
    padding: 8px 12px; color: #f6d98a;
}}
QLabel#CardTitle {{ font-size: 15px; font-weight: 600; }}
QLabel#CardSub {{ color: {PALETTE['text_dim']}; font-size: 11.5px; }}
QLabel#CardUsed {{ font-size: 15px; font-weight: 600; }}

QFrame#Card {{
    background: {PALETTE['panel']}; border: 1px solid {PALETTE['line']};
    border-radius: 12px;
}}
QFrame#Panel {{
    background: {PALETTE['panel']}; border: 1px solid {PALETTE['line']};
    border-radius: 12px;
}}
QFrame#Separator {{ background: {PALETTE['line']}; max-height: 1px; border: 0; }}

QProgressBar {{
    background: #232842; border: 0; border-radius: 4px; height: 8px; text-align: center;
}}
QProgressBar::chunk {{ border-radius: 4px; }}

QPushButton {{
    background: {PALETTE['accent']}; color: white; border: 0; border-radius: 8px;
    padding: 7px 14px; font-size: 12.5px;
}}
QPushButton:hover {{ background: #6a78ea; }}
QPushButton:disabled {{ background: #2a3050; color: {PALETTE['text_dim']}; }}
QPushButton#Ghost {{
    background: transparent; border: 1px solid {PALETTE['line']}; color: {PALETTE['text']};
}}
QPushButton#Ghost:hover {{ background: {PALETTE['panel_alt']}; }}
QPushButton#Danger {{ background: #b23b3b; }}
QPushButton#Danger:hover {{ background: #c74a4a; }}

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTimeEdit {{
    background: #101322; border: 1px solid {PALETTE['line']}; border-radius: 8px;
    padding: 6px 8px; selection-background-color: {PALETTE['accent']};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {PALETTE['accent']}; }}
QComboBox QAbstractItemView {{
    background: #101322; border: 1px solid {PALETTE['line']};
    selection-background-color: {PALETTE['accent']};
}}
QCheckBox, QRadioButton {{ spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QGroupBox {{
    border: 1px solid {PALETTE['line']}; border-radius: 10px; margin-top: 14px;
    padding: 12px 10px 10px;
}}
QGroupBox::title {{
    subcontrol-origin: margin; left: 10px; padding: 0 4px;
    color: {PALETTE['text_dim']}; font-size: 11.5px;
}}
QScrollArea {{ border: 0; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2f3760; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QMenu {{
    background: {PALETTE['panel']}; border: 1px solid {PALETTE['line']}; padding: 6px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {PALETTE['accent']}; }}
QMenu::separator {{ height: 1px; background: {PALETTE['line']}; margin: 5px 8px; }}
QStatusBar {{ color: {PALETTE['text_dim']}; }}
QToolTip {{
    background: {PALETTE['panel_alt']}; color: {PALETTE['text']};
    border: 1px solid {PALETTE['line']}; padding: 5px;
}}
"""

TRAY_ICON_COLORS = {
    "ok": "#5a6ae8",
    "warn": "#f0b429",
    "bad": "#e05252",
    "muted": "#5b6180",
}
