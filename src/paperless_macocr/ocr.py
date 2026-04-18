"""macOCR HTTP server client."""

import logging
from typing import Any

import httpx

from paperless_macocr.config import Settings

logger = logging.getLogger(__name__)

# Horizontal gap (in average char widths) that splits boxes into clusters.
_CLUSTER_GAP_CHARS = 4
# Clusters separated by more than this many char widths are treated as
# independent columns (e.g. address left / date right in a letter) rather
# than table columns.
_COLUMN_GAP_CHARS = 15


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


def _cluster_text(cluster: list[dict[str, Any]]) -> str:
    """Join boxes within a single cluster into a text string."""
    return " ".join(b["text"].strip() for b in cluster if b.get("text", "").strip())


def _split_into_clusters(boxes: list[dict[str, Any]], char_w: float) -> list[list[dict[str, Any]]]:
    """Split a sorted line of boxes into horizontal clusters.

    A new cluster starts when the pixel gap to the previous box exceeds
    ``_CLUSTER_GAP_CHARS`` average character widths.
    """
    if not boxes:
        return []
    if char_w <= 0:
        return [boxes]
    clusters: list[list[dict[str, Any]]] = [[boxes[0]]]
    for box in boxes[1:]:
        prev = clusters[-1][-1]
        gap = box["x"] - (prev["x"] + prev["w"])
        if gap > char_w * _CLUSTER_GAP_CHARS:
            clusters.append([box])
        else:
            clusters[-1].append(box)
    return clusters


def _format_table(rows: list[list[str]]) -> list[str]:
    """Format rows of cell strings as an aligned table with consistent widths."""
    if not rows:
        return []
    num_cols = max(len(r) for r in rows)
    for row in rows:
        while len(row) < num_cols:
            row.append("")
    col_widths = [max(len(r[c]) for r in rows) for c in range(num_cols)]
    out: list[str] = []
    for row in rows:
        parts: list[str] = []
        for c, cell in enumerate(row):
            if c < num_cols - 1:
                parts.append(cell.ljust(col_widths[c]))
            else:
                parts.append(cell)
        out.append("  ".join(parts).rstrip())
    return out


def _avg_cluster_gap(run: list[list[list[dict[str, Any]]]], char_w: float) -> float:
    """Return the average inter-cluster gap in char widths for a run."""
    total = 0.0
    count = 0
    for clusters in run:
        for j in range(1, len(clusters)):
            prev_end = max(b["x"] + b["w"] for b in clusters[j - 1])
            next_start = min(b["x"] for b in clusters[j])
            total += (next_start - prev_end) / char_w
            count += 1
    return total / count if count else 0.0


def _reconstruct_text(data: dict[str, Any]) -> str:
    """Reconstruct reading-order text from macOCR bounding boxes.

    Handles three layout types:

    * **Single-column text** -- joined with spaces, no leading margin.
    * **Column layout** (e.g. address left, date right separated by a
      wide gap) -- columns are emitted sequentially, top-to-bottom.
    * **Tables** (multiple columns with moderate spacing) -- formatted
      with consistent column widths.
    """
    boxes: list[dict[str, Any]] = data.get("ocr_boxes", [])
    if not boxes:
        return data.get("ocr_result", "")

    boxes = [b for b in boxes if b.get("text", "").strip()]
    if not boxes:
        return ""

    # Strip left margin so text starts at column 0
    min_x = min(b["x"] for b in boxes)
    for b in boxes:
        b["x"] -= min_x

    char_w = _avg_char_width(boxes)

    # Sort top-to-bottom, left-to-right
    boxes.sort(key=lambda b: (b["y"], b["x"]))

    # --- group into lines ---
    lines: list[list[dict[str, Any]]] = []
    current_line: list[dict[str, Any]] = [boxes[0]]
    for box in boxes[1:]:
        prev = current_line[0]
        prev_mid = prev["y"] + prev["h"] / 2
        cur_mid = box["y"] + box["h"] / 2
        ref_height = min(prev["h"], box["h"])
        if ref_height > 0 and abs(cur_mid - prev_mid) < ref_height * 0.6:
            current_line.append(box)
        else:
            lines.append(current_line)
            current_line = [box]
    lines.append(current_line)

    for line in lines:
        line.sort(key=lambda b: b["x"])

    # --- paragraph gap detection ---
    line_tops = [min(b["y"] for b in ln) for ln in lines]
    line_heights = [max(b["h"] for b in ln) for ln in lines]
    gaps: list[float] = [line_tops[i] - (line_tops[i - 1] + line_heights[i - 1]) for i in range(1, len(lines))]

    para_threshold = 0.0
    if gaps:
        sg = sorted(gaps)
        med = sg[(len(sg) - 1) // 2]
        para_threshold = max(med * 1.8, line_heights[0] * 0.5) if med > 0 else line_heights[0] * 0.5

    # --- cluster each line ---
    all_clusters = [_split_into_clusters(ln, char_w) for ln in lines]

    # --- walk lines and emit text ---
    result: list[str] = []
    i = 0
    while i < len(lines):
        if len(all_clusters[i]) < 2:
            # Single-column line
            if i > 0 and gaps[i - 1] > para_threshold:
                result.append("")
            text = _cluster_text(all_clusters[i][0]) if all_clusters[i] else ""
            result.append(text)
            i += 1
        else:
            # Collect maximal run of consecutive multi-cluster lines
            run_start = i
            while i < len(lines) and len(all_clusters[i]) >= 2:
                i += 1
            run = all_clusters[run_start:i]

            if run_start > 0 and gaps[run_start - 1] > para_threshold:
                result.append("")

            avg_gap = _avg_cluster_gap(run, char_w) if char_w > 0 else 999

            if avg_gap > _COLUMN_GAP_CHARS:
                # Wide gap -> column layout: emit each column top-to-bottom
                num_cols = max(len(cl) for cl in run)
                for col_idx in range(num_cols):
                    if col_idx > 0:
                        result.append("")  # blank line between columns
                    for clusters in run:
                        if col_idx < len(clusters):
                            text = _cluster_text(clusters[col_idx])
                            if text:
                                result.append(text)
            else:
                # Moderate gap -> table: align columns
                rows = [[_cluster_text(c) for c in clusters] for clusters in run]
                result.extend(_format_table(rows))

    return "\n".join(result)


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
