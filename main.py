import os
import uuid
import shutil
import re
from collections import Counter
from typing import List, Dict, Any

from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from dotenv import load_dotenv

import fitz  # PyMuPDF
import spacy
from langchain_groq import ChatGroq
from langchain.schema import HumanMessage

# ----------------- Config & App -----------------
load_dotenv()
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY missing in .env — add it before running")

app = FastAPI(title="Explainify", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# serve static UI
STATIC_DIR = "static"
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ----------------- NLP & LLM -----------------
nlp = spacy.load("en_core_web_sm")

# Groq — using llama-3.3-70b-versatile (fast, free tier available)
print(f"DEBUG: Loaded GROQ API Key: {GROQ_API_KEY[:5]}...{GROQ_API_KEY[-5:]}")
llm = ChatGroq(
    model="llama-3.3-70b-versatile",
    temperature=0.0,
    max_retries=2,
    api_key=GROQ_API_KEY
)

# ----------------- In-memory sessions -----------------
# session_id -> {"text": str, "summary": str, "terms": List[dict], "chat": List}
SESSIONS: Dict[str, Dict[str, Any]] = {}

# ----------------- Helpers -----------------
def extract_text_from_pdf(path: str) -> str:
    doc = fitz.open(path)
    parts = [p.get_text() for p in doc]
    return "\n".join(parts)

def detect_complex_terms(text: str, max_terms: int = 30) -> List[str]:
    clean = re.sub(r"[^A-Za-z0-9\s\-/().,]", " ", text)
    doc = nlp(clean)
    entities = [ent.text.strip() for ent in doc.ents if ent.text.strip()]
    tokens = [t.text.lower() for t in doc if t.is_alpha and not t.is_stop and len(t.text) > 2]
    freq = Counter(tokens)
    rare = [w for w, c in freq.items() if c <= 2]
    merged = list({t for t in (entities + rare) if 2 <= len(t) <= 60})
    merged.sort(key=lambda x: (-len(x), x))
    return merged[:max_terms]

def summarize_text_with_llm(text: str, max_chars: int = 7000) -> str:
    chunk = text[:max_chars]
    prompt = (
        "You are a helpful study assistant. Create a short structured summary (bullet points) "
        "of the following document in simple language suitable for an undergraduate student. "
        "Keep it concise.\n\nDocument:\n" + chunk
    )
    resp = llm.invoke([HumanMessage(content=prompt)])
    return getattr(resp, "content", str(resp)).strip()

def explain_terms_with_llm(terms: List[str], limit: int = 12) -> List[Dict[str,str]]:
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
@app.get("/")
def serve_ui():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return JSONResponse({"error": "UI not found. Place static files in ./static directory."}, status_code=404)

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

    # Summary, terms, explanations
    summary = summarize_text_with_llm(text)
    candidates = detect_complex_terms(text, max_terms=30)
    term_explanations = explain_terms_with_llm(candidates, limit=12)

    # store session
    SESSIONS[sid] = {"text": text, "summary": summary, "terms": term_explanations, "chat": []}

    # First answer with PDF context
    context = text[:6000]
    user_prompt = (
        "You are an assistant using this document to answer questions.\n\n"
        f"Document excerpt:\n{context}\n\nUser question: {prompt}\n\nAnswer clearly and concisely."
    )
    resp = llm.invoke([HumanMessage(content=user_prompt)])
    first_answer = getattr(resp, "content", str(resp)).strip()

    SESSIONS[sid]["chat"].append({"role":"user","content":prompt})
    SESSIONS[sid]["chat"].append({"role":"assistant","content":first_answer})

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

    # If session exists, include PDF context; else, just answer generally
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

    resp = llm.invoke([HumanMessage(content=prompt_text)])
    answer = getattr(resp, "content", str(resp)).strip()

    # update chat if session exists
    if sid in SESSIONS:
        h = SESSIONS[sid].get("chat", [])
        h.append({"role":"user","content": question})
        h.append({"role":"assistant","content": answer})
        SESSIONS[sid]["chat"] = h[-40:]

    return {"session_id": sid, "answer": answer}

@app.get("/api/session/{session_id}")
def get_session(session_id: str):
    s = SESSIONS.get(session_id)
    if not s:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {"session_id": session_id, "summary": s["summary"], "terms": s["terms"]}
