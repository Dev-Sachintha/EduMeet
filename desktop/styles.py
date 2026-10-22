"""Qt style sheet for the whole desktop app."""

STYLESHEET = """
* { font-family: "Segoe UI", sans-serif; font-size: 10pt; }
QWidget { background: #f5f7fb; color: #1f2937; }
QLabel { background: transparent; }
QLabel#title { font-size: 22pt; font-weight: 700; color: #1e3a8a; }
QLabel#subtitle, QLabel#muted { color: #64748b; }
QLabel#error { color: #b91c1c; }
QLabel#userBadge { color: #475569; }

QLineEdit, QComboBox, QDateTimeEdit, QPlainTextEdit {
    background: #ffffff; border: 1px solid #cbd5e1; border-radius: 6px; padding: 6px 8px;
}
QLineEdit:focus, QComboBox:focus, QDateTimeEdit:focus, QPlainTextEdit:focus { border: 1px solid #2563eb; }

QPushButton {
    background: #2563eb; color: #ffffff; border: none; border-radius: 6px; padding: 8px 16px;
}
QPushButton:hover { background: #1d4ed8; }
QPushButton:disabled { background: #cbd5e1; color: #64748b; }
QPushButton[secondary="true"] { background: #e2e8f0; color: #1f2937; }
QPushButton[secondary="true"]:hover { background: #cbd5e1; }
QPushButton[danger="true"] { background: #dc2626; }
QPushButton[danger="true"]:hover { background: #b91c1c; }

QTableWidget {
    background: #ffffff; border: 1px solid #e2e8f0; gridline-color: #eef2f7;
    selection-background-color: #dbeafe; selection-color: #111827;
}
QHeaderView::section { background: #eef2f7; border: none; padding: 7px; font-weight: 600; }

QTabWidget::pane { border: 1px solid #e2e8f0; border-radius: 6px; top: -1px; }
QTabBar::tab { background: #e2e8f0; padding: 9px 18px; margin-right: 2px;
               border-top-left-radius: 6px; border-top-right-radius: 6px; }
QTabBar::tab:selected { background: #ffffff; font-weight: 600; color: #1e3a8a; }
QCheckBox { background: transparent; }
"""


def make_secondary(button, danger: bool = False):
    """Mark a QPushButton as a secondary (grey) or danger (red) button."""
    button.setProperty("danger" if danger else "secondary", True)
    return button
