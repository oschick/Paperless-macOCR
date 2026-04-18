"""Tests for PDF processing utilities."""

import pymupdf
import pytest

from paperless_macocr.pdf import pdf_has_text, pdf_page_count, pdf_page_to_png


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
