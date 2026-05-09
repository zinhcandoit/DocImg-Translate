"""
PDF Renderer — Reconstructs a translated PDF using a word-level overlay pipeline.

Approach (V8-Isolated):
  1. Per-page extraction from layout.json (no cross-page state)
  2. Tokenize blocks into (text, word) + (eq, latex) tokens
  3. Binary-search font size to fit text perfectly in bbox
  4. Render word-by-word: insert_text for text, insert_image for equations
  5. Multi-angle support (0, 90, 180, 270)
  6. Direct insert_text + insert_image (No HTMLBox)
"""

import json
import io
import re
from pathlib import Path
import fitz
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from typing import Optional

# -------------------------------------------------------------------
# Equation Renderer: LaTeX -> PNG bytes (in-memory)
# -------------------------------------------------------------------
plt.rcParams.update({
    "text.usetex": False,
    "mathtext.fontset": "stix",
    "font.family": "STIXGeneral",
    "mathtext.fallback": "cm"
})

TEXT_TYPE = "notos"
TEXT_TYPE_BOLD = "notosbo"

class EquationRenderer:
    """Renders LaTeX internally using Matplotlib's STIX fonts."""
    _cache: dict[str, dict] = {}

    def _clean_mineru_latex(self, tex: str) -> str:
        tex = tex.strip()
        tex = tex.replace('$', '')
        tex = tex.replace('&', r'\quad ')
        tex = tex.replace(r'\\', r'\quad ')
        tex = re.sub(r'\\begin\s*\{[a-zA-Z*]+\}\s*(\{[^\}]*\})?', '', tex)
        tex = re.sub(r'\\end\s*\{[a-zA-Z*]+\}', '', tex)
        fixes = [
            (r'\\operatorname\*', r'\\operatorname'), 
            (r'\\dotsc|\\dotsb|\\dotsi|\\dotso', r'\\dots'), 
            (r'\\le\b', r'\\leq'),                     
            (r'\\ge\b', r'\\geq'),                     
            (r'\\cal\b', r'\\mathcal'),                
            (r'\\rm\b', r'\\mathrm'),                  
            (r'\\bf\b', r'\\mathbf'),                  
            (r'\\mathbbm\b', r'\\mathbb'),             
            (r'\\stackrel', r'\\overset'),             
            (r'\\textstyle', r''),                     
            (r'\\displaystyle', r''),                  
            (r'\\tag\s*\{[^\}]*\}', r''),
            (r'\\tag\b', r''),
            (r'\\Biggl\b|\\Biggr\b', r'\\Bigg'),
            (r'\\biggl\b|\\biggr\b', r'\\bigg'),
            (r'\\Bigl\b|\\Bigr\b', r'\\Big'),
            (r'\\bigl\b|\\bigr\b', r'\\big'),
        ]
        for pattern, repl in fixes:
            tex = re.sub(pattern, repl, tex)
        for cmd in ['mathcal', 'mathbb', 'mathbf', 'mathrm', 'mathscr', 'mathfrak']:
            tex = re.sub(r'\{\s*\\' + cmd + r'\s+([A-Za-z0-9])\s*\}', r'\\' + cmd + r'{\1}', tex)
            tex = re.sub(r'\\' + cmd + r'\s+([A-Za-z0-9])', r'\\' + cmd + r'{\1}', tex)
            tex = re.sub(r'\\' + cmd + r'\s*\{\s*([A-Za-z0-9])\s*\}', r'\\' + cmd + r'{\1}', tex)
        tex = tex.strip()
        if tex.startswith('{') and tex.endswith('}'):
            open_braces = 0
            is_valid_wrap = True
            for i, char in enumerate(tex):
                if char == '{': open_braces += 1
                elif char == '}': open_braces -= 1
                if open_braces == 0 and i < len(tex) - 1:
                    is_valid_wrap = False
                    break
            if is_valid_wrap:
                tex = tex[1:-1].strip()
        return tex

    def render_and_metrics(self, latex: str, dpi: int = 300) -> dict | None:
        raw_tex = latex.strip()
        if not raw_tex: return None
        if raw_tex in self._cache: return self._cache[raw_tex]
        clean_tex = self._clean_mineru_latex(raw_tex)
        try:
            fig, ax = plt.subplots(figsize=(0.01, 0.01))
            ax.axis('off')
            t = ax.text(0, 0, f"${clean_tex}$", fontsize=40, va='baseline')
            fig.canvas.draw()
            renderer = fig.canvas.get_renderer()
            bbox = t.get_window_extent(renderer)
            y_baseline = ax.transData.transform((0, 0))[1]
            descent_px = max(0, y_baseline - bbox.y0)
            fig.set_size_inches(bbox.width / dpi, bbox.height / dpi)
            buf = io.BytesIO()
            fig.savefig(buf, format='png', dpi=dpi, bbox_inches='tight', pad_inches=0, transparent=True)
            plt.close(fig)
            self._cache[raw_tex] = {
                'png_bytes': buf.getvalue(),
                'aspect_ratio': bbox.width / bbox.height if bbox.height > 0 else 1,
                'descent_ratio': descent_px / bbox.height if bbox.height > 0 else 0
            }
            return self._cache[raw_tex]
        except Exception as e:
            print(f"[MathText Error] Failed on: {clean_tex[:30]}... | Error: {e}")
            plt.close('all')
            return None

# -------------------------------------------------------------------
# Helper Constants & Font Objects
# -------------------------------------------------------------------
FONT = fitz.Font(TEXT_TYPE)
FONT_BOLD = fitz.Font(TEXT_TYPE_BOLD)
SPACE_RATIO = 0.3
global_cross_page_lines = []

# -------------------------------------------------------------------
# PDFRenderer Class
# -------------------------------------------------------------------
class PDFRenderer:
    def __init__(self, images_dir: Optional[str] = None):
        self.images_dir = Path(images_dir) if images_dir else None
        self.eq_renderer = EquationRenderer()

    def simulate_layout(self, tokens, rect, fontsize):
        """Simulate word-wrap layout. Returns True if all tokens fit in rect."""
        line_h = fontsize * 1.3
        space_w = fontsize * SPACE_RATIO
        x = rect.x0
        y = rect.y0 + fontsize
        for kind, content in tokens:
            if kind == "word":
                w = FONT.text_length(content, fontsize=fontsize)
                if x > rect.x0 and x + w > rect.x1:
                    x = rect.x0
                    y += line_h
                x += w + space_w
            elif kind == "eq":
                metrics = self.eq_renderer.render_and_metrics(content)
                if metrics:
                    disp_h = fontsize * 1.2
                    disp_w = disp_h * metrics['aspect_ratio']
                    if x > rect.x0 and x + disp_w > rect.x1:
                        x = rect.x0
                        y += line_h
                    x += disp_w + space_w
                else:
                    w = FONT.text_length(content, fontsize=fontsize)
                    if x > rect.x0 and x + w > rect.x1:
                        x = rect.x0
                        y += line_h
                    x += w + space_w
        return y <= rect.y1 + 1

    def fit_fontsize(self, tokens, rect, lo=1.0, hi=18.0) -> float:
        if not tokens: return 10.0
        for _ in range(20):
            mid = (lo + hi) / 2
            if self.simulate_layout(tokens, rect, mid):
                lo = mid
            else:
                hi = mid
        return lo

    def extract_page_blocks(self, page_data: dict) -> list[dict]:
        global global_cross_page_lines
        result = []
        if global_cross_page_lines:
            tokens = []
            valid_bboxes = []
            for line in global_cross_page_lines:
                for span in line.get('spans', []):
                    stype = span.get('type', '')
                    content = span.get('content', '').strip()
                    if not content: continue
                    if stype == 'text':
                        for w in content.split(): tokens.append(("word", w))
                    elif stype in ('inline_equation', 'interline_equation'):
                        tokens.append(("eq", content))
                if line.get('bbox'): valid_bboxes.append(line['bbox'])
            if tokens and valid_bboxes:
                new_bbox = [min(b[0] for b in valid_bboxes), min(b[1] for b in valid_bboxes),
                            max(b[2] for b in valid_bboxes), max(b[3] for b in valid_bboxes)]
                result.append({'bbox': new_bbox, 'type': 'text', 'tokens': tokens, 'n_lines': len(valid_bboxes)})
            global_cross_page_lines = []

        all_blocks = (
            page_data.get('para_blocks', []) + 
            page_data.get('preproc_blocks', []) + 
            page_data.get('discarded_blocks', [])
        )
        next_carry_over = []
        for block in all_blocks:
            btype = block.get('type', '')
            sub_blocks = block.get('blocks', [block]) if 'blocks' in block else [block]
            for sub in sub_blocks:
                valid_lines = []
                sub_angle = sub.get('angle', 0)
                for line in sub.get('lines', []):
                    if any(span.get('cross_page', False) for span in line.get('spans', [])):
                        next_carry_over.append(line)
                    else:
                        valid_lines.append(line)
                if valid_lines:
                    tokens = []
                    valid_bboxes = []
                    for line in valid_lines:
                        for span in line.get('spans', []):
                            stype = span.get('type', '')
                            content = span.get('content', '').strip()
                            if not content: continue
                            if stype == 'text':
                                for w in content.split(): tokens.append(("word", w))
                            elif stype in ('inline_equation', 'interline_equation'):
                                tokens.append(("eq", content))
                        if line.get('bbox'): valid_bboxes.append(line['bbox'])
                    if tokens and valid_bboxes:
                        new_bbox = [min(b[0] for b in valid_bboxes), min(b[1] for b in valid_bboxes),
                                    max(b[2] for b in valid_bboxes), max(b[3] for b in valid_bboxes)]
                        result.append({'bbox': new_bbox, 'type': btype, 'tokens': tokens, 'n_lines': len(valid_bboxes), 'angle': sub_angle})
        global_cross_page_lines.extend(next_carry_over)
        return result

    def render_block(self, page, block, nllb_service=None):
        raw_angle = block.get('angle', 0)
        angle = int(round(raw_angle / 90) * 90) % 360
        bbox = block['bbox']
        rect = fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3])
        if angle in [90, 270]:
            logical_width, logical_height = rect.height, rect.width
        else:
            logical_width, logical_height = rect.width, rect.height
        logical_rect = fitz.Rect(0, 0, logical_width, logical_height)
        if logical_width < 2 or logical_height < 2: return

        tokens = block.get('tokens', [])
        if not tokens: return

        # Translate if NLLB service provided
        if nllb_service:
            original_text = ""
            for kind, content in tokens:
                if kind == "word": original_text += content + " "
                else: original_text += f"${content}$ "
            
            # Use paragraph translation logic if available, or simple translation
            translated_text = nllb_service._translate_text(original_text.strip())
            
            # Re-tokenize translated text
            new_tokens = []
            # This is a naive re-tokenization; better would be to use nllb's equation protection
            # but for now we split by spaces and try to find equations
            parts = re.split(r'(\$.*?\$)', translated_text)
            for p in parts:
                p = p.strip()
                if not p: continue
                if p.startswith('$') and p.endswith('$'):
                    new_tokens.append(("eq", p[1:-1]))
                else:
                    for w in p.split():
                        new_tokens.append(("word", w))
            tokens = new_tokens

        btype = block['type']
        is_bold = btype == 'title'
        fontname = TEXT_TYPE_BOLD if is_bold else TEXT_TYPE
        font_obj = FONT_BOLD if is_bold else FONT

        fs = self.fit_fontsize(tokens, logical_rect)
        fs = min(fs, logical_height * 0.9)
        if btype == 'page_footnote': fs = min(fs, 8.0)
        if btype == 'image_caption': fs = min(fs, 9.0)
        line_h = fs * 1.3
        ly = fs
        rot_map = {0: 0, 90: 270, 180: 180, 270: 90}
        pdf_rotate = rot_map.get(angle, 0)

        def to_physical(lx, ly):
            if angle == 90: return fitz.Point(rect.x0 + ly, rect.y0 + lx)
            elif angle == 180: return fitz.Point(rect.x1 - lx, rect.y1 - ly)
            elif angle == 270: return fitz.Point(rect.x1 - ly, rect.y1 - lx)
            return fitz.Point(rect.x0 + lx, rect.y0 + ly)

        i = 0
        while i < len(tokens):
            line_tokens = []
            while i < len(tokens):
                kind, content = tokens[i]
                if kind == "word": w = font_obj.text_length(content, fontsize=fs)
                else:
                    metrics = self.eq_renderer.render_and_metrics(content)
                    w = (fs * 1.2 * metrics['aspect_ratio']) if metrics else font_obj.text_length(content, fontsize=fs)
                if not line_tokens:
                    line_tokens.append((kind, content, w))
                    i += 1
                    continue
                current_content_w = sum(t[2] for t in line_tokens)
                if current_content_w + w + len(line_tokens) * (fs * 0.1) > logical_width: break
                line_tokens.append((kind, content, w))
                i += 1

            is_last_line = (i == len(tokens)) or (ly + line_h > logical_height + fs * 0.5)
            total_content_w = sum(t[2] for t in line_tokens)
            num_spaces = len(line_tokens) - 1
            space_w_at_fs = fs * 0.15
            needed_w = total_content_w + (num_spaces * space_w_at_fs)
            line_fs, squeeze_factor = fs, 1.0
            if needed_w > logical_width:
                combined_scale = logical_width / needed_w
                if combined_scale >= 0.75:
                    line_fs, dynamic_space_w = fs * combined_scale, space_w_at_fs * combined_scale
                else:
                    line_fs, squeeze_factor = fs * 0.75, combined_scale / 0.75
                    dynamic_space_w = space_w_at_fs * 0.75 * squeeze_factor
            elif num_spaces > 0 and not is_last_line:
                dynamic_space_w = min((logical_width - total_content_w) / num_spaces, fs * 0.6)
            else:
                dynamic_space_w = fs * 0.25

            lx = 0
            for kind, content, w_orig in line_tokens:
                w_scaled = w_orig * (line_fs / fs)
                p = to_physical(lx, ly)
                if kind == "word":
                    morph = (p, fitz.Matrix(squeeze_factor, 1.0)) if squeeze_factor < 1.0 else None
                    page.insert_text(p, content, fontsize=line_fs, fontname=fontname, morph=morph, rotate=pdf_rotate)
                elif kind == "eq":
                    metrics = self.eq_renderer.render_and_metrics(content)
                    if metrics:
                        disp_h = line_fs * 1.2
                        descent_offset = disp_h * metrics['descent_ratio']
                        p_bl = to_physical(lx, ly + descent_offset)
                        p_tr = to_physical(lx + (w_scaled * squeeze_factor), ly - disp_h + descent_offset)
                        eq_rect = fitz.Rect(p_bl, p_tr)
                        eq_rect.normalize()
                        if eq_rect.is_valid and not eq_rect.is_empty:
                            page.insert_image(eq_rect, stream=metrics['png_bytes'], rotate=pdf_rotate)
                    else:
                        morph = (p, fitz.Matrix(squeeze_factor, 1.0)) if squeeze_factor < 1.0 else None
                        page.insert_text(p, content, fontsize=line_fs, fontname=fontname, morph=morph, rotate=pdf_rotate)
                lx += (w_scaled * squeeze_factor) + dynamic_space_w
            ly += line_h
            if ly > logical_height + line_h: break

    def render(self, layout_data: dict, origin_pdf_path: str, output_path: str, nllb_service=None) -> str:
        global global_cross_page_lines
        global_cross_page_lines = []
        src_doc = fitz.open(origin_pdf_path)
        final_doc = fitz.open()
        for page_data in layout_data.get('pdf_info', []):
            page_idx = page_data.get('page_idx')
            if page_idx is None or page_idx >= len(src_doc): continue
            temp_page_doc = fitz.open()
            temp_page_doc.insert_pdf(src_doc, from_page=page_idx, to_page=page_idx)
            page = temp_page_doc[0]
            blocks = self.extract_page_blocks(page_data)
            blocks = sorted(blocks, key=lambda b: (int(b['bbox'][1] // 15), b['bbox'][0]))
            for b in blocks:
                page.add_redact_annot(fitz.Rect(b['bbox']), fill=(1, 1, 1))
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            for b in blocks:
                self.render_block(page, b, nllb_service)
            final_doc.insert_pdf(temp_page_doc)
            temp_page_doc.close()
        final_doc.save(output_path, garbage=4, deflate=True)
        final_doc.close()
        src_doc.close()
        return output_path
