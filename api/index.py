import os
import uuid
import shutil
import re
from typing import List, Dict, Any

from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

import fitz  # PyMuPDF
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage

# ----------------- Config & App -----------------
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    import warnings
    warnings.warn("GROQ_API_KEY not set — LLM features will fail until you set it in the environment.")

app = FastAPI(title="Explainify", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# Vercel: /tmp is the only writable directory
UPLOAD_DIR = "/tmp/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ----------------- LLM -----------------
llm = None
if GROQ_API_KEY:
    llm = ChatGroq(
        model="llama-3.3-70b-versatile",
        temperature=0.0,
        max_retries=2,
        api_key=GROQ_API_KEY
    )

# ----------------- In-memory sessions -----------------
# NOTE: On Vercel serverless, sessions may not persist between invocations.
# This is acceptable for single upload+ask flows within one function instance.
SESSIONS: Dict[str, Dict[str, Any]] = {}

# ----------------- Helpers -----------------
def extract_text_from_pdf(path: str) -> str:
    doc = fitz.open(path)
    parts = [p.get_text() for p in doc]
    return "\n".join(parts)


def detect_complex_terms_with_llm(text: str, max_terms: int = 12) -> List[str]:
    """Use LLM to detect complex/technical terms instead of spaCy."""
    if not llm:
        return []
    chunk = text[:5000]
    prompt = (
        "You are a study assistant. From the following document text, identify up to "
        f"{max_terms} complex or technical terms that a college student might not know. "
        "Return ONLY a JSON array of strings, nothing else. Example: [\"term1\", \"term2\"]\n\n"
        f"Document:\n{chunk}"
    )
    try:
        resp = llm.invoke([HumanMessage(content=prompt)])
        content = getattr(resp, "content", str(resp)).strip()
        # Parse JSON array from response
        import json
        # Try to extract JSON array even if wrapped in markdown
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        terms = json.loads(content)
        if isinstance(terms, list):
            return [str(t) for t in terms[:max_terms]]
    except Exception:
        pass
    return []


def summarize_text_with_llm(text: str, max_chars: int = 7000) -> str:
    if not llm:
        return "LLM unavailable — GROQ_API_KEY not configured."
    chunk = text[:max_chars]
    prompt = (
        "You are a helpful study assistant. Create a short structured summary (bullet points) "
        "of the following document in simple language suitable for an undergraduate student. "
        "Keep it concise.\n\nDocument:\n" + chunk
    )
    resp = llm.invoke([HumanMessage(content=prompt)])
    return getattr(resp, "content", str(resp)).strip()


def explain_terms_with_llm(terms: List[str], limit: int = 12) -> List[Dict[str, str]]:
    if not llm:
        return [{"term": t, "explanation": "LLM unavailable — GROQ_API_KEY not configured."} for t in terms[:limit]]
    out = []
    for term in terms[:limit]:
        prompt = f"Explain the term '{term}' in 1-3 simple sentences for a college student. Keep it concise."
        try:
            resp = llm.invoke([HumanMessage(content=prompt)])
            text = getattr(resp, "content", str(resp)).strip()
        except Exception as e:
            text = f"Error generating explanation: {e}"
        out.append({"term": term, "explanation": text[:450]})
    return out


# ----------------- Routes -----------------
class AskRequest(BaseModel):
    session_id: str
    message: str


@app.post("/api/upload_and_ask")
async def upload_and_ask(file: UploadFile = File(...), prompt: str = Form(...)):
    if not file.filename.lower().endswith(".pdf"):
        return JSONResponse({"error": "Only PDF files allowed"}, status_code=400)

    sid = str(uuid.uuid4())
    filename = f"{sid}_{file.filename}"
    path = os.path.join(UPLOAD_DIR, filename)
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    text = extract_text_from_pdf(path)
    if not text.strip():
        return JSONResponse({"error": "No text could be extracted from PDF"}, status_code=400)

    # Clean up the uploaded file after extraction
    try:
        os.remove(path)
    except OSError:
        pass

    # Summary, terms, explanations
    summary = summarize_text_with_llm(text)
    candidates = detect_complex_terms_with_llm(text, max_terms=12)
    term_explanations = explain_terms_with_llm(candidates, limit=12)

    # store session
    SESSIONS[sid] = {"text": text, "summary": summary, "terms": term_explanations, "chat": []}

    # First answer with PDF context
    context = text[:6000]
    user_prompt = (
        "You are an assistant using this document to answer questions.\n\n"
        f"Document excerpt:\n{context}\n\nUser question: {prompt}\n\nAnswer clearly and concisely."
    )
    if not llm:
        first_answer = "LLM unavailable — GROQ_API_KEY not configured on the server."
    else:
        resp = llm.invoke([HumanMessage(content=user_prompt)])
        first_answer = getattr(resp, "content", str(resp)).strip()

    SESSIONS[sid]["chat"].append({"role": "user", "content": prompt})
    SESSIONS[sid]["chat"].append({"role": "assistant", "content": first_answer})

    return {
        "session_id": sid,
        "filename": file.filename,
        "summary": summary,
        "term_explanations": term_explanations,
        "first_answer": first_answer
    }


@app.post("/api/ask")
async def api_ask(body: AskRequest):
    sid = body.session_id
    question = body.message

    if sid in SESSIONS:
        doc_text = SESSIONS[sid]["text"]
        context = doc_text[:6000]
        prompt_text = (
            "You are an assistant using the document context to answer. "
            "If the document does not contain the answer, give a general explanation.\n\n"
            f"Document excerpt:\n{context}\n\nUser question: {question}\n\nAnswer clearly."
        )
    else:
        prompt_text = (
            "You are an AI assistant. Answer the following question clearly and concisely:\n\n"
            f"User question: {question}"
        )

    if not llm:
        return JSONResponse({"error": "LLM unavailable — GROQ_API_KEY not configured on the server."}, status_code=503)
    resp = llm.invoke([HumanMessage(content=prompt_text)])
    answer = getattr(resp, "content", str(resp)).strip()

    if sid in SESSIONS:
        h = SESSIONS[sid].get("chat", [])
        h.append({"role": "user", "content": question})
        h.append({"role": "assistant", "content": answer})
        SESSIONS[sid]["chat"] = h[-40:]

    return {"session_id": sid, "answer": answer}


@app.get("/api/session/{session_id}")
def get_session(session_id: str):
    s = SESSIONS.get(session_id)
    if not s:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"session_id": session_id, "summary": s["summary"], "terms": s["terms"]}
