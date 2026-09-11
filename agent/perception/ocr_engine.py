"""OCR engines. Tesseract is preferred; EasyOCR is optional; tests inject a scripted engine.

Screen-seeing is split in two:

* **capture** (``world.capture``) — pixels
* **read** (this module) — pixels → words + boxes

Coordinates returned by engines are in the *original* raster space even if
the image was upscaled for recognition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent.core.errors import PerceptionError
from agent.geometry.bbox import BoundingBox
from agent.geometry.raster import Raster


@dataclass(frozen=True, slots=True)
class OcrToken:
    text: str
    x: int
    y: int
    width: int
    height: int
    confidence: float  # 0..1


@runtime_checkable
class OcrEngine(Protocol):
    name: str

    def read(self, screenshot: Raster, region: BoundingBox | None = None) -> list[OcrToken]: ...


class EmptyOcr:
    """Always returns no tokens. Used when no backend is installed."""

    name = "empty"

    def read(self, screenshot: Raster, region: BoundingBox | None = None) -> list[OcrToken]:
        _ = (screenshot, region)
        return []


class ScriptedOcr:
    """Deterministic engine for tests — ignores pixels, returns canned tokens."""

    name = "scripted"

    def __init__(self, tokens: list[OcrToken] | None = None) -> None:
        self.tokens = list(tokens or [])

    def read(self, screenshot: Raster, region: BoundingBox | None = None) -> list[OcrToken]:
        _ = screenshot
        if region is None:
            return list(self.tokens)
        return [
            tok
            for tok in self.tokens
            if region.x <= tok.x < region.right and region.y <= tok.y < region.bottom
        ]


class TesseractOcr:
    name = "tesseract"

    def __init__(self, lang: str | None = None, *, scale: float = 2.0) -> None:
        try:
            import pytesseract
        except ImportError as exc:
            raise PerceptionError("pytesseract is not installed") from exc
        try:
            pytesseract.get_tesseract_version()
        except Exception as exc:
            raise PerceptionError("tesseract binary is not on PATH") from exc
        self._pytesseract = pytesseract
        self.lang = lang or _tesseract_lang(pytesseract)
        self.scale = scale if scale >= 1.0 else 1.0

    @classmethod
    def maybe(cls) -> TesseractOcr | None:
        try:
            return cls()
        except PerceptionError:
            return None

    def read(self, screenshot: Raster, region: BoundingBox | None = None) -> list[OcrToken]:
        from pytesseract import Output

        raster = screenshot.crop(region) if region is not None else screenshot
        offset_x = region.x if region is not None else 0
        offset_y = region.y if region is not None else 0
        image, used_scale = _prepare_image(raster, self.scale)
        try:
            data = self._pytesseract.image_to_data(
                image, lang=self.lang, output_type=Output.DICT
            )
        except Exception as exc:
            raise PerceptionError("OCR extraction failed", cause=str(exc)) from exc

        tokens: list[OcrToken] = []
        texts: list[object] = list(data.get("text", []))
        for i, raw in enumerate(texts):
            word = str(raw).strip()
            if not word:
                continue
            try:
                conf_raw = float(data["conf"][i])
            except (TypeError, ValueError, KeyError):
                conf_raw = 50.0
            conf = max(0.0, min(1.0, conf_raw / 100.0)) if conf_raw >= 0 else 0.5
            try:
                left = int(data["left"][i])
                top = int(data["top"][i])
                width = max(1, int(data["width"][i]))
                height = max(1, int(data["height"][i]))
            except (TypeError, ValueError, KeyError):
                continue
            tokens.append(
                OcrToken(
                    text=word,
                    x=int(left / used_scale) + offset_x,
                    y=int(top / used_scale) + offset_y,
                    width=max(1, int(width / used_scale)),
                    height=max(1, int(height / used_scale)),
                    confidence=conf,
                )
            )
        return tokens


class EasyOcr:
    name = "easyocr"

    def __init__(self) -> None:
        try:
            import easyocr
        except ImportError as exc:
            raise PerceptionError("easyocr is not installed") from exc
        langs = ["en"]
        try:
            # Bengali pack if the user has it; EasyOCR downloads on first use.
            self._reader = easyocr.Reader(langs, gpu=False, verbose=False)
        except Exception as exc:
            raise PerceptionError("easyocr failed to load", cause=str(exc)) from exc

    @classmethod
    def maybe(cls) -> EasyOcr | None:
        try:
            return cls()
        except PerceptionError:
            return None

    def read(self, screenshot: Raster, region: BoundingBox | None = None) -> list[OcrToken]:
        import numpy as np

        raster = screenshot.crop(region) if region is not None else screenshot
        offset_x = region.x if region is not None else 0
        offset_y = region.y if region is not None else 0
        arr = np.frombuffer(raster.rgb, dtype=np.uint8).reshape(
            raster.height, raster.width, 3
        )
        try:
            hits = self._reader.readtext(arr)
        except Exception as exc:
            raise PerceptionError("easyocr failed", cause=str(exc)) from exc
        tokens: list[OcrToken] = []
        for box, text, conf in hits:
            word = str(text).strip()
            if not word:
                continue
            xs = [int(p[0]) for p in box]
            ys = [int(p[1]) for p in box]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
            tokens.append(
                OcrToken(
                    text=word,
                    x=x0 + offset_x,
                    y=y0 + offset_y,
                    width=max(1, x1 - x0),
                    height=max(1, y1 - y0),
                    confidence=max(0.0, min(1.0, float(conf))),
                )
            )
        return tokens


_DETECTED: OcrEngine | None = None


def detect_engine() -> OcrEngine:
    global _DETECTED
    if _DETECTED is not None:
        return _DETECTED
    tess = TesseractOcr.maybe()
    if tess is not None:
        _DETECTED = tess
        return tess
    easy = EasyOcr.maybe()
    if easy is not None:
        _DETECTED = easy
        return easy
    _DETECTED = EmptyOcr()
    return _DETECTED


def engine_name(engine: OcrEngine) -> str:
    return getattr(engine, "name", type(engine).__name__)


def group_lines(tokens: list[OcrToken]) -> list[str]:
    """Cluster tokens into reading-order lines."""
    if not tokens:
        return []
    ordered = sorted(tokens, key=lambda tok: (tok.y, tok.x))
    lines: list[list[OcrToken]] = []
    for tok in ordered:
        if not lines:
            lines.append([tok])
            continue
        prev = lines[-1][-1]
        threshold = max(prev.height, tok.height) * 0.6
        if abs(tok.y - prev.y) <= threshold:
            lines[-1].append(tok)
        else:
            lines.append([tok])
    return [" ".join(item.text for item in line) for line in lines]


def _tesseract_lang(pytesseract: object) -> str:
    getter = getattr(pytesseract, "get_languages", None)
    if getter is None:
        return "eng"
    try:
        langs = {str(x) for x in getter(config="")}
    except Exception:
        return "eng"
    chosen: list[str] = []
    if "eng" in langs:
        chosen.append("eng")
    if "ben" in langs:
        chosen.append("ben")
    return "+".join(chosen) or "eng"


def _prepare_image(raster: Raster, scale: float) -> tuple[object, float]:
    """Grayscale + optional 2× upscale. Falls back to raw PIL if OpenCV missing."""
    try:
        import cv2
        import numpy as np
        from PIL import Image

        arr = np.frombuffer(raster.rgb, dtype=np.uint8).reshape(
            raster.height, raster.width, 3
        )
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        used = 1.0
        if scale > 1.0:
            gray = cv2.resize(
                gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
            )
            used = scale
        # Light denoise; Otsu can destroy low-contrast UI text so we skip it.
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        return Image.fromarray(gray), used
    except Exception:
        image = raster.to_pil()
        if scale > 1.0:
            try:
                from PIL import Image as PilImage

                if isinstance(image, PilImage.Image):
                    new_size = (int(raster.width * scale), int(raster.height * scale))
                    image = image.resize(new_size, resample=PilImage.Resampling.BICUBIC)
                    return image, scale
            except Exception:
                return image, 1.0
        return image, 1.0
