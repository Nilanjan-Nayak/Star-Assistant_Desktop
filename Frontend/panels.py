"""
Glass panels — Premium frosted cyber glass (v2.0)

panel.in   420ms  trace+scan   corner brackets draw, scanline fill, content fade
panel.out  280ms  sweep        content fades, frame sweeps to Z

Enhancements: frosted depth gradient, top-edge glass refraction, cyber
crosshair corners, ambient neon border glow, smooth opacity handling.
"""

from PySide6.QtCore import Qt, QPropertyAnimation, QEasingCurve, Property, QRectF
from PySide6.QtGui import (
    QPainter, QPen, QBrush, QColor, QLinearGradient, QFont
)
from PySide6.QtWidgets import QWidget, QGraphicsOpacityEffect, QVBoxLayout

from tokens import (
    NAVY, CYAN, CYAN_GLOW, GOLD, DEEP_NAVY, FROST_TOP, FROST_BOT,
    PANEL_RADIUS, BRACKET_INSET, LINE_WIDTH, MOTION, PANEL_BORDER,
    FONT_MONO_FAMILY, FONT_HUD_SIZE_SM, TEXT_DIM
)


class GlassPanel(QWidget):
    """A translucent navy card with frosted glass depth, cyan corner brackets
    with cyber crosshairs, and a scanline materialize-in / sweep-out transition.
    Drop any content widget in via set_content()."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(22, 22, 22, 22)

        self._scan = 0.0        # 0..1 scanline fill progress (panel.in)
        self._bracket = 0.0     # 0..1 corner bracket draw progress
        self._sweep = 0.0       # 0..1 sweep-out progress (panel.out)

        self._opacity_fx = QGraphicsOpacityEffect(self)
        self._opacity_fx.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_fx)

        # Optional meta tag text (e.g. "SYS.MONITOR // 01")
        self._meta_tag = ""

    def set_meta_tag(self, text: str):
        """Set a cyber meta-tag rendered at top-right of panel."""
        self._meta_tag = text

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
        """panel.in — 420ms trace corners, scanline fill, fade content."""
        self.show()
        self._sweep = 0.0

        self._bracket_anim = QPropertyAnimation(self, b"bracket")
        self._bracket_anim.setDuration(int(MOTION["panel.in"] * 0.4))
        self._bracket_anim.setStartValue(0.0)
        self._bracket_anim.setEndValue(1.0)
        self._bracket_anim.setEasingCurve(QEasingCurve.OutCubic)

        self._scan_anim = QPropertyAnimation(self, b"scan")
        self._scan_anim.setDuration(int(MOTION["panel.in"] * 0.6))
        self._scan_anim.setStartValue(0.0)
        self._scan_anim.setEndValue(1.0)
        self._scan_anim.setEasingCurve(QEasingCurve.Linear)

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
        """panel.out — 280ms content fades, frame sweeps to Z."""
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
        offset_x = self._sweep * w * 0.15
        rect = QRectF(offset_x, 0, w - offset_x, h)

        # ── Frosted glass background with depth gradient ──
        sweep_alpha = 1.0 - self._sweep
        glass_grad = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
        c_top = QColor(DEEP_NAVY)
        c_top.setAlpha(int(220 * sweep_alpha))
        c_mid = QColor(NAVY)
        c_mid.setAlpha(int(210 * sweep_alpha))
        c_bot = QColor(8, 14, 30)
        c_bot.setAlpha(int(230 * sweep_alpha))
        glass_grad.setColorAt(0.0, c_top)
        glass_grad.setColorAt(0.5, c_mid)
        glass_grad.setColorAt(1.0, c_bot)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(glass_grad))
        p.drawRoundedRect(rect, PANEL_RADIUS, PANEL_RADIUS)

        # ── Top-edge frosted glass refraction highlight ──
        highlight_rect = QRectF(rect.left() + 8, rect.top() + 1, rect.width() - 16, 2.5)
        hl_grad = QLinearGradient(highlight_rect.left(), 0, highlight_rect.right(), 0)
        hl_c = QColor(180, 230, 255, int(55 * sweep_alpha))
        hl_t = QColor(180, 230, 255, 0)
        hl_grad.setColorAt(0.0, hl_t)
        hl_grad.setColorAt(0.3, hl_c)
        hl_grad.setColorAt(0.7, hl_c)
        hl_grad.setColorAt(1.0, hl_t)
        p.setBrush(QBrush(hl_grad))
        p.drawRoundedRect(highlight_rect, 1, 1)

        # ── Corner brackets with cyber crosshairs ──
        bracket_alpha = int(180 * self._bracket * sweep_alpha)
        pen = QPen(QColor(CYAN.red(), CYAN.green(), CYAN.blue(), bracket_alpha), LINE_WIDTH)
        p.setPen(pen)
        arm = 22 * self._bracket
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
            # Cyber crosshair dot at corner intersection
            if self._bracket > 0.5:
                cross_a = int(120 * (self._bracket - 0.5) * 2 * sweep_alpha)
                cross_c = QColor(CYAN_GLOW.red(), CYAN_GLOW.green(), CYAN_GLOW.blue(), cross_a)
                p.setPen(QPen(cross_c, 1.0))
                cross_size = 3.0
                p.drawLine(int(x - cross_size), int(y), int(x + cross_size), int(y))
                p.drawLine(int(x), int(y - cross_size), int(x), int(y + cross_size))
                p.setPen(pen)  # restore

        # ── Scanline fill sweeping top->bottom on materialize ──
        if 0.0 < self._scan < 1.0:
            scan_y = rect.top() + rect.height() * self._scan
            grad = QLinearGradient(0, scan_y - 16, 0, scan_y + 16)
            c = QColor(CYAN)
            c.setAlpha(0)
            grad.setColorAt(0.0, c)
            c2 = QColor(CYAN_GLOW)
            c2.setAlpha(70)
            grad.setColorAt(0.45, c2)
            c3 = QColor(255, 255, 255, 30)
            grad.setColorAt(0.5, c3)
            c4 = QColor(CYAN)
            c4.setAlpha(50)
            grad.setColorAt(0.55, c4)
            grad.setColorAt(1.0, c)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(grad))
            p.drawRect(QRectF(rect.left(), scan_y - 16, rect.width(), 32))

        # ── Ambient neon outer border ──
        outline_alpha = int(45 * sweep_alpha + 15 * self._bracket)
        outline = QPen(QColor(CYAN.red(), CYAN.green(), CYAN.blue(), outline_alpha), LINE_WIDTH)
        p.setPen(outline)
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect, PANEL_RADIUS, PANEL_RADIUS)

        # ── Meta tag (top-right) ──
        if self._meta_tag and self._bracket > 0.8:
            tag_alpha = int(100 * (self._bracket - 0.8) * 5 * sweep_alpha)
            p.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM - 1))
            p.setPen(QColor(TEXT_DIM.red(), TEXT_DIM.green(), TEXT_DIM.blue(), tag_alpha))
            tag_rect = QRectF(rect.right() - 180, rect.top() + 6, 170, 14)
            p.drawText(tag_rect, Qt.AlignRight | Qt.AlignVCenter, self._meta_tag)

        p.end()
