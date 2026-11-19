"""
PRODUCT SAFETY INTELLIGENCE
AI-Powered Product Recall Investigation and Consumer Safety Intelligence System

Everything on the backend lives in this one file, split into clearly marked sections:
  1. Configuration
  2. Database
  3. Document processing (text extraction, chunking)
  4. Simple RAG (keyword search over chunks)
  5. Nexus API client
  6. The five AI investigation functions
  7. Investigation runner
  8. PDF report
  9. Web routes (API used by the page)
"""

import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from xml.sax.saxutils import escape

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from pypdf import PdfReader
from docx import Document as DocxDocument
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# ---------------------------------------------------------------------------
# 1. CONFIGURATION
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)
print("ENV FILE EXISTS:", os.path.isfile(os.path.join(BASE_DIR, ".env")))
print("API KEY LOADED:", bool(os.getenv("NEXUS_API_KEY")))
print("BASE URL LOADED:", os.getenv("NEXUS_API_BASE_URL") or os.getenv("NEXUS_BASE_URL"))

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
REPORT_DIR = os.path.join(BASE_DIR, "reports")
DB_PATH = os.path.join(BASE_DIR, "database.db")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REPORT_DIR, exist_ok=True)

ALLOWED_EXTENSIONS = {"pdf", "docx", "txt"}
PLACEHOLDERS = {"YOUR_PRIVATE_API_KEY", "YOUR_AVAILABLE_MODEL", "YOUR_NEXUS_API_KEY"}

RISK_LEVELS = ["Critical", "High", "Medium", "Low", "Unknown"]
VERIFY_STATUSES = ["Verified", "Partially Verified", "Not Verified",
                   "Conflicting Information", "Insufficient Evidence"]

RISK_DISCLAIMER = ("This risk assessment was produced by an AI model. It is preliminary and is "
                   "NOT an official regulatory classification.")
GENERAL_DISCLAIMER = (
    "This report is generated with AI assistance from the documents uploaded by the user. "
    "Findings from uploaded documents are not official recall records. The absence of recall "
    "information in the uploaded documents does not mean a product is safe. Always confirm "
    "recall status and safety classification with the official regulator (for example US CPSC, "
    "US FDA or EU Safety Gate) before taking action."
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB per request


def env_value(name, default=""):
    """Read a setting from .env and ignore the example placeholders."""
    value = (os.getenv(name) or "").strip()
    return default if (not value or value in PLACEHOLDERS) else value


def nexus_key():
    return env_value("NEXUS_API_KEY")


def nexus_base_url():
    return env_value(
        "NEXUS_API_BASE_URL",
        env_value(
            "NEXUS_BASE_URL",
            "https://nexusapi.navigatelabs.ai"
        )
    ).rstrip("/")

def recall_sources():
    """Official recall websites (configurable in .env)."""
    return [
        {"name": "US CPSC (Consumer Product Safety Commission)",
         "url": env_value("CPSC_URL", "https://www.cpsc.gov/Recalls")},
        {"name": "US FDA (Food and Drug Administration)",
         "url": env_value("FDA_URL", "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts")},
        {"name": "EU Safety Gate (Rapid Alert System)",
         "url": env_value("SAFETY_GATE_URL",
                          "https://ec.europa.eu/safety-gate-alerts/screen/search?resetSearch=true")},
    ]


# ---------------------------------------------------------------------------
# 2. DATABASE (SQLite)
# ---------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def query(sql, args=(), one=False):
    conn = db()
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=()):
    conn = db()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS documents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename TEXT, stored_name TEXT, file_type TEXT,
        pages INTEGER DEFAULT 0, chunk_count INTEGER DEFAULT 0,
        status TEXT, error TEXT, uploaded_at TEXT
    );
    CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_id INTEGER, page INTEGER, chunk_index INTEGER, text TEXT
    );
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        investigation_id INTEGER, product_name TEXT, brand TEXT, manufacturer TEXT,
        model_number TEXT, batch_number TEXT, serial_number TEXT, category TEXT,
        manufacturing_date TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS investigations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_name TEXT, brand TEXT, manufacturer TEXT, model_number TEXT,
        batch_number TEXT, serial_number TEXT, category TEXT, description TEXT,
        status TEXT, progress INTEGER DEFAULT 0, current_step TEXT,
        risk_level TEXT, verification_status TEXT, recall_found INTEGER DEFAULT 0,
        error TEXT, results_json TEXT, created_at TEXT, completed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS recall_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        investigation_id INTEGER, product_name TEXT, recall_date TEXT, reason TEXT,
        affected_models TEXT, affected_batches TEXT, hazards TEXT,
        source_type TEXT, source_file TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS investigation_reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        investigation_id INTEGER, summary TEXT, report_json TEXT, pdf_path TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    """)
    # An investigation that was still running when the server stopped can never finish.
    conn.execute("UPDATE investigations SET status='Failed', "
                 "error='The application was restarted while this investigation was running.' "
                 "WHERE status='Running'")
    conn.commit()
    conn.close()


def get_setting(key):
    row = query("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return row["value"] if row else ""


def set_setting(key, value):
    execute("INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

def current_model():
    """Model chosen in Settings wins; otherwise use the model from .env."""
    return get_setting("nexus_model") or env_value(
        "NYAYA_MODEL",
        env_value("NEXUS_MODEL", "nova-micro")
    )


# ---------------------------------------------------------------------------
# 3. DOCUMENT PROCESSING
# ---------------------------------------------------------------------------
def extract_pages(path, ext):
    """Return a list of (page_number_or_None, text). Page numbers exist only for PDFs."""
    if ext == "pdf":
        reader = PdfReader(path)
        if reader.is_encrypted:
            reader.decrypt("")
        return [(i + 1, page.extract_text() or "") for i, page in enumerate(reader.pages)]
    if ext == "docx":
        doc = DocxDocument(path)
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return [(None, "\n".join(parts))]
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return [(None, f.read())]


def chunk_text(text, size=1000, overlap=150):
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = max(text.rfind("\n", start, end), text.rfind(". ", start, end))
            if cut > start + size * 0.5:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def process_document(doc_id, path, ext):
    try:
        pages = extract_pages(path, ext)
        total = 0
        conn = db()
        try:
            for page_no, text in pages:
                for idx, piece in enumerate(chunk_text(text)):
                    conn.execute("INSERT INTO chunks(document_id, page, chunk_index, text) VALUES(?,?,?,?)",
                                 (doc_id, page_no, idx, piece))
                    total += 1
            conn.commit()
        finally:
            conn.close()
        if total == 0:
            raise ValueError("No readable text found (the file may be a scanned image).")
        execute("UPDATE documents SET status='Processed', pages=?, chunk_count=? WHERE id=?",
                (len(pages) if ext == "pdf" else 0, total, doc_id))
    except Exception as e:  # keep the app alive, show the reason in the table
        execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
        execute("UPDATE documents SET status='Failed', error=? WHERE id=?", (str(e)[:300], doc_id))


# ---------------------------------------------------------------------------
# 4. SIMPLE RAG: keyword-based retrieval
# ---------------------------------------------------------------------------
STOP_WORDS = set("the a an and or of to in for on with is are was were be this that it by as at from "
                 "what which who how do does".split())


def tokens(text):
    words = [w.strip(".-") for w in re.findall(r"[a-z0-9][a-z0-9.\-]*", (text or "").lower())]
    return [w for w in words if len(w) > 1 and w not in STOP_WORDS]


def search_chunks(text_query, limit=8):
    """Rank chunks of processed documents by keyword matches."""
    toks = set(tokens(text_query))
    if not toks:
        return []
    rows = query("SELECT c.id, c.page, c.text, d.filename, d.id AS document_id FROM chunks c "
                 "JOIN documents d ON d.id = c.document_id WHERE d.status='Processed'")
    scored = []
    for r in rows:
        low = r["text"].lower()
        score, matched = 0.0, 0
        for t in toks:
            n = low.count(t)
            if n:
                matched += 1
                score += min(n, 5) * (2 if len(t) > 4 else 1)
        if matched:
            item = dict(r)
            item["score"] = score
            item["coverage"] = round(matched / len(toks), 2)
            scored.append(item)
    scored.sort(key=lambda x: (x["coverage"], x["score"]), reverse=True)
    return scored[:limit]


def first_chunks(limit=6):
    """Fallback when no keyword matches: take the opening text of each document."""
    rows = query("SELECT c.id, c.page, c.text, d.filename, d.id AS document_id FROM chunks c "
                 "JOIN documents d ON d.id = c.document_id "
                 "WHERE d.status='Processed' AND c.chunk_index = 0 ORDER BY d.id, c.page LIMIT ?", (limit,))
    out = []
    for r in rows:
        item = dict(r)
        item["score"], item["coverage"] = 0, 0
        out.append(item)
    return out


def snippet(text, toks, width=400):
    low = text.lower()
    pos = min([low.find(t) for t in toks if low.find(t) >= 0] or [0])
    start = max(0, pos - width // 4)
    piece = text[start:start + width].strip()
    return ("..." if start > 0 else "") + piece + ("..." if start + width < len(text) else "")


def build_evidence(chunks, max_chars=12000):
    """Give every retrieved chunk an id (S1, S2...). The app, not the AI, owns file names/pages."""
    evidence, total = [], 0
    for i, c in enumerate(chunks, 1):
        if total + len(c["text"]) > max_chars and evidence:
            break
        evidence.append({"id": f"S{i}", "filename": c["filename"], "page": c["page"],
                         "text": c["text"], "coverage": c.get("coverage", 0)})
        total += len(c["text"])
    return evidence


def evidence_text(evidence):
    return "\n\n".join(
        f"[{e['id']}] File: {e['filename']} | Page: {e['page'] if e['page'] else 'not available'}\n{e['text']}"
        for e in evidence)


def resolve_sources(ids, evidence, note_map=None):
    by_id = {e["id"]: e for e in evidence}
    out = []
    for i in ids:
        e = by_id.get(i)
        if e:
            out.append({"id": i, "filename": e["filename"], "page": e["page"],
                        "excerpt": e["text"][:500], "note": (note_map or {}).get(i, "")})
    return out


# ---------------------------------------------------------------------------
# 5. NEXUS API CLIENT (OpenAI-compatible)
# ---------------------------------------------------------------------------
class NexusError(Exception):
    """An error with a message that is safe and clear enough to show to the user."""


def nexus_headers():
    return {"Authorization": f"Bearer {nexus_key()}", "Content-Type": "application/json"}


def explain_http_error(resp):
    try:
        body = resp.json()
        err = body.get("error", body)
        detail = err.get("message") if isinstance(err, dict) else str(err)
    except Exception:
        detail = resp.text[:200]
    detail = (detail or "").strip()
    low = detail.lower()
    code = resp.status_code
    if any(w in low for w in ("balance", "quota", "credit", "insufficient")) or code == 402:
        return f"Nexus API: insufficient balance or quota. Please top up your account. ({detail})"
    if code in (401, 403):
        return f"Nexus API: the API key is invalid or not allowed. Check NEXUS_API_KEY in .env. ({detail})"
    if code == 404:
        return f"Nexus API: model or endpoint not found. Choose a valid model in Settings. ({detail})"
    if code == 429:
        return f"Nexus API: rate limit reached. Wait a moment and try again. ({detail})"
    return f"Nexus API error {code}: {detail}"


def require_ready():
    """Return a user-friendly message if the AI cannot be used yet, else None."""
    if not nexus_key():
        return "NEXUS_API_KEY is missing. Add your key to the .env file and restart the application."
    if not current_model():
        return "No AI model selected. Open Settings, click TEST API CONNECTION and choose a model."
    return None


def list_models():
    """GET /models. Returns a list of model ids available for this key."""
    if not nexus_key():
        raise NexusError("NEXUS_API_KEY is missing. Add your key to the .env file and restart the application.")
    try:
        resp = requests.get(f"{nexus_base_url()}/models", headers=nexus_headers(), timeout=30)
    except requests.exceptions.RequestException as e:
        raise NexusError(f"Could not reach the Nexus API ({e.__class__.__name__}). Check your internet connection.")
    if resp.status_code != 200:
        raise NexusError(explain_http_error(resp))
    try:
        data = resp.json()
    except ValueError:
        raise NexusError("Nexus API returned an unreadable response for /models.")
    items = data.get("data", data) if isinstance(data, dict) else data
    ids = []
    for m in items if isinstance(items, list) else []:
        ids.append(m.get("id") if isinstance(m, dict) else str(m))
    return sorted(i for i in ids if i)


def nexus_chat(system_prompt, user_prompt):
    """POST /chat/completions and return the text of the answer."""
    payload = {
        "model": current_model(),
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": user_prompt}],
        "temperature": 0.1,
    }
    try:
        resp = requests.post(f"{nexus_base_url()}/chat/completions", headers=nexus_headers(),
                             json=payload, timeout=240)
    except requests.exceptions.Timeout:
        raise NexusError("The Nexus API took too long to answer. Please try again.")
    except requests.exceptions.RequestException as e:
        raise NexusError(f"Could not reach the Nexus API ({e.__class__.__name__}). Check your internet connection.")
    if resp.status_code != 200:
        raise NexusError(explain_http_error(resp))
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NexusError("Nexus API returned an unexpected response format.")
    if isinstance(content, list):  # some gateways return a list of parts
        content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    if not content or not str(content).strip():
        raise NexusError("Nexus API returned an empty answer.")
    return str(content)


def parse_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def nexus_json(system_prompt, user_prompt):
    """Ask the AI for JSON. If the answer is not valid JSON, ask once more."""
    last_error = None
    for attempt in range(2):
        extra = "" if attempt == 0 else "\n\nIMPORTANT: your previous answer was not valid JSON. Reply with ONE valid JSON object only."
        text = nexus_chat(system_prompt, user_prompt + extra)
        try:
            data = parse_json(text)
            if isinstance(data, dict):
                return data
        except ValueError as e:
            last_error = e
    raise NexusError("The AI answer could not be read as JSON. Please try again.")


# ---------------------------------------------------------------------------
# 6. THE FIVE AI FUNCTIONS (all use the Nexus API)
# ---------------------------------------------------------------------------
SYSTEM_RULES = (
    "You are a product safety investigation assistant. Use ONLY the product details and the numbered "
    "evidence excerpts you are given. Never invent facts, recall notices, dates, regulators, file names "
    "or page numbers. If something is not in the evidence, write \"Not found\". Findings from uploaded "
    "documents are NOT official recall records. Cite evidence only with the ids given, such as \"S1\". "
    "Reply with ONE valid JSON object and nothing else (no markdown)."
)

TASKS = {
    "document_analysis": """TASK_NAME: document_analysis
Extract product identification details from the evidence. Return JSON with exactly these keys:
{"product_name": "", "brand": "", "manufacturer": "", "model_number": "", "batch_number": "",
 "serial_number": "", "manufacturing_date": "", "safety_warnings": [""], "summary": "", "evidence_ids": ["S1"]}
Use "Not found" for any value that is not in the evidence. safety_warnings is an empty list if none are stated.""",

    "recall_investigation": """TASK_NAME: recall_investigation
Find product recall information inside the evidence. Return JSON with exactly these keys:
{"recall_found_in_documents": true, "recall_information": "", "recall_date": "", "recall_reason": "",
 "affected_models": [""], "affected_batches": [""], "safety_hazards": [""], "note": "", "evidence_ids": ["S1"]}
recall_found_in_documents is true only if the evidence itself describes a recall, withdrawal or safety notice.
Use "Not found" or empty lists when absent. Do NOT invent official recall notices.""",

    "product_verification": """TASK_NAME: product_verification
Compare the product details provided by the user with the evidence. Return JSON with exactly these keys:
{"overall_status": "Verified | Partially Verified | Not Verified | Conflicting Information | Insufficient Evidence",
 "fields": [{"field": "Product Name", "provided": "", "found_in_evidence": "", "status": "Match | Mismatch | Not found"}],
 "matching_details": [""], "mismatched_details": [""], "explanation": "", "evidence_ids": ["S1"]}
Check these fields: Product Name, Brand, Manufacturer, Model Number, Batch Number.
overall_status must be exactly one of the five listed values.""",

    "risk_assessment": """TASK_NAME: risk_assessment
Assess the potential consumer safety risk using only the evidence and previous findings. Return JSON with exactly these keys:
{"risk_level": "Critical | High | Medium | Low | Unknown", "identified_hazards": [""], "risk_explanation": "",
 "potential_consumer_impact": "", "safety_precautions": [""], "evidence_ids": ["S1"]}
risk_level must be exactly one of the five listed values. Use "Unknown" if the evidence is not enough.""",

    "investigation_report": """TASK_NAME: investigation_report
Combine the previous findings into an investigation report. Return JSON with exactly these keys:
{"investigation_summary": "", "product_details": "", "recall_findings": "", "verification_results": "",
 "risk_assessment": "", "supporting_evidence": [{"evidence_id": "S1", "note": "why this excerpt matters"}],
 "recommendations": [""], "limitations": ""}
Do not add facts that are not in the previous findings or the evidence. Never state that the product is safe
only because no recall was found.""",
}


def as_str(value, default="Not found"):
    if value is None or value == "" or value == []:
        return default
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    return str(value)


def as_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [as_str(v) for v in value if v not in (None, "")]
    return [str(value)]


def as_bool(value):
    return value is True or str(value).strip().lower() in ("true", "yes", "1")


def pick(value, allowed, default):
    for a in allowed:
        if str(value).strip().lower() == a.lower():
            return a
    return default


def valid_ids(ids, evidence):
    known = {e["id"] for e in evidence}
    return [i for i in as_list(ids) if i in known]


def run_task(task_key, product, evidence, previous=None):
    user = f"PRODUCT DETAILS PROVIDED BY THE USER (JSON):\n{json.dumps(product, ensure_ascii=False)}\n\n"
    if previous:
        user += f"PREVIOUS FINDINGS (JSON):\n{json.dumps(previous, ensure_ascii=False)}\n\n"
    user += f"EVIDENCE EXCERPTS FROM UPLOADED DOCUMENTS:\n{evidence_text(evidence) or '(none)'}\n\n{TASKS[task_key]}"
    return nexus_json(SYSTEM_RULES, user)


# FUNCTION 1 -----------------------------------------------------------------
def ai_document_analysis(product, evidence):
    raw = run_task("document_analysis", product, evidence)
    out = {k: as_str(raw.get(k)) for k in ["product_name", "brand", "manufacturer", "model_number",
                                          "batch_number", "serial_number", "manufacturing_date"]}
    out["safety_warnings"] = as_list(raw.get("safety_warnings"))
    out["summary"] = as_str(raw.get("summary"), "")
    out["evidence_ids"] = valid_ids(raw.get("evidence_ids"), evidence)
    return out


# FUNCTION 2 -----------------------------------------------------------------
def ai_recall_investigation(product, evidence, analysis):
    raw = run_task("recall_investigation", product, evidence, {"document_analysis": analysis})
    return {
        "recall_found_in_documents": as_bool(raw.get("recall_found_in_documents")),
        "recall_information": as_str(raw.get("recall_information")),
        "recall_date": as_str(raw.get("recall_date")),
        "recall_reason": as_str(raw.get("recall_reason")),
        "affected_models": as_list(raw.get("affected_models")),
        "affected_batches": as_list(raw.get("affected_batches")),
        "safety_hazards": as_list(raw.get("safety_hazards")),
        "note": as_str(raw.get("note"), ""),
        "evidence_ids": valid_ids(raw.get("evidence_ids"), evidence),
        "source_type": "Uploaded-document finding (AI-generated). This is NOT an official recall record.",
    }


# FUNCTION 3 -----------------------------------------------------------------
def ai_product_verification(product, evidence, analysis=None):
    raw = run_task("product_verification", product, evidence, {"document_analysis": analysis} if analysis else None)
    fields = []
    for f in raw.get("fields", []) if isinstance(raw.get("fields"), list) else []:
        if isinstance(f, dict):
            fields.append({"field": as_str(f.get("field")), "provided": as_str(f.get("provided")),
                           "found_in_evidence": as_str(f.get("found_in_evidence")),
                           "status": pick(f.get("status"), ["Match", "Mismatch", "Not found"], "Not found")})
    return {
        "overall_status": pick(raw.get("overall_status"), VERIFY_STATUSES, "Insufficient Evidence"),
        "fields": fields,
        "matching_details": as_list(raw.get("matching_details")),
        "mismatched_details": as_list(raw.get("mismatched_details")),
        "explanation": as_str(raw.get("explanation"), ""),
        "evidence_ids": valid_ids(raw.get("evidence_ids"), evidence),
    }


# FUNCTION 4 -----------------------------------------------------------------
def ai_risk_assessment(product, evidence, analysis, recall, verification):
    raw = run_task("risk_assessment", product, evidence,
                   {"document_analysis": analysis, "recall_investigation": recall,
                    "product_verification": verification})
    return {
        "risk_level": pick(raw.get("risk_level"), RISK_LEVELS, "Unknown"),
        "identified_hazards": as_list(raw.get("identified_hazards")),
        "risk_explanation": as_str(raw.get("risk_explanation"), ""),
        "potential_consumer_impact": as_str(raw.get("potential_consumer_impact"), ""),
        "safety_precautions": as_list(raw.get("safety_precautions")),
        "evidence_ids": valid_ids(raw.get("evidence_ids"), evidence),
        "disclaimer": RISK_DISCLAIMER,
    }


# FUNCTION 5 -----------------------------------------------------------------
def ai_investigation_report(product, evidence, analysis, recall, verification, risk):
    raw = run_task("investigation_report", product, evidence,
                   {"document_analysis": analysis, "recall_investigation": recall,
                    "product_verification": verification, "risk_assessment": risk})
    notes = {}
    supporting = raw.get("supporting_evidence")
    ids = []
    for item in supporting if isinstance(supporting, list) else []:
        if isinstance(item, dict):
            eid = str(item.get("evidence_id", "")).strip()
            notes[eid] = as_str(item.get("note"), "")
            ids.append(eid)
        else:
            ids.append(str(item).strip())
    ids = valid_ids(ids, evidence)
    return {
        "investigation_summary": as_str(raw.get("investigation_summary")),
        "product_details": as_str(raw.get("product_details")),
        "recall_findings": as_str(raw.get("recall_findings")),
        "verification_results": as_str(raw.get("verification_results")),
        "risk_assessment": as_str(raw.get("risk_assessment")),
        "supporting_evidence": resolve_sources(ids, evidence, notes),
        "recommendations": as_list(raw.get("recommendations")),
        "limitations": as_str(raw.get("limitations"), ""),
    }


# Extra: RAG question answering over the uploaded documents -------------------
def ai_answer_question(question, evidence):
    user = (f"QUESTION:\n{question}\n\nEVIDENCE EXCERPTS FROM UPLOADED DOCUMENTS:\n{evidence_text(evidence)}\n\n"
            'TASK_NAME: rag_answer\nAnswer the question using only the evidence. Return JSON with exactly these keys:\n'
            '{"information_available": true, "answer": "", "confidence": "High | Medium | Low", "evidence_ids": ["S1"]}\n'
            'If the evidence does not contain the answer, set information_available to false and say so in "answer".')
    raw = nexus_json(SYSTEM_RULES, user)
    return {
        "information_available": as_bool(raw.get("information_available")),
        "answer": as_str(raw.get("answer"), ""),
        "confidence": pick(raw.get("confidence"), ["High", "Medium", "Low"], "Low"),
        "evidence_ids": valid_ids(raw.get("evidence_ids"), evidence),
    }


# ---------------------------------------------------------------------------
# 7. INVESTIGATION RUNNER
# ---------------------------------------------------------------------------
PRODUCT_FIELDS = ["product_name", "brand", "manufacturer", "model_number",
                  "batch_number", "serial_number", "category", "description"]


def clean_product(data):
    out = {}
    for k in PRODUCT_FIELDS:
        limit = 5000 if k == "description" else 300
        out[k] = str((data or {}).get(k) or "").strip()[:limit]
    return out


def product_query(product):
    return " ".join(product.get(k, "") for k in PRODUCT_FIELDS)


def gather_evidence(text_query, limit=10):
    chunks = search_chunks(text_query, limit)
    if not chunks:
        chunks = first_chunks()
    return build_evidence(chunks)


def update_investigation(inv_id, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE investigations SET {cols} WHERE id=?", (*fields.values(), inv_id))


def run_investigation(inv_id, product, evidence):
    """Runs in a background thread so the page can show live progress."""
    results = {"evidence": evidence}
    try:
        update_investigation(inv_id, current_step="1/5 Document Analysis", progress=0)
        results["analysis"] = ai_document_analysis(product, evidence)
        update_investigation(inv_id, progress=20, current_step="2/5 Recall Investigation",
                             results_json=json.dumps(results))

        results["recall"] = ai_recall_investigation(product, evidence, results["analysis"])
        update_investigation(inv_id, progress=40, current_step="3/5 Product Verification",
                             results_json=json.dumps(results))

        results["verification"] = ai_product_verification(product, evidence, results["analysis"])
        update_investigation(inv_id, progress=60, current_step="4/5 Risk Assessment",
                             results_json=json.dumps(results))

        results["risk"] = ai_risk_assessment(product, evidence, results["analysis"],
                                             results["recall"], results["verification"])
        update_investigation(inv_id, progress=80, current_step="5/5 Investigation Report",
                             results_json=json.dumps(results))

        results["report"] = ai_investigation_report(product, evidence, results["analysis"], results["recall"],
                                                    results["verification"], results["risk"])
        save_investigation_results(inv_id, product, results)
    except NexusError as e:
        update_investigation(inv_id, status="Failed", error=str(e), results_json=json.dumps(results))
    except Exception as e:
        update_investigation(inv_id, status="Failed", error=f"Unexpected error: {e}",
                             results_json=json.dumps(results))


def save_investigation_results(inv_id, product, results):
    analysis, recall = results["analysis"], results["recall"]
    update_investigation(
        inv_id, status="Completed", progress=100, current_step="Completed",
        risk_level=results["risk"]["risk_level"], verification_status=results["verification"]["overall_status"],
        recall_found=1 if recall["recall_found_in_documents"] else 0,
        results_json=json.dumps(results), completed_at=now())

    def best(user_value, found_value):
        return user_value or ("" if found_value == "Not found" else found_value)

    execute("INSERT INTO products(investigation_id, product_name, brand, manufacturer, model_number, "
            "batch_number, serial_number, category, manufacturing_date, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (inv_id, best(product["product_name"], analysis["product_name"]),
             best(product["brand"], analysis["brand"]), best(product["manufacturer"], analysis["manufacturer"]),
             best(product["model_number"], analysis["model_number"]),
             best(product["batch_number"], analysis["batch_number"]),
             best(product["serial_number"], analysis["serial_number"]), product["category"],
             "" if analysis["manufacturing_date"] == "Not found" else analysis["manufacturing_date"], now()))

    if recall["recall_found_in_documents"]:
        files = sorted({e["filename"] for e in results["evidence"] if e["id"] in recall["evidence_ids"]})
        execute("INSERT INTO recall_records(investigation_id, product_name, recall_date, reason, affected_models, "
                "affected_batches, hazards, source_type, source_file, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (inv_id, product["product_name"], recall["recall_date"], recall["recall_reason"],
                 "; ".join(recall["affected_models"]), "; ".join(recall["affected_batches"]),
                 "; ".join(recall["safety_hazards"]), "Uploaded document (not official)", ", ".join(files), now()))

    report_path = build_pdf(inv_id)
    execute("INSERT INTO investigation_reports(investigation_id, summary, report_json, pdf_path, created_at) "
            "VALUES(?,?,?,?,?)", (inv_id, results["report"]["investigation_summary"],
                                  json.dumps(results["report"]), report_path, now()))


# ---------------------------------------------------------------------------
# 8. PDF REPORT (ReportLab)
# ---------------------------------------------------------------------------
def to_text(value):
    if value is None or value == "":
        return "Not found"
    if isinstance(value, list):
        return "; ".join(to_text(v) for v in value) if value else "None"
    return str(value)


def build_pdf(inv_id):
    inv = query("SELECT * FROM investigations WHERE id=?", (inv_id,), one=True)
    res = json.loads(inv["results_json"] or "{}")
    analysis, recall = res.get("analysis", {}), res.get("recall", {})
    verification, risk, report = res.get("verification", {}), res.get("risk", {}), res.get("report", {})
    evidence = res.get("evidence", [])

    styles = getSampleStyleSheet()
    title = ParagraphStyle("T", parent=styles["Title"], textColor=colors.HexColor("#0b1f3a"), fontSize=20)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], textColor=colors.HexColor("#0369a1"),
                        spaceBefore=12, spaceAfter=4, fontSize=13)
    body = ParagraphStyle("B", parent=styles["BodyText"], fontSize=10, leading=14)
    small = ParagraphStyle("S", parent=body, fontSize=8.5, leading=11)

    def P(text, style=body):
        return Paragraph(escape(to_text(text)).replace("\n", "<br/>"), style)

    def bullets(items):
        return [P("•  " + to_text(i)) for i in items] or [P("None found")]

    def table(rows, widths):
        t = Table([[P(c, small) for c in row] for row in rows], colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0f2fe")),
                               ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#94a3b8")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return t

    story = [Paragraph("PRODUCT SAFETY INTELLIGENCE", title),
             P("Product Recall Investigation Report", styles["Heading3"]), Spacer(1, 6),
             table([["Investigation ID", f"INV-{inv_id:04d}"], ["Investigation Date", inv["created_at"]],
                    ["Status", inv["status"]]], [5 * cm, 12 * cm])]

    story += [Paragraph("1. Product Details", h2), table([
        ["Field", "Provided by user", "Found in documents"],
        ["Product Name", inv["product_name"], analysis.get("product_name")],
        ["Brand", inv["brand"], analysis.get("brand")],
        ["Manufacturer", inv["manufacturer"], analysis.get("manufacturer")],
        ["Model Number", inv["model_number"], analysis.get("model_number")],
        ["Batch Number", inv["batch_number"], analysis.get("batch_number")],
        ["Serial Number", inv["serial_number"], analysis.get("serial_number")],
        ["Manufacturing Date", "", analysis.get("manufacturing_date")],
        ["Category", inv["category"], ""]], [4 * cm, 6.5 * cm, 6.5 * cm])]
    story += [Spacer(1, 4), P(report.get("product_details"))]

    story += [Paragraph("2. Recall Findings", h2),
              P("Source type: " + to_text(recall.get("source_type")), small),
              P("Recall found in uploaded documents: " + ("Yes" if recall.get("recall_found_in_documents") else "No")),
              P("Recall information: " + to_text(recall.get("recall_information"))),
              P("Recall date: " + to_text(recall.get("recall_date"))),
              P("Recall reason: " + to_text(recall.get("recall_reason"))),
              P("Affected models: " + to_text(recall.get("affected_models"))),
              P("Affected batches: " + to_text(recall.get("affected_batches"))),
              P("Safety hazards: " + to_text(recall.get("safety_hazards"))),
              Spacer(1, 4), P(report.get("recall_findings"))]

    story += [Paragraph("3. Verification Results", h2),
              P("Overall status: " + to_text(verification.get("overall_status")))]
    if verification.get("fields"):
        story.append(table([["Field", "Provided", "Found in evidence", "Result"]] +
                           [[f["field"], f["provided"], f["found_in_evidence"], f["status"]]
                            for f in verification["fields"]], [3.5 * cm, 4.5 * cm, 6 * cm, 3 * cm]))
    story += [Spacer(1, 4), P(verification.get("explanation")), P(report.get("verification_results"))]

    story += [Paragraph("4. Risk Assessment", h2), P("Risk level: " + to_text(risk.get("risk_level"))),
              P("Identified hazards:")] + bullets(risk.get("identified_hazards", [])) + [
              P("Explanation: " + to_text(risk.get("risk_explanation"))),
              P("Potential consumer impact: " + to_text(risk.get("potential_consumer_impact"))),
              P("Safety precautions:")] + bullets(risk.get("safety_precautions", [])) + [
              Spacer(1, 4), P(RISK_DISCLAIMER, small)]

    story += [Paragraph("5. Supporting Evidence", h2)]
    supp = report.get("supporting_evidence", [])
    story.append(table([["ID", "File", "Page", "Why it matters / excerpt"]] +
                       [[s["id"], s["filename"], s["page"] or "n/a", (s.get("note") or "") + "\n" + s["excerpt"][:300]]
                        for s in supp], [1.2 * cm, 4 * cm, 1.5 * cm, 10.3 * cm]) if supp else P("No specific excerpts were cited."))

    story += [Paragraph("6. Source References", h2)]
    story.append(table([["ID", "File", "Page"]] + [[e["id"], e["filename"], e["page"] or "n/a"] for e in evidence],
                       [2 * cm, 11 * cm, 4 * cm]) if evidence else P("No sources."))

    story += [Paragraph("7. Investigation Summary", h2), P(report.get("investigation_summary")),
              Paragraph("8. Recommendations", h2)] + bullets(report.get("recommendations", [])) + [
              Paragraph("9. Limitations and Disclaimer", h2), P(report.get("limitations")), P(GENERAL_DISCLAIMER, small)]

    path = os.path.join(REPORT_DIR, f"investigation_{inv_id}.pdf")
    SimpleDocTemplate(path, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm,
                      bottomMargin=2 * cm, title=f"Investigation INV-{inv_id:04d}").build(story)
    return path


# ---------------------------------------------------------------------------
# 9. WEB ROUTES
# ---------------------------------------------------------------------------
def error(message, code=400):
    return jsonify({"error": message}), code


def investigation_dict(row, full=False):
    d = {k: row[k] for k in ["id", "product_name", "brand", "manufacturer", "model_number", "batch_number",
                             "serial_number", "category", "description", "status", "progress", "current_step",
                             "risk_level", "verification_status", "recall_found", "error", "created_at",
                             "completed_at"]}
    if full:
        d["results"] = json.loads(row["results_json"] or "{}")
    return d


@app.get("/")
def index():
    return render_template("index.html")


# ---- Dashboard -------------------------------------------------------------
@app.get("/api/dashboard")
def api_dashboard():
    def count(sql):
        return query(sql, one=True)["c"]

    stats = {
        "total_investigations": count("SELECT COUNT(*) c FROM investigations"),
        "documents_uploaded": count("SELECT COUNT(*) c FROM documents"),
        "products_identified": count("SELECT COUNT(*) c FROM products"),
        "recall_alerts": count("SELECT COUNT(*) c FROM recall_records"),
        "high_risk": count("SELECT COUNT(*) c FROM investigations WHERE risk_level IN ('Critical','High')"),
        "verified": count("SELECT COUNT(*) c FROM investigations WHERE verification_status='Verified'"),
    }
    dist = {lvl: 0 for lvl in RISK_LEVELS}
    for r in query("SELECT risk_level, COUNT(*) c FROM investigations WHERE status='Completed' GROUP BY risk_level"):
        if r["risk_level"] in dist:
            dist[r["risk_level"]] = r["c"]
    return jsonify({
        "stats": stats, "risk_distribution": dist,
        "history": [investigation_dict(r) for r in
                    query("SELECT * FROM investigations ORDER BY id DESC LIMIT 8")],
        "recalls": [dict(r) for r in query("SELECT * FROM recall_records ORDER BY id DESC LIMIT 8")],
        "documents": [dict(r) for r in
                      query("SELECT id, filename, file_type, status, uploaded_at FROM documents ORDER BY id DESC LIMIT 6")],
    })


# ---- Documents ---------------------------------------------------------------
@app.get("/api/documents")
def api_documents():
    return jsonify([dict(r) for r in query(
        "SELECT id, filename, file_type, pages, chunk_count, status, error, uploaded_at "
        "FROM documents ORDER BY id DESC")])


@app.post("/api/documents/upload")
def api_upload():
    files = request.files.getlist("files")
    if not files or all(not f.filename for f in files):
        return error("Choose at least one PDF, DOCX or TXT file.")
    results = []
    for f in files:
        if not f.filename:
            continue
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
        if ext not in ALLOWED_EXTENSIONS:
            results.append({"filename": f.filename, "status": "Rejected", "error": "Only PDF, DOCX and TXT are allowed."})
            continue
        stored = f"{uuid.uuid4().hex}.{ext}"
        path = os.path.join(UPLOAD_DIR, stored)
        f.save(path)
        doc_id = execute("INSERT INTO documents(filename, stored_name, file_type, status, uploaded_at) "
                         "VALUES(?,?,?,?,?)", (os.path.basename(f.filename), stored, ext.upper(), "Processing", now()))
        process_document(doc_id, path, ext)
        row = query("SELECT status, error FROM documents WHERE id=?", (doc_id,), one=True)
        results.append({"filename": f.filename, "status": row["status"], "error": row["error"]})
    return jsonify({"results": results})


@app.delete("/api/documents/<int:doc_id>")
def api_delete_document(doc_id):
    row = query("SELECT stored_name FROM documents WHERE id=?", (doc_id,), one=True)
    if not row:
        return error("Document not found.", 404)
    execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
    execute("DELETE FROM documents WHERE id=?", (doc_id,))
    try:
        os.remove(os.path.join(UPLOAD_DIR, row["stored_name"]))
    except OSError:
        pass
    return jsonify({"ok": True})


# ---- Investigations ------------------------------------------------------------
@app.post("/api/investigations")
def api_start_investigation():
    problem = require_ready()
    if problem:
        return error(problem)
    product = clean_product(request.get_json(silent=True))
    if not product["product_name"]:
        return error("Product Name is required.")
    if not any(product[k] for k in PRODUCT_FIELDS if k != "product_name"):
        return error("Add at least one more detail (brand, model, batch, category or description).")
    if query("SELECT COUNT(*) c FROM documents WHERE status='Processed'", one=True)["c"] == 0:
        return error("Upload at least one document before starting an investigation.")
    evidence = gather_evidence(product_query(product))
    if not evidence:
        return error("No readable document text was found.")
    inv_id = execute(
        "INSERT INTO investigations(product_name, brand, manufacturer, model_number, batch_number, serial_number, "
        "category, description, status, progress, current_step, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (product["product_name"], product["brand"], product["manufacturer"], product["model_number"],
         product["batch_number"], product["serial_number"], product["category"], product["description"],
         "Running", 0, "Retrieving evidence", now()))
    threading.Thread(target=run_investigation, args=(inv_id, product, evidence), daemon=True).start()
    return jsonify({"id": inv_id})


@app.get("/api/investigations")
def api_list_investigations():
    return jsonify([investigation_dict(r) for r in query("SELECT * FROM investigations ORDER BY id DESC")])


@app.get("/api/investigations/<int:inv_id>")
def api_get_investigation(inv_id):
    row = query("SELECT * FROM investigations WHERE id=?", (inv_id,), one=True)
    if not row:
        return error("Investigation not found.", 404)
    return jsonify(investigation_dict(row, full=True))


@app.get("/api/investigations/<int:inv_id>/pdf")
def api_pdf(inv_id):
    row = query("SELECT status FROM investigations WHERE id=?", (inv_id,), one=True)
    if not row:
        return error("Investigation not found.", 404)
    if row["status"] != "Completed":
        return error("The report is available after the investigation is completed.")
    path = os.path.join(REPORT_DIR, f"investigation_{inv_id}.pdf")
    if not os.path.exists(path):
        path = build_pdf(inv_id)
    return send_file(path, as_attachment=True, download_name=f"investigation_INV-{inv_id:04d}.pdf",
                     mimetype="application/pdf")


# ---- Recall search -----------------------------------------------------------------
@app.get("/api/sources")
def api_sources():
    return jsonify(recall_sources())


@app.post("/api/recall-search")
def api_recall_search():
    data = request.get_json(silent=True) or {}
    fields = {k: str(data.get(k) or "").strip()[:300] for k in ["product_name", "brand", "manufacturer", "category"]}
    text_query = " ".join(fields.values())
    if not text_query.strip():
        return error("Enter at least one search term.")
    toks = tokens(text_query)
    matches = []
    for c in search_chunks(text_query, 10):
        matches.append({"filename": c["filename"], "page": c["page"], "coverage": c["coverage"],
                        "snippet": snippet(c["text"], toks)})
    like = [f"%{v}%" for v in fields.values() if v]
    clauses = " OR ".join(["product_name LIKE ? OR reason LIKE ?"] * len(like))
    args = [x for v in like for x in (v, v)]
    saved = [dict(r) for r in query(f"SELECT * FROM recall_records WHERE {clauses} ORDER BY id DESC LIMIT 10", args)]
    return jsonify({
        "uploaded_document_matches": matches, "saved_recall_findings": saved,
        "official_sources": recall_sources(), "search_text": text_query,
        "limitation": ("Live search of official databases (US CPSC, US FDA, EU Safety Gate) is not built into this "
                       "application. Open the official sites below and search there. Matches above come only from "
                       "your uploaded documents and are not official recall records."),
    })


# ---- Verification -----------------------------------------------------------------------
@app.post("/api/verify")
def api_verify():
    problem = require_ready()
    if problem:
        return error(problem)
    product = clean_product(request.get_json(silent=True))
    if not product["product_name"]:
        return error("Product Name is required.")
    if query("SELECT COUNT(*) c FROM documents WHERE status='Processed'", one=True)["c"] == 0:
        return error("Upload at least one document so there is evidence to compare against.")
    evidence = gather_evidence(product_query(product))
    try:
        result = ai_product_verification(product, evidence)
    except NexusError as e:
        return error(str(e), 502)
    result["sources"] = resolve_sources(result["evidence_ids"], evidence)
    result["note"] = "Finding no matching record does not mean the product is safe."
    return jsonify(result)


# ---- Ask the documents (RAG) ---------------------------------------------------------------
@app.post("/api/ask")
def api_ask():
    problem = require_ready()
    if problem:
        return error(problem)
    question = str((request.get_json(silent=True) or {}).get("question") or "").strip()[:1000]
    if not question:
        return error("Type a question first.")
    chunks = search_chunks(question, 6)
    if not chunks:
        return jsonify({"answer": "No relevant information was found in the uploaded documents.",
                        "confidence": "None", "information_available": False, "cited": [], "retrieved": []})
    evidence = build_evidence(chunks)
    try:
        result = ai_answer_question(question, evidence)
    except NexusError as e:
        return error(str(e), 502)
    top = max(e["coverage"] for e in evidence)
    return jsonify({
        "answer": result["answer"], "confidence": result["confidence"],
        "information_available": result["information_available"],
        "retrieval_match": round(top * 100),
        "cited": resolve_sources(result["evidence_ids"], evidence),
        "retrieved": resolve_sources([e["id"] for e in evidence], evidence),
    })


# ---- Settings ----------------------------------------------------------------------------------
@app.get("/api/settings")
def api_settings():
    """Never returns the key itself, only whether one is configured."""
    return jsonify({"api_key_configured": bool(nexus_key()), "base_url": nexus_base_url(),
                    "model": current_model(), "ready": require_ready() is None})


@app.post("/api/settings/test")
def api_test_connection():
    try:
        models = list_models()
    except NexusError as e:
        return jsonify({"ok": False, "error": str(e), "model": current_model(), "models": []})
    model = current_model()
    return jsonify({"ok": True, "models": models, "model": model,
                    "model_valid": model in models if model else False})


@app.post("/api/settings/model")
def api_set_model():
    model = str((request.get_json(silent=True) or {}).get("model") or "").strip()
    if not model:
        return error("Choose a model.")
    try:
        if model not in list_models():
            return error("That model is not available for your API key.")
    except NexusError as e:
        return error(str(e), 502)
    set_setting("nexus_model", model)
    return jsonify({"ok": True, "model": model})


init_db()

if __name__ == "__main__":
    print("Open http://127.0.0.1:5000 in your browser")
    app.run(host="127.0.0.1", port=5000, debug=False)
