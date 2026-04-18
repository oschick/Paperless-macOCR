"""macOCR HTTP server client."""

import logging
from typing import Any

import httpx

from paperless_macocr.config import Settings

logger = logging.getLogger(__name__)


def _avg_char_width(boxes: list[dict[str, Any]]) -> float:
    """Estimate the average character width in pixels from bounding boxes."""
    total_w = 0.0
    total_chars = 0
    for b in boxes:
        text = b.get("text", "").strip()
        if text and b.get("w", 0) > 0:
            total_w += b["w"]
            total_chars += len(text)
    return total_w / total_chars if total_chars > 0 else 0.0


def _join_line(boxes: list[dict[str, Any]], char_w: float) -> str:
    """Join boxes on a single line, preserving horizontal spacing.

    Uses estimated character width to convert pixel gaps into spaces
    so that left-aligned and right-aligned blocks (e.g. address vs date
    in a letter) keep their visual separation.
    """
    if char_w <= 0:
        return " ".join(b["text"].strip() for b in boxes)

    parts: list[str] = []
    cursor = 0
    for box in boxes:
        target_col = round(box["x"] / char_w)
        text = box["text"].strip()
        if target_col > cursor:
            parts.append(" " * (target_col - cursor))
        elif parts:
            parts.append(" ")  # at least one space between fragments
        parts.append(text)
        cursor = target_col + len(text)
    return "".join(parts).rstrip()


def _reconstruct_text(data: dict[str, Any]) -> str:
    """Reconstruct reading-order text from macOCR bounding boxes.

    Groups text fragments into lines based on vertical position,
    sorts left-to-right within each line, preserves horizontal
    spacing proportionally (important for letter-style layouts with
    left/right aligned blocks), and inserts paragraph breaks where
    vertical gaps are significantly larger than the typical line
    spacing.
    """
    boxes: list[dict[str, Any]] = data.get("ocr_boxes", [])
    if not boxes:
        return data.get("ocr_result", "")

    # Filter out empty text boxes
    boxes = [b for b in boxes if b.get("text", "").strip()]
    if not boxes:
        return ""

    char_w = _avg_char_width(boxes)

    # Sort primarily by y (top-to-bottom)
    boxes.sort(key=lambda b: (b["y"], b["x"]))

    # Group boxes into lines: boxes with similar y belong to the same line.
    # Two boxes are on the same line if their vertical overlap is significant
    # relative to their height.
    lines: list[list[dict[str, Any]]] = []
    current_line: list[dict[str, Any]] = [boxes[0]]

    for box in boxes[1:]:
        prev = current_line[0]
        prev_mid = prev["y"] + prev["h"] / 2
        cur_mid = box["y"] + box["h"] / 2
        # Use the smaller height as reference for line grouping tolerance
        ref_height = min(prev["h"], box["h"])
        if ref_height > 0 and abs(cur_mid - prev_mid) < ref_height * 0.6:
            current_line.append(box)
        else:
            lines.append(current_line)
            current_line = [box]
    lines.append(current_line)

    # Sort each line left-to-right by x position
    for line in lines:
        line.sort(key=lambda b: b["x"])

    # Detect paragraph breaks by looking at vertical gaps between lines.
    # A gap notably larger than the median line spacing indicates a paragraph.
    line_tops = [min(b["y"] for b in line) for line in lines]
    line_heights = [max(b["h"] for b in line) for line in lines]
    gaps: list[float] = []
    for i in range(1, len(lines)):
        gap = line_tops[i] - (line_tops[i - 1] + line_heights[i - 1])
        gaps.append(gap)

    paragraph_threshold = 0.0
    if gaps:
        sorted_gaps = sorted(gaps)
        median_gap = sorted_gaps[(len(sorted_gaps) - 1) // 2]
        paragraph_threshold = (
            max(median_gap * 1.8, line_heights[0] * 0.5)
            if median_gap > 0
            else line_heights[0] * 0.5
        )

    # Build the final text
    result_parts: list[str] = []
    for i, line in enumerate(lines):
        line_text = _join_line(line, char_w)
        if i > 0 and gaps and gaps[i - 1] > paragraph_threshold:
            result_parts.append("")  # blank line = paragraph break
        result_parts.append(line_text)

    return "\n".join(result_parts)


class MacOCRClient:
    """Async client for the macOCR HTTP server."""

    def __init__(self, settings: Settings) -> None:
        base_url = str(settings.macocr_url).rstrip("/")

        auth = None
        if settings.macocr_auth:
            parts = settings.macocr_auth.split(":", 1)
            if len(parts) == 2:
                auth = httpx.BasicAuth(username=parts[0], password=parts[1])

        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=httpx.Timeout(120.0, connect=10.0),
        )

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def ocr_image(
        self,
        image_bytes: bytes,
        filename: str = "page.png",
        content_type: str = "image/png",
    ) -> str:
        """Send an image to macOCR and return the recognized text.

        Args:
            image_bytes: Image data.
            filename: Filename hint for the upload.
            content_type: MIME type of the image.

        Returns:
            Extracted text string.
        """
        response = await self._client.post(
            "/upload",
            files={"file": (filename, image_bytes, content_type)},
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("success"):
            msg = data.get("message", "Unknown macOCR error")
            raise RuntimeError(f"macOCR returned failure: {msg}")

        return _reconstruct_text(data)
