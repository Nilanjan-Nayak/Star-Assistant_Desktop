"""
Content widgets hosted inside GlassPanel instances (v3.0 — Ultra-Premium)

Typewriter telemetry log with timestamps & blinking cyber cursor,
interactive hover mission cards with neon status pills and glass effects.

v3.0 Enhancements:
  - Glass-morphism mission cards with gradient accent bar
  - Animated status pills with breathing glow dots
  - Improved activity log with gradient dividers
  - Better typography and spacing
  - Smooth hover animations with subtle glow expansion
"""

import datetime
import math

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, Property, QRectF
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QBrush, QLinearGradient
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
    QGraphicsOpacityEffect,
)

from tokens import (
    CYAN, CYAN_GLOW, CYAN_NEON, GOLD, OK, AMBER, ALERT,
    FONT_MONO_FAMILY, FONT_HUD_FAMILY,
    FONT_HUD_SIZE, FONT_HUD_SIZE_LG, FONT_HUD_SIZE_SM, FONT_HUD_SIZE_XL,
    MOTION, TEXT_PRIMARY, TEXT_DIM, TEXT_BRIGHT, TEXT_CYAN, TEXT_GOLD,
    GLASS_DARK, GLASS_MID, PURPLE_DIM, PURPLE_ACCENT,
    ACCENT_BAR_TOP, ACCENT_BAR_BOT,
    STATUS_GLOW_OK, STATUS_GLOW_GOLD, STATUS_GLOW_AMBER, STATUS_GLOW_ALERT,
    PANEL_RADIUS,
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
        layout.setSpacing(8)

        # Header with gradient accent
        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        # Sector indicator dot
        dot = QLabel("◆")
        dot.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE_LG))
        dot.setStyleSheet(f"color: {_qcolor_css(CYAN_GLOW)}; background: transparent;")
        header_row.addWidget(dot)

        header = QLabel("ACTIVITY TELEMETRY")
        header.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE_LG, QFont.DemiBold))
        header.setStyleSheet(
            f"color: {_qcolor_css(TEXT_BRIGHT)}; "
            f"letter-spacing: 3px; background: transparent;"
        )
        header_row.addWidget(header)
        header_row.addStretch(1)

        # Live indicator
        self._live_dot = QLabel("● LIVE")
        self._live_dot.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM - 1, QFont.DemiBold))
        self._live_dot.setStyleSheet(
            f"color: {_qcolor_css(OK, 180)}; background: {_qcolor_css(OK, 15)}; "
            f"border: 1px solid {_qcolor_css(OK, 50)}; border-radius: 3px; "
            f"padding: 1px 6px;"
        )
        header_row.addWidget(self._live_dot)
        layout.addLayout(header_row)

        # Gradient rule with cyan-purple gradient
        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setStyleSheet(
            f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 transparent, "
            f"stop:0.1 {_qcolor_css(CYAN, 40)}, "
            f"stop:0.3 {_qcolor_css(CYAN, 90)}, "
            f"stop:0.6 {_qcolor_css(PURPLE_ACCENT, 60)}, "
            f"stop:0.85 {_qcolor_css(CYAN, 70)}, "
            f"stop:1 transparent);"
        )
        layout.addWidget(rule)

        for i, (text, color) in enumerate(lines):
            delay = i * 500
            self._add_delayed_line(layout, text, color, delay)

        layout.addStretch(1)

        # Pulse live dot
        self._live_visible = True
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(MOTION["status.pulse"])
        self._live_timer.timeout.connect(self._pulse_live)
        self._live_timer.start()

    def _pulse_live(self):
        self._live_visible = not self._live_visible
        dot = "●" if self._live_visible else "○"
        self._live_dot.setText(f"{dot} LIVE")

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
# Status pill widget (neon badge) — v3.0 with glow dot
# ──────────────────────────────────────────────────────────────────────

class StatusPill(QWidget):
    """A neon-styled status badge with animated breathing glow: [● ONLINE // VERIFIED]"""

    STATUS_CONFIG = {
        "ok":       {"label": "ONLINE",   "color": OK,    "suffix": "VERIFIED", "glow": STATUS_GLOW_OK},
        "pending":  {"label": "PENDING",  "color": GOLD,  "suffix": "QUEUED",   "glow": STATUS_GLOW_GOLD},
        "degraded": {"label": "DEGRADED", "color": AMBER, "suffix": "RETRY",    "glow": STATUS_GLOW_AMBER},
        "error":    {"label": "ERROR",    "color": ALERT, "suffix": "HALTED",   "glow": STATUS_GLOW_ALERT},
    }

    def __init__(self, status: str = "ok", parent=None):
        super().__init__(parent)
        cfg = self.STATUS_CONFIG.get(status, self.STATUS_CONFIG["ok"])
        color = cfg["color"]
        label = cfg["label"]
        suffix = cfg["suffix"]

        self.setFixedHeight(22)
        self.setStyleSheet("background: transparent;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        pill = QLabel(f"  ● {label} // {suffix}  ")
        pill.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM - 1, QFont.DemiBold))
        pill.setStyleSheet(
            f"color: {_qcolor_css(color)}; "
            f"background: {_qcolor_css(color, 18)}; "
            f"border: 1px solid {_qcolor_css(color, 65)}; "
            f"border-radius: 4px; "
            f"padding: 2px 6px;"
        )
        layout.addWidget(pill)
        layout.addStretch(1)

        # Pulse dot animation for non-ok statuses
        if status in ("pending", "degraded"):
            self._pulse_timer = QTimer(self)
            self._pulse_timer.setInterval(900 if status == "pending" else 1200)
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
# Interactive mission card with glass effect & hover animations (v3.0)
# ──────────────────────────────────────────────────────────────────────

class MissionCard(QFrame):
    """A single skill/mission card with glass-morphism effect,
    gradient accent bar, and smooth hover glow lift."""

    def __init__(self, skill_id: str, name: str, status: str = "ok", parent=None):
        super().__init__(parent)
        self._base_bg_alpha = 14
        self._hover_val = 0.0
        self._status = status
        self._glow_t = 0.0

        # Determine accent color from status
        status_colors = {"ok": OK, "pending": GOLD, "degraded": AMBER, "error": ALERT}
        self._accent = status_colors.get(status, CYAN)

        self.setMinimumHeight(82)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_TranslucentBackground)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 14, 12)
        layout.setSpacing(5)

        # Top row: skill ID + status pill
        top = QHBoxLayout()
        id_label = QLabel(skill_id)
        id_label.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM))
        id_label.setStyleSheet(
            f"color: {_qcolor_css(CYAN, 170)}; background: transparent; border: none;"
        )
        top.addWidget(id_label)
        top.addStretch(1)

        pill = StatusPill(status)
        top.addWidget(pill)
        layout.addLayout(top)

        # Name
        name_label = QLabel(name)
        name_label.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE))
        name_label.setStyleSheet(
            f"color: {_qcolor_css(TEXT_PRIMARY)}; background: transparent; border: none;"
        )
        name_label.setWordWrap(True)
        layout.addWidget(name_label)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # Subtle breathing timer for the accent bar
        self._breathe_timer = QTimer(self)
        self._breathe_timer.setInterval(40)
        self._breathe_timer.timeout.connect(self._breathe_tick)
        self._breathe_timer.start()

    def _breathe_tick(self):
        self._glow_t += 0.04 / (MOTION["border.breathe"] / 1000.0)
        if self._hover_val > 0.01 or self.isVisible():
            self.update()

    # hover property for animation
    def get_hover_val(self):
        return self._hover_val

    def set_hover_val(self, v):
        self._hover_val = v
        self.update()

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

    def paintEvent(self, event):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        rect = QRectF(0, 0, w, h)
        hover = self._hover_val
        glow_phase = 0.5 + 0.5 * math.sin(self._glow_t * 2 * math.pi)

        # ── Glass background ──
        bg_alpha = self._base_bg_alpha + int(hover * 20)
        glass = QLinearGradient(0, 0, 0, h)
        glass.setColorAt(0.0, QColor(8, 18, 42, bg_alpha + 6))
        glass.setColorAt(0.5, QColor(5, 12, 30, bg_alpha + 10))
        glass.setColorAt(1.0, QColor(3, 8, 22, bg_alpha + 8))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(glass))
        radius = 10
        p.drawRoundedRect(rect, radius, radius)

        # ── Left accent bar (gradient) ──
        accent = self._accent
        bar_w = 3 + hover * 1.5
        bar_grad = QLinearGradient(0, 4, 0, h - 4)
        ba1 = QColor(accent.red(), accent.green(), accent.blue(),
                     int(160 + 60 * glow_phase))
        ba2 = QColor(accent.red(), accent.green(), accent.blue(),
                     int(80 + 40 * glow_phase))
        bar_grad.setColorAt(0.0, ba1)
        bar_grad.setColorAt(1.0, ba2)
        p.setBrush(QBrush(bar_grad))
        p.drawRoundedRect(QRectF(1, 6, bar_w, h - 12), 2, 2)

        # ── Accent bar glow ──
        if hover > 0.1 or glow_phase > 0.6:
            glow_alpha = int((15 + 15 * hover) * glow_phase)
            glow_grad = QLinearGradient(0, 0, 20, 0)
            glow_grad.setColorAt(0.0, QColor(accent.red(), accent.green(),
                                              accent.blue(), glow_alpha))
            glow_grad.setColorAt(1.0, QColor(accent.red(), accent.green(),
                                              accent.blue(), 0))
            p.setBrush(QBrush(glow_grad))
            p.drawRoundedRect(QRectF(0, 4, 22, h - 8), 4, 4)

        # ── Border ──
        border_alpha = int(30 + hover * 50 + 10 * glow_phase)
        border = QPen(QColor(CYAN.red(), CYAN.green(), CYAN.blue(), border_alpha), 1.0)
        p.setPen(border)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        # ── Top highlight ──
        if hover > 0.2:
            hl_a = int(30 * hover)
            hl = QLinearGradient(12, 1, w - 12, 1)
            hl.setColorAt(0.0, QColor(180, 240, 255, 0))
            hl.setColorAt(0.3, QColor(180, 240, 255, hl_a))
            hl.setColorAt(0.7, QColor(180, 240, 255, hl_a))
            hl.setColorAt(1.0, QColor(180, 240, 255, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(hl))
            p.drawRect(QRectF(12, 0.5, w - 24, 1.2))

        p.end()
