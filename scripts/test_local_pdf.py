#!/usr/bin/env python3
"""Process a local PDF through macOCR and produce a searchable PDF.

Usage:
    python scripts/test_local_pdf.py <input.pdf> [--ocr-url URL] [--ocr-auth USER:PASS] [--dpi 300] [--visible]

Outputs (next to the input file):
    <input>_searchable.pdf  -- invisible text layer (production behaviour)
    <input>_debug.pdf       -- visible coloured text layer (when --visible is passed)
    <input>_page<N>.json    -- raw macOCR JSON per page

Requires a running macOCR or iOS-OCR-Server instance.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any

import httpx
import pymupdf

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from paperless_macocr.ocr import OcrPageData, _get_rect_corners, _reconstruct_text
from paperless_macocr.pdf import (
    _ASCENDER,
    _VIS_HEIGHT,
    _page_tilt_deg,
    pdf_embed_text_layer,
    pdf_page_count,
    pdf_page_to_png,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


async def ocr_image(
    client: httpx.AsyncClient,
    image_bytes: bytes,
    filename: str = "page.png",
) -> dict[str, Any]:
    """Send an image to macOCR and return the raw JSON response."""
    resp = await client.post(
        "/upload",
        files={"file": (filename, image_bytes, "image/png")},
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        msg = data.get("message", "Unknown error")
        raise RuntimeError(f"macOCR error: {msg}")
    return data


def build_debug_pdf(
    pdf_bytes: bytes,
    all_page_data: list[OcrPageData],
    all_raw: list[dict[str, Any]],
) -> bytes:
    """Build a PDF with *visible* coloured text overlays for visual debugging."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page_idx, page in enumerate(doc):
            if page_idx >= len(all_page_data):
                continue
            ocr = all_page_data[page_idx]
            if not ocr.boxes or ocr.image_width <= 0 or ocr.image_height <= 0:
                continue

            scale_x = page.rect.width / ocr.image_width
            scale_y = page.rect.height / ocr.image_height
            font = pymupdf.Font("helv")
            page_tilt = _page_tilt_deg(ocr.boxes)

            for box in ocr.boxes:
                text = box.get("text", "").strip()
                if not text:
                    continue

                corners = _get_rect_corners(box)
                if corners:
                    tl, tr, _br, bl = corners
                    tl_pdf = (tl[0] * scale_x, tl[1] * scale_y)
                    tr_pdf = (tr[0] * scale_x, tr[1] * scale_y)
                    bl_pdf = (bl[0] * scale_x, bl[1] * scale_y)
                    br_pdf = (_br[0] * scale_x, _br[1] * scale_y)

                    # Draw the oriented bounding box
                    shape = page.new_shape()
                    shape.draw_line(pymupdf.Point(*tl_pdf), pymupdf.Point(*tr_pdf))
                    shape.draw_line(pymupdf.Point(*tr_pdf), pymupdf.Point(*br_pdf))
                    shape.draw_line(pymupdf.Point(*br_pdf), pymupdf.Point(*bl_pdf))
                    shape.draw_line(pymupdf.Point(*bl_pdf), pymupdf.Point(*tl_pdf))
                    shape.finish(color=(1, 0, 0), width=0.5)
                    shape.commit()

                    dx = tr_pdf[0] - tl_pdf[0]
                    dy = tr_pdf[1] - tl_pdf[1]
                    angle_deg = math.degrees(math.atan2(dy, dx))

                    if abs(angle_deg) < 0.05 and abs(page_tilt) > 0.05:
                        angle_deg = page_tilt

                    text_w = math.sqrt(dx * dx + dy * dy)
                    perp_dx = bl_pdf[0] - tl_pdf[0]
                    perp_dy = bl_pdf[1] - tl_pdf[1]
                    text_h = math.sqrt(perp_dx * perp_dx + perp_dy * perp_dy)
                    if text_w <= 0 or text_h <= 0:
                        continue

                    unit_width = font.text_length(text, fontsize=1)
                    fontsize = min(text_w / unit_width, text_h) if unit_width > 0 else text_h
                    fontsize = max(fontsize, 1.0)

                    pivot = pymupdf.Point(tl_pdf[0], tl_pdf[1])
                    if fontsize < text_h * 0.9:
                        vis_h = fontsize * _VIS_HEIGHT
                        top_margin = (text_h - vis_h) / 2
                        baseline_y = tl_pdf[1] + top_margin + fontsize * _ASCENDER
                    else:
                        baseline_y = tl_pdf[1] + text_h * _ASCENDER / _VIS_HEIGHT

                    page.insert_text(
                        pymupdf.Point(tl_pdf[0], baseline_y),
                        text,
                        fontsize=fontsize,
                        fontname="helv",
                        color=(0, 0, 1),  # blue visible text
                        morph=(pivot, pymupdf.Matrix(-angle_deg)),
                    )
                else:
                    # Axis-aligned fallback
                    x = box["x"] * scale_x
                    y = box["y"] * scale_y
                    w = box["w"] * scale_x
                    h = box["h"] * scale_y
                    if w <= 0 or h <= 0:
                        continue

                    # Draw axis-aligned rect
                    page.draw_rect(
                        pymupdf.Rect(x, y, x + w, y + h),
                        color=(0, 0.8, 0),
                        width=0.5,
                    )

                    unit_width = font.text_length(text, fontsize=1)
                    fontsize = min(w / unit_width, h) if unit_width > 0 else h
                    fontsize = max(fontsize, 1.0)

                    if fontsize < h * 0.9:
                        vis_h_f = fontsize * _VIS_HEIGHT
                        top_margin = (h - vis_h_f) / 2
                        baseline_y = y + top_margin + fontsize * _ASCENDER
                    else:
                        baseline_y = y + h * _ASCENDER / _VIS_HEIGHT
                    page.insert_text(
                        pymupdf.Point(x, baseline_y),
                        text,
                        fontsize=fontsize,
                        fontname="helv",
                        color=(0, 0.6, 0),  # green visible text
                    )

        return doc.tobytes()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Process a local PDF through macOCR")
    parser.add_argument("input_pdf", type=Path, help="Path to input PDF")
    parser.add_argument("--ocr-url", default="http://localhost:8080", help="macOCR / iOS-OCR-Server URL")
    parser.add_argument("--ocr-auth", default="", help="HTTP Basic Auth user:pass")
    parser.add_argument("--dpi", type=int, default=300, help="Render DPI")
    parser.add_argument("--visible", action="store_true", help="Also produce a debug PDF with visible text")
    args = parser.parse_args()

    input_path: Path = args.input_pdf
    if not input_path.exists():
        logger.error("File not found: %s", input_path)
        sys.exit(1)

    pdf_bytes = input_path.read_bytes()
    num_pages = pdf_page_count(pdf_bytes)
    logger.info("Input: %s (%d pages)", input_path.name, num_pages)

    auth = None
    if args.ocr_auth:
        parts = args.ocr_auth.split(":", 1)
        if len(parts) == 2:
            auth = httpx.BasicAuth(username=parts[0], password=parts[1])

    out_dir = input_path.parent
    stem = input_path.stem

    all_page_data: list[OcrPageData] = []
    all_raw: list[dict[str, Any]] = []

    async with httpx.AsyncClient(
        base_url=args.ocr_url,
        auth=auth,
        headers={"Accept": "application/json"},
        timeout=httpx.Timeout(120.0, connect=10.0),
    ) as client:
        for page_num in range(num_pages):
            logger.info("Page %d/%d: rendering at %d DPI...", page_num + 1, num_pages, args.dpi)
            png_bytes = pdf_page_to_png(pdf_bytes, page_num, dpi=args.dpi)

            logger.info("Page %d/%d: sending to OCR...", page_num + 1, num_pages)
            raw = await ocr_image(client, png_bytes, filename=f"page_{page_num}.png")

            # Save raw JSON
            json_path = out_dir / f"{stem}_page{page_num + 1}.json"
            json_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))
            num_boxes = len(raw.get("ocr_boxes", []))
            logger.info("Page %d/%d: saved %s (%d boxes)", page_num + 1, num_pages, json_path.name, num_boxes)

            page_data = OcrPageData(
                text=_reconstruct_text(raw),
                boxes=raw.get("ocr_boxes", []),
                image_width=raw.get("image_width", 0),
                image_height=raw.get("image_height", 0),
            )
            all_page_data.append(page_data)
            all_raw.append(raw)

    # Build searchable PDF (invisible text layer)
    logger.info("Building searchable PDF...")
    searchable_bytes = pdf_embed_text_layer(pdf_bytes, all_page_data)
    searchable_path = out_dir / f"{stem}_searchable.pdf"
    searchable_path.write_bytes(searchable_bytes)
    logger.info("Wrote %s (%d bytes)", searchable_path.name, len(searchable_bytes))

    # Build debug PDF (visible text + bounding boxes)
    if args.visible:
        logger.info("Building debug PDF with visible text...")
        debug_bytes = build_debug_pdf(pdf_bytes, all_page_data, all_raw)
        debug_path = out_dir / f"{stem}_debug.pdf"
        debug_path.write_bytes(debug_bytes)
        logger.info("Wrote %s (%d bytes)", debug_path.name, len(debug_bytes))

    # Print reconstructed text summary
    for i, pd in enumerate(all_page_data):
        print(f"\n{'=' * 60}")
        print(f"PAGE {i + 1} — reconstructed text:")
        print(f"{'=' * 60}")
        print(pd.text[:500] + ("..." if len(pd.text) > 500 else ""))

    logger.info("Done!")


if __name__ == "__main__":
    asyncio.run(main())
