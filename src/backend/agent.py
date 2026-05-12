"""
AI Agent — Q4 score verification, keyword extraction, WikiSearch URLs.

Roles:
1. Verify extracted elements from Q4 (bottom 25th percentile) of "score"
   in middle.json spans. Flag low-confidence OCR/equation extractions.
2. Extract keywords from the .md file (from Abstract Keywords line or
   full content scan) and present WikiSearch URLs in target language.
3. Translate skipped translatable elements (e.g. table cell text that
   the main pipeline might miss).
"""

import os
import re
import json
import uuid
import numpy as np
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from sqlalchemy.orm import Session

from .database import EvalRun, EvalCase, EvalKeyword

load_dotenv()

# Wikipedia URL template per language
WIKI_TEMPLATES = {
    "vie_Latn": "https://vi.wikipedia.org/wiki/{}",
    "eng_Latn": "https://en.wikipedia.org/wiki/{}",
}


class AIAgent:
    def __init__(self, db_session: Session, target_lang: str = "vie_Latn"):
        self.db = db_session
        self.target_lang = target_lang

        api_key = os.environ.get("GEMMA_4_API_KEY", os.environ.get("GOOGLE_API_KEY"))
        if api_key:
            os.environ["GOOGLE_API_KEY"] = api_key

        try:
            self.llm = ChatGoogleGenerativeAI(
                model="gemma-4-31b-it",
                temperature=0.0,
            )
        except Exception as e:
            print(f"[Agent] LLM init failed: {e}")
            self.llm = None

        self.verify_prompt = PromptTemplate(
            input_variables=["low_score_elements"],
            template="""You are an AI agent verifying OCR/extraction quality.
The following elements were extracted with low confidence scores (bottom 25%).
For each, determine if the content looks correct or needs manual review.

Elements:
{low_score_elements}

Respond as JSON array. Each item: {{"index": <int>, "content": "...", "verdict": "OK"|"REVIEW", "suggestion": "..."}}
Only output the JSON array, no markdown fences."""
        )

    # ── Q4 Score Verification ───────────────────────────────────

    def collect_scores(self, middle_data: dict) -> list:
        """Collect all spans with scores from middle.json."""
        spans_with_scores = []
        for page in middle_data.get("pdf_info", []):
            page_idx = page.get("page_idx", 0)
            for block in page.get("para_blocks", page.get("preproc_blocks", [])):
                self._collect_from_block(block, page_idx, spans_with_scores)
        return spans_with_scores

    def _collect_from_block(self, block, page_idx, results):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if "score" in span:
                    results.append({
                        "page": page_idx,
                        "type": span.get("type"),
                        "content": span.get("content", "")[:100],
                        "score": span["score"],
                        "bbox": span.get("bbox"),
                    })
        for sub in block.get("blocks", []):
            self._collect_from_block(sub, page_idx, results)

    def get_q4_elements(self, middle_data: dict) -> list:
        """Return spans in the bottom 25th percentile (Q4) of scores."""
        all_spans = self.collect_scores(middle_data)
        if not all_spans:
            return []
        scores = [s["score"] for s in all_spans]
        q1_threshold = float(np.percentile(scores, 25))
        return [s for s in all_spans if s["score"] <= q1_threshold]

    def verify_q4_elements(self, middle_data: dict) -> dict:
        """Use LLM to verify low-score elements."""
        q4 = self.get_q4_elements(middle_data)
        if not q4:
            return {"q4_count": 0, "results": [], "threshold": None}

        threshold = max(s["score"] for s in q4)

        if not self.llm:
            results = [
                {"index": i, "content": s["content"][:50], "score": s["score"],
                 "verdict": "REVIEW" if s["score"] < 0.5 else "OK",
                 "suggestion": "Low confidence — manual check recommended" if s["score"] < 0.5 else ""}
                for i, s in enumerate(q4[:20])
            ]
            return {"q4_count": len(q4), "threshold": threshold, "results": results}

        all_results = []
        for batch_start in range(0, min(len(q4), 40), 20):
            batch = q4[batch_start:batch_start + 20]
            elements_text = "\n".join(
                f"[{i}] type={s['type']}, score={s['score']:.3f}, content=\"{s['content']}\""
                for i, s in enumerate(batch, start=batch_start)
            )
            try:
                resp = self.llm.invoke(self.verify_prompt.format(low_score_elements=elements_text))
                content = resp.content.strip()
                if content.startswith("```"):
                    content = re.sub(r"^```\w*\n?", "", content)
                    content = re.sub(r"\n?```$", "", content)
                all_results.extend(json.loads(content))
            except Exception as e:
                print(f"[Agent] Verification error: {e}")
                all_results.extend([
                    {"index": i, "content": s["content"][:50], "score": s["score"],
                     "verdict": "REVIEW", "suggestion": f"LLM error: {e}"}
                    for i, s in enumerate(batch, start=batch_start)
                ])

        return {"q4_count": len(q4), "threshold": threshold, "results": all_results}

    # ── Keyword Extraction & WikiSearch ─────────────────────────

    def extract_keywords(self, markdown: str) -> list:
        """Extract keywords from the paper's Abstract section or full scan."""
        keywords = []

        kw_match = re.search(
            r"(?:Keywords?|Key\s*words?)\s*[:：]\s*(.+?)(?:\n\n|\n##|\n#|\Z)",
            markdown,
            re.IGNORECASE | re.DOTALL,
        )
        if kw_match:
            raw = kw_match.group(1).strip()
            parts = re.split(r"[,;]\s*", raw)
            keywords = [p.strip().rstrip(".") for p in parts if p.strip() and len(p.strip()) > 2]

        if not keywords and self.llm:
            try:
                resp = self.llm.invoke(
                    f"Extract the main technical keywords from this research paper abstract. "
                    f"Return ONLY a JSON array of strings.\n\n{markdown[:3000]}"
                )
                content = resp.content.strip()
                if content.startswith("```"):
                    content = re.sub(r"^```\w*\n?", "", content)
                    content = re.sub(r"\n?```$", "", content)
                keywords = json.loads(content)
            except Exception:
                pass

        return keywords[:15]

    def get_keyword_wiki_urls(self, keywords: list) -> list:
        """Generate Wikipedia URLs for keywords in target language."""
        template = WIKI_TEMPLATES.get(self.target_lang, WIKI_TEMPLATES["eng_Latn"])
        return [
            {"keyword": kw, "url": template.format(kw.strip().replace(" ", "_"))}
            for kw in keywords
        ]

    # ── DB persistence ──────────────────────────────────────────

    def _save_keywords_to_db(self, run_id: str, wiki_urls: list):
        for item in wiki_urls:
            self.db.add(EvalKeyword(
                id=str(uuid.uuid4()),
                run_id=run_id,
                keyword=item["keyword"],
                wiki_url=item["url"],
            ))
        self.db.commit()

    # ── Combined Agent Run ──────────────────────────────────────

    def run(self, middle_data: dict, markdown: str, doc_id: str, pdf_name: str) -> dict:
        """Full agent run: Q4 verify + keywords + WikiSearch."""
        q4_result = self.verify_q4_elements(middle_data)
        keywords = self.extract_keywords(markdown)
        wiki_urls = self.get_keyword_wiki_urls(keywords)
        self._save_keywords_to_db(doc_id, wiki_urls)

        return {
            "q4_verification": q4_result,
            "keywords": keywords,
            "wiki_references": wiki_urls,
        }