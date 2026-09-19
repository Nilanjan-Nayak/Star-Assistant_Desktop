"""HUD result surface for Agent Reach responses (v3.0 — Premium).

v3.0 Enhancements:
  - Glass-morphism result cards with gradient accent bars
  - Better typography and spacing
  - Animated scroll area with subtle glass effects
"""

from __future__ import annotations

import webbrowser
from typing import Any, Dict

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QLinearGradient, QPen, QBrush
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from tokens import (
    ALERT,
    CYAN,
    CYAN_GLOW,
    FONT_HUD_FAMILY,
    FONT_HUD_SIZE,
    FONT_HUD_SIZE_LG,
    FONT_HUD_SIZE_SM,
    FONT_MONO_FAMILY,
    GOLD,
    OK,
    TEXT_DIM,
    TEXT_PRIMARY,
    TEXT_BRIGHT,
    PURPLE_ACCENT,
)


class ReachResultPanel(QWidget):
    """Scrollable, bounded result cards that preserve the existing Star style."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 0, 0, 0)
        self._root.setSpacing(10)

        # Header row
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        dot = QLabel("◆")
        dot.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE_LG))
        dot.setStyleSheet(f"color: {CYAN_GLOW.name()}; background: transparent;")
        header_row.addWidget(dot)

        self._header = QLabel("INTERNET RESULT")
        self._header.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE_LG, QFont.DemiBold))
        self._header.setStyleSheet(
            f"color: {TEXT_BRIGHT.name()}; letter-spacing: 2px; background: transparent;"
        )
        header_row.addWidget(self._header)
        header_row.addStretch(1)
        self._root.addLayout(header_row)

        self._status = QLabel("")
        self._status.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM))
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {TEXT_DIM.name()}; background: transparent;")
        self._root.addWidget(self._status)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { background: rgba(5,12,30,180); width: 6px; border: none; border-radius: 3px; }"
            "QScrollBar::handle:vertical { background: rgba(0,200,255,60); border-radius: 3px; min-height: 30px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )
        self._cards = QWidget()
        self._cards.setStyleSheet("background: transparent;")
        self._cards_layout = QVBoxLayout(self._cards)
        self._cards_layout.setContentsMargins(2, 2, 8, 2)
        self._cards_layout.setSpacing(10)
        self._cards_layout.addStretch(1)
        self._scroll.setWidget(self._cards)
        self._root.addWidget(self._scroll, 1)

    def set_result(self, result: Dict[str, Any]) -> None:
        self._clear_cards()
        channel = str(result.get("channel", "internet"))
        backend = str(result.get("backend", "unknown"))
        success = bool(result.get("success"))
        auth = bool(result.get("requires_auth"))
        summary = str(result.get("summary") or result.get("error") or "")
        self._header.setText(f"{channel.upper()} // {backend.upper()}")
        self._status.setText(summary[:1800])

        if auth:
            self._status.setStyleSheet(f"color: {GOLD.name()}; background: transparent;")
            self._add_message("AUTH REQUIRED", summary, GOLD)
            return

        if not success:
            self._status.setStyleSheet(f"color: {ALERT.name()}; background: transparent;")
            self._add_message("REACH ERROR", summary, ALERT)
            return

        self._status.setStyleSheet(f"color: {OK.name()}; background: transparent;")
        items = result.get("items") or []
        for item in list(items)[:10]:
            if isinstance(item, dict):
                self._add_item(item)

        text = str(result.get("text") or "").strip()
        if text and not items:
            self._add_message("PAGE TEXT", text[:9000], CYAN)
        elif not items:
            self._add_message("READY", summary or "Result পেয়েছি।", CYAN)

    def _clear_cards(self) -> None:
        while self._cards_layout.count() > 1:
            item = self._cards_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _add_item(self, item: Dict[str, Any]) -> None:
        title = str(item.get("title") or "Untitled result")[:300]
        url = str(item.get("url") or "")[:2000]
        snippet = str(item.get("snippet") or "")[:900]
        source = str(item.get("source") or "")
        backend = str(item.get("backend") or "")

        card = QFrame()
        card.setFrameShape(QFrame.StyledPanel)
        card.setStyleSheet(
            "QFrame { background: rgba(5, 14, 32, 230); "
            "border: 1px solid rgba(0, 210, 255, 0.2); border-radius: 8px; }"
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        title_label = QLabel(title)
        title_label.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE, QFont.DemiBold))
        title_label.setWordWrap(True)
        title_label.setStyleSheet(
            f"color: {TEXT_PRIMARY.name()}; background: transparent; border: none;"
        )
        layout.addWidget(title_label)

        meta = QLabel(f"{source} // {backend}")
        meta.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM))
        meta.setStyleSheet(f"color: {CYAN.name()}; background: transparent; border: none;")
        layout.addWidget(meta)

        if snippet:
            snippet_label = QLabel(snippet)
            snippet_label.setWordWrap(True)
            snippet_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            snippet_label.setStyleSheet(
                f"color: {TEXT_DIM.name()}; background: transparent; border: none;"
            )
            layout.addWidget(snippet_label)

        if _safe_url(url):
            row = QHBoxLayout()
            link_label = QLabel(url)
            link_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            link_label.setStyleSheet(
                f"color: rgba(0,200,255,0.7); background: transparent; border: none; "
                f"font-size: 9px;"
            )
            link_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            row.addWidget(link_label, 1)
            button = QPushButton("OPEN ›")
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton {{ color: {GOLD.name()}; background: rgba(201,162,39,0.08); "
                "border: 1px solid rgba(201,162,39,0.3); border-radius: 5px; "
                "padding: 4px 12px; font-weight: bold; letter-spacing: 1px; }"
                f"QPushButton:hover {{ background: rgba(201,162,39,0.2); "
                "border: 1px solid rgba(201,162,39,0.6); }}"
            )
            button.clicked.connect(lambda _checked=False, target=url: webbrowser.open(target))
            row.addWidget(button)
            layout.addLayout(row)

        self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)

    def _add_message(self, label: str, message: str, color: QColor) -> None:
        card = QLabel(f"{label}\n\n{message[:9000]}")
        card.setWordWrap(True)
        card.setTextInteractionFlags(Qt.TextSelectableByMouse)
        card.setStyleSheet(
            f"color: {color.name()}; background: rgba(5, 14, 32, 230); "
            "border: 1px solid rgba(0, 210, 255, 0.2); border-radius: 8px; padding: 14px;"
        )
        self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)


def _safe_url(url: str) -> bool:
    return url.startswith("https://") or url.startswith("http://")
