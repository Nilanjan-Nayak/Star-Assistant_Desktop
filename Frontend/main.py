"""
STAR ASSISTANT — Frontend  (v2.0 — Premium HUD)

A PySide6 desktop-overlay implementing the HUD visual language from the
architecture bible: navy glass, cyan life, gold confirmation, corner brackets,
scanline materialize, and a central multi-ring reactor.

Enhancements over v1.0:
  - Cached hex grid background (pre-rendered QPainterPath, 60 FPS)
  - Draggable ORB mode (drag anywhere on desktop)
  - RAIL mode (400px docked sidebar on right edge)
  - Header bar with live clock, status badge, minimize/close controls
  - Smooth window geometry transitions (no setFixedSize lock during anim)
  - Meta-tagged glass panels

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
from pathlib import Path

_frontend_dir = Path(__file__).resolve().parent
_backend_root = _frontend_dir.parent
if str(_frontend_dir) not in sys.path:
    sys.path.insert(0, str(_frontend_dir))
if str(_backend_root) not in sys.path:
    sys.path.insert(0, str(_backend_root))

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, QRect, QPoint
from PySide6.QtGui import (
    QPainter, QColor, QFont, QGuiApplication, QPainterPath, QPen, QBrush, QRadialGradient
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGridLayout,
    QPushButton, QFrame, QLineEdit,
)

from tokens import (
    NAVY, CYAN, CYAN_GLOW, GOLD, OK, AMBER, ALERT, MOTION, MODE_ORB, MODE_CENTER, MODE_RAIL,
    SPACE_ORB_MAX, SPACE_RAIL,
    FONT_HUD_FAMILY, FONT_HUD_SIZE_LG, FONT_HUD_SIZE, FONT_HUD_SIZE_SM,
    FONT_MONO_FAMILY, DEEP_NAVY, TEXT_DIM, TEXT_PRIMARY,
)
from reactor import Reactor, STATE_IDLE, STATE_THINK, STATE_SPEAK, STATE_ACT
from panels import GlassPanel
from content import ActivityLog, MissionCard
from Backend.bridge import AssistantBridge

ORB_SIZE = SPACE_ORB_MAX + 48     # window padding around reactor for unclipped glow & shockwaves
CENTER_SIZE = (980, 640)
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
    QPainterPath so each frame is a single drawPath() call."""

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
        size = 32
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
            alpha = 0.04 + 0.05 * (0.5 + 0.5 * math.sin(self._t * 2 * math.pi))

            if is_orb:
                cx, cy = w / 2.0, h / 2.0
                r_max = min(w, h) / 2.0
                mask_grad = QRadialGradient(cx, cy, r_max)
                c_center = QColor(CYAN)
                c_center.setAlphaF(alpha)
                c_mid = QColor(CYAN)
                c_mid.setAlphaF(alpha * 0.35)
                c_zero = QColor(CYAN)
                c_zero.setAlphaF(0.0)
                mask_grad.setColorAt(0.0, c_center)
                mask_grad.setColorAt(0.65, c_mid)
                mask_grad.setColorAt(0.92, c_zero)
                p.setPen(QPen(QBrush(mask_grad), 0.7))
            else:
                pen_color = QColor(CYAN)
                pen_color.setAlphaF(alpha)
                p.setPen(QPen(pen_color, 0.6))

            p.drawPath(self._cached_path)
        finally:
            if p.isActive():
                p.end()


# ═══════════════════════════════════════════════════════════════════════
# Cyber control button (minimize / close / mode toggle)
# ═══════════════════════════════════════════════════════════════════════

class CyberButton(QPushButton):
    """Tiny HUD-styled control button with hover glow."""

    def __init__(self, text: str, color: QColor = CYAN, parent=None):
        super().__init__(text, parent)
        self._color = color
        self.setFixedSize(28, 22)
        self.setCursor(Qt.PointingHandCursor)
        self.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM, QFont.Bold))
        self._set_style(False)

    def _set_style(self, hovered: bool):
        c = self._color
        bg_alpha = 40 if hovered else 12
        border_alpha = 120 if hovered else 50
        text_alpha = 255 if hovered else 180
        self.setStyleSheet(
            f"QPushButton {{ "
            f"color: rgba({c.red()},{c.green()},{c.blue()},{text_alpha}); "
            f"background: rgba({c.red()},{c.green()},{c.blue()},{bg_alpha}); "
            f"border: 1px solid rgba({c.red()},{c.green()},{c.blue()},{border_alpha}); "
            f"border-radius: 4px; padding: 0px; }}"
        )

    def enterEvent(self, event):
        self._set_style(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_style(False)
        super().leaveEvent(event)


# ═══════════════════════════════════════════════════════════════════════
# Header bar (title + clock + status + controls)
# ═══════════════════════════════════════════════════════════════════════

class HeaderBar(QWidget):
    """Top bar: logo / title, live clock, online status, and control buttons."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        self.setFixedHeight(36)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(12)

        # Title
        title = QLabel("STAR  HUD  v2.4")
        title.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE_LG, QFont.DemiBold))
        title.setStyleSheet(f"color: {GOLD.name()}; letter-spacing: 4px; background: transparent;")
        layout.addWidget(title)

        layout.addStretch(1)

        # Live clock
        self._clock = QLabel("")
        self._clock.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM))
        self._clock.setStyleSheet(f"color: rgba(180,210,230,160); background: transparent;")
        layout.addWidget(self._clock)

        # Status badge
        status_badge = QLabel("  ● ONLINE  ")
        status_badge.setFont(QFont(FONT_MONO_FAMILY, FONT_HUD_SIZE_SM - 1, QFont.DemiBold))
        status_badge.setStyleSheet(
            f"color: {GOLD.name()}; "
            f"background: rgba(201,162,39,18); "
            f"border: 1px solid rgba(201,162,39,70); "
            f"border-radius: 4px;"
        )
        layout.addWidget(status_badge)

        # Separator
        sep = QLabel("│")
        sep.setStyleSheet("color: rgba(0,212,255,40); background: transparent;")
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

        self.reactor = Reactor(diameter=SPACE_ORB_MAX, parent=self)
        self.reactor.clicked.connect(self._on_reactor_clicked)

        # Header bar (shown in CENTER / RAIL mode)
        self.header = HeaderBar(self)
        self.header.btn_min.clicked.connect(self._collapse_to_orb)
        self.header.btn_close.clicked.connect(QApplication.quit)
        self.header.btn_rail.clicked.connect(self._toggle_rail)
        self.header.hide()

        # Glass panels
        self.activity_log = ActivityLog(DEMO_LOG_LINES)
        self.log_panel = GlassPanel(self)
        self.log_panel.set_meta_tag("SYS.TELEMETRY // 01")
        self.log_panel.set_content(self.activity_log)
        self.log_panel.hide()

        self.mission_panel = GlassPanel(self)
        self.mission_panel.set_meta_tag("MISSION.RAIL // 02")
        self._build_mission_grid()
        self.mission_panel.hide()

        # HUD Command Input Bar
        self.cmd_input = QLineEdit(self)
        self.cmd_input.setPlaceholderText("💬 Ask Star or type command (e.g. 'volume 20% barao', 'kemon acho')...")
        self.cmd_input.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE))
        self.cmd_input.setStyleSheet(
            f"QLineEdit {{ "
            f"color: {TEXT_PRIMARY.name()}; "
            f"background: rgba(6, 16, 36, 0.90); "
            f"border: 1px solid rgba(0, 212, 255, 0.45); "
            f"border-radius: 6px; "
            f"padding: 6px 14px; "
            f"}} "
            f"QLineEdit:focus {{ "
            f"border: 1px solid rgba(0, 229, 255, 0.95); "
            f"background: rgba(8, 26, 56, 0.98); "
            f"}}"
        )
        self.cmd_input.returnPressed.connect(self._on_cmd_submitted)
        self.cmd_input.hide()

        # Connect Assistant Brain Bridge (replaces dummy demo timer)
        self.bridge = AssistantBridge(self)
        self.bridge.state_changed.connect(self.reactor.set_state)
        self.bridge.log_emitted.connect(self._on_bridge_log)

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
        grid.setSpacing(8)

        title = QLabel("◆  MISSION RAIL")
        title.setFont(QFont(FONT_HUD_FAMILY, FONT_HUD_SIZE_LG, QFont.DemiBold))
        title.setStyleSheet(f"color: {GOLD.name()}; letter-spacing: 2px; background: transparent;")
        grid.addWidget(title)

        # Gradient rule
        rule = QFrame()
        rule.setFixedHeight(1)
        rule.setStyleSheet(
            f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 transparent, stop:0.15 rgba(0,212,255,80), "
            f"stop:0.85 rgba(0,212,255,80), stop:1 transparent);"
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
        self.header.hide()
        self.log_panel.hide()
        self.mission_panel.hide()
        if hasattr(self, "cmd_input"):
            self.cmd_input.hide()

    def _place_on_screen(self):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        if self.mode == MODE_ORB:
            self.move(screen.right() - ORB_SIZE - 24, screen.bottom() - ORB_SIZE - 24)
        elif self.mode == MODE_CENTER:
            w, h = CENTER_SIZE
            self.move(screen.center().x() - w // 2, screen.center().y() - h // 2)
        elif self.mode == MODE_RAIL:
            rail_h = screen.height()
            self.move(screen.right() - SPACE_RAIL, screen.top())

    def _layout_center(self):
        w, h = CENTER_SIZE
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.resize(w, h)
        self.hex_bg.setGeometry(0, 0, w, h)

        self.reactor.setFixedSize(76, 76)
        self.reactor.move(24, 46)

        self.header.setGeometry(0, 6, w, 36)
        self.header.show()

        self.mission_panel.setGeometry(24, 128, w - 48, 215)
        self.log_panel.setGeometry(24, 355, w - 48, h - 355 - 68)

        if hasattr(self, "cmd_input"):
            self.cmd_input.setGeometry(24, h - 56, w - 48, 38)
            self.cmd_input.show()

    def _layout_rail(self):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        w = SPACE_RAIL
        h = screen.height()
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.resize(w, h)
        self.hex_bg.setGeometry(0, 0, w, h)

        self.reactor.setFixedSize(60, 60)
        self.reactor.move(20, 44)

        self.header.setGeometry(0, 6, w, 36)
        self.header.show()

        self.mission_panel.setGeometry(16, 112, w - 32, 280)
        self.log_panel.setGeometry(16, 404, w - 32, h - 404 - 68)

        if hasattr(self, "cmd_input"):
            self.cmd_input.setGeometry(16, h - 56, w - 32, 38)
            self.cmd_input.show()

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
        QTimer.singleShot(100, self.mission_panel.materialize)
        QTimer.singleShot(200, self.log_panel.materialize)
        QTimer.singleShot(350, self.cmd_input.setFocus)

    def _collapse_to_orb(self):
        self.reactor.set_state(STATE_IDLE)
        if hasattr(self, "cmd_input"):
            self.cmd_input.hide()

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
        self.header.hide()

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

            QTimer.singleShot(200, self.mission_panel.materialize)
            QTimer.singleShot(300, self.log_panel.materialize)

        QTimer.singleShot(MOTION["panel.out"] + 50, do_rail)

    def _switch_to_center_from_rail(self):
        self.mode = MODE_CENTER
        start_geo = self.geometry()

        self.mission_panel.dismiss()
        self.log_panel.dismiss()

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

            QTimer.singleShot(200, self.mission_panel.materialize)
            QTimer.singleShot(300, self.log_panel.materialize)

        QTimer.singleShot(MOTION["panel.out"] + 50, do_center)

    # -- Brain & Command handling ----------------------------------------------
    def _on_cmd_submitted(self):
        text = self.cmd_input.text().strip()
        if text:
            self.cmd_input.clear()
            self.bridge.send_query(text)

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
