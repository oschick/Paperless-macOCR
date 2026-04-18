"""Tests for OCR text reconstruction from bounding boxes."""

from paperless_macocr.ocr import _avg_char_width, _join_line, _reconstruct_text


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


class TestJoinLine:
    def test_adjacent_boxes(self):
        """Boxes close together get single space separation."""
        boxes = [
            {"text": "Hello", "x": 0, "w": 50, "y": 0, "h": 10},
            {"text": "World", "x": 60, "w": 50, "y": 0, "h": 10},
        ]
        result = _join_line(boxes, char_w=10.0)
        assert result == "Hello World"

    def test_large_gap_preserved(self):
        """Boxes far apart get proportional spacing (letter layout)."""
        boxes = [
            {"text": "John Doe", "x": 0, "w": 80, "y": 0, "h": 10},
            {"text": "April 18, 2026", "x": 500, "w": 140, "y": 0, "h": 10},
        ]
        result = _join_line(boxes, char_w=10.0)
        # "John Doe" at col 0, "April 18, 2026" at col 50
        assert "John Doe" in result
        assert "April 18, 2026" in result
        # There should be significant spacing in between
        gap = result.index("April") - len("John Doe")
        assert gap > 10

    def test_zero_char_width_fallback(self):
        boxes = [
            {"text": "A", "x": 0, "w": 10, "y": 0, "h": 10},
            {"text": "B", "x": 500, "w": 10, "y": 0, "h": 10},
        ]
        result = _join_line(boxes, char_w=0)
        assert result == "A B"


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
        result = _reconstruct_text(data)
        assert "Hello" in result
        assert "World" in result

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
        # There should be a blank line between paragraphs
        assert "\n\n" in result

    def test_letter_layout(self):
        """Address on the left, date on the right -- same line."""
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
        lines = result.strip().split("\n")
        # Both items should be on the same line with spacing
        line1 = lines[0]
        assert "John Doe" in line1
        assert "April 18, 2026" in line1
        gap1 = line1.index("April") - line1.index("John Doe") - len("John Doe")
        assert gap1 > 5, "Expected significant spacing between left and right blocks"

        line2 = lines[1]
        assert "123 Main St" in line2
        assert "Ref: 42" in line2

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
