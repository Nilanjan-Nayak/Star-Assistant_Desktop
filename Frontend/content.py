"""
Content widgets hosted inside GlassPanel instances (v2.0 — Premium)

Typewriter telemetry log with timestamps & blinking cyber cursor,
interactive hover mission cards with neon status pills.
"""

import datetime

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, Property
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
    QGraphicsOpacityEffect,
)

from tokens import (
    CYAN, CYAN_GLOW, GOLD, OK, AMBER, ALERT,
    FONT_MONO_FAMILY, FONT_HUD_FAMILY,
    FONT_HUD_SIZE, FONT_HUD_SIZE_LG, FONT_HUD_SIZE_SM,
    MOTION, TEXT_PRIMARY, TEXT_DIM,
)


def _qcolor_css(c: QColor, alpha=None) -> str:
    if alpha is not None:
        c = QColor(c)
        c.setAlpha(alpha)
        return f"rgba({c.red()},{c.green()},{c.blue()},{c.alphaF():.2f})"
    return c.name()


# ──────────────────────────────────────────────────────────────────────
# Typewriter line with timestamp prefix and blinking cursor
# ──────────────────────────────────────────────────────────────────────

class TypewriterLine(QLabel):
    """One line in the activity log, revealed glyph by glyph with a
    blinking block cursor and optional timestamp prefix."""

    def __init__(self, text: str, color: QColor = CYAN, show_timestamp: bool = True, parent=None):
        super().__init__(parent)
        self._full_text = text
        self._color = color
        self._i = 0
        self._cursor_visible = True
        self._done = False
        self.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE))
        self.setStyleSheet(f"color: {_qcolor_css(color)}; background: transparent;")
        self.setTextFormat(Qt.RichText)

        # Timestamp prefix
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        ts_color = _qcolor_css(TEXT_DIM)
        self._prefix = f'<span style="color:{ts_color}">[{ts}] </span>' if show_timestamp else ""
        self.setText(self._prefix)

        # Typing timer
        self._timer = QTimer(self)
        self._timer.setInterval(MOTION["type.log"])
        self._timer.timeout.connect(self._advance)
        self._timer.start()

        # Cursor blink timer
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(MOTION["cursor.blink"])
        self._blink_timer.timeout.connect(self._toggle_cursor)
        self._blink_timer.start()

    def _advance(self):
        self._i += 1
        self._update_display()
        if self._i >= len(self._full_text):
            self._timer.stop()
            self._done = True

    def _toggle_cursor(self):
        self._cursor_visible = not self._cursor_visible
        self._update_display()

    def _update_display(self):
        typed = self._full_text[:self._i]
        cursor = '<span style="color:#00D4FF">▌</span>' if self._cursor_visible and not self._done else ""
        self.setText(self._prefix + typed + cursor)


# ──────────────────────────────────────────────────────────────────────
# Activity log panel
# ──────────────────────────────────────────────────────────────────────

class ActivityLog(QWidget):
    """Scrolling-free, fixed activity log panel content: header + typewriter lines."""

    def __init__(self, lines, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self)
        layout.setSpacing(7)

        header = QLabel("◆  ACTIVITY TELEMETRY")
        header.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE_LG, QFont.DemiBold))
        header.setStyleSheet(f"color: {_qcolor_css(GOLD)}; letter-spacing: 2px; background: transparent;")
        layout.addWidget(header)

        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setStyleSheet(
            f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 transparent, stop:0.2 {_qcolor_css(CYAN, 80)}, "
            f"stop:0.8 {_qcolor_css(CYAN, 80)}, stop:1 transparent);"
        )
        layout.addWidget(rule)

        for i, (text, color) in enumerate(lines):
            delay = i * 600
            self._add_delayed_line(layout, text, color, delay)

        layout.addStretch(1)

    def _add_delayed_line(self, layout, text, color, delay_ms):
        placeholder = QLabel("")
        placeholder.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE))
        placeholder.setFixedHeight(20)
        layout.addWidget(placeholder)

        def spawn():
            real = TypewriterLine(text, color, parent=self)
            layout.replaceWidget(placeholder, real)
            placeholder.deleteLater()

        QTimer.singleShot(delay_ms, spawn)

    def append_log(self, text: str, color: QColor = CYAN):
        """Append a live telemetry line with typewriter reveal."""
        layout = self.layout()
        count = layout.count()
        insert_idx = max(0, count - 1)
        line = TypewriterLine(text, color, parent=self)
        layout.insertWidget(insert_idx, line)
        if count > 10:
            first_item = layout.itemAt(2)
            if first_item and first_item.widget():
                w = first_item.widget()
                layout.removeWidget(w)
                w.deleteLater()


# ──────────────────────────────────────────────────────────────────────
# Status pill widget (neon badge)
# ──────────────────────────────────────────────────────────────────────

class StatusPill(QWidget):
    """A neon-styled status badge: [● ONLINE // VERIFIED]"""

    STATUS_CONFIG = {
        "ok":       {"label": "ONLINE",   "color": OK,    "suffix": "VERIFIED"},
        "pending":  {"label": "PENDING",  "color": GOLD,  "suffix": "QUEUED"},
        "degraded": {"label": "DEGRADED", "color": AMBER, "suffix": "RETRY"},
        "error":    {"label": "ERROR",    "color": ALERT, "suffix": "HALTED"},
    }

    def __init__(self, status: str = "ok", parent=None):
        super().__init__(parent)
        cfg = self.STATUS_CONFIG.get(status, self.STATUS_CONFIG["ok"])
        color = cfg["color"]
        label = cfg["label"]
        suffix = cfg["suffix"]

        self.setFixedHeight(20)
        self.setStyleSheet("background: transparent;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        pill = QLabel(f"  ● {label} // {suffix}  ")
        pill.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM - 1, QFont.DemiBold))
        pill.setStyleSheet(
            f"color: {_qcolor_css(color)}; "
            f"background: {_qcolor_css(color, 22)}; "
            f"border: 1px solid {_qcolor_css(color, 80)}; "
            f"border-radius: 4px; "
            f"padding: 1px 4px;"
        )
        layout.addWidget(pill)
        layout.addStretch(1)

        # Pulse dot animation for non-ok statuses
        if status == "pending":
            self._pulse_timer = QTimer(self)
            self._pulse_timer.setInterval(900)
            self._pulse_visible = True
            self._pill_ref = pill
            self._color = color
            self._label = label
            self._suffix = suffix
            self._pulse_timer.timeout.connect(self._pulse)
            self._pulse_timer.start()

    def _pulse(self):
        self._pulse_visible = not self._pulse_visible
        dot = "●" if self._pulse_visible else "○"
        self._pill_ref.setText(f"  {dot} {self._label} // {self._suffix}  ")


# ──────────────────────────────────────────────────────────────────────
# Interactive mission card with hover effects
# ──────────────────────────────────────────────────────────────────────

class MissionCard(QFrame):
    """A single skill/mission card with hover glow lift effect
    and neon status pill."""

    def __init__(self, skill_id: str, name: str, status: str = "ok", parent=None):
        super().__init__(parent)
        self._base_bg_alpha = 18
        self._hover_val = 0.0
        self._status = status

        # Determine accent color from status
        status_colors = {"ok": OK, "pending": GOLD, "degraded": AMBER, "error": ALERT}
        self._accent = status_colors.get(status, CYAN)

        self.setStyleSheet(self._build_style(0.0))
        self.setMinimumHeight(76)
        self.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)

        # Top row: skill ID + status pill
        top = QHBoxLayout()
        id_label = QLabel(skill_id)
        id_label.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM))
        id_label.setStyleSheet(f"color: {_qcolor_css(CYAN, 160)}; background: transparent; border: none;")
        top.addWidget(id_label)
        top.addStretch(1)

        pill = StatusPill(status)
        top.addWidget(pill)
        layout.addLayout(top)

        # Name
        name_label = QLabel(name)
        name_label.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE))
        name_label.setStyleSheet(f"color: {_qcolor_css(TEXT_PRIMARY)}; background: transparent; border: none;")
        name_label.setWordWrap(True)
        layout.addWidget(name_label)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def _build_style(self, hover: float) -> str:
        bg_alpha = self._base_bg_alpha + int(hover * 22)
        border_alpha = 55 + int(hover * 60)
        accent = self._accent
        return (
            f"MissionCard {{ "
            f"background-color: rgba({accent.red()},{accent.green()},{accent.blue()},{bg_alpha / 255:.3f}); "
            f"border: 1px solid rgba({CYAN.red()},{CYAN.green()},{CYAN.blue()},{border_alpha / 255:.3f}); "
            f"border-radius: 9px; }}"
        )

    # hover property for animation
    def get_hover_val(self):
        return self._hover_val

    def set_hover_val(self, v):
        self._hover_val = v
        self.setStyleSheet(self._build_style(v))

    hover_val = Property(float, get_hover_val, set_hover_val)

    def enterEvent(self, event):
        anim = QPropertyAnimation(self, b"hover_val")
        anim.setDuration(MOTION["hover.glow"])
        anim.setStartValue(self._hover_val)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        self._hover_anim = anim
        super().enterEvent(event)

    def leaveEvent(self, event):
        anim = QPropertyAnimation(self, b"hover_val")
        anim.setDuration(MOTION["hover.out"])
        anim.setStartValue(self._hover_val)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.start()
        self._hover_anim = anim
        super().leaveEvent(event)
