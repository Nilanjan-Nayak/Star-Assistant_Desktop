"""
STAR ASSISTANT — Frontend  (v3.0 — Ultra-Premium Futuristic HUD)

A PySide6 desktop-overlay implementing the HUD visual language from the
architecture bible: navy glass, cyan life, gold confirmation, corner brackets,
scanline materialize, and a central multi-ring reactor.

v3.0 Enhancements:
  - Gradient header bar with animated separator and breathing glow
  - Glowing input bar with animated focus border (cyan-purple gradient)
  - Animated status footer with live metrics and breathing indicators
  - Ambient floating particle dust overlay
  - Smoother window transitions with spring-like easing
  - Better layout spacing and visual hierarchy
  - Purple accent depth for premium feel

Controls (demo):
    Click the reactor (ORB mode)   -> expand to CENTER (command center)
    Esc (CENTER mode)              -> collapse back to ORB
    R                              -> toggle RAIL mode
    Q                              -> toggle quiet-hours dim
    Ctrl+C / close window          -> quit
"""

import sys
import math
import datetime
import random
from pathlib import Path

_frontend_dir = Path(__file__).resolve().parent
_backend_root = _frontend_dir.parent
if str(_frontend_dir) not in sys.path:
    sys.path.insert(0, str(_frontend_dir))
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, QRect, QPoint, QRectF, Property
from PySide6.QtGui import (
    QPainter, QColor, QFont, QGuiApplication, QPainterPath, QPen, QBrush,
    QRadialGradient, QLinearGradient
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGridLayout,
    QPushButton, QFrame, QLineEdit,
)

from tokens import (
    NAVY, CYAN, CYAN_GLOW, CYAN_NEON, GOLD, OK, AMBER, ALERT,
    MOTION, MODE_ORB, MODE_CENTER, MODE_RAIL,
    SPACE_ORB_MAX, SPACE_RAIL,
    FONT_HUD_FAMILY, FONT_HUD_SIZE_LG, FONT_HUD_SIZE, FONT_HUD_SIZE_SM, FONT_HUD_SIZE_XL,
    FONT_MONO_FAMILY, DEEP_NAVY, TEXT_DIM, TEXT_PRIMARY, TEXT_BRIGHT,
    GLASS_DEEP, GLASS_DARK, GLASS_MID, PURPLE_DIM, PURPLE_ACCENT,
    BORDER_GLOW_CYAN, BORDER_GLOW_PURPLE,
)
from reactor import Reactor, STATE_IDLE, STATE_THINK, STATE_SPEAK, STATE_ACT
from panels import GlassPanel
from content import ActivityLog, MissionCard
from Backend.bridge import AssistantBridge
from result_panel import ReachResultPanel

ORB_SIZE = SPACE_ORB_MAX + 48     # window padding around reactor for unclipped glow & shockwaves
CENTER_SIZE = (1020, 680)          # slightly larger for breathing room
RAIL_SIZE = (SPACE_RAIL, 0)       # height = screen height

DEMO_LOG_LINES = [
    ("▸ hey star, what's on my plate today", CYAN),
    ("[ROUTE] FAST PATH matched: calendar.today + weather.now", GOLD),
    ("[RECV] 2 events, rain after 5pm. umbrella note -> recap.", CYAN),
    ("[OK] verify-after-act: calendar read OK, weather fetch OK", GOLD),
]

DEMO_MISSIONS = [
    ("SKILL_047", "Weather — live conditions", "ok"),
    ("SKILL_112", "Calendar — today's events", "ok"),
    ("SKILL_364", "Desktop & System Control", "pending"),
    ("SKILL_281", "Inbox summary", "degraded"),
]


# ═══════════════════════════════════════════════════════════════════════
# Cached hex background — pre-render path once, repaint with breathing alpha
# ═══════════════════════════════════════════════════════════════════════

class HexBackground(QWidget):
    """Faint breathing hex grid — hex.breathe, 6s loop, opacity 0.04-0.09.
    Purely decorative; sits behind panels. Hex geometry is pre-cached as a
    QPainterPath so each frame is a single drawPath() call.
    v3.0: Added subtle purple color shift in breathing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._t = 0.0
        self._cached_path = None
        self._cached_size = (0, 0)

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        self._t += 0.05 / (MOTION["hex.breathe"] / 1000.0)
        self.update()

    def _rebuild_cache(self, w, h):
        """Pre-compute entire hex grid as a single QPainterPath."""
        path = QPainterPath()
        size = 34  # slightly larger hexes
        hex_h = size * 0.87
        r = size / 2
        row = 0
        y = -hex_h
        while y < h + hex_h:
            x_offset = (size * 0.75) if row % 2 else 0
            x = -size + x_offset
            while x < w + size:
                pts = []
                for i in range(6):
                    angle = math.pi / 3 * i
                    pts.append((x + r * math.cos(angle), y + r * math.sin(angle)))
                for i in range(6):
                    x1, y1 = pts[i]
                    x2, y2 = pts[(i + 1) % 6]
                    path.moveTo(x1, y1)
                    path.lineTo(x2, y2)
                x += size * 1.5
            y += hex_h
            row += 1
        self._cached_path = path
        self._cached_size = (w, h)

    def paintEvent(self, event):
        p = QPainter(self)
        if not p.isActive():
            return
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            w, h = self.width(), self.height()
            if (w, h) != self._cached_size or self._cached_path is None:
                self._rebuild_cache(w, h)

            # In ORB mode, apply a soft radial vignette so the hex grid fades smoothly to 0% alpha
            # without hard square clipping boundaries.
            is_orb = getattr(self.parent(), "mode", None) == MODE_ORB
            phase = 0.5 + 0.5 * math.sin(self._t * 2 * math.pi)
            alpha = 0.03 + 0.05 * phase

            # v3.0: subtle color shift between cyan and purple
            cyan_mix = 0.7 + 0.3 * phase
            r_val = int(0 + 80 * (1 - cyan_mix))
            g_val = int(200 * cyan_mix + 50 * (1 - cyan_mix))
            b_val = 255

            if is_orb:
                cx, cy = w / 2.0, h / 2.0
                r_max = min(w, h) / 2.0
                mask_grad = QRadialGradient(cx, cy, r_max)
                c_center = QColor(r_val, g_val, b_val)
                c_center.setAlphaF(alpha)
                c_mid = QColor(r_val, g_val, b_val)
                c_mid.setAlphaF(alpha * 0.3)
                c_zero = QColor(r_val, g_val, b_val)
                c_zero.setAlphaF(0.0)
                mask_grad.setColorAt(0.0, c_center)
                mask_grad.setColorAt(0.6, c_mid)
                mask_grad.setColorAt(0.9, c_zero)
                p.setPen(QPen(QBrush(mask_grad), 0.6))
            else:
                pen_color = QColor(r_val, g_val, b_val)
                pen_color.setAlphaF(alpha)
                p.setPen(QPen(pen_color, 0.5))

            p.drawPath(self._cached_path)
        finally:
            if p.isActive():
                p.end()


# ═══════════════════════════════════════════════════════════════════════
# Floating ambient particles (v3.0)
# ═══════════════════════════════════════════════════════════════════════

class AmbientParticles(QWidget):
    """Subtle floating particles for futuristic depth atmosphere."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._t = 0.0
        self._particles = []
        for _ in range(18):
            self._particles.append({
                "x": random.uniform(0.05, 0.95),
                "y": random.uniform(0.05, 0.95),
                "speed": random.uniform(0.0003, 0.0012),
                "drift_x": random.uniform(-0.0004, 0.0004),
                "size": random.uniform(1.2, 2.8),
                "alpha": random.uniform(0.08, 0.25),
                "phase": random.uniform(0, math.pi * 2),
            })

        self._timer = QTimer(self)
        self._timer.setInterval(45)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        self._t += 0.045
        for pt in self._particles:
            pt["y"] -= pt["speed"]
            pt["x"] += pt["drift_x"] + 0.0001 * math.sin(self._t + pt["phase"])
            if pt["y"] < -0.02:
                pt["y"] = 1.02
                pt["x"] = random.uniform(0.05, 0.95)
            if pt["x"] < -0.02 or pt["x"] > 1.02:
                pt["x"] = random.uniform(0.05, 0.95)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        if not p.isActive():
            return
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            w, h = self.width(), self.height()
            for pt in self._particles:
                px = pt["x"] * w
                py = pt["y"] * h
                breath = 0.5 + 0.5 * math.sin(self._t * 0.8 + pt["phase"])
                alpha = pt["alpha"] * (0.4 + 0.6 * breath)
                size = pt["size"] * (0.8 + 0.4 * breath)

                # Particle glow
                glow_c = QColor(0, 210, 255, int(alpha * 80))
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(glow_c))
                p.drawEllipse(int(px - size * 2), int(py - size * 2),
                             int(size * 4), int(size * 4))

                # Core dot
                core_c = QColor(180, 240, 255, int(alpha * 255))
                p.setBrush(QBrush(core_c))
                p.drawEllipse(int(px - size / 2), int(py - size / 2),
                             int(size), int(size))
        finally:
            if p.isActive():
                p.end()


# ═══════════════════════════════════════════════════════════════════════
# Cyber control button (minimize / close / mode toggle)
# ═══════════════════════════════════════════════════════════════════════

class CyberButton(QPushButton):
    """Tiny HUD-styled control button with hover glow — v3.0 premium."""

    def __init__(self, text: str, color: QColor = CYAN, parent=None):
        super().__init__(text, parent)
        self._color = color
        self.setFixedSize(30, 24)
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM, QFont.Bold))
        self._set_style(False)

    def _set_style(self, hovered: bool):
        c = self._color
        bg_alpha = 45 if hovered else 10
        border_alpha = 130 if hovered else 40
        text_alpha = 255 if hovered else 170
        shadow = f"0 0 8px rgba({c.red()},{c.green()},{c.blue()},0.3)" if hovered else "none"
        self.setStyleSheet(
            f"QPushButton {{ "
            f"color: rgba({c.red()},{c.green()},{c.blue()},{text_alpha}); "
            f"background: rgba({c.red()},{c.green()},{c.blue()},{bg_alpha}); "
            f"border: 1px solid rgba({c.red()},{c.green()},{c.blue()},{border_alpha}); "
            f"border-radius: 5px; padding: 0px; }}"
        )

    def enterEvent(self, event):
        self._set_style(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_style(False)
        super().leaveEvent(event)


# ═══════════════════════════════════════════════════════════════════════
# Header bar (title + clock + status + controls) — v3.0 premium
# ═══════════════════════════════════════════════════════════════════════

class HeaderBar(QWidget):
    """Top bar with gradient glass backdrop, logo, live clock,
    online status, and control buttons."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedHeight(42)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 10, 0)
        layout.setSpacing(14)

        # Title with letter-spacing
        title = QLabel("✦  STAR  HUD  v3.0")
        title.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE_LG, QFont.DemiBold))
        title.setStyleSheet(
            f"color: {GOLD.name()}; letter-spacing: 5px; background: transparent;"
        )
        layout.addWidget(title)

        layout.addStretch(1)

        # Live clock
        self._clock = QLabel("")
        self._clock.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM))
        self._clock.setStyleSheet(
            f"color: rgba(150, 200, 230, 0.65); background: transparent;"
        )
        layout.addWidget(self._clock)

        # Status badge
        status_badge = QLabel("  ● NEURAL LINK  ")
        status_badge.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM - 1, QFont.DemiBold))
        status_badge.setStyleSheet(
            f"color: {OK.name()}; "
            f"background: rgba(61,255,154,0.06); "
            f"border: 1px solid rgba(61,255,154,0.25); "
            f"border-radius: 4px; padding: 1px 6px;"
        )
        layout.addWidget(status_badge)

        # Separator
        sep = QLabel("│")
        sep.setStyleSheet("color: rgba(0,212,255,0.2); background: transparent;")
        layout.addWidget(sep)

        # Control buttons
        self.btn_rail = CyberButton("◧", CYAN)
        self.btn_rail.setToolTip("Toggle RAIL mode (R)")
        layout.addWidget(self.btn_rail)

        self.btn_min = CyberButton("─", CYAN)
        self.btn_min.setToolTip("Minimize to ORB (Esc)")
        layout.addWidget(self.btn_min)

        self.btn_close = CyberButton("✕", QColor("#FF4D4D"))
        self.btn_close.setToolTip("Close (Ctrl+Q)")
        layout.addWidget(self.btn_close)

        # Clock tick
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start()
        self._update_clock()

    def _update_clock(self):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        self._clock.setText(f"SYS.TIME  {now}")

    def paintEvent(self, event):
        """Render gradient glass backdrop for the header."""
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Gradient glass backdrop
        grad = QLinearGradient(0, 0, w, 0)
        grad.setColorAt(0.0, QColor(4, 10, 26, 200))
        grad.setColorAt(0.5, QColor(6, 14, 32, 180))
        grad.setColorAt(1.0, QColor(4, 10, 26, 200))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRect(0, 0, w, h)

        # Bottom gradient separator line (cyan -> purple -> cyan)
        line_grad = QLinearGradient(0, h - 1, w, h - 1)
        line_grad.setColorAt(0.0, QColor(0, 200, 255, 0))
        line_grad.setColorAt(0.15, QColor(0, 200, 255, 80))
        line_grad.setColorAt(0.5, QColor(120, 80, 255, 60))
        line_grad.setColorAt(0.85, QColor(0, 200, 255, 80))
        line_grad.setColorAt(1.0, QColor(0, 200, 255, 0))
        p.setPen(QPen(QBrush(line_grad), 1.0))
        p.drawLine(0, h - 1, w, h - 1)

        p.end()


# ═══════════════════════════════════════════════════════════════════════
# Status footer bar (v3.0 — animated metrics)
# ═══════════════════════════════════════════════════════════════════════

class StatusFooter(QWidget):
    """Bottom status bar with live animated metrics and breathing indicators."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedHeight(26)
        self._t = 0.0

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        self._t += 0.05
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # Gradient backdrop
        grad = QLinearGradient(0, 0, w, 0)
        grad.setColorAt(0.0, QColor(3, 8, 20, 210))
        grad.setColorAt(0.5, QColor(5, 14, 30, 190))
        grad.setColorAt(1.0, QColor(3, 8, 20, 210))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRect(0, 0, w, h)

        # Top separator (gradient line)
        line_grad = QLinearGradient(0, 0, w, 0)
        line_grad.setColorAt(0.0, QColor(0, 200, 255, 0))
        line_grad.setColorAt(0.2, QColor(0, 200, 255, 50))
        line_grad.setColorAt(0.5, QColor(100, 70, 255, 35))
        line_grad.setColorAt(0.8, QColor(0, 200, 255, 50))
        line_grad.setColorAt(1.0, QColor(0, 200, 255, 0))
        p.setPen(QPen(QBrush(line_grad), 1.0))
        p.drawLine(0, 0, w, 0)

        phase = 0.5 + 0.5 * math.sin(self._t * 0.5)

        # Left metrics
        p.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM - 1))
        p.setPen(QColor(120, 180, 210, 130))

        # System status dot
        dot_alpha = int(100 + 80 * phase)
        p.setPen(QColor(61, 255, 154, dot_alpha))
        p.drawText(12, h - 8, "●")

        p.setPen(QColor(120, 180, 210, 130))
        p.drawText(24, h - 8, "SYSTEM NOMINAL")

        p.setPen(QColor(80, 150, 190, 100))
        p.drawText(145, h - 8, "•")

        p.setPen(QColor(120, 180, 210, 130))
        p.drawText(155, h - 8, "NEURAL LINK: ACTIVE")

        p.setPen(QColor(80, 150, 190, 100))
        p.drawText(300, h - 8, "•")

        latency = 8 + int(5 * phase)
        p.setPen(QColor(120, 180, 210, 130))
        p.drawText(310, h - 8, f"LATENCY: {latency}ms")

        # Right side: uptime
        now = datetime.datetime.now()
        uptime_str = now.strftime("%H:%M:%S")
        p.setPen(QColor(100, 160, 200, 110))
        uptime_text = f"UPTIME: {uptime_str}"
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(uptime_text)
        p.drawText(w - tw - 14, h - 8, uptime_text)

        p.end()


# ═══════════════════════════════════════════════════════════════════════
# Glowing Input Bar (v3.0)
# ═══════════════════════════════════════════════════════════════════════

class GlowingInputBar(QWidget):
    """Premium input bar with animated focus glow border."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._focus_val = 0.0
        self._glow_t = 0.0

        self.input = QLineEdit(self)
        self.input.setPlaceholderText(
            "💬 Ask Star or type command (e.g. 'volume 20% barao', 'kemon acho')..."
        )
        self.input.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE))
        self.input.setStyleSheet(
            f"QLineEdit {{ "
            f"color: {TEXT_PRIMARY.name()}; "
            f"background: transparent; "
            f"border: none; "
            f"padding: 6px 16px; "
            f"selection-background-color: rgba(0, 200, 255, 0.25); "
            f"}} "
        )

        # Focus animations
        self.input.installEventFilter(self)

        # Glow timer
        self._glow_timer = QTimer(self)
        self._glow_timer.setInterval(35)
        self._glow_timer.timeout.connect(self._glow_tick)
        self._glow_timer.start()

    def _glow_tick(self):
        self._glow_t += 0.035 / (MOTION["glow.pulse"] / 1000.0)
        self.update()

    def eventFilter(self, obj, event):
        if obj == self.input:
            from PySide6.QtCore import QEvent
            if event.type() == QEvent.FocusIn:
                self._animate_focus(1.0)
            elif event.type() == QEvent.FocusOut:
                self._animate_focus(0.0)
        return super().eventFilter(obj, event)

    def _animate_focus(self, target):
        anim = QPropertyAnimation(self, b"focus_val")
        anim.setDuration(MOTION["input.focus"] if target > 0 else MOTION["input.blur"])
        anim.setStartValue(self._focus_val)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        self._focus_anim = anim

    def get_focus_val(self):
        return self._focus_val

    def set_focus_val(self, v):
        self._focus_val = v
        self.update()

    focus_val = Property(float, get_focus_val, set_focus_val)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.input.setGeometry(0, 0, self.width(), self.height())

    def paintEvent(self, event):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        rect = QRectF(0, 0, w, h)
        radius = 8
        focus = self._focus_val
        glow_phase = 0.5 + 0.5 * math.sin(self._glow_t * 2 * math.pi)

        # ── Glass background ──
        bg_alpha = int(210 + 30 * focus)
        glass = QLinearGradient(0, 0, w, 0)
        glass.setColorAt(0.0, QColor(5, 14, 32, bg_alpha))
        glass.setColorAt(0.5, QColor(8, 20, 44, bg_alpha - 10))
        glass.setColorAt(1.0, QColor(5, 14, 32, bg_alpha))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(glass))
        p.drawRoundedRect(rect, radius, radius)

        # ── Outer focus glow ──
        if focus > 0.05:
            glow_expand = 3 + 2 * focus
            glow_alpha = int(15 * focus * (0.6 + 0.4 * glow_phase))
            glow_grad = QRadialGradient(w / 2, h / 2, max(w, h) * 0.6)
            glow_grad.setColorAt(0.0, QColor(0, 200, 255, glow_alpha))
            glow_grad.setColorAt(0.5, QColor(100, 70, 255, int(glow_alpha * 0.4)))
            glow_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(glow_grad))
            p.drawRoundedRect(rect.adjusted(-glow_expand, -glow_expand,
                                            glow_expand, glow_expand),
                             radius + 2, radius + 2)

        # ── Animated gradient border ──
        border_alpha = int(50 + 90 * focus + 20 * glow_phase * focus)
        border_grad = QLinearGradient(0, 0, w, h)
        border_grad.setColorAt(0.0, QColor(0, 210, 255, border_alpha))
        border_grad.setColorAt(0.4, QColor(80, 60, 255, int(border_alpha * 0.5 * glow_phase)))
        border_grad.setColorAt(0.7, QColor(0, 220, 255, int(border_alpha * 0.8)))
        border_grad.setColorAt(1.0, QColor(100, 80, 255, int(border_alpha * 0.4)))

        border_w = 1.0 + 0.5 * focus
        p.setPen(QPen(QBrush(border_grad), border_w))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        p.end()


# ═══════════════════════════════════════════════════════════════════════
# Main window
# ═══════════════════════════════════════════════════════════════════════

class StarWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.mode = MODE_ORB
        self._quiet = False
        self._drag_pos = None   # for ORB dragging

        self.hex_bg = HexBackground(self)
        self.hex_bg.lower()

        # Ambient particles (v3.0)
        self.particles = AmbientParticles(self)
        self.particles.lower()

        self.reactor = Reactor(diameter=SPACE_ORB_MAX, parent=self)
        self.reactor.clicked.connect(self._on_reactor_clicked)

        # Header bar (shown in CENTER / RAIL mode)
        self.header = HeaderBar(self)
        self.header.btn_min.clicked.connect(self._collapse_to_orb)
        self.header.btn_close.clicked.connect(QApplication.quit)
        self.header.btn_rail.clicked.connect(self._toggle_rail)
        self.header.hide()

        # Status footer (v3.0)
        self.footer = StatusFooter(self)
        self.footer.hide()

        # Glass panels
        self.activity_log = ActivityLog(DEMO_LOG_LINES)
        self.log_panel = GlassPanel(self)
        self.log_panel.set_meta_tag("SYS.TELEMETRY // 01")
        self.log_panel.set_sector_tag("SECTOR.ACTIVE")
        self.log_panel.set_content(self.activity_log)
        self.log_panel.hide()

        self.mission_panel = GlassPanel(self)
        self.mission_panel.set_meta_tag("MISSION.RAIL // 02")
        self.mission_panel.set_sector_tag("SECTOR.ACTIVE")
        self._build_mission_grid()
        self.mission_panel.hide()

        # Agent Reach result surface. It reuses the same Star glass HUD; no
        # second result window is created for each Internet query.
        self._result_visible = False
        self.result_panel = GlassPanel(self)
        self.result_panel.set_meta_tag("REACH.RESULT // 03")
        self.result_panel.set_sector_tag("SECTOR.REACH")
        self.result_content = ReachResultPanel()
        self.result_panel.set_content(self.result_content)
        self.result_panel.hide()

        # HUD Command Input Bar (v3.0 — glowing)
        self.cmd_bar = GlowingInputBar(self)
        self.cmd_bar.input.returnPressed.connect(self._on_cmd_submitted)
        self.cmd_bar.hide()

        # Connect Assistant Brain Bridge (replaces dummy demo timer)
        self.bridge = AssistantBridge(self)
        self.bridge.state_changed.connect(self.reactor.set_state)
        self.bridge.log_emitted.connect(self._on_bridge_log)
        self.bridge.reach_result_ready.connect(self._on_reach_result)

        # Use native window opacity instead of QGraphicsOpacityEffect
        # to avoid QPainter buffer conflicts with child widget paintEvents
        self.setWindowOpacity(1.0)

        self._layout_orb()
        self._place_on_screen()

    # -- building content ---------------------------------------------------
    def _build_mission_grid(self):
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        grid = QVBoxLayout(content)
        grid.setSpacing(10)

        # Header with gradient
        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        dot = QLabel("◆")
        dot.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE_LG))
        dot.setStyleSheet(f"color: {CYAN_GLOW.name()}; background: transparent;")
        header_row.addWidget(dot)

        title = QLabel("MISSION RAIL")
        title.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE_LG, QFont.DemiBold))
        title.setStyleSheet(
            f"color: {TEXT_BRIGHT.name()}; letter-spacing: 3px; background: transparent;"
        )
        header_row.addWidget(title)
        header_row.addStretch(1)
        grid.addLayout(header_row)

        # Gradient rule (cyan -> purple -> cyan)
        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setStyleSheet(
            f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 transparent, "
            f"stop:0.1 rgba(0,212,255,40), "
            f"stop:0.3 rgba(0,212,255,90), "
            f"stop:0.5 rgba(120,80,255,55), "
            f"stop:0.7 rgba(0,212,255,90), "
            f"stop:0.9 rgba(0,212,255,40), "
            f"stop:1 transparent);"
        )
        grid.addWidget(rule)

        row_layout = QGridLayout()
        row_layout.setSpacing(10)
        for i, (sid, name, status) in enumerate(DEMO_MISSIONS):
            card = MissionCard(sid, name, status)
            row_layout.addWidget(card, i // 2, i % 2)
        grid.addLayout(row_layout)
        grid.addStretch(1)
        self.mission_panel.set_content(content)

    # -- geometry per mode ----------------------------------------------------
    def _layout_orb(self):
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.resize(ORB_SIZE, ORB_SIZE)
        self.reactor.setFixedSize(SPACE_ORB_MAX, SPACE_ORB_MAX)
        self.reactor.move((ORB_SIZE - SPACE_ORB_MAX) // 2, (ORB_SIZE - SPACE_ORB_MAX) // 2)
        self.hex_bg.setGeometry(0, 0, ORB_SIZE, ORB_SIZE)
        self.particles.setGeometry(0, 0, ORB_SIZE, ORB_SIZE)
        self.header.hide()
        self.footer.hide()
        self.log_panel.hide()
        self.mission_panel.hide()
        self.result_panel.hide()
        if hasattr(self, "cmd_bar"):
            self.cmd_bar.hide()

    def _place_on_screen(self):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        if self.mode == MODE_ORB:
            self.move(screen.right() - ORB_SIZE - 24, screen.bottom() - ORB_SIZE - 24)
        elif self.mode == MODE_CENTER:
            w, h = CENTER_SIZE
            self.move(screen.center().x() - w // 2, screen.center().y() - h // 2)
        elif self.mode == MODE_RAIL:
            self.move(screen.right() - SPACE_RAIL, screen.top())

    def _layout_center(self):
        w, h = CENTER_SIZE
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.resize(w, h)
        self.hex_bg.setGeometry(0, 0, w, h)
        self.particles.setGeometry(0, 0, w, h)

        self.reactor.setFixedSize(72, 72)
        self.reactor.move(24, 50)

        self.header.setGeometry(0, 6, w, 42)
        self.header.show()

        self.footer.setGeometry(0, h - 26, w, 26)
        self.footer.show()

        if self._result_visible:
            self.mission_panel.hide()
            self.log_panel.hide()
            self.result_panel.setGeometry(24, 96, w - 48, h - 186)
            self.result_panel.show()
        else:
            self.result_panel.hide()
            self.mission_panel.setGeometry(24, 130, w - 48, 225)
            self.log_panel.setGeometry(24, 368, w - 48, h - 368 - 90)

        if hasattr(self, "cmd_bar"):
            self.cmd_bar.setGeometry(24, h - 68, w - 48, 38)
            self.cmd_bar.show()

    def _layout_rail(self):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        w = SPACE_RAIL
        h = screen.height()
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.resize(w, h)
        self.hex_bg.setGeometry(0, 0, w, h)
        self.particles.setGeometry(0, 0, w, h)

        self.reactor.setFixedSize(58, 58)
        self.reactor.move(20, 48)

        self.header.setGeometry(0, 6, w, 42)
        self.header.show()

        self.footer.setGeometry(0, h - 26, w, 26)
        self.footer.show()

        if self._result_visible:
            self.mission_panel.hide()
            self.log_panel.hide()
            self.result_panel.setGeometry(16, 96, w - 32, h - 186)
            self.result_panel.show()
        else:
            self.result_panel.hide()
            self.mission_panel.setGeometry(16, 116, w - 32, 290)
            self.log_panel.setGeometry(16, 418, w - 32, h - 418 - 90)

        if hasattr(self, "cmd_bar"):
            self.cmd_bar.setGeometry(16, h - 68, w - 32, 38)
            self.cmd_bar.show()

    # -- mode transitions -----------------------------------------------------
    def _on_reactor_clicked(self):
        # If currently speaking, tapping the reactor stops speech immediately
        if hasattr(self, "bridge") and hasattr(self.bridge, "player") and self.bridge.player.is_playing():
            self.bridge.stop_speaking()
            return

        if self.mode == MODE_ORB:
            self._expand_to_center()
        elif self.mode == MODE_CENTER:
            # When clicked in Center mode, trigger greeting or prompt
            self.bridge.send_query("kemon acho")


    def _expand_to_center(self):
        self.mode = MODE_CENTER
        start_geo = self.geometry()
        self._layout_center()
        self._place_on_screen()
        end_geo = self.geometry()
        self.setGeometry(start_geo)

        anim = QPropertyAnimation(self, b"geometry")
        anim.setDuration(MOTION["mode.center"])
        anim.setStartValue(start_geo)
        anim.setEndValue(end_geo)
        anim.setEasingCurve(QEasingCurve.InOutCubic)
        anim.start()
        self._anim_ref = anim

        # Delay panel materialization until geometry is partially settled
        if self._result_visible:
            QTimer.singleShot(180, self.result_panel.materialize)
        else:
            QTimer.singleShot(120, self.mission_panel.materialize)
            QTimer.singleShot(220, self.log_panel.materialize)
        QTimer.singleShot(380, lambda: self.cmd_bar.input.setFocus())

    def _collapse_to_orb(self):
        self.reactor.set_state(STATE_IDLE)
        if hasattr(self, "cmd_bar"):
            self.cmd_bar.hide()

        def after_dismiss():
            self.mode = MODE_ORB
            start_geo = self.geometry()
            self._layout_orb()
            self._place_on_screen()
            end_geo = self.geometry()
            self.setGeometry(start_geo)

            anim = QPropertyAnimation(self, b"geometry")
            anim.setDuration(MOTION["mode.orb"])
            anim.setStartValue(start_geo)
            anim.setEndValue(end_geo)
            anim.setEasingCurve(QEasingCurve.InOutCubic)
            anim.start()
            self._anim_ref = anim

        self.mission_panel.dismiss()
        self.log_panel.dismiss(on_finished=after_dismiss)
        if self._result_visible:
            self.result_panel.dismiss()
            self._result_visible = False
        self.header.hide()
        self.footer.hide()

    def _toggle_rail(self):
        if self.mode == MODE_CENTER:
            self._switch_to_rail()
        elif self.mode == MODE_RAIL:
            self._switch_to_center_from_rail()

    def _switch_to_rail(self):
        self.mode = MODE_RAIL
        start_geo = self.geometry()

        # Dismiss panels, then re-layout and re-materialize
        self.mission_panel.dismiss()
        self.log_panel.dismiss()
        if self._result_visible:
            self.result_panel.dismiss()

        def do_rail():
            self._layout_rail()
            self._place_on_screen()
            end_geo = self.geometry()
            self.setGeometry(start_geo)

            anim = QPropertyAnimation(self, b"geometry")
            anim.setDuration(MOTION["mode.rail"])
            anim.setStartValue(start_geo)
            anim.setEndValue(end_geo)
            anim.setEasingCurve(QEasingCurve.InOutCubic)
            anim.start()
            self._anim_ref = anim

            if self._result_visible:
                QTimer.singleShot(200, self.result_panel.materialize)
            else:
                QTimer.singleShot(200, self.mission_panel.materialize)
                QTimer.singleShot(300, self.log_panel.materialize)

        QTimer.singleShot(MOTION["panel.out"] + 50, do_rail)

    def _switch_to_center_from_rail(self):
        self.mode = MODE_CENTER
        start_geo = self.geometry()

        self.mission_panel.dismiss()
        self.log_panel.dismiss()
        if self._result_visible:
            self.result_panel.dismiss()

        def do_center():
            self._layout_center()
            self._place_on_screen()
            end_geo = self.geometry()
            self.setGeometry(start_geo)

            anim = QPropertyAnimation(self, b"geometry")
            anim.setDuration(MOTION["mode.center"])
            anim.setStartValue(start_geo)
            anim.setEndValue(end_geo)
            anim.setEasingCurve(QEasingCurve.InOutCubic)
            anim.start()
            self._anim_ref = anim

            if self._result_visible:
                QTimer.singleShot(200, self.result_panel.materialize)
            else:
                QTimer.singleShot(200, self.mission_panel.materialize)
                QTimer.singleShot(300, self.log_panel.materialize)

        QTimer.singleShot(MOTION["panel.out"] + 50, do_center)

    # -- Brain & Command handling ----------------------------------------------
    def _on_cmd_submitted(self):
        text = self.cmd_bar.input.text().strip()
        if text:
            self.cmd_bar.input.clear()
            self.bridge.send_query(text)

    def _on_reach_result(self, result: dict):
        """Render Internet results in the existing Star visual desktop."""
        self._result_visible = True
        self.result_content.set_result(result)
        if self.mode == MODE_ORB:
            self._expand_to_center()
        else:
            self._layout_center() if self.mode == MODE_CENTER else self._layout_rail()
            QTimer.singleShot(120, self.result_panel.materialize)

    def _on_bridge_log(self, text: str, color_tag: str):
        cmap = {
            "cyan": CYAN,
            "gold": GOLD,
            "ok": OK,
            "alert": ALERT,
            "amber": AMBER,
        }
        color = cmap.get(color_tag.lower(), CYAN)
        if hasattr(self, "activity_log") and self.activity_log:
            self.activity_log.append_log(text, color)

    # -- quiet hours (quiet.dim, 800ms, opacity 1->0.35) ------------------------
    def _toggle_quiet(self):
        self._quiet = not self._quiet
        anim = QPropertyAnimation(self, b"windowOpacity")
        anim.setDuration(MOTION["quiet.dim"])
        anim.setStartValue(self.windowOpacity())
        anim.setEndValue(0.35 if self._quiet else 1.0)
        anim.start()
        self._quiet_anim_ref = anim

    # -- ORB dragging ----------------------------------------------------------
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.mode == MODE_ORB:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and self.mode == MODE_ORB:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    # -- keyboard events -------------------------------------------------------
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            if self.mode in (MODE_CENTER, MODE_RAIL):
                self._collapse_to_orb()
        elif event.key() == Qt.Key_Q:
            if event.modifiers() & Qt.ControlModifier:
                QApplication.quit()
            else:
                self._toggle_quiet()
        elif event.key() == Qt.Key_R:
            if self.mode in (MODE_CENTER, MODE_RAIL):
                self._toggle_rail()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        if hasattr(self, "bridge") and self.bridge:
            self.bridge.shutdown()
        super().closeEvent(event)

    def paintEvent(self, event):
        # Window itself stays fully transparent; children paint their own glass.
        super().paintEvent(event)


def main():
    import signal
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = QApplication(sys.argv)

    # Periodic timer to allow Python signal handling and clean Ctrl+C shutdown
    timer = QTimer()
    timer.timeout.connect(lambda: None)
    timer.start(500)

    win = StarWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
