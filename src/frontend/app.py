"""
Streamlit Frontend — DIMT Document Translation Pipeline.

Features:
- Upload PDF → Extract via MinerU → Translate via NLLB
- Rendered markdown preview + editable text area
- Download translated PDF (rendered from middle.json + images)
- AI Agent panel: Q4 verification results + keyword WikiSearch links
- User feedback with MLflow metrics logging
"""

import streamlit as st
import requests
import time

st.set_page_config(layout="wide", page_title="DIMT — Document Translation", page_icon="📄")

API_BASE = "http://localhost:8000"

st.title("📄 Document Intelligent Machine Translation")
st.caption("PDF → MinerU extraction → NLLB translation → Translated PDF + Markdown")

# ── Session State ───────────────────────────────────────────
for key in ["doc_id", "original_markdown", "translated_markdown",
            "agent_result", "pdf_ready", "has_middle"]:
    if key not in st.session_state:
        st.session_state[key] = None

# ── Sidebar ─────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Settings")
    uploaded_file = st.file_uploader("Upload PDF document", type=["pdf"])
    st.slider("Max convert pages", 1, 200, 200)
    st.selectbox("MinerU Engine", ["vlm", "pipeline"])
    st.selectbox("Target Language", ["Vietnamese (vie_Latn)"])

    st.divider()
    col1, col2 = st.columns(2)
    convert_btn = col1.button("🚀 Convert", use_container_width=True)
    clear_btn = col2.button("🗑️ Clear", use_container_width=True)

    st.divider()
    st.subheader("🤖 AI Agent")
    agent_btn = st.button("Run Agent Analysis", use_container_width=True)

if clear_btn:
    st.session_state.clear()
    st.rerun()

# ── Pipeline Execution ─────────────────────────────────────
if convert_btn and uploaded_file:
    # Step 1: Upload & Extract
    with st.spinner("📤 Uploading & extracting with MinerU..."):
        files = {"file": (uploaded_file.name, uploaded_file.getvalue())}
        res = requests.post(f"{API_BASE}/upload", files=files)
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == "success":
                st.session_state.doc_id = data["doc_id"]
                st.session_state.original_markdown = data["markdown"]
                st.success(f"✅ Extraction complete (doc_id: {data['doc_id']})")
            else:
                st.error(f"❌ Extraction failed: {data.get('message')}")
        else:
            st.error(f"❌ API error: {res.status_code}")

    # Step 2: Translate
    if st.session_state.original_markdown and st.session_state.doc_id:
        with st.spinner("🔄 Translating with NLLB-1.3B..."):
            res = requests.post(
                f"{API_BASE}/translate",
                json={"doc_id": st.session_state.doc_id},
            )
            if res.status_code == 200:
                data = res.json()
                st.session_state.translated_markdown = data.get("translated_markdown")
                st.session_state.has_middle = data.get("has_middle_json", False)
                st.success("✅ Translation complete")

    # Step 3: Render PDF
    if st.session_state.has_middle and st.session_state.doc_id:
        with st.spinner("📄 Rendering translated PDF..."):
            res = requests.post(
                f"{API_BASE}/render-pdf",
                json={"doc_id": st.session_state.doc_id},
            )
            if res.status_code == 200 and res.json().get("status") == "success":
                st.session_state.pdf_ready = True
                st.success("✅ PDF rendered successfully")

# ── Agent ──────────────────────────────────────────────────
if agent_btn and st.session_state.doc_id:
    with st.spinner("🤖 Agent analyzing Q4 scores & extracting keywords..."):
        res = requests.post(
            f"{API_BASE}/agent",
            json={"doc_id": st.session_state.doc_id},
        )
        if res.status_code == 200:
            st.session_state.agent_result = res.json()
            st.success("✅ Agent analysis complete")

# ── Results Display ────────────────────────────────────────
if st.session_state.translated_markdown:
    st.header("📊 Results")

    tab1, tab2, tab3, tab4 = st.tabs([
        "📝 Translated Markdown", "✏️ Editable Text", "🤖 Agent Analysis", "📥 Downloads"
    ])

    start_edit_time = time.time()

    with tab1:
        st.markdown(st.session_state.translated_markdown, unsafe_allow_html=True)

    with tab2:
        edited_text = st.text_area(
            "Edit translated markdown (feedback loop)",
            st.session_state.translated_markdown,
            height=500,
        )
        st.subheader("📊 Submit Evaluation")
        rating = st.slider("Rate the translation quality (1-5)", 1, 5, 3)
        downloaded = st.checkbox("Downloaded generated file?")
        if st.button("📤 Submit Feedback & Save Metrics"):
            time_consumed = time.time() - start_edit_time
            res = requests.post(f"{API_BASE}/feedback", json={
                "doc_id": st.session_state.doc_id,
                "original_md": st.session_state.translated_markdown,
                "modified_md": edited_text,
                "user_rating": rating,
                "downloaded": downloaded,
                "time_consumed": time_consumed,
            })
            if res.status_code == 200:
                st.success(f"✅ Metrics logged: {res.json()['metrics']}")

    with tab3:
        if st.session_state.agent_result:
            result = st.session_state.agent_result

            # Q4 Verification
            q4 = result.get("q4_verification", {})
            st.subheader(f"🔍 Q4 Score Verification ({q4.get('q4_count', 0)} elements)")
            if q4.get("threshold"):
                st.caption(f"Score threshold (25th percentile): {q4['threshold']:.3f}")
            for item in q4.get("results", [])[:20]:
                icon = "✅" if item.get("verdict") == "OK" else "⚠️"
                st.markdown(
                    f"{icon} **[{item.get('index')}]** score={item.get('score', 'N/A'):.3f} "
                    f"→ {item.get('verdict', 'N/A')} | `{item.get('content', '')[:60]}`"
                )
                if item.get("suggestion"):
                    st.caption(f"  💡 {item['suggestion']}")

            st.divider()

            # Keywords & Wiki
            st.subheader("🔑 Keywords & Wikipedia References")
            keywords = result.get("keywords", [])
            wiki = result.get("wiki_references", [])
            if wiki:
                for ref in wiki:
                    st.markdown(f"- **{ref['keyword']}** → [{ref['url']}]({ref['url']})")
            elif keywords:
                st.write(", ".join(keywords))
            else:
                st.info("No keywords extracted")
        else:
            st.info("Click 'Run Agent Analysis' in the sidebar to see results.")

    with tab4:
        st.subheader("📥 Download & Preview")

        # Download translated markdown
        if st.session_state.translated_markdown:
            st.download_button(
                "📄 Download Translated Markdown (.md)",
                st.session_state.translated_markdown,
                file_name="translated.md",
                mime="text/markdown",
            )

        # Download/Preview translated PDF
        if st.session_state.pdf_ready and st.session_state.doc_id:
            try:
                # PDF Preview logic
                pdf_res = requests.get(f"{API_BASE}/stream-pdf/{st.session_state.doc_id}")
                if pdf_res.status_code == 200:
                    import base64
                    pdf_bytes = pdf_res.content
                    
                    st.divider()
                    st.subheader("👁️ PDF Preview")
                    
                    # Encode to base64 for embedding
                    base64_pdf = base64.b64encode(pdf_bytes).decode('utf-8')
                    pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="100%" height="800" type="application/pdf"></iframe>'
                    st.markdown(pdf_display, unsafe_allow_html=True)
                    
                    st.divider()
                    st.download_button(
                        "💾 Save Translated PDF",
                        pdf_bytes,
                        file_name=f"{st.session_state.doc_id}_translated.pdf",
                        mime="application/pdf",
                        use_container_width=True
                    )
                else:
                    st.warning("PDF stream not available")
            except Exception as e:
                st.error(f"PDF preview error: {e}")
        else:
            st.info("PDF will be available after conversion with middle.json data")
