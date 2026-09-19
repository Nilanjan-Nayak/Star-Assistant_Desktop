"""
The Reactor — Star's Mascot & Central Power Core. (v4.0 — JARVIS Cinematic Arc Reactor)

A high-tech, multi-layered, ultra-smooth holographic fusion reactor:
- 10/12 electromagnetic power inductor coils arranged radially with sequential energy propagation
- Dual counter-rotating quantum gyroscopic rings with micro-dashed tracks and orbital nodes
- Segmented outer stator arcs with energy heads and tapering glow trails
- 360° precision calibration reticle with cardinal chevrons and corner brackets
- 30 floating quantum plasma motes with firefly fade-trails (was 20)
- Multi-pass volumetric bloom singularity with an anamorphic starburst cross-glint
- Interactive shockwave ripple feedback on hover and click
- Bezier-interpolated organic breathing curve (replaces dual-sine)
- Ambient halo ring visible in CENTER/RAIL mode
- Core shimmer: rapid micro-flicker over the plasma seed
- 60 FPS delta-time physics with smooth velocity lerping and harmonic breathing

States:
  idle     hypnotic breathing, slow multi-ring counter-rotation, drifting motes, star glint
  wake     shockwave ripple expansion, vibrant cyan core bloom
  hear     outer bloom brightens, gyro rings align, core focuses
  think    twin counter-rotating high-energy plasma comets with particle trails
  act      energized golden power coils, radiant gold shockwave, neon checkmark
  speak    fluid harmonic sine-spline audio visualizer ribbon inside the core
  stop     collapsing crimson field lines
"""

import math
import random
import time
from typing import List, Dict, Any

from PySide6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QEasingCurve, Property, QRectF, Signal, QPointF
)
from PySide6.QtGui import (
    QPainter, QPen, QBrush, QRadialGradient, QColor, QLinearGradient, QPainterPath
)
from PySide6.QtWidgets import QWidget

from tokens import (
    NAVY, CYAN, CYAN_GLOW, CYAN_DIM, CYAN_CORE, CYAN_HOT, COIL_BASE, COIL_ACTIVE, COIL_GOLD,
    GOLD, GOLD_GLOW, ALERT, OK, MOTION, DEEP_NAVY, CORE_BRIGHT, RING_OUTER, RING_MID,
    ARC_GLOW,
)

STATE_IDLE = "idle"
STATE_WAKE = "wake"
STATE_HEAR = "hear"
STATE_THINK = "think"
STATE_ACT = "act"
STATE_SPEAK = "speak"
STATE_STOP = "stop"

_WAVE_BARS = 28
_NUM_COILS = 10
_NUM_MOTES = 30           # v4.0: 20→30 motes for denser JARVIS energy field


class Reactor(QWidget):
    """Cinematic Arc Reactor widget. Adapts smoothly to any diameter."""

    clicked = Signal()

    def __init__(self, diameter: int = 120, parent=None):
        super().__init__(parent)
        self._diameter = diameter
        self.setFixedSize(diameter, diameter)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

        self._state = STATE_IDLE
        self._breath = 0.0          # 0..1, drives breathing
        self._breath_phase = random.uniform(0, math.pi * 2)
        self._bloom = 0.0           # hear.bloom brightness 0..1
        self._wake_bump = 0.0       # wake.tick extra radius
        self._collapse = 0.0        # stop.collapse 0..1
        self._hover = 0.0           # 0..1 hover intensity
        self._speed_mult = 1.0      # rotational speed multiplier
        self._act_pulse = 0.0       # 0..1 gold act pulse
        self._shimmer_phase = 0.0   # v4.0: core micro-shimmer

        # Continuous rotations (degrees)
        self._spin_outer = 0.0      # slow clockwise stator
        self._spin_mid = 0.0        # counter-clockwise gyro
        self._spin_iris = 0.0       # inner iris
        self._spin_glint = 0.0      # star glint flare rotation

        # Think state comets
        self._orbit_angle1 = 0.0
        self._orbit_angle2 = 0.0

        # Speak waveform
        self._wave = [0.0] * _WAVE_BARS
        self._wave_target = [0.0] * _WAVE_BARS

        # Interactive shockwave ripples: each is [current_r_norm, max_r_norm, alpha, speed, QColor]
        self._ripples: List[List[Any]] = []

        # v4.0: Floating quantum plasma motes with firefly trails
        self._motes: List[Dict[str, float]] = []
        for _ in range(_NUM_MOTES):
            self._motes.append({
                "angle": random.uniform(0, 360),
                "r_ratio": random.uniform(0.18, 0.62),   # v4.1: tighter — stay inside reactor body
                "speed": random.uniform(18.0, 55.0) * random.choice([-1, 1]),
                "size": random.uniform(1.0, 2.2),
                "alpha": random.uniform(0.30, 0.85),
                "radial_drift": random.uniform(0.03, 0.08),  # v4.1: less drift outside
                "drift_phase": random.uniform(0, math.pi * 2),
                # Firefly trail: store previous 2 positions as (px, py) pairs
                "trail": [],
            })

        # Delta-time tracking
        self._last_time = time.perf_counter()

        # 60 FPS unified render timer
        self._render_timer = QTimer(self)
        self._render_timer.setInterval(16)
        self._render_timer.timeout.connect(self._render_tick)
        self._render_timer.start()

    # -- Qt Properties -----------------------------------------------------
    def get_bloom(self):
        return self._bloom

    def set_bloom(self, v):
        self._bloom = v
        self.update()

    bloom = Property(float, get_bloom, set_bloom)

    def get_wake_bump(self):
        return self._wake_bump

    def set_wake_bump(self, v):
        self._wake_bump = v
        self.update()

    wake_bump = Property(float, get_wake_bump, set_wake_bump)

    def get_collapse(self):
        return self._collapse

    def set_collapse(self, v):
        self._collapse = v
        self.update()

    collapse = Property(float, get_collapse, set_collapse)

    def get_hover(self):
        return self._hover

    def set_hover(self, v):
        self._hover = v
        self.update()

    hover = Property(float, get_hover, set_hover)

    def get_act_pulse(self):
        return self._act_pulse

    def set_act_pulse(self, v):
        self._act_pulse = v
        self.update()

    act_pulse = Property(float, get_act_pulse, set_act_pulse)

    # -- Shockwave Ripple Spawner -----------------------------------------
    def spawn_ripple(self, color: QColor = None, speed: float = 1.6):
        """Spawn an expanding holographic shockwave ring."""
        c = color if color is not None else (ALERT if self._state == STATE_STOP else CYAN_CORE)
        self._ripples.append([0.15, 1.25, 1.0, speed, c])

    # -- v4.0: Bezier-interpolated organic breathing curve ----------------
    @staticmethod
    def _bezier_breath(t: float) -> float:
        """Attempt a smooth organic breathing curve using cubic bezier-like
        interpolation rather than sharp sine waves.  Produces a softer
        inhale/exhale rhythm similar to real respiration."""
        # Normalize t to [0, 1] within a breath cycle
        cycle = t % 1.0
        # Inhale: fast ramp up (0..0.4)
        if cycle < 0.4:
            p = cycle / 0.4
            # Cubic ease-out for gentle arrival at peak
            val = 1.0 - (1.0 - p) ** 3
        # Hold at peak briefly (0.4..0.5)
        elif cycle < 0.5:
            val = 1.0
        # Exhale: slow relaxed falloff (0.5..0.95)
        elif cycle < 0.95:
            p = (cycle - 0.5) / 0.45
            # Cubic ease-in for gradual release
            val = 1.0 - p ** 2.5
        # Rest at bottom (0.95..1.0)
        else:
            val = 0.0
        return 0.35 + 0.65 * val  # remap to 0.35..1.0 range

    # -- Unified Physics & Render Tick ------------------------------------
    def _render_tick(self):
        now = time.perf_counter()
        dt = now - self._last_time
        self._last_time = now
        # Cap dt to avoid huge jumps if window was minimized
        dt = min(0.05, dt)

        # Smooth hover speed boost lerp (1.0 -> 2.2x on hover)
        target_speed = 1.0 + self._hover * 1.2 + (0.8 if self._state == STATE_THINK else 0.0)
        self._speed_mult += (target_speed - self._speed_mult) * min(1.0, dt * 6.0)

        # v4.0: Bezier-interpolated organic breathing (replaces dual-sine)
        breath_speed = 1.0 / (MOTION["hex.breathe"] / 1000.0)
        if self._state in (STATE_WAKE, STATE_HEAR):
            breath_speed *= 2.0
        self._breath_phase = (self._breath_phase + dt * breath_speed) % 1.0
        self._breath = self._bezier_breath(self._breath_phase)

        # v4.0: Core shimmer — rapid micro-flicker
        self._shimmer_phase = (self._shimmer_phase + dt * 25.0) % (math.pi * 2)

        # Dynamic continuous rotations
        self._spin_outer = (self._spin_outer + dt * 14.0 * self._speed_mult) % 360.0
        self._spin_mid = (self._spin_mid - dt * 22.0 * self._speed_mult) % 360.0
        self._spin_iris = (self._spin_iris + dt * 18.0 * self._speed_mult) % 360.0
        self._spin_glint = (self._spin_glint + dt * 9.0) % 360.0

        # Quantum plasma motes physics + firefly trail capture
        cx_norm = 0.5
        cy_norm = 0.5
        for m in self._motes:
            m["angle"] = (m["angle"] + dt * m["speed"] * self._speed_mult) % 360.0
            m["drift_phase"] = (m["drift_phase"] + dt * 1.5) % (math.pi * 2)

            # Record trail positions (store as angle+r_ratio snapshots)
            drift = math.sin(m["drift_phase"]) * m["radial_drift"]
            trail_entry = (m["angle"], m["r_ratio"] + drift)
            trail = m["trail"]
            trail.append(trail_entry)
            if len(trail) > 3:
                trail.pop(0)

        # Update shockwave ripples
        alive_ripples = []
        for r in self._ripples:
            r[0] += dt * r[3]
            r[2] = max(0.0, 1.0 - (r[0] / r[1]))
            if r[0] < r[1] and r[2] > 0.02:
                alive_ripples.append(r)
        self._ripples = alive_ripples

        # Think comets orbit
        if self._state == STATE_THINK:
            revs_per_sec = 1000.0 / MOTION["think.orbit"]
            step = 360.0 * revs_per_sec * dt
            self._orbit_angle1 = (self._orbit_angle1 + step) % 360.0
            self._orbit_angle2 = (self._orbit_angle2 - step * 0.75) % 360.0

        # Speak fluid waveform
        if self._state == STATE_SPEAK:
            if random.random() < 0.3:
                self._wave_target = [random.uniform(0.15, 1.0) for _ in range(_WAVE_BARS)]
            lerp_speed = min(1.0, dt * 14.0)
            self._wave = [
                cur + (tgt - cur) * lerp_speed
                for cur, tgt in zip(self._wave, self._wave_target)
            ]

        self.update()

    # -- Public State Transitions ----------------------------------------
    def set_state(self, state: str):
        self._state = state

        if state == STATE_WAKE:
            self.spawn_ripple(CYAN_HOT, speed=2.2)
            anim = QPropertyAnimation(self, b"wake_bump")
            anim.setDuration(MOTION["wake.tick"])
            anim.setStartValue(0.0)
            anim.setKeyValueAt(0.4, 7.0)
            anim.setEndValue(0.0)
            anim.setEasingCurve(QEasingCurve.OutQuad)
            anim.start()
            self._anim_ref = anim

        elif state == STATE_HEAR:
            anim = QPropertyAnimation(self, b"bloom")
            anim.setDuration(MOTION["hear.bloom"])
            anim.setStartValue(self._bloom)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.start()
            self._anim_ref = anim

        elif state == STATE_ACT:
            self.spawn_ripple(GOLD_GLOW, speed=2.5)
            anim = QPropertyAnimation(self, b"act_pulse")
            anim.setDuration(MOTION["act.gold"])
            anim.setStartValue(0.0)
            anim.setKeyValueAt(0.4, 1.0)
            anim.setEndValue(0.5)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.start()
            self._anim_ref = anim

        elif state == STATE_SPEAK:
            self._wave = [0.0] * _WAVE_BARS
            self._wave_target = [random.uniform(0.15, 1.0) for _ in range(_WAVE_BARS)]

        elif state == STATE_STOP:
            self.spawn_ripple(ALERT, speed=1.8)
            anim = QPropertyAnimation(self, b"collapse")
            anim.setDuration(MOTION["stop.collapse"])
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.InCubic)
            anim.start()
            self._anim_ref = anim

        elif state == STATE_IDLE:
            anim_b = QPropertyAnimation(self, b"bloom")
            anim_b.setDuration(300)
            anim_b.setEndValue(0.0)
            anim_b.start()
            self._bloom_anim = anim_b
            self._wake_bump = 0.0
            self._collapse = 0.0
            self._act_pulse = 0.0
            self._wave = [0.0] * _WAVE_BARS

    # -- Painting ---------------------------------------------------------
    def paintEvent(self, event):
        p = QPainter(self)
        if not p.isActive():
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        # Compute base radius with breathing room for glows & reticle
        base_r = min(w, h) / 2.0 - 6.0

        collapse_scale = 1.0 - 0.45 * self._collapse
        r = (base_r + self._wake_bump) * collapse_scale
        if r <= 2.0:
            p.end()
            return

        is_stop = (self._state == STATE_STOP)
        is_act = (self._state == STATE_ACT)
        theme_color = ALERT if is_stop else (GOLD if is_act else CYAN)
        theme_glow = ALERT if is_stop else (GOLD_GLOW if is_act else CYAN_GLOW)

        hover_boost = self._hover * 0.35
        energy = self._breath * 0.5 + 0.5 + hover_boost + self._bloom * 0.4

        # ── 0. v4.0: Ambient Halo Ring (visible in CENTER/RAIL mode) ──
        # A faint secondary glow ring outside the main reactor body,
        # like JARVIS's ambient arc reactor halo.
        parent_mode = getattr(self.parent(), "mode", None) if self.parent() else None
        if parent_mode and parent_mode != "ORB":
            halo_r = r * 1.35
            halo_grad = QRadialGradient(cx, cy, halo_r)
            halo_inner = QColor(ARC_GLOW)
            halo_inner.setAlpha(int(30 + 25 * energy))
            halo_mid = QColor(CYAN_DIM)
            halo_mid.setAlpha(int(15 + 12 * energy))
            halo_outer = QColor(0, 0, 0, 0)
            halo_grad.setColorAt(0.65, halo_inner)
            halo_grad.setColorAt(0.82, halo_mid)
            halo_grad.setColorAt(1.0, halo_outer)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(halo_grad))
            p.drawEllipse(QRectF(cx - halo_r, cy - halo_r, halo_r * 2.0, halo_r * 2.0))

            # Thin halo ring line
            halo_ring_c = QColor(CYAN)
            halo_ring_c.setAlpha(int(25 + 20 * energy))
            p.setPen(QPen(halo_ring_c, 0.8))
            p.setBrush(Qt.NoBrush)
            ring_r = r * 1.18
            p.drawEllipse(QRectF(cx - ring_r, cy - ring_r, ring_r * 2.0, ring_r * 2.0))

        # ── 1. Deep Atmospheric Space Glow (Volumetric Halo) ──
        # v4.1: Tighter glow in ORB mode for a clean floating look
        is_orb_mode = getattr(self.parent(), "mode", None) == "ORB" if self.parent() else False
        glow_r = r * (1.10 if is_orb_mode else 1.45)
        ambient_g = QRadialGradient(cx, cy, glow_r)
        c_glow = QColor(theme_glow)
        glow_base = (20 + 35 * energy) if is_orb_mode else (35 + 55 * energy)
        glow_alpha = int(max(10, min(180, glow_base)))
        c_glow.setAlpha(glow_alpha)
        ambient_g.setColorAt(0.0, c_glow)
        c_mid = QColor(0, 160, 240 if not is_stop else 40)
        c_mid.setAlpha(int(glow_alpha * 0.35))
        ambient_g.setColorAt(0.45, c_mid)
        c_outer = QColor(theme_color)
        c_outer.setAlpha(0)
        ambient_g.setColorAt(1.0, c_outer)

        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(ambient_g))
        p.drawEllipse(QRectF(cx - glow_r, cy - glow_r, glow_r * 2.0, glow_r * 2.0))

        # ── 2. Expanding Shockwave Ripples ──
        for ripple in self._ripples:
            rip_r = r * ripple[0]
            rip_alpha = int(220 * ripple[2])
            if rip_alpha > 0 and rip_r > 1.0:
                rip_pen = QPen(ripple[4], max(1.0, 3.2 * ripple[2]))
                rip_c = QColor(ripple[4])
                rip_c.setAlpha(rip_alpha)
                rip_pen.setColor(rip_c)
                p.setPen(rip_pen)
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QRectF(cx - rip_r, cy - rip_r, rip_r * 2.0, rip_r * 2.0))

        # ── 3. Cyber Reticle & Corner Brackets ──
        reticle_r = r * 1.04       # v4.1: tighter reticle
        bracket_len = reticle_r * 0.16
        p.save()
        p.translate(cx, cy)
        brk_c = QColor(CYAN)
        brk_c.setAlpha(int(30 + 28 * energy))  # v4.1: subtler brackets
        p.setPen(QPen(brk_c, 1.0))
        # 4 Corner brackets at 45°, 135°, 225°, 315°
        for corner_angle in [45, 135, 225, 315]:
            p.save()
            p.rotate(corner_angle)
            bx, by = reticle_r * 0.72, reticle_r * 0.72
            p.drawLine(QPointF(bx - bracket_len, by), QPointF(bx, by))
            p.drawLine(QPointF(bx, by), QPointF(bx, by - bracket_len))
            p.restore()
        p.restore()

        # ── 4. Outer 360° Precision Calibration Dial (Rotating Stator) ──
        p.save()
        p.translate(cx, cy)
        p.rotate(self._spin_outer)
        dial_r = r * 0.98
        num_ticks = 48
        tick_pen_minor = QPen(QColor(CYAN_GLOW), 1.0)
        tick_pen_major = QPen(QColor(theme_glow), 2.0)
        for i in range(num_ticks):
            is_cardinal = (i % (num_ticks // 4) == 0)
            is_sub = (i % 3 == 0)
            ang = math.radians(i * (360.0 / num_ticks))

            if is_cardinal:
                t_in = dial_r * 0.86
                t_out = dial_r
                alpha = int(140 + 90 * energy)
                tc = QColor(theme_glow)
                tc.setAlpha(min(255, alpha))
                tick_pen_major.setColor(tc)
                p.setPen(tick_pen_major)
            elif is_sub:
                t_in = dial_r * 0.90
                t_out = dial_r
                alpha = int(70 + 55 * energy)
                tc = QColor(CYAN)
                tc.setAlpha(min(255, alpha))
                tick_pen_minor.setColor(tc)
                p.setPen(tick_pen_minor)
            else:
                t_in = dial_r * 0.94
                t_out = dial_r
                alpha = int(35 + 30 * energy)
                tc = QColor(CYAN_DIM)
                tc.setAlpha(min(255, alpha))
                tick_pen_minor.setColor(tc)
                p.setPen(tick_pen_minor)

            cos_a = math.cos(ang)
            sin_a = math.sin(ang)
            p.drawLine(QPointF(t_in * cos_a, t_in * sin_a), QPointF(t_out * cos_a, t_out * sin_a))

        # Thin outer gauge boundary ring
        boundary_c = QColor(theme_color)
        boundary_c.setAlpha(int(60 + 50 * energy))
        p.setPen(QPen(boundary_c, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(-dial_r, -dial_r, dial_r * 2.0, dial_r * 2.0))
        p.restore()

        # ── 5. Segmented Kinetic Stator Arcs ──
        p.save()
        p.translate(cx, cy)
        p.rotate(self._spin_outer * 1.5)
        stator_r = r * 0.88
        stator_pen = QPen(QColor(theme_glow), 2.2, Qt.SolidLine, Qt.RoundCap)
        for arc_i in range(3):
            start_deg = arc_i * 120 + 15
            span_deg = 70
            arc_alpha = int(120 + 80 * energy)
            ac = QColor(theme_glow)
            ac.setAlpha(min(255, arc_alpha))
            stator_pen.setColor(ac)
            p.setPen(stator_pen)
            p.setBrush(Qt.NoBrush)
            p.drawArc(QRectF(-stator_r, -stator_r, stator_r * 2.0, stator_r * 2.0),
                      int(start_deg * 16), int(span_deg * 16))

            # Glowing head bead on each arc
            head_ang = math.radians(start_deg + span_deg)
            hx = stator_r * math.cos(head_ang)
            hy = -stator_r * math.sin(head_ang)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(255, 255, 255, min(255, arc_alpha + 50))))
            p.drawEllipse(QPointF(hx, hy), 2.2, 2.2)
        p.restore()

        # ── 6. Electromagnetic Arc Reactor Inductor Coils Array ──
        # The iconic signature feature of the Arc Reactor: 10 symmetrical coils!
        p.save()
        p.translate(cx, cy)
        coil_mid_r = r * 0.74
        coil_w = (coil_mid_r * 2.0 * math.pi / _NUM_COILS) * 0.58
        coil_h = r * 0.12

        for ci in range(_NUM_COILS):
            coil_deg = ci * (360.0 / _NUM_COILS)
            p.save()
            p.rotate(coil_deg)

            # Sequential energy wave traveling through the coils
            wave_offset = (self._spin_outer * 2.5 + ci * (360.0 / _NUM_COILS)) % 360.0
            coil_glow = 0.5 + 0.5 * math.sin(math.radians(wave_offset))
            if is_act:
                coil_c = QColor(COIL_GOLD)
            elif is_stop:
                coil_c = QColor(ALERT)
            else:
                coil_c = QColor(COIL_ACTIVE)

            coil_rect = QRectF(-coil_w / 2.0, -coil_mid_r - coil_h / 2.0, coil_w, coil_h)

            # Coil housing backdrop
            p.setPen(QPen(QColor(0, 200, 255, 70), 1.0))
            p.setBrush(QBrush(QColor(6, 20, 45, 220)))
            p.drawRoundedRect(coil_rect, 2.0, 2.0)

            # Coil illuminated core wire
            core_alpha = int(70 + 130 * coil_glow * energy)
            coil_c.setAlpha(min(255, core_alpha))
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(coil_c))
            inner_rect = coil_rect.adjusted(1.5, 1.5, -1.5, -1.5)
            p.drawRoundedRect(inner_rect, 1.5, 1.5)

            # Central intense filament line
            fil_c = QColor(255, 255, 255, min(255, int(core_alpha * 0.85)))
            p.setPen(QPen(fil_c, 1.0))
            p.drawLine(QPointF(inner_rect.left() + 2, inner_rect.center().y()),
                       QPointF(inner_rect.right() - 2, inner_rect.center().y()))

            p.restore()
        p.restore()

        # ── 7. Mid Counter-Rotating Gyroscopic Ring (Dashed Cyber Orbit) ──
        p.save()
        p.translate(cx, cy)
        p.rotate(self._spin_mid)
        gyro_r = r * 0.60
        gyro_pen = QPen(QColor(RING_MID), 1.4)
        gyro_pen.setColor(QColor(0, 220, 255, int(90 + 60 * energy)))
        gyro_pen.setDashPattern([3, 7])
        p.setPen(gyro_pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(-gyro_r, -gyro_r, gyro_r * 2.0, gyro_r * 2.0))

        # 3 Orbiting energy nodes on the gyro track
        for ni in range(3):
            nang = math.radians(ni * 120)
            nx = gyro_r * math.cos(nang)
            ny = gyro_r * math.sin(nang)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(QColor(CORE_BRIGHT)))
            p.drawEllipse(QPointF(nx, ny), 2.2, 2.2)
            # Halo around node
            halo_c = QColor(CYAN_GLOW)
            halo_c.setAlpha(int(70 + 80 * energy))
            p.setBrush(QBrush(halo_c))
            p.drawEllipse(QPointF(nx, ny), 4.5, 4.5)
        p.restore()

        # ── 8. Inner Iris Stator (Notched Mechanical Teeth) ──
        p.save()
        p.translate(cx, cy)
        p.rotate(self._spin_iris)
        iris_r = r * 0.46
        iris_c = QColor(CYAN)
        iris_c.setAlpha(int(70 + 50 * energy))
        p.setPen(QPen(iris_c, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(-iris_r, -iris_r, iris_r * 2.0, iris_r * 2.0))

        # Inner aperture teeth
        for ti in range(16):
            tang = math.radians(ti * (360.0 / 16))
            tx1 = iris_r * 0.90 * math.cos(tang)
            ty1 = iris_r * 0.90 * math.sin(tang)
            tx2 = iris_r * math.cos(tang)
            ty2 = iris_r * math.sin(tang)
            p.drawLine(QPointF(tx1, ty1), QPointF(tx2, ty2))
        p.restore()

        # ── 9. Floating Quantum Plasma Motes with Firefly Trails ──
        for m in self._motes:
            # Subtle radial breathing drift
            drift = math.sin(m["drift_phase"]) * m["radial_drift"]
            mote_dist = r * (m["r_ratio"] + drift)
            mote_rad = math.radians(m["angle"])
            mx = cx + mote_dist * math.cos(mote_rad)
            my = cy + mote_dist * math.sin(mote_rad)

            m_alpha = int(m["alpha"] * (120 + 100 * energy))
            mc = QColor(CYAN_HOT if not is_stop else ALERT)
            mc.setAlpha(min(255, m_alpha))

            p.setPen(Qt.NoPen)

            # v4.0: Firefly fade-trail (paint previous positions with decreasing alpha)
            trail = m.get("trail", [])
            for ti, (t_angle, t_r_ratio) in enumerate(trail[:-1]):  # skip current
                trail_fade = (ti + 1) / max(1, len(trail))
                trail_dist = r * t_r_ratio
                t_rad = math.radians(t_angle)
                tx = cx + trail_dist * math.cos(t_rad)
                ty = cy + trail_dist * math.sin(t_rad)
                trail_c = QColor(CYAN_GLOW if not is_stop else ALERT)
                trail_c.setAlpha(int(m_alpha * 0.25 * trail_fade))
                p.setBrush(QBrush(trail_c))
                trail_size = m["size"] * 0.5 * trail_fade
                p.drawEllipse(QPointF(tx, ty), trail_size, trail_size)

            # Soft outer halo
            mc_halo = QColor(theme_glow)
            mc_halo.setAlpha(int(m_alpha * 0.4))
            p.setBrush(QBrush(mc_halo))
            p.drawEllipse(QPointF(mx, my), m["size"] * 1.8, m["size"] * 1.8)
            # Bright core
            p.setBrush(QBrush(mc))
            p.drawEllipse(QPointF(mx, my), m["size"] * 0.8, m["size"] * 0.8)

        # ── 10. Central Fusion Singularity (Multi-Stage Super-Glow Core) ──
        core_r = r * 0.32
        # Deep backdrop glass
        core_bg = QRadialGradient(cx, cy, core_r * 1.1)
        core_bg.setColorAt(0.0, QColor(8, 26, 60, 245))
        core_bg.setColorAt(0.75, QColor(DEEP_NAVY))
        core_bg.setColorAt(1.0, QColor(4, 12, 28, 255))
        p.setBrush(QBrush(core_bg))
        p.setPen(QPen(QColor(theme_color).darker(110), 1.5))
        p.drawEllipse(QRectF(cx - core_r, cy - core_r, core_r * 2.0, core_r * 2.0))

        # Intense plasma burst gradient
        plasma_g = QRadialGradient(cx, cy, core_r * 0.95)
        hot_white = QColor(255, 255, 255, min(255, int(200 + 55 * energy)))
        core_cyan = QColor(CYAN_CORE if not is_stop else ALERT)
        core_cyan.setAlpha(min(255, int(150 + 90 * energy)))
        outer_cyan = QColor(theme_glow)
        outer_cyan.setAlpha(0)

        plasma_g.setColorAt(0.0, hot_white)
        plasma_g.setColorAt(0.25, core_cyan)
        plasma_g.setColorAt(0.65, QColor(0, 140, 255, int(70 * energy)))
        plasma_g.setColorAt(1.0, outer_cyan)

        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(plasma_g))
        p.drawEllipse(QRectF(cx - core_r * 0.95, cy - core_r * 0.95, core_r * 1.9, core_r * 1.9))

        # v4.0: Core shimmer — rapid micro-flicker over the plasma seed
        shimmer_alpha = int(15 + 25 * abs(math.sin(self._shimmer_phase)))
        shimmer_c = QColor(255, 255, 255, shimmer_alpha)
        p.setBrush(QBrush(shimmer_c))
        shimmer_r = core_r * (0.55 + 0.12 * math.sin(self._shimmer_phase * 1.7))
        p.drawEllipse(QRectF(cx - shimmer_r, cy - shimmer_r, shimmer_r * 2.0, shimmer_r * 2.0))

        # Diamond energy seed (center dot)
        seed_r = max(2.5, core_r * 0.28)
        p.setBrush(QBrush(QColor(255, 255, 255, min(255, int(220 + 35 * energy)))))
        p.drawEllipse(QRectF(cx - seed_r, cy - seed_r, seed_r * 2.0, seed_r * 2.0))

        # ── 11. Anamorphic Starburst Cross-Glint (Cinematic Glint) ──
        p.save()
        p.translate(cx, cy)
        p.rotate(self._spin_glint)
        glint_len = core_r * (1.1 + 0.35 * self._breath)
        glint_w = max(1.2, core_r * 0.12)
        glint_alpha = int(90 + 90 * energy)

        for flare_angle in [0, 90]:
            p.save()
            p.rotate(flare_angle)
            flare_grad = QLinearGradient(-glint_len, 0, glint_len, 0)
            c_trans = QColor(255, 255, 255, 0)
            c_center = QColor(255, 255, 255, min(255, glint_alpha))
            flare_grad.setColorAt(0.0, c_trans)
            flare_grad.setColorAt(0.5, c_center)
            flare_grad.setColorAt(1.0, c_trans)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(flare_grad))
            p.drawRoundedRect(QRectF(-glint_len, -glint_w / 2.0, glint_len * 2.0, glint_w), 1.0, 1.0)
            p.restore()
        p.restore()

        # ── 12. State Overlays ──

        # think.orbit: Twin high-energy plasma comets racing on gyro track
        if self._state == STATE_THINK:
            for orbit_ang, comet_color, span_deg in [
                (self._orbit_angle1, GOLD_GLOW, 90),
                (self._orbit_angle2, CYAN_HOT, 75),
            ]:
                p.save()
                p.translate(cx, cy)
                p.rotate(orbit_ang)
                track_r = r * 0.58
                comet_pen = QPen(comet_color, 2.8, Qt.SolidLine, Qt.RoundCap)
                p.setPen(comet_pen)
                p.setBrush(Qt.NoBrush)
                p.drawArc(QRectF(-track_r, -track_r, track_r * 2.0, track_r * 2.0),
                          0, int(span_deg * 16))

                # Trailing spark particles
                for si in range(5):
                    t = si / 5.0
                    spark_rad = math.radians(-t * span_deg * 0.8)
                    sx = track_r * math.cos(spark_rad)
                    sy = -track_r * math.sin(spark_rad)
                    spark_a = int(220 * (1.0 - t))
                    sc = QColor(comet_color)
                    sc.setAlpha(spark_a)
                    p.setPen(Qt.NoPen)
                    p.setBrush(QBrush(sc))
                    p.drawEllipse(QPointF(sx, sy), 2.4 * (1.0 - t * 0.5), 2.4 * (1.0 - t * 0.5))
                p.restore()

        # speak.wave: Fluid glowing audio spectrum ribbon inside the core
        if self._state == STATE_SPEAK:
            wave_w = core_r * 1.8
            x0 = cx - wave_w / 2.0
            bar_spacing = wave_w / _WAVE_BARS
            p.save()
            for i, v in enumerate(self._wave):
                # Harmonic sine envelope
                env = math.sin(math.pi * i / (_WAVE_BARS - 1))
                bar_h = max(2.0, v * env * core_r * 0.85)
                bx = x0 + i * bar_spacing
                bw = max(1.5, bar_spacing * 0.65)

                b_alpha = int(160 + 95 * v)
                bc = QColor(CYAN_HOT if i % 2 == 0 else CYAN_CORE)
                bc.setAlpha(min(255, b_alpha))
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(bc))
                p.drawRoundedRect(QRectF(bx, cy - bar_h / 2.0, bw, bar_h), 1.0, 1.0)
            p.restore()

        # act.gold: Radiant golden surge & neon checkmark
        if self._state == STATE_ACT:
            if self._act_pulse > 0.01:
                pulse_r = r * (0.4 + 0.55 * self._act_pulse)
                gc = QColor(GOLD_GLOW)
                gc.setAlpha(int(200 * (1.0 - self._act_pulse * 0.5)))
                p.setPen(QPen(gc, 2.8))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(QRectF(cx - pulse_r, cy - pulse_r, pulse_r * 2.0, pulse_r * 2.0))

            # Razor-sharp neon checkmark
            check_pen = QPen(QColor(255, 240, 140), 3.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
            p.setPen(check_pen)
            p.drawLine(
                QPointF(cx - r * 0.22, cy + r * 0.02),
                QPointF(cx - r * 0.04, cy + r * 0.20)
            )
            p.drawLine(
                QPointF(cx - r * 0.04, cy + r * 0.20),
                QPointF(cx + r * 0.26, cy - r * 0.20)
            )

        p.end()

    # -- Mouse Interaction ------------------------------------------------
    def enterEvent(self, event):
        anim = QPropertyAnimation(self, b"hover")
        anim.setDuration(MOTION["hover.glow"])
        anim.setStartValue(self._hover)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        self._hover_anim = anim
        super().enterEvent(event)

    def leaveEvent(self, event):
        anim = QPropertyAnimation(self, b"hover")
        anim.setDuration(MOTION["hover.out"])
        anim.setStartValue(self._hover)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.start()
        self._hover_anim = anim
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # Instantly spawn a tactile holographic energy wave
            self.spawn_ripple(CYAN_HOT, speed=2.4)
            self.clicked.emit()
        super().mousePressEvent(event)
