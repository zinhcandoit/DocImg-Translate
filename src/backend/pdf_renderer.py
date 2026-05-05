"""
PDF Renderer — Reconstructs a translated PDF from middle.json + images.

Design principles:
- Equations/interline_equations: rendered as IMAGES (from images/ folder)
  instead of LaTeX text, as specified by the user.
- Tables: EXCEPTION — translated text is rendered (not image).
- Text/title blocks: Vietnamese translated text placed at original bbox.
- Uses PyMuPDF (fitz) for PDF creation.

Coordinate system:
  middle.json bboxes use PDF points (1/72 inch) with origin at top-left.
  PyMuPDF uses the same coordinate system, so bboxes map directly.
"""

import re
import fitz  # PyMuPDF
from pathlib import Path
from html.parser import HTMLParser


class TableHTMLParser(HTMLParser):
    """Extract rows/cells from simple HTML table for PDF rendering."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self._current_row = []
        self._current_cell = ""
        self._in_cell = False
        self._colspan = 1

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._current_row = []
        elif tag in ("td", "th"):
            self._in_cell = True
            self._current_cell = ""
            attrs_dict = dict(attrs)
            self._colspan = int(attrs_dict.get("colspan", 1))

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._in_cell = False
            # Clean up equation markers
            cell_text = self._current_cell.strip()
            cell_text = re.sub(r"<eq>(.*?)</eq>", r"\1", cell_text)
            self._current_row.append({
                "text": cell_text,
                "colspan": self._colspan,
            })
        elif tag == "tr":
            if self._current_row:
                self.rows.append(self._current_row)

    def handle_data(self, data):
        if self._in_cell:
            self._current_cell += data


class PDFRenderer:
    """Render translated PDF from middle.json structure + equation images."""

    # Minimum font size (points)
    MIN_FONT_SIZE = 6
    DEFAULT_FONT_SIZE = 10
    TITLE_FONT_SIZE_SCALE = 1.2

    def __init__(self, images_dir: str = None, font_path: str = None):
        self.images_dir = Path(images_dir) if images_dir else None
        # For Vietnamese support, try to find a system font with diacritics
        self.font_path = font_path
        if not self.font_path:
            # Try common system fonts with Vietnamese support
            candidates = [
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/times.ttf",
                "C:/Windows/Fonts/calibri.ttf",
                "C:/Windows/Fonts/segoeui.ttf",
            ]
            for c in candidates:
                if Path(c).exists():
                    self.font_path = c
                    break

    def render(self, translated_middle: dict, output_path: str) -> str:
        """
        Build a new PDF from translated middle.json.

        For each page:
          - text/title blocks → insert translated text at bbox
          - interline_equation → insert equation image at bbox
          - table → render translated table cells
          - list → insert list items at bbox
        """
        doc = fitz.open()

        for page_data in translated_middle.get("pdf_info", []):
            page_size = page_data.get("page_size", [612, 792])  # default Letter
            page = doc.new_page(width=page_size[0], height=page_size[1])

            # Use para_blocks (they're in reading order)
            blocks = page_data.get("para_blocks", page_data.get("preproc_blocks", []))

            for block in blocks:
                if block.get("_merged"):
                    continue  # Skip blocks merged into previous

                btype = block.get("type", "")
                bbox = block.get("bbox")
                if not bbox:
                    continue

                if btype in ("text", "title"):
                    self._render_text_block(page, block, is_title=(btype == "title"))
                elif btype == "interline_equation":
                    self._render_equation_block(page, block)
                elif btype == "table":
                    self._render_table_block(page, block)
                elif btype == "list":
                    self._render_list_block(page, block)

        # Save with garbage collection and compression to ensure a valid, non-corrupted PDF
        doc.save(output_path, garbage=3, deflate=True)
        doc.close()
        return output_path

    def _render_text_block(self, page, block, is_title=False):
        """Insert translated text into the block's bounding box."""
        bbox = block.get("bbox")
        if not bbox:
            return

        text = self._get_block_text(block)
        if not text.strip():
            return

        rect = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3])

        # Determine font size from bbox height or line_avg_height
        line_height = block.get("line_avg_height")
        if line_height:
            font_size = max(self.MIN_FONT_SIZE, line_height * 0.75)
        else:
            box_height = bbox[3] - bbox[1]
            num_lines = max(1, text.count("\n") + 1)
            font_size = max(self.MIN_FONT_SIZE, min((box_height / num_lines) * 0.75, 14))

        if is_title:
            font_size = min(font_size * self.TITLE_FONT_SIZE_SCALE, 20)

        # Expand the rect height downwards by 50% to prevent PyMuPDF from silently dropping text 
        # due to custom font line-height metrics being slightly taller than the strict layout.json bbox
        rect.y1 += font_size * 1.5

        self._insert_text(page, rect, text, font_size, is_title)

    def _render_equation_block(self, page, block):
        """Insert equation image at the block's bounding box."""
        bbox = block.get("bbox")
        if not bbox:
            return

        # Find image_path from spans
        image_path = None
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if "image_path" in span:
                    image_path = span["image_path"]
                    break
            if image_path:
                break

        if image_path and self.images_dir:
            img_file = self.images_dir / image_path
            if img_file.exists():
                rect = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3])
                try:
                    page.insert_image(rect, filename=str(img_file))
                    return
                except Exception as e:
                    print(f"[PDFRenderer] Image insert failed: {e}")

        # Fallback: render LaTeX content as text
        text = self._get_block_text(block)
        if text.strip():
            rect = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3])
            self._insert_text(page, rect, text, self.DEFAULT_FONT_SIZE)

    def _render_table_block(self, page, block):
        """Render translated table at the block's bbox."""
        bbox = block.get("bbox")
        if not bbox:
            return

        # Find HTML content
        html = None
        for sub in block.get("blocks", []):
            for line in sub.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("type") == "table" and "html" in span:
                        html = span["html"]
                        break

        if not html:
            return

        # Parse HTML into rows/cells
        parser = TableHTMLParser()
        try:
            parser.feed(html)
        except Exception:
            return

        rows = parser.rows
        if not rows:
            return

        table_rect = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3])
        num_rows = len(rows)
        max_cols = max(sum(c["colspan"] for c in row) for row in rows) if rows else 1

        row_height = (table_rect.height) / max(num_rows, 1)
        col_width = (table_rect.width) / max(max_cols, 1)

        font_size = max(self.MIN_FONT_SIZE, min(row_height * 0.6, 9))

        y = table_rect.y0
        for row in rows:
            x = table_rect.x0
            for cell in row:
                cw = col_width * cell["colspan"]
                cell_rect = fitz.Rect(x, y, x + cw, y + row_height)

                # Draw cell border
                page.draw_rect(cell_rect, color=(0.7, 0.7, 0.7), width=0.5)

                # Insert text with small padding
                text_rect = fitz.Rect(x + 2, y + 1, x + cw - 2, y + row_height - 1)
                if cell["text"].strip():
                    self._insert_text(page, text_rect, cell["text"], font_size)
                x += cw
            y += row_height

    def _render_list_block(self, page, block):
        """Render list items at their bounding boxes."""
        sub_type = block.get("sub_type", "bullet")
        for idx, sub in enumerate(block.get("blocks", [])):
            bbox = sub.get("bbox", block.get("bbox"))
            if not bbox:
                continue
            text = self._get_block_text(sub)
            if not text.strip():
                continue
            prefix = f"{idx + 1}. " if sub_type == "numbered" else "• "
            rect = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3])
            box_h = bbox[3] - bbox[1]
            font_size = max(self.MIN_FONT_SIZE, min(box_h * 0.7, 10))
            self._insert_text(page, rect, prefix + text, font_size)

    # ── Helpers ─────────────────────────────────────────────────

    def _get_block_text(self, block) -> str:
        """Extract all text content from a block's spans."""
        parts = []
        for line in block.get("lines", []):
            line_parts = []
            for span in line.get("spans", []):
                content = span.get("content", "")
                if content:
                    line_parts.append(content)
            if line_parts:
                parts.append(" ".join(line_parts))
        return " ".join(parts)

    def _insert_text(self, page, rect, text, font_size, bold=False):
        """Insert UTF-8 text into rect with font that supports Vietnamese.
           Automatically reduces font size if the text doesn't fit.
        """
        if not text.strip():
            return

        # Ensure rect is valid and has some area
        if rect.width <= 0 or rect.height <= 0:
            return

        current_fs = font_size
        success = False

        # Try to fit text by reducing font size if necessary
        while current_fs >= self.MIN_FONT_SIZE:
            kwargs = {
                "rect": rect,
                "buffer": text,
                "fontsize": current_fs,
                "fontname": "helv",
                "align": fitz.TEXT_ALIGN_LEFT,
            }

            if self.font_path and Path(self.font_path).exists():
                kwargs["fontfile"] = self.font_path
                kwargs["fontname"] = "custom"

            try:
                res = page.insert_textbox(**kwargs)
                if res >= 0:
                    success = True
                    break
            except Exception:
                pass
            
            current_fs -= 0.5 # Reduce in small steps

        if not success:
            # Final fallback: force insert at smallest size even if it overflows
            try:
                page.insert_textbox(
                    rect=rect, buffer=text, fontsize=self.MIN_FONT_SIZE,
                    fontname="helv", align=fitz.TEXT_ALIGN_LEFT,
                )
            except Exception as e:
                print(f"[PDFRenderer] Critical text insert failure: {e}")
