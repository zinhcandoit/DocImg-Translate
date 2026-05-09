"""
NLLB Translation Service — Paragraph-level translation from middle.json.

Key mechanisms:
- Uses `para_blocks` from middle.json (lines pre-grouped into paragraphs)
- `merge_prev`: when True on a text block, the block is a continuation of the
  previous paragraph — we concatenate them before translation for better context
- Inline equations are protected with placeholders [EQ_n] during translation
- Translates at paragraph level (not line-by-line) for coherent output
- Tables: extracts translatable text from HTML, translates, reconstructs
"""

import re
import copy
import torch
import threading
from pathlib import Path
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
from html.parser import HTMLParser

# ── Protection patterns for markdown translation ───────────────
PROTECTED_PATTERNS = [
    (r"```[\s\S]*?```", "CODE"),
    (r"`[^`\n]+`", "INLINECODE"),
    (r"\$\$[\s\S]*?\$\$", "MATH"),
    (r"\\begin\{[^}]+\}[\s\S]*?\\end\{[^}]+\}", "MATH"),
    (r"\\\[[\s\S]*?\\\]", "MATH"),
    (r"\\\([\s\S]*?\\\)", "MATH"),
    (r"(?<![\w\$])\$(?!\$)(?:[^\$\n\\]|\\.)+\$(?!(?:\w|\$))", "MATH"),
    # Markdown syntax protection
    (r"\[([^\]]+)\]\(([^)]+)\)", "LINK"),
    (r"\!\[([^\]]*)\]\(([^)]+)\)", "IMAGE"),
    (r"\*\*[^*]+\*\*", "BOLD"),
    (r"\*[^*]+\*", "ITALIC"),
]


class NLLBService:
    def __init__(self, device=None, lazy_load: bool = False):
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.loaded = False
        self.src_lang = "eng_Latn"
        self.tgt_lang = "vie_Latn"
        self.model_name = "facebook/nllb-200-1.3B"
        self._load_event = threading.Event()

        if not lazy_load:
            self.load_model()

    def load_model(self):
        if self.loaded:
            return
        try:
            print(f"[NLLB] Loading pretrained model {self.model_name} on {self.device}...")
            
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
            
            self.model = AutoModelForSeq2SeqLM.from_pretrained(
                self.model_name,
                torch_dtype=torch.float16 if self.device == "cuda" else torch.float32,
                low_cpu_mem_usage=True,
            )

            self.model.to(self.device).eval()
            self.loaded = True
            print("[NLLB] Model loaded successfully")
        except Exception:
            import traceback
            print("[NLLB] Load failed:")
            traceback.print_exc()
            self._load_event.set()

    def wait_for_load(self):
        self._load_event.wait()

    # ── Core translate ──────────────────────────────────────────

    def _translate_text(self, text: str) -> str:
        """Translate a single string. Returns mock if model not loaded."""
        if not text.strip():
            return text
        if not self.loaded:
            return f"[VI] {text}"
        
        # Ensure tokenizer has language codes set
        self.tokenizer.src_lang = self.src_lang
        self.tokenizer.tgt_lang = self.tgt_lang
        
        inputs = self.tokenizer(text, return_tensors="pt", truncation=True, max_length=1024).to(self.device)
        with torch.no_grad():
            # Use convert_tokens_to_ids to get the target language token ID
            tgt_lang_id = self.tokenizer.convert_tokens_to_ids(self.tgt_lang)
            out = self.model.generate(
                **inputs,
                forced_bos_token_id=tgt_lang_id,
                max_length=1024,
            )
        return self.tokenizer.batch_decode(out, skip_special_tokens=True)[0]

    # ── Paragraph extraction from middle.json ───────────────────

    def translate_middle_json(self, middle_data: dict) -> dict:
        """
        Translate all translatable content in middle.json in-place.

        Uses para_blocks for paragraph-level grouping.
        Respects merge_prev to concatenate continuation blocks.
        Protects inline_equation spans with placeholders.
        Returns a deep-copied middle_data with translated content.
        """
        translated = copy.deepcopy(middle_data)

        for page in translated.get("pdf_info", []):
            blocks = page.get("para_blocks", page.get("preproc_blocks", []))
            self._translate_block_list(blocks)
        return translated

    def _translate_block_list(self, blocks: list):
        """Walk a list of blocks, merging merge_prev blocks and translating."""
        i = 0
        while i < len(blocks):
            block = blocks[i]
            btype = block.get("type", "")

            if btype in ("text", "title"):
                # Collect merge_prev chain
                chain = [block]
                j = i + 1
                while j < len(blocks) and blocks[j].get("merge_prev") is True:
                    chain.append(blocks[j])
                    j += 1

                # Extract paragraph text with equation placeholders
                paragraph, eq_map = self._extract_paragraph(chain)
                if paragraph.strip():
                    translated = self._translate_text(paragraph)
                    translated = self._restore_equations(translated, eq_map)
                    # Write back into first block's spans (simplified)
                    self._write_back_translated(chain, translated)
                i = j

            elif btype == "table":
                self._translate_table_block(block)
                i += 1

            elif btype == "list":
                # Lists have nested sub-blocks
                for sub in block.get("blocks", []):
                    if sub.get("lines"):
                        para, eq_map = self._extract_paragraph([sub])
                        if para.strip():
                            tr = self._translate_text(para)
                            tr = self._restore_equations(tr, eq_map)
                            self._write_back_translated([sub], tr)
                i += 1
            else:
                # interline_equation — skip (use image in PDF)
                i += 1

    def _extract_paragraph(self, block_chain: list) -> tuple:
        """
        Extract all text from a chain of blocks into one paragraph string.
        Inline equations are replaced with [EQ_n] placeholders.
        Returns (paragraph_text, {placeholder: original_latex}).
        """
        parts = []
        eq_map = {}
        eq_idx = 0

        for block in block_chain:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    stype = span.get("type", "")
                    content = span.get("content", "")
                    if stype == "text":
                        parts.append(content)
                    elif stype == "inline_equation":
                        placeholder = f"[EQ_{eq_idx}]"
                        eq_map[placeholder] = content
                        parts.append(placeholder)
                        eq_idx += 1
                    # interline_equation spans in text blocks are rare; protect them too
                    elif stype == "interline_equation":
                        placeholder = f"[EQ_{eq_idx}]"
                        eq_map[placeholder] = content
                        parts.append(placeholder)
                        eq_idx += 1
                # Add space between lines
                parts.append(" ")

        return " ".join(parts).strip(), eq_map

    def _restore_equations(self, text: str, eq_map: dict) -> str:
        for placeholder, original in sorted(eq_map.items(), key=lambda x: len(x[0]), reverse=True):
            text = text.replace(placeholder, original)
        return text

    def _write_back_translated(self, chain: list, translated_text: str):
        """Write translated text back into the first block, clear others."""
        first = chain[0]
        # Replace all lines/spans with single translated span
        first["lines"] = [{
            "bbox": first.get("bbox", [0, 0, 0, 0]),
            "spans": [{
                "bbox": first.get("bbox", [0, 0, 0, 0]),
                "type": "text",
                "content": translated_text,
                "score": 1.0,
                "translated": True,
            }]
        }]
        # Clear continuation blocks
        for b in chain[1:]:
            b["lines"] = []
            b["_merged"] = True

    # ── Table translation ───────────────────────────────────────

    def _translate_table_block(self, block: dict):
        """Extract text from table HTML, translate, reconstruct."""
        for sub in block.get("blocks", []):
            for line in sub.get("lines", []):
                for span in line.get("spans", []):
                    if span.get("type") == "table" and "html" in span:
                        span["html"] = self._translate_table_html(span["html"])

    def _translate_table_html(self, html: str) -> str:
        """Translate text content within <td>/<th> cells, preserving <eq> tags."""
        def replace_cell(match):
            tag = match.group(1)      # td or th with attributes
            content = match.group(2)  # cell content
            close = match.group(3)    # /td or /th

            # Protect <eq>...</eq> tags
            eq_map = {}
            eq_idx = 0
            def protect_eq(m):
                nonlocal eq_idx
                key = f"[TEQ_{eq_idx}]"
                eq_map[key] = m.group(0)
                eq_idx += 1
                return key
            protected = re.sub(r"<eq>.*?</eq>", protect_eq, content)

            # Only translate if there's actual text
            text_only = re.sub(r"<[^>]+>", "", protected).strip()
            if text_only:
                translated = self._translate_text(protected)
                # Restore equations
                for k, v in eq_map.items():
                    translated = translated.replace(k, v)
                return f"<{tag}>{translated}</{close}>"
            return match.group(0)

        return re.sub(
            r"<(t[dh][^>]*)>(.*?)</(t[dh])>",
            replace_cell,
            html,
            flags=re.DOTALL,
        )

    # ── Markdown-level translation (for .md output) ─────────────

    def translate_markdown(self, md_content: str) -> str:
        """Translate markdown content line-by-line to perfectly preserve structure."""
        translated_lines = []
        for line in md_content.split("\n"):
            if not line.strip():
                translated_lines.append("")
                continue
            # Skip lines that are pure math/code fences
            if line.strip().startswith("\\[") or line.strip().startswith("$$") or line.strip().startswith("```"):
                translated_lines.append(line)
                continue
                
            protected_text, mapping = self._protect(line)
            translated_text = self._translate_text(protected_text)
            final_text = self._restore(translated_text, mapping)
            translated_lines.append(final_text)
            
        return "\n".join(translated_lines)

    def _protect(self, text: str) -> tuple:
        from collections import defaultdict
        placeholders = {}
        counts = defaultdict(int)

        all_matches = []
        for pattern, tag in PROTECTED_PATTERNS:
            for m in re.finditer(pattern, text):
                all_matches.append((m.start(), m.end(), m.group(), tag))

        all_matches.sort(key=lambda x: (x[0], -x[1]))
        filtered = []
        last_end = -1
        for s, e, c, t in all_matches:
            if s >= last_end:
                filtered.append((s, e, c, t))
                last_end = e

        result = text
        for s, e, c, t in sorted(filtered, key=lambda x: x[0], reverse=True):
            idx = counts[t]
            ph = f"[{t}_{idx}]"
            counts[t] += 1
            placeholders[ph] = c
            result = result[:s] + ph + result[e:]

        return result, placeholders

    def _restore(self, text: str, placeholders: dict) -> str:
        for ph, orig in sorted(placeholders.items(), key=lambda x: len(x[0]), reverse=True):
            text = text.replace(ph, orig)
        return text
