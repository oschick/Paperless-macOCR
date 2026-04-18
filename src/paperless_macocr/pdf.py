"""PDF processing utilities."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import pymupdf

if TYPE_CHECKING:
    from paperless_macocr.ocr import OcrPageData

logger = logging.getLogger(__name__)


def pdf_has_text(pdf_bytes: bytes) -> bool:
    """Check whether a PDF already contains extractable text.

    Returns True if any page has meaningful text content.
    """
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            text = page.get_text().strip()
            if text:
                return True
    return False


def pdf_page_count(pdf_bytes: bytes) -> int:
    """Return the number of pages in a PDF."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        return len(doc)


def pdf_page_to_png(pdf_bytes: bytes, page_number: int, dpi: int = 300) -> bytes:
    """Render a single PDF page to a PNG image.

    Args:
        pdf_bytes: Raw PDF file content.
        page_number: Zero-based page index.
        dpi: Resolution for rendering (default 300).

    Returns:
        PNG image bytes.
    """
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        page = doc[page_number]
        zoom = dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        pixmap = page.get_pixmap(matrix=matrix)
        return pixmap.tobytes(output="png")


def _overlay_boxes(
    page: Any,
    boxes: list[dict[str, Any]],
    image_width: float,
    image_height: float,
) -> None:
    """Insert invisible text onto *page* from OCR bounding boxes.

    The font size is computed so that the rendered text width exactly
    matches the bounding box width.  Text is placed at the baseline
    using ``insert_text`` to avoid line-height padding issues.
    """
    if not boxes or image_width <= 0 or image_height <= 0:
        return

    scale_x = page.rect.width / image_width
    scale_y = page.rect.height / image_height

    # Pre-load the font so we can measure text widths
    font = pymupdf.Font("helv")

    for box in boxes:
        text = box.get("text", "").strip()
        if not text:
            continue
        x = box["x"] * scale_x
        y = box["y"] * scale_y
        w = box["w"] * scale_x
        h = box["h"] * scale_y
        if w <= 0 or h <= 0:
            continue

        # Compute the font size that makes the text exactly as wide
        # as the bounding box.  Also cap at box height so it doesn't
        # bleed vertically.
        unit_width = font.text_length(text, fontsize=1)
        fontsize = min(w / unit_width, h) if unit_width > 0 else h
        fontsize = max(fontsize, 1.0)

        # Baseline is at the bottom of the box minus a small descender
        # allowance (~20% of fontsize).
        baseline_y = y + h - fontsize * 0.2
        page.insert_text(
            pymupdf.Point(x, baseline_y),
            text,
            fontsize=fontsize,
            fontname="helv",
            render_mode=3,  # invisible
        )


def pdf_embed_text_layer(
    pdf_bytes: bytes,
    page_data: list[OcrPageData],
) -> bytes:
    """Return a copy of *pdf_bytes* with an invisible OCR text layer.

    For each page the bounding-box coordinates from *page_data* are
    scaled from image-pixel space to PDF-point space and inserted as
    invisible (render-mode 3) text so that PDF viewers can search /
    copy the text.
    """
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page_idx, page in enumerate(doc):
            if page_idx >= len(page_data):
                break
            ocr = page_data[page_idx]
            if not ocr.boxes:
                continue
            _overlay_boxes(page, ocr.boxes, ocr.image_width, ocr.image_height)
        return doc.tobytes()


def image_to_searchable_pdf(
    image_bytes: bytes,
    ocr_data: OcrPageData,
    image_format: str = "png",
) -> bytes:
    """Create a single-page searchable PDF from an image.

    The image becomes the visible page content, and the OCR text is
    overlaid as an invisible layer.
    """
    with pymupdf.open(stream=image_bytes, filetype=image_format) as img_doc:
        pdf_raw = img_doc.convert_to_pdf()

    with pymupdf.open("pdf", pdf_raw) as doc:
        page = doc[0]
        _overlay_boxes(page, ocr_data.boxes, ocr_data.image_width, ocr_data.image_height)
        return doc.tobytes()
