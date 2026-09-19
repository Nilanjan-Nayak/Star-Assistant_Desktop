"""
STAR ASSISTANT — Design Tokens  (v3.0 — Ultra-Premium Futuristic HUD)
Pulled from Chapter 32 (Color, type, layout tokens) and the Complete Motion Table.
Single source of truth for the HUD: every widget imports from here rather than
hardcoding a hex value or a millisecond count.

v3.0 Enhancements:
  - Richer gradient palette with purple/violet accents for depth
  - Neon glow colors for animated borders
  - Enhanced glass colors with multi-stop gradients
  - Better typography tokens with modern sizing
  - New motion tokens for smooth micro-animations
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
CYAN_NEON = QColor(0, 240, 255, 255)  # ultra-bright neon cyan for borders
ARC_GLOW = QColor("#00E5FF")        # ambient arc reactor halo glow
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
# v3.0 — New accent & glass tokens for ultra-premium visuals
# ---------------------------------------------------------------------------
PURPLE_ACCENT = QColor("#7B5CFF")   # purple accent for depth layering
PURPLE_GLOW = QColor(140, 100, 255, 50)  # subtle purple glow wash
PURPLE_DIM = QColor(100, 70, 200, 30)    # very subtle purple tint
VIOLET_EDGE = QColor(160, 120, 255, 40)  # violet edge highlight

GLASS_DEEP = QColor(3, 6, 18, 245)      # deepest glass layer
GLASS_DARK = QColor(5, 12, 30, 235)     # dark glass mid
GLASS_MID = QColor(8, 18, 42, 220)      # standard glass
GLASS_LIGHT = QColor(12, 28, 58, 180)   # lighter glass for hover

BORDER_GLOW_CYAN = QColor(0, 210, 255, 90)    # breathing border glow
BORDER_GLOW_PURPLE = QColor(120, 80, 255, 50)  # purple accent border

ACCENT_BAR_TOP = QColor(0, 230, 255, 200)     # gradient accent bar start
ACCENT_BAR_BOT = QColor(100, 60, 255, 160)     # gradient accent bar end

# Status glow colors
STATUS_GLOW_OK = QColor(61, 255, 154, 40)
STATUS_GLOW_GOLD = QColor(201, 162, 39, 35)
STATUS_GLOW_AMBER = QColor(255, 193, 77, 30)
STATUS_GLOW_ALERT = QColor(255, 77, 77, 30)

# Text accent
TEXT_BRIGHT = QColor("#F0FBFF")     # bright white-cyan for headers
TEXT_CYAN = QColor(0, 212, 255, 200)  # cyan-tinted text
TEXT_GOLD = QColor(220, 180, 60, 220)  # warm gold text

# ---------------------------------------------------------------------------
# Layout tokens
# ---------------------------------------------------------------------------
GRID = 8                 # 8px base grid
PANEL_RADIUS = 14        # slightly more rounded for premium glass feel
BLUR_RADIUS = 24         # 16-24px glass blur
LINE_WIDTH = 1.2         # hairline refined
BRACKET_INSET = 10       # corner brackets sit 10px in from panel corners
BRACKET_ARM = 26         # corner bracket arm length

SPACE_RAIL = 420         # side mode width — slightly wider for breathing room
SPACE_ORB_MIN = 72
SPACE_ORB_MAX = 120

# ---------------------------------------------------------------------------
# Type tokens
# ---------------------------------------------------------------------------
FONT_HUD_FAMILY = "Segoe UI"       # geometric sans stand-in for HUD chrome
FONT_HUD_SIZE = 11
FONT_HUD_SIZE_LG = 14
FONT_HUD_SIZE_SM = 9
FONT_HUD_SIZE_XL = 16              # extra-large for hero headers
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
    "panel.in": 500,         # slightly longer for cinematic effect
    "panel.out": 320,        # slightly longer for smooth exit
    "bracket.flash": 180,
    "alert.once": 240,
    "deny.shake": 200,
    "allow.lift": 200,
    "stop.collapse": 200,
    "quiet.dim": 800,
    "mode.orb": 400,         # smoother mode transitions
    "mode.rail": 400,
    "mode.center": 500,
    "hex.breathe": 6000,     # idle breathing loop
    "type.log": 12,          # ms per glyph (bumped from 8 for Windows timer compat)
    "verify.ok": 140,
    "verify.bad": 140,
    "remember.write": 220,
    "hover.glow": 220,       # hover-in glow transition
    "hover.out": 350,        # hover-out fade
    "cursor.blink": 530,     # blinking cyber cursor interval
    # v3.0 — new smooth micro-animations
    "glow.pulse": 2800,      # border glow pulse cycle (ms)
    "float.drift": 4000,     # floating element drift
    "card.lift": 260,        # card hover lift
    "border.breathe": 3200,  # border breathing cycle
    "status.pulse": 1400,    # status indicator pulse
    "ambient.cycle": 8000,   # ambient color shift
    "input.focus": 300,      # input bar focus glow
    "input.blur": 500,       # input bar blur fade
    "footer.tick": 2000,     # footer metrics refresh
}

# Presence modes (Chapter 23 — Desktop Presence Modes)
MODE_ORB = "ORB"
MODE_RAIL = "RAIL"
MODE_CENTER = "CENTER"
MODE_GHOST = "GHOST"
