"""Tests for PDF processing utilities."""

import pymupdf
import pytest

from paperless_macocr.ocr import OcrPageData
from paperless_macocr.pdf import (
    image_to_searchable_pdf,
    pdf_embed_text_layer,
    pdf_has_text,
    pdf_page_count,
    pdf_page_to_png,
)


@pytest.fixture
def blank_pdf() -> bytes:
    """Create a blank single-page PDF."""
    doc = pymupdf.open()
    doc.new_page(width=200, height=200)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def text_pdf() -> bytes:
    """Create a single-page PDF with text."""
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    page.insert_text((50, 100), "Hello World")
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def multi_page_pdf() -> bytes:
    """Create a 3-page PDF."""
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page(width=200, height=200)
    data = doc.tobytes()
    doc.close()
    return data


def test_pdf_has_text_blank(blank_pdf):
    assert pdf_has_text(blank_pdf) is False


def test_pdf_has_text_with_text(text_pdf):
    assert pdf_has_text(text_pdf) is True


def test_pdf_page_count_single(blank_pdf):
    assert pdf_page_count(blank_pdf) == 1


def test_pdf_page_count_multi(multi_page_pdf):
    assert pdf_page_count(multi_page_pdf) == 3


def test_pdf_page_to_png(blank_pdf):
    png_data = pdf_page_to_png(blank_pdf, 0, dpi=72)
    # PNG files start with a specific magic header
    assert png_data[:4] == b"\x89PNG"
    assert len(png_data) > 0


def test_pdf_page_to_png_high_dpi(blank_pdf):
    png_low = pdf_page_to_png(blank_pdf, 0, dpi=72)
    png_high = pdf_page_to_png(blank_pdf, 0, dpi=300)
    # Higher DPI should produce a larger image
    assert len(png_high) > len(png_low)


# ---- pdf_embed_text_layer tests ----


def test_pdf_embed_text_layer_adds_searchable_text(blank_pdf):
    """Embedding OCR boxes should produce a PDF with extractable text."""
    page_data = [
        OcrPageData(
            text="Hello World",
            boxes=[
                {"text": "Hello", "x": 10, "y": 20, "w": 50, "h": 14},
                {"text": "World", "x": 70, "y": 20, "w": 50, "h": 14},
            ],
            image_width=200.0,
            image_height=200.0,
        )
    ]
    result = pdf_embed_text_layer(blank_pdf, page_data)
    assert pdf_has_text(result)
    with pymupdf.open(stream=result, filetype="pdf") as doc:
        text = doc[0].get_text()
        assert "Hello" in text
        assert "World" in text


def test_pdf_embed_text_layer_no_boxes(blank_pdf):
    """Empty boxes should return a valid PDF without adding text."""
    page_data = [OcrPageData(text="", boxes=[], image_width=200, image_height=200)]
    result = pdf_embed_text_layer(blank_pdf, page_data)
    assert not pdf_has_text(result)


def test_pdf_embed_text_layer_multi_page(multi_page_pdf):
    """Should handle multi-page PDFs."""
    page_data = [
        OcrPageData(
            text="Page 1",
            boxes=[{"text": "Page 1", "x": 10, "y": 20, "w": 60, "h": 14}],
            image_width=200.0,
            image_height=200.0,
        ),
        OcrPageData(text="", boxes=[], image_width=200, image_height=200),
        OcrPageData(
            text="Page 3",
            boxes=[{"text": "Page 3", "x": 10, "y": 20, "w": 60, "h": 14}],
            image_width=200.0,
            image_height=200.0,
        ),
    ]
    result = pdf_embed_text_layer(multi_page_pdf, page_data)
    with pymupdf.open(stream=result, filetype="pdf") as doc:
        assert "Page 1" in doc[0].get_text()
        assert doc[1].get_text().strip() == ""
        assert "Page 3" in doc[2].get_text()


# ---- image_to_searchable_pdf tests ----


def test_image_to_searchable_pdf():
    """Should produce a searchable PDF from a PNG image."""
    # Create a small PNG via pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=100, height=100)
    page.draw_rect(pymupdf.Rect(10, 10, 90, 90), color=(0, 0, 0))
    pix = page.get_pixmap()
    png_bytes = pix.tobytes(output="png")
    doc.close()

    ocr_data = OcrPageData(
        text="Test",
        boxes=[{"text": "Test", "x": 10, "y": 10, "w": 40, "h": 12}],
        image_width=100.0,
        image_height=100.0,
    )
    result = image_to_searchable_pdf(png_bytes, ocr_data, image_format="png")

    # Should be a valid PDF
    assert result[:5] == b"%PDF-"
    with pymupdf.open(stream=result, filetype="pdf") as doc:
        assert "Test" in doc[0].get_text()
