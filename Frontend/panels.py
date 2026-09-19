"""
Glass panels — Ultra-Premium Futuristic Glass (v3.0)

panel.in   500ms  trace+scan   corner brackets draw, scanline fill, content fade
panel.out  320ms  sweep        content fades, frame sweeps to Z

v3.0 Enhancements:
  - Animated breathing neon border glow with cyan-purple gradient
  - Multi-layer frosted glass with 5-stop depth gradient
  - Inner edge glow for volumetric depth
  - Enhanced corner brackets with glowing nodes
  - Ambient purple accent wash
  - Smooth opacity transitions
"""

import math

from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, Property, QRectF, QTimer
from PySide6.QtGui import (
    QPainter, QPen, QBrush, QColor, QLinearGradient, QFont, QRadialGradient
)
from PySide6.QtWidgets import QWidget, QGraphicsOpacityEffect, QVBoxLayout

from tokens import (
    NAVY, CYAN, CYAN_GLOW, CYAN_NEON, GOLD, DEEP_NAVY, FROST_TOP, FROST_BOT,
    PANEL_RADIUS, BRACKET_INSET, BRACKET_ARM, LINE_WIDTH, MOTION, PANEL_BORDER,
    FONT_MONO_FAMILY, FONT_HUD_SIZE_SM, TEXT_DIM,
    GLASS_DEEP, GLASS_DARK, GLASS_MID, GLASS_LIGHT,
    PURPLE_GLOW, PURPLE_DIM, VIOLET_EDGE,
    BORDER_GLOW_CYAN, BORDER_GLOW_PURPLE,
)


class GlassPanel(QWidget):
    """A translucent ultra-premium glass card with:
    - Animated breathing neon border
    - Multi-layer frosted glass depth
    - Corner brackets with glowing intersection nodes
    - Scanline materialize-in / sweep-out transition
    - Ambient purple accent wash for depth
    Drop any content widget in via set_content()."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(22, 24, 22, 22)

        self._scan = 0.0        # 0..1 scanline fill progress (panel.in)
        self._bracket = 0.0     # 0..1 corner bracket draw progress
        self._sweep = 0.0       # 0..1 sweep-out progress (panel.out)
        self._glow_t = 0.0      # breathing glow timer (continuous)

        self._opacity_fx = QGraphicsOpacityEffect(self)
        self._opacity_fx.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_fx)

        # Optional meta tag text (e.g. "SYS.MONITOR // 01")
        self._meta_tag = ""
        self._sector_tag = ""   # optional sector label top-left

        # Breathing glow timer — runs always for ambient life
        self._glow_timer = QTimer(self)
        self._glow_timer.setInterval(33)  # ~30fps
        self._glow_timer.timeout.connect(self._glow_tick)
        self._glow_timer.start()

    def _glow_tick(self):
        self._glow_t += 0.033 / (MOTION["glow.pulse"] / 1000.0)
        if self._opacity_fx.opacity() > 0.01:
            self.update()

    def set_meta_tag(self, text: str):
        """Set a cyber meta-tag rendered at top-right of panel."""
        self._meta_tag = text

    def set_sector_tag(self, text: str):
        """Set a sector label rendered at top-left of panel."""
        self._sector_tag = text

    def set_content(self, widget: QWidget):
        # clear existing
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)
        self._layout.addWidget(widget)

    # -- animatable properties ------------------------------------------
    def get_scan(self):
        return self._scan

    def set_scan(self, v):
        self._scan = v
        self.update()

    scan = Property(float, get_scan, set_scan)

    def get_bracket(self):
        return self._bracket

    def set_bracket(self, v):
        self._bracket = v
        self.update()

    bracket = Property(float, get_bracket, set_bracket)

    def get_sweep(self):
        return self._sweep

    def set_sweep(self, v):
        self._sweep = v
        self.update()

    sweep = Property(float, get_sweep, set_sweep)

    def get_opacity(self):
        return self._opacity_fx.opacity()

    def set_opacity(self, v):
        self._opacity_fx.setOpacity(v)

    contentOpacity = Property(float, get_opacity, set_opacity)

    # -- transitions -------------------------------------------------------
    def materialize(self, on_finished=None):
        """panel.in — 500ms trace corners, scanline fill, fade content."""
        self.show()
        self._sweep = 0.0

        self._bracket_anim = QPropertyAnimation(self, b"bracket")
        self._bracket_anim.setDuration(int(MOTION["panel.in"] * 0.4))
        self._bracket_anim.setStartValue(0.0)
        self._bracket_anim.setEndValue(1.0)
        self._bracket_anim.setEasingCurve(QEasingCurve.OutCubic)

        self._scan_anim = QPropertyAnimation(self, b"scan")
        self._scan_anim.setDuration(int(MOTION["panel.in"] * 0.65))
        self._scan_anim.setStartValue(0.0)
        self._scan_anim.setEndValue(1.0)
        self._scan_anim.setEasingCurve(QEasingCurve.OutQuad)

        self._fade_anim = QPropertyAnimation(self, b"contentOpacity")
        self._fade_anim.setDuration(MOTION["panel.in"])
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)

        self._bracket_anim.start()
        self._scan_anim.start()
        self._fade_anim.start()
        if on_finished:
            self._fade_anim.finished.connect(on_finished)

    def dismiss(self, on_finished=None):
        """panel.out — 320ms content fades, frame sweeps to Z."""
        self._sweep_anim = QPropertyAnimation(self, b"sweep")
        self._sweep_anim.setDuration(MOTION["panel.out"])
        self._sweep_anim.setStartValue(0.0)
        self._sweep_anim.setEndValue(1.0)
        self._sweep_anim.setEasingCurve(QEasingCurve.InCubic)

        self._fade_out_anim = QPropertyAnimation(self, b"contentOpacity")
        self._fade_out_anim.setDuration(int(MOTION["panel.out"] * 0.7))
        self._fade_out_anim.setStartValue(1.0)
        self._fade_out_anim.setEndValue(0.0)

        self._sweep_anim.start()
        self._fade_out_anim.start()

        def finish():
            self.hide()
            if on_finished:
                on_finished()

        self._sweep_anim.finished.connect(finish)

    # -- painting -----------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # sweep-out: slide frame off to the right as we dismiss
        offset_x = self._sweep * w * 0.12
        rect = QRectF(offset_x, 0, w - offset_x, h)
        sweep_alpha = 1.0 - self._sweep

        # Breathing glow phase
        glow_phase = 0.5 + 0.5 * math.sin(self._glow_t * 2 * math.pi)
        glow_phase_offset = 0.5 + 0.5 * math.sin(self._glow_t * 2 * math.pi + 1.2)

        # ── Layer 1: Outer ambient glow shadow ──
        if sweep_alpha > 0.1:
            outer_glow = QRadialGradient(rect.center().x(), rect.center().y(),
                                          max(rect.width(), rect.height()) * 0.7)
            gc1 = QColor(0, 180, 255, int(8 * sweep_alpha * glow_phase))
            gc2 = QColor(100, 60, 255, int(4 * sweep_alpha * glow_phase_offset))
            gc0 = QColor(0, 0, 0, 0)
            outer_glow.setColorAt(0.0, gc1)
            outer_glow.setColorAt(0.5, gc2)
            outer_glow.setColorAt(1.0, gc0)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(outer_glow))
            expand = 6
            p.drawRoundedRect(QRectF(rect.left() - expand, rect.top() - expand,
                                      rect.width() + expand * 2, rect.height() + expand * 2),
                              PANEL_RADIUS + 4, PANEL_RADIUS + 4)

        # ── Layer 2: Multi-stop frosted glass background ──
        glass_grad = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
        c1 = QColor(GLASS_DEEP)
        c1.setAlpha(int(c1.alpha() * sweep_alpha))
        c2 = QColor(GLASS_DARK)
        c2.setAlpha(int(c2.alpha() * sweep_alpha))
        c3 = QColor(GLASS_MID)
        c3.setAlpha(int(c3.alpha() * sweep_alpha))
        c4 = QColor(6, 14, 32)
        c4.setAlpha(int(240 * sweep_alpha))
        c5 = QColor(GLASS_DEEP)
        c5.setAlpha(int(c5.alpha() * sweep_alpha))
        glass_grad.setColorAt(0.0, c1)
        glass_grad.setColorAt(0.15, c2)
        glass_grad.setColorAt(0.4, c3)
        glass_grad.setColorAt(0.75, c4)
        glass_grad.setColorAt(1.0, c5)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(glass_grad))
        p.drawRoundedRect(rect, PANEL_RADIUS, PANEL_RADIUS)

        # ── Layer 3: Purple accent wash (subtle depth effect) ──
        purple_wash = QLinearGradient(rect.right() - 80, rect.top(),
                                      rect.right(), rect.bottom())
        pw1 = QColor(PURPLE_DIM)
        pw1.setAlpha(int(pw1.alpha() * sweep_alpha * 0.7))
        pw0 = QColor(0, 0, 0, 0)
        purple_wash.setColorAt(0.0, pw0)
        purple_wash.setColorAt(0.6, pw1)
        purple_wash.setColorAt(1.0, pw0)
        p.setBrush(QBrush(purple_wash))
        p.drawRoundedRect(rect, PANEL_RADIUS, PANEL_RADIUS)

        # ── Layer 4: Top-edge frosted glass refraction highlight ──
        hl_rect = QRectF(rect.left() + 12, rect.top() + 1, rect.width() - 24, 1.5)
        hl_grad = QLinearGradient(hl_rect.left(), 0, hl_rect.right(), 0)
        hl_base = int(65 * sweep_alpha)
        hl_c = QColor(180, 240, 255, hl_base)
        hl_t = QColor(180, 240, 255, 0)
        hl_p = QColor(160, 140, 255, int(hl_base * 0.5))
        hl_grad.setColorAt(0.0, hl_t)
        hl_grad.setColorAt(0.2, hl_c)
        hl_grad.setColorAt(0.5, hl_p)
        hl_grad.setColorAt(0.8, hl_c)
        hl_grad.setColorAt(1.0, hl_t)
        p.setBrush(QBrush(hl_grad))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(hl_rect, 1, 1)

        # ── Layer 5: Inner edge glow (volumetric depth) ──
        inner_glow = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.top() + 40)
        ig1 = QColor(0, 200, 255, int(12 * sweep_alpha))
        ig0 = QColor(0, 0, 0, 0)
        inner_glow.setColorAt(0.0, ig1)
        inner_glow.setColorAt(1.0, ig0)
        p.setBrush(QBrush(inner_glow))
        p.drawRoundedRect(QRectF(rect.left() + 2, rect.top() + 2,
                                  rect.width() - 4, 38),
                          PANEL_RADIUS - 2, PANEL_RADIUS - 2)

        # ── Corner brackets with glowing intersection nodes ──
        bracket_alpha = int(200 * self._bracket * sweep_alpha)
        pen = QPen(QColor(CYAN.red(), CYAN.green(), CYAN.blue(), bracket_alpha),
                   LINE_WIDTH)
        p.setPen(pen)
        arm = BRACKET_ARM * self._bracket
        inset = BRACKET_INSET
        corners = [
            (rect.left() + inset, rect.top() + inset, 1, 1),
            (rect.right() - inset, rect.top() + inset, -1, 1),
            (rect.left() + inset, rect.bottom() - inset, 1, -1),
            (rect.right() - inset, rect.bottom() - inset, -1, -1),
        ]
        for x, y, dx, dy in corners:
            # L-bracket arms
            p.drawLine(int(x), int(y), int(x + arm * dx), int(y))
            p.drawLine(int(x), int(y), int(x), int(y + arm * dy))

            # Glowing node at corner intersection
            if self._bracket > 0.4:
                node_progress = (self._bracket - 0.4) / 0.6
                node_alpha = int(180 * node_progress * sweep_alpha)
                node_glow = int(40 * node_progress * sweep_alpha * glow_phase)

                # Glow halo
                glow_r = 4 + 2 * glow_phase
                glow_c = QColor(0, 230, 255, node_glow)
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(glow_c))
                p.drawEllipse(int(x - glow_r), int(y - glow_r),
                             int(glow_r * 2), int(glow_r * 2))

                # Crosshair
                cross_c = QColor(CYAN_GLOW.red(), CYAN_GLOW.green(),
                                CYAN_GLOW.blue(), node_alpha)
                p.setPen(QPen(cross_c, 1.0))
                cs = 4.0
                p.drawLine(int(x - cs), int(y), int(x + cs), int(y))
                p.drawLine(int(x), int(y - cs), int(x), int(y + cs))

                # Bright center dot
                dot_c = QColor(200, 240, 255, node_alpha)
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(dot_c))
                p.drawEllipse(int(x - 1), int(y - 1), 3, 3)

                p.setPen(pen)  # restore bracket pen

        # ── Scanline fill sweeping top->bottom on materialize ──
        if 0.0 < self._scan < 1.0:
            scan_y = rect.top() + rect.height() * self._scan
            grad = QLinearGradient(0, scan_y - 20, 0, scan_y + 20)
            c_t = QColor(CYAN)
            c_t.setAlpha(0)
            grad.setColorAt(0.0, c_t)
            c_c1 = QColor(0, 200, 255, 50)
            grad.setColorAt(0.35, c_c1)
            c_w = QColor(200, 240, 255, 60)
            grad.setColorAt(0.48, c_w)
            c_p = QColor(140, 120, 255, 35)
            grad.setColorAt(0.52, c_p)
            c_c2 = QColor(0, 200, 255, 40)
            grad.setColorAt(0.65, c_c2)
            grad.setColorAt(1.0, c_t)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(grad))
            p.drawRect(QRectF(rect.left(), scan_y - 20, rect.width(), 40))

        # ── Animated breathing neon border ──
        # Two-tone gradient border: cyan + purple shift
        border_alpha_base = 35 + int(25 * glow_phase)
        border_alpha = int(border_alpha_base * sweep_alpha + 20 * self._bracket)

        border_grad = QLinearGradient(rect.left(), rect.top(),
                                      rect.right(), rect.bottom())
        bc1 = QColor(0, 210, 255, border_alpha)
        bc2 = QColor(120, 90, 255, int(border_alpha * 0.6 * glow_phase_offset))
        bc3 = QColor(0, 200, 255, int(border_alpha * 0.8))
        border_grad.setColorAt(0.0, bc1)
        border_grad.setColorAt(0.5, bc2)
        border_grad.setColorAt(1.0, bc3)

        outline = QPen(QBrush(border_grad), LINE_WIDTH)
        p.setPen(outline)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5),
                          PANEL_RADIUS, PANEL_RADIUS)

        # ── Sector tag (top-left) ──
        if self._sector_tag and self._bracket > 0.7:
            tag_a = int(80 * (self._bracket - 0.7) / 0.3 * sweep_alpha)
            p.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM - 1))
            p.setPen(QColor(0, 200, 255, tag_a))
            tag_r = QRectF(rect.left() + 14, rect.top() + 7, 180, 14)
            p.drawText(tag_r, Qt.AlignLeft | Qt.AlignVCenter, self._sector_tag)

        # ── Meta tag (top-right) ──
        if self._meta_tag and self._bracket > 0.7:
            tag_alpha = int(90 * (self._bracket - 0.7) / 0.3 * sweep_alpha)
            p.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM - 1))
            p.setPen(QColor(TEXT_DIM.red(), TEXT_DIM.green(), TEXT_DIM.blue(), tag_alpha))
            tag_rect = QRectF(rect.right() - 190, rect.top() + 7, 180, 14)
            p.drawText(tag_rect, Qt.AlignRight | Qt.AlignVCenter, self._meta_tag)

        # ── Data integrity indicator (bottom-right) ──
        if self._bracket > 0.9:
            di_a = int(60 * (self._bracket - 0.9) * 10 * sweep_alpha)
            di_val = 95 + int(4.5 * glow_phase)
            p.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM - 2))
            p.setPen(QColor(TEXT_DIM.red(), TEXT_DIM.green(), TEXT_DIM.blue(), di_a))
            di_r = QRectF(rect.right() - 160, rect.bottom() - 16, 150, 12)
            p.drawText(di_r, Qt.AlignRight | Qt.AlignVCenter,
                       f"DATA INTEGRITY: {di_val}.{int(glow_phase * 9)}% ● NOM")

        p.end()
