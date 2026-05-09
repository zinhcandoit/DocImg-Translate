"""
FastAPI Backend — Production pipeline API.

Endpoints:
  POST /upload         — Upload PDF → MinerU extraction (or mock)
  POST /translate      — Translate via NLLB (middle.json paragraph-level)
  POST /render-pdf     — Render translated PDF from middle.json + images
  POST /agent          — AI agent: Q4 verify + keywords + WikiSearch
  POST /feedback       — Log user metrics to MLflow
  GET  /download/{id}  — Download rendered PDF
"""

import json
import uuid
import time
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, UploadFile, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

from .mineru_client import MinerUClient
from .nllb_service import NLLBService
from .agent import AIAgent
from .evaluation import Evaluator
from .pdf_renderer import PDFRenderer

# ── Services ────────────────────────────────────────────────
mineru = MinerUClient()
nllb = NLLBService(lazy_load=True)
agent = AIAgent()
evaluator = Evaluator()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background loading of NLLB model
    asyncio.create_task(asyncio.to_thread(nllb.load_model))
    yield

app = FastAPI(title="DIMT — Document Intelligent Machine Translation", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
# In-memory document store (production would use a DB)
doc_store = {}

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)


# ── Models ──────────────────────────────────────────────────
class TranslateRequest(BaseModel):
    doc_id: str

class AgentRequest(BaseModel):
    doc_id: str

class FeedbackRequest(BaseModel):
    doc_id: str
    original_md: str
    modified_md: str
    user_rating: int
    downloaded: bool
    time_consumed: float


# ── Endpoints ───────────────────────────────────────────────

@app.post("/upload")
async def upload_file(file: UploadFile):
    """Upload PDF → extract via MinerU (mock fallback for demo)."""
    doc_id = str(uuid.uuid4())[:8]
    evaluator.start_inference(doc_id)

    # Save uploaded file
    input_dir = Path("input_docs")
    input_dir.mkdir(exist_ok=True)
    save_path = input_dir / file.filename
    content = await file.read()
    save_path.write_bytes(content)

    # Try mock first (for demo with pre-existing data), else real API
    res = mineru.extract_local_mock(file.filename)
    if res["status"] != "success":
        res = mineru.extract_from_file(str(save_path))

    if res["status"] == "success":
        doc_store[doc_id] = {
            "filename": file.filename,
            "markdown": res["markdown"],
            "middle_json": res.get("middle_json"),
            "images_dir": res.get("images_dir"),
            "extract_dir": res.get("extract_dir"),
        }
        return {"status": "success", "doc_id": doc_id, "markdown": res["markdown"]}

    return {"status": "error", "message": res.get("message", "Extraction failed")}


@app.post("/translate")
async def translate_document(req: TranslateRequest):
    """Translate document using NLLB — paragraph-level from middle.json."""
    if not nllb.loaded:
        print("[API] NLLB is still loading. Waiting for it to finish...")
        await asyncio.to_thread(nllb.wait_for_load)
        if not nllb.loaded:
            print("[API] Warning: NLLB finished loading attempt but is not loaded (mock mode).")

    doc = doc_store.get(req.doc_id)
    if not doc:
        return {"status": "error", "message": "Document not found"}

    middle_data = doc.get("middle_json")
    translated_md = ""
    translated_middle = None

    if middle_data:
        # Paragraph-level translation from middle.json
        translated_middle = nllb.translate_middle_json(middle_data)
        doc["translated_middle"] = translated_middle
        # Also generate translated markdown
        translated_md = nllb.translate_markdown(doc["markdown"])
    else:
        # Fallback: markdown-only translation
        translated_md = nllb.translate_markdown(doc["markdown"])

    doc["translated_md"] = translated_md
    evaluator.end_inference(req.doc_id)

    return {
        "status": "success",
        "translated_markdown": translated_md,
        "has_middle_json": middle_data is not None,
    }


@app.post("/render-pdf")
async def render_pdf(req: TranslateRequest):
    """Render translated PDF from translated middle.json + images."""
    doc = doc_store.get(req.doc_id)
    if not doc:
        return {"status": "error", "message": "Document not found"}

    middle_data = doc.get("middle_json")
    if not middle_data:
        return {"status": "error", "message": "No middle.json found"}

    # Find origin PDF path
    input_dir = Path("input_docs")
    origin_pdf_path = input_dir / doc["filename"]
    
    images_dir = doc.get("images_dir")
    renderer = PDFRenderer(images_dir=images_dir)

    output_path = OUTPUT_DIR / f"{req.doc_id}_translated.pdf"
    
    # Render with NLLB service for live translation if requested, 
    # but here we use the original layout data and translate inside the renderer
    renderer.render(middle_data, str(origin_pdf_path), str(output_path), nllb_service=nllb)

    doc["pdf_path"] = str(output_path)
    return {"status": "success", "pdf_path": str(output_path)}


@app.get("/stream-pdf/{doc_id}")
async def stream_pdf(doc_id: str):
    """Stream the rendered translated PDF for preview."""
    doc = doc_store.get(doc_id)
    if not doc or "pdf_path" not in doc:
        return {"status": "error", "message": "PDF not found"}
    return FileResponse(
        doc["pdf_path"],
        media_type="application/pdf",
    )


@app.get("/download/{doc_id}")
async def download_pdf(doc_id: str):
    """Download the rendered translated PDF."""
    doc = doc_store.get(doc_id)
    if not doc or "pdf_path" not in doc:
        return {"status": "error", "message": "PDF not found"}
    return FileResponse(
        doc["pdf_path"],
        media_type="application/pdf",
        filename=f"{Path(doc.get('filename', 'translated')).stem}_vi.pdf",
    )


@app.post("/agent")
async def run_agent(req: AgentRequest):
    """AI Agent: Q4 verification + keyword extraction + WikiSearch URLs."""
    doc = doc_store.get(req.doc_id)
    if not doc:
        return {"status": "error", "message": "Document not found"}

    middle_data = doc.get("middle_json", {})
    markdown = doc.get("markdown", "")

    result = agent.run(middle_data, markdown)
    doc["agent_result"] = result
    return result


@app.post("/feedback")
async def log_feedback(req: FeedbackRequest):
    """Log user feedback metrics to MLflow."""
    metrics = evaluator.log_metrics(
        doc_id=req.doc_id,
        original_md=req.original_md,
        modified_md=req.modified_md,
        user_rating=req.user_rating,
        download=req.downloaded,
        time_consumed=req.time_consumed,
    )
    return {"status": "success", "metrics": metrics}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
