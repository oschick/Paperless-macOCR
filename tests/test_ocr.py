"""Tests for OCR text reconstruction from bounding boxes."""

from paperless_macocr.ocr import (
    _avg_char_width,
    _cluster_text,
    _format_table,
    _reconstruct_text,
    _split_into_clusters,
)


class TestAvgCharWidth:
    def test_basic(self):
        boxes = [
            {"text": "Hello", "w": 50, "x": 0, "y": 0, "h": 10},
            {"text": "World", "w": 50, "x": 60, "y": 0, "h": 10},
        ]
        assert _avg_char_width(boxes) == 10.0  # 100px / 10 chars

    def test_empty(self):
        assert _avg_char_width([]) == 0.0

    def test_skips_empty_text(self):
        boxes = [
            {"text": "Hi", "w": 20, "x": 0, "y": 0, "h": 10},
            {"text": "  ", "w": 10, "x": 30, "y": 0, "h": 10},
        ]
        assert _avg_char_width(boxes) == 10.0


class TestClusterText:
    def test_basic(self):
        boxes = [
            {"text": "Hello", "x": 0, "y": 0, "w": 50, "h": 10},
            {"text": "World", "x": 60, "y": 0, "w": 50, "h": 10},
        ]
        assert _cluster_text(boxes) == "Hello World"

    def test_skips_empty(self):
        boxes = [
            {"text": "Hi", "x": 0, "y": 0, "w": 20, "h": 10},
            {"text": "  ", "x": 30, "y": 0, "w": 10, "h": 10},
        ]
        assert _cluster_text(boxes) == "Hi"


class TestSplitIntoClusters:
    def test_no_split_when_close(self):
        boxes = [
            {"text": "A", "x": 0, "w": 10, "y": 0, "h": 10},
            {"text": "B", "x": 15, "w": 10, "y": 0, "h": 10},
        ]
        clusters = _split_into_clusters(boxes, char_w=10.0)
        assert len(clusters) == 1

    def test_splits_on_large_gap(self):
        boxes = [
            {"text": "Left", "x": 0, "w": 40, "y": 0, "h": 10},
            {"text": "Right", "x": 200, "w": 50, "y": 0, "h": 10},
        ]
        # gap = 160px, threshold = 4 * 10 = 40px -> split
        clusters = _split_into_clusters(boxes, char_w=10.0)
        assert len(clusters) == 2
        assert clusters[0][0]["text"] == "Left"
        assert clusters[1][0]["text"] == "Right"

    def test_empty_input(self):
        assert _split_into_clusters([], char_w=10.0) == []

    def test_zero_char_width(self):
        boxes = [
            {"text": "A", "x": 0, "w": 10, "y": 0, "h": 10},
            {"text": "B", "x": 500, "w": 10, "y": 0, "h": 10},
        ]
        clusters = _split_into_clusters(boxes, char_w=0)
        assert len(clusters) == 1  # no splitting possible


class TestFormatTable:
    def test_basic(self):
        rows = [["Name", "Qty", "Price"], ["Widget", "10", "$5.00"]]
        result = _format_table(rows)
        assert len(result) == 2
        # Columns should be aligned
        assert result[0].index("Qty") == result[1].index("10")
        assert result[0].index("Price") == result[1].index("$5.00")

    def test_uneven_rows(self):
        rows = [["A", "B", "C"], ["X", "Y"]]
        result = _format_table(rows)
        assert len(result) == 2
        # Short row padded with empty string
        assert "C" in result[0]

    def test_empty(self):
        assert _format_table([]) == []


class TestReconstructText:
    def test_fallback_no_boxes(self):
        data = {"ocr_result": "plain text", "ocr_boxes": []}
        assert _reconstruct_text(data) == "plain text"

    def test_fallback_missing_boxes(self):
        data = {"ocr_result": "fallback"}
        assert _reconstruct_text(data) == "fallback"

    def test_single_line(self):
        data = {
            "ocr_boxes": [
                {"text": "Hello", "x": 0, "y": 0, "w": 50, "h": 12},
                {"text": "World", "x": 60, "y": 0, "w": 50, "h": 12},
            ],
        }
        assert _reconstruct_text(data) == "Hello World"

    def test_two_lines(self):
        data = {
            "ocr_boxes": [
                {"text": "Line1", "x": 0, "y": 0, "w": 50, "h": 12},
                {"text": "Line2", "x": 0, "y": 20, "w": 50, "h": 12},
            ],
        }
        lines = _reconstruct_text(data).split("\n")
        assert any("Line1" in part for part in lines)
        assert any("Line2" in part for part in lines)

    def test_left_margin_stripped(self):
        """Large left margin should not produce leading spaces."""
        data = {
            "ocr_boxes": [
                {"text": "Hello", "x": 200, "y": 0, "w": 50, "h": 12},
                {"text": "World", "x": 200, "y": 20, "w": 50, "h": 12},
            ],
        }
        result = _reconstruct_text(data)
        for line in result.split("\n"):
            if line:  # skip blank paragraph breaks
                assert not line.startswith(" "), f"Unexpected leading spaces: {line!r}"

    def test_paragraph_break(self):
        """Large vertical gap produces a blank line (paragraph break)."""
        data = {
            "ocr_boxes": [
                {"text": "Para1", "x": 0, "y": 0, "w": 50, "h": 12},
                {"text": "Para1b", "x": 0, "y": 14, "w": 60, "h": 12},
                {"text": "Para2", "x": 0, "y": 80, "w": 50, "h": 12},
            ],
        }
        result = _reconstruct_text(data)
        assert "\n\n" in result

    def test_letter_layout_columns_stacked(self):
        """Address left and date right are emitted as stacked blocks."""
        data = {
            "ocr_boxes": [
                # Left: sender address
                {"text": "John Doe", "x": 10, "y": 10, "w": 80, "h": 14},
                {"text": "123 Main St", "x": 10, "y": 28, "w": 110, "h": 14},
                # Right: date (same y as first line)
                {"text": "April 18, 2026", "x": 500, "y": 12, "w": 140, "h": 14},
                # Right: ref number (same y as second line)
                {"text": "Ref: 42", "x": 500, "y": 30, "w": 70, "h": 14},
            ],
        }
        result = _reconstruct_text(data)
        lines = [ln for ln in result.split("\n") if ln.strip()]
        # Left block should come first (top), right block after
        texts = [ln.strip() for ln in lines]
        assert "John Doe" in texts
        assert "123 Main St" in texts
        assert "April 18, 2026" in texts
        assert "Ref: 42" in texts
        # Left column lines appear before right column lines
        john_idx = next(i for i, t in enumerate(texts) if "John Doe" in t)
        main_idx = next(i for i, t in enumerate(texts) if "123 Main St" in t)
        april_idx = next(i for i, t in enumerate(texts) if "April 18, 2026" in t)
        ref_idx = next(i for i, t in enumerate(texts) if "Ref: 42" in t)
        assert john_idx < april_idx
        assert main_idx < april_idx
        assert april_idx < ref_idx or ref_idx > main_idx

    def test_table_aligned_columns(self):
        """Table cells should be padded to consistent column widths."""
        # char_w ~ 10, gaps ~90-130px = 9-13 char widths -> table (< 15)
        data = {
            "ocr_boxes": [
                {"text": "Name", "x": 0, "y": 0, "w": 40, "h": 12},
                {"text": "Qty", "x": 150, "y": 0, "w": 30, "h": 12},
                {"text": "Price", "x": 300, "y": 0, "w": 50, "h": 12},
                {"text": "Widget", "x": 0, "y": 20, "w": 60, "h": 12},
                {"text": "10", "x": 150, "y": 20, "w": 20, "h": 12},
                {"text": "$5.00", "x": 300, "y": 20, "w": 50, "h": 12},
            ],
        }
        result = _reconstruct_text(data)
        lines = result.strip().split("\n")
        assert len(lines) == 2
        # "Qty" and "10" should start at the same column
        assert lines[0].index("Qty") == lines[1].index("10")
        assert lines[0].index("Price") == lines[1].index("$5.00")

    def test_only_whitespace_boxes_ignored(self):
        data = {
            "ocr_boxes": [
                {"text": "   ", "x": 0, "y": 0, "w": 30, "h": 12},
                {"text": "", "x": 50, "y": 0, "w": 30, "h": 12},
            ],
        }
        assert _reconstruct_text(data) == ""

    def test_reading_order_preserved(self):
        """Boxes given out of order are sorted into reading order."""
        data = {
            "ocr_boxes": [
                {"text": "second", "x": 100, "y": 0, "w": 60, "h": 12},
                {"text": "first", "x": 0, "y": 0, "w": 50, "h": 12},
                {"text": "third", "x": 0, "y": 20, "w": 50, "h": 12},
            ],
        }
        result = _reconstruct_text(data)
        assert result.index("first") < result.index("second")
        assert result.index("second") < result.index("third")
