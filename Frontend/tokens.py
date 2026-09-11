"""
STAR ASSISTANT — Design Tokens  (v2.0 — Premium HUD Upgrade)
Pulled from Chapter 32 (Color, type, layout tokens) and the Complete Motion Table.
Single source of truth for the HUD: every widget imports from here rather than
hardcoding a hex value or a millisecond count.
"""

from PySide6.QtGui import QColor

# ---------------------------------------------------------------------------
# Color tokens — Core palette
# ---------------------------------------------------------------------------
NAVY = QColor("#061024")     # header, overlay veil
CYAN = QColor("#00D4FF")     # life, hear, links
GOLD = QColor("#C9A227")     # maker, confirm, complete
ALERT = QColor("#FF4D4D")    # once, never a loop
OK = QColor("#3DFF9A")       # verify true
AMBER = QColor("#FFC14D")    # degraded / warning

# ---------------------------------------------------------------------------
# Extended color tokens — Glow, frost, & accent hues
# ---------------------------------------------------------------------------
CYAN_GLOW = QColor("#00E5FF")       # brighter cyan for reactor glow rings
CYAN_DIM = QColor(0, 180, 220, 80)  # dimmed cyan for subtle elements
CYAN_CORE = QColor("#80F5FF")       # super-radiant core photon hue
CYAN_HOT = QColor("#E6FFFF")        # central plasma seed white-cyan
COIL_BASE = QColor(0, 45, 80, 160)  # reactor coil housing
COIL_ACTIVE = QColor(0, 230, 255, 240) # reactor coil active glow
COIL_GOLD = QColor(255, 200, 50, 240)  # reactor coil gold energized
DEEP_NAVY = QColor("#040B1A")       # deeper background for panels
FROST_TOP = QColor(120, 200, 255, 28)  # top-edge frosted glass highlight
FROST_BOT = QColor(0, 40, 80, 8)    # bottom frost fade
GOLD_GLOW = QColor("#E0B830")       # warmer gold for act pulse
TEXT_PRIMARY = QColor("#E8F6FF")     # primary text on dark
TEXT_DIM = QColor(180, 210, 230, 160)  # secondary / timestamp text
RING_OUTER = QColor(0, 200, 255, 100)  # outer tech ring base
RING_MID = QColor(0, 220, 255, 140)    # mid gyroscopic ring
CORE_BRIGHT = QColor("#40F0FF")     # reactor core brightest point
PANEL_BORDER = QColor(0, 212, 255, 55)  # panel outline

# ---------------------------------------------------------------------------
# Layout tokens
# ---------------------------------------------------------------------------
GRID = 8                 # 8px base grid
PANEL_RADIUS = 11        # 10-12px
BLUR_RADIUS = 20         # 16-24px glass blur
LINE_WIDTH = 1.5         # 1-2px hairlines
BRACKET_INSET = 8        # corner brackets sit 8px in from panel corners

SPACE_RAIL = 400         # side mode width
SPACE_ORB_MIN = 72
SPACE_ORB_MAX = 120

# ---------------------------------------------------------------------------
# Type tokens
# ---------------------------------------------------------------------------
FONT_HUD_FAMILY = "Segoe UI"       # geometric sans stand-in for HUD chrome
FONT_HUD_SIZE = 11
FONT_HUD_SIZE_LG = 13
FONT_HUD_SIZE_SM = 9
FONT_MONO_FAMILY = "Consolas"      # activity log / mono readouts
FONT_BN_FAMILY = "Noto Sans Bengali"

# ---------------------------------------------------------------------------
# Motion table (ms unless noted) — Chapter "Complete motion table"
# Only the subset this frontend actually animates.
# ---------------------------------------------------------------------------
MOTION = {
    "boot.cinema": 4800,
    "boot.fast": 900,
    "wake.tick": 180,
    "hear.bloom": 120,
    "think.orbit": 1200,     # ms per revolution, looping
    "act.gold": 160,
    "barge.cut": 90,
    "panel.in": 420,
    "panel.out": 280,
    "bracket.flash": 180,
    "alert.once": 240,
    "deny.shake": 200,
    "allow.lift": 200,
    "stop.collapse": 200,
    "quiet.dim": 800,
    "mode.orb": 350,
    "mode.rail": 350,
    "mode.center": 450,
    "hex.breathe": 6000,     # idle breathing loop
    "type.log": 12,          # ms per glyph (bumped from 8 for Windows timer compat)
    "verify.ok": 140,
    "verify.bad": 140,
    "remember.write": 220,
    "hover.glow": 200,       # hover-in glow transition
    "hover.out": 300,        # hover-out fade
    "cursor.blink": 530,     # blinking cyber cursor interval
}

# Presence modes (Chapter 23 — Desktop Presence Modes)
MODE_ORB = "ORB"
MODE_RAIL = "RAIL"
MODE_CENTER = "CENTER"
MODE_GHOST = "GHOST"
