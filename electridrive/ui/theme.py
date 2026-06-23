"""Electric-Dark designer theme: color tokens + generated QSS stylesheet."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    name: str
    window: str
    surface: str       # sidebar / header
    surface2: str      # cards, inputs
    surface3: str      # hover
    border: str
    border_strong: str
    text: str
    muted: str
    accent: str
    accent2: str
    accent_text: str
    danger: str
    success: str
    warning: str
    selection: str


ELECTRIC_DARK = Palette(
    name="electric_dark",
    window="#0E1116",
    surface="#161B22",
    surface2="#1C2330",
    surface3="#222C3A",
    border="#232B36",
    border_strong="#303B49",
    text="#E6EDF3",
    muted="#8B98A9",
    accent="#22D3EE",
    accent2="#3B82F6",
    accent_text="#04141A",
    danger="#F87171",
    success="#34D399",
    warning="#FBBF24",
    selection="#16313B",
)

ELECTRIC_LIGHT = Palette(
    name="electric_light",
    window="#F6F8FA",
    surface="#FFFFFF",
    surface2="#FFFFFF",
    surface3="#EEF2F6",
    border="#E1E7EC",
    border_strong="#CBD5E0",
    text="#1F2328",
    muted="#5B6675",
    accent="#0EA5E9",
    accent2="#2563EB",
    accent_text="#FFFFFF",
    danger="#DC2626",
    success="#059669",
    warning="#D97706",
    selection="#E0F2FE",
)

PALETTES = {p.name: p for p in (ELECTRIC_DARK, ELECTRIC_LIGHT)}


def get_palette(name: str) -> Palette:
    return PALETTES.get(name, ELECTRIC_DARK)


def build_qss(p: Palette) -> str:
    return f"""
    * {{
        font-family: "Inter", "Ubuntu", "Cantarell", "Noto Sans", sans-serif;
        font-size: 14px;
        color: {p.text};
        outline: 0;
    }}
    QWidget#Root, QMainWindow {{ background: {p.window}; }}

    /* ---- Sidebar ---- */
    QWidget#Sidebar {{
        background: {p.surface};
        border-right: 1px solid {p.border};
    }}
    QLabel#Brand {{ font-size: 18px; font-weight: 800; color: {p.text}; }}
    QLabel#BrandSub {{ font-size: 11px; color: {p.muted}; }}

    QPushButton[nav="true"] {{
        text-align: left;
        padding: 9px 12px;
        border: none;
        border-radius: 10px;
        color: {p.muted};
        background: transparent;
        font-weight: 600;
    }}
    QPushButton[nav="true"]:hover {{ background: {p.surface3}; color: {p.text}; }}
    QPushButton[nav="true"]:checked {{
        background: {p.selection};
        color: {p.text};
    }}

    /* ---- Header ---- */
    QWidget#Header {{ background: {p.window}; border-bottom: 1px solid {p.border}; }}
    QLabel#ViewTitle {{ font-size: 20px; font-weight: 800; }}
    QLabel#Crumb {{ color: {p.muted}; font-weight: 600; }}
    QLabel#CrumbActive {{ color: {p.text}; font-weight: 700; }}

    QLineEdit#Search, QLineEdit {{
        background: {p.surface2};
        border: 1px solid {p.border};
        border-radius: 10px;
        padding: 8px 12px;
        selection-background-color: {p.accent};
        selection-color: {p.accent_text};
    }}
    QLineEdit:focus {{ border: 1px solid {p.accent}; }}

    /* ---- Buttons ---- */
    QPushButton {{
        background: {p.surface2};
        border: 1px solid {p.border_strong};
        border-radius: 10px;
        padding: 8px 14px;
        color: {p.text};
        font-weight: 600;
    }}
    QPushButton:hover {{ background: {p.surface3}; border-color: {p.accent}; }}
    QPushButton:disabled {{ color: {p.muted}; border-color: {p.border}; background: {p.surface}; }}

    QPushButton[accent="true"] {{
        border: none;
        color: {p.accent_text};
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {p.accent}, stop:1 {p.accent2});
        font-weight: 700;
    }}
    QPushButton[accent="true"]:hover {{
        background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {p.accent2}, stop:1 {p.accent});
    }}
    QPushButton[ghost="true"] {{ background: transparent; border: 1px solid {p.border_strong}; }}
    QPushButton[ghost="true"]:hover {{ border-color: {p.accent}; background: {p.surface2}; }}
    QPushButton[danger="true"]:hover {{ border-color: {p.danger}; color: {p.danger}; }}
    QPushButton[iconbtn="true"] {{ padding: 7px; border-radius: 9px; }}

    /* ---- Cards ---- */
    QFrame#Card {{
        background: {p.surface}; border: 1px solid {p.border}; border-radius: 14px;
    }}
    QFrame#StatCard {{
        background: {p.surface2}; border: 1px solid {p.border}; border-radius: 12px;
    }}

    /* ---- Tables / trees ---- */
    QTreeView, QTableView, QListView {{
        background: transparent; border: none;
        alternate-background-color: transparent;
        selection-background-color: {p.selection};
        selection-color: {p.text};
    }}
    QTreeView::item, QTableView::item {{
        padding: 6px 4px; border-radius: 8px; min-height: 26px;
    }}
    QTreeView::item:hover, QTableView::item:hover {{ background: {p.surface2}; }}
    QTreeView::item:selected, QTableView::item:selected {{ background: {p.selection}; }}
    QHeaderView::section {{
        background: transparent; color: {p.muted}; border: none;
        border-bottom: 1px solid {p.border};
        padding: 8px 6px; font-weight: 700; font-size: 12px;
    }}
    QTableView {{ gridline-color: transparent; }}
    QTableCornerButton::section {{ background: transparent; border: none; }}

    /* ---- Scroll areas ---- */
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget#qt_scrollarea_viewport {{ background: transparent; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QAbstractScrollArea {{ background: transparent; }}

    /* ---- Scrollbars ---- */
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
    QScrollBar::handle:vertical {{ background: {p.border_strong}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::handle:vertical:hover {{ background: {p.accent}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
    QScrollBar::handle:horizontal {{ background: {p.border_strong}; border-radius: 5px; min-width: 30px; }}
    QScrollBar::handle:horizontal:hover {{ background: {p.accent}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    /* ---- Progress ---- */
    QProgressBar {{
        background: {p.surface2}; border: none; border-radius: 6px;
        height: 8px; text-align: center; color: transparent;
    }}
    QProgressBar::chunk {{
        border-radius: 6px;
        background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {p.accent}, stop:1 {p.accent2});
    }}

    /* ---- Menus / tooltips ---- */
    QMenu {{
        background: {p.surface2}; border: 1px solid {p.border_strong};
        border-radius: 10px; padding: 6px;
    }}
    QMenu::item {{ padding: 7px 22px 7px 14px; border-radius: 7px; }}
    QMenu::item:selected {{ background: {p.selection}; }}
    QMenu::separator {{ height: 1px; background: {p.border}; margin: 5px 8px; }}
    QToolTip {{
        background: {p.surface2}; color: {p.text};
        border: 1px solid {p.border_strong}; border-radius: 8px; padding: 6px 8px;
    }}

    /* ---- Misc labels ---- */
    QLabel[role="title"] {{ font-size: 17px; font-weight: 800; }}
    QLabel[role="muted"] {{ color: {p.muted}; }}
    QLabel[role="h1"] {{ font-size: 26px; font-weight: 800; }}
    QLabel[role="badge"] {{
        color: {p.accent}; background: {p.selection};
        border-radius: 8px; padding: 3px 8px; font-weight: 700; font-size: 11px;
    }}
    QComboBox {{
        background: {p.surface2}; border: 1px solid {p.border}; border-radius: 10px;
        padding: 7px 12px;
    }}
    QComboBox:hover {{ border-color: {p.accent}; }}
    QComboBox QAbstractItemView {{
        background: {p.surface2}; border: 1px solid {p.border_strong};
        border-radius: 10px; selection-background-color: {p.selection};
    }}
    QCheckBox {{ spacing: 8px; }}
    QCheckBox::indicator {{
        width: 18px; height: 18px; border-radius: 5px;
        border: 1px solid {p.border_strong}; background: {p.surface2};
    }}
    QCheckBox::indicator:checked {{ background: {p.accent}; border-color: {p.accent}; }}
    """
