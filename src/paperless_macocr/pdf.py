"""PDF processing utilities."""

import logging

import pymupdf

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
