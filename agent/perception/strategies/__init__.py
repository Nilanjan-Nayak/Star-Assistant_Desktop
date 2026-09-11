from __future__ import annotations

from agent.perception.strategies.a11y import A11yPerception
from agent.perception.strategies.ocr import OCRPerception
from agent.perception.strategies.template import TemplatePerception
from agent.perception.strategies.vision_llm import VisionLLMPerception

__all__ = [
    "A11yPerception",
    "OCRPerception",
    "TemplatePerception",
    "VisionLLMPerception",
]
