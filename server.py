"""
HireRight ETL Agent — Full Server
Handles PDF, Excel, bulk upload, structure detection, agent routing
"""
import os, json, re, uuid, datetime, tempfile, hashlib, time
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
import requests as http_requests

# PDF extraction
try:
    from pypdf import PdfReader
    PDF_OK = True
except: PDF_OK = False

# Excel extraction
try:
    import pandas as pd
    import openpyxl
    XL_OK = True
except: XL_OK = False

app = Flask(__name__, static_folder='static_agent')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max

ALLOWED_EXTENSIONS = {'.pdf', '.xlsx', '.xls', '.csv', '.txt'}
MAX_FILE_SIZE_MB = 50

# ── Text extraction from files ────────────────────────────────────────────────

def extract_text_from_pdf(filepath):
    """Extract text from PDF. Returns (text, page_count, is_scanned)"""
    if not PDF_OK:
        return "", 0, False
    try:
        reader = PdfReader(filepath)
        pages = []
        for page in reader.pages:
            t = page.extract_text() or ""
            pages.append(t)
        full_text = "\n".join(pages)
        # If very little text extracted, likely scanned
        is_scanned = len(full_text.strip()) < 100 and len(reader.pages) > 0
        return full_text, len(reader.pages), is_scanned
    except Exception as e:
        return f"PDF_ERROR: {str(e)}", 0, False

def extract_text_from_excel(filepath):
    """Extract text from Excel/CSV. Returns (text, sheet_count)"""
    if not XL_OK:
        return "", 0
    try:
        ext = Path(filepath).suffix.lower()
        if ext == '.csv':
            df = pd.read_csv(filepath)
            rows = []
            for _, row in df.iterrows():
                parts = [f"{col}: {val}" for col, val in row.items()
                        if pd.notna(val) and str(val).strip()]
                if parts: rows.append(" | ".join(parts))
            return "\n".join(rows), 1
        else:
            xl = pd.ExcelFile(filepath)
            all_text = []
            for sheet in xl.sheet_names:
                df = pd.read_excel(filepath, sheet_name=sheet)
                all_text.append(f"=== Sheet: {sheet} ===")
                for _, row in df.iterrows():
                    parts = [f"{col}: {val}" for col, val in row.items()
                            if pd.notna(val) and str(val).strip()]
                    if parts: all_text.append(" | ".join(parts))
            return "\n".join(all_text), len(xl.sheet_names)
    except Exception as e:
        return f"EXCEL_ERROR: {str(e)}", 0

def read_text_file(filepath):
    encodings = ['utf-8', 'latin-1', 'cp1252']
    for enc in encodings:
        try:
            with open(filepath, encoding=enc) as f:
                return f.read()
        except: continue
    return ""

# ── Structure analyser ────────────────────────────────────────────────────────

def analyse_structure(text, filename):
    """
    Analyse document structure and decide extraction path.
    Returns detailed analysis + recommendation.
    """
    if not text or len(text.strip()) < 20:
        return {"error": "insufficient_text", "recommended_extractor": "llm",
                "structure_score": 0}

    lines = [l.strip() for l in text.split('\n') if l.strip()]
    tl = text.lower()

    # Structural signals
    colon_lines = [l for l in lines if ':' in l and len(l) < 200]
    label_lines = [l for l in lines if re.match(
        r'^(?:Client|Company|Customer|Effective|Start|End|Date|Value|Package|'
        r'Price|Product|Total|Name|ACV|TAT|Includes|Discount|Annual|Contract|'
        r'Number|Checks|Turnaround|Unit|Validity|Valid)', l, re.I)]
    table_lines = [l for l in lines if '\t' in l or re.search(r'\s{3,}', l)]
    narrative_lines = [l for l in lines if len(l) > 150]
    legal_phrases = ['hereinafter', 'whereas', 'notwithstanding',
                     'pursuant to', 'in witness whereof', 'party of the first part',
                     'further to our', 'i am pleased to confirm', 'as discussed']
    legal_count = sum(1 for p in legal_phrases if p in tl)
    currency_hits = len(re.findall(r'(?:USD|INR|\$|Rs\.?|EUR|GBP)\s*[\d,]+', text))
    date_hits = len(re.findall(
        r'\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|'
        r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}',
        text, re.I))

    # Score calculation
    score = 0
    score += min(len(colon_lines) * 6, 30)   # up to 30 pts
    score += min(len(label_lines) * 8, 35)   # up to 35 pts
    score += min(len(table_lines) * 5, 15)   # up to 15 pts
    score += min(currency_hits * 5, 10)      # up to 10 pts
    score += min(date_hits * 5, 10)          # up to 10 pts
    score -= min(len(narrative_lines) * 4, 20)  # penalty
    score -= min(legal_count * 5, 15)           # penalty
    score = max(0, min(100, score))

    # Document type signals
    fc = sum(1 for k in ["contract","agreement","effective date","acv",
                          "annual contract value","hereinafter","client name",
                          "commencement"] if k in tl)
    ps = sum(1 for k in ["unit price","per unit","discount","quote",
                          "pricing","rate card","price list"] if k in tl)
    pk = sum(1 for k in ["package","plan","includes","turnaround","tat",
                          "check types","components","background check"] if k in tl)

    fn = filename.lower()
    if any(k in fn for k in ["contract","agreement","msa","sow"]): fc += 5
    if any(k in fn for k in ["pricing","price","quote","rate"]): ps += 5
    if any(k in fn for k in ["package","plan","config"]): pk += 5

    type_scores = {"financial_contract": fc, "pricing_sheet": ps, "package_config": pk}
    best_type = max(type_scores, key=type_scores.get)
    type_conf = min(type_scores[best_type] / 6.0, 1.0)

    # Decision
    if score >= 50 and type_conf >= 0.4:
        rec = "regex"
        reason = f"Well-structured document (score {score}/100) with {len(label_lines)} labelled fields and clear {best_type.replace('_',' ')} pattern. Regex extraction will be reliable."
    elif score >= 30 and type_conf >= 0.3:
        rec = "regex"
        reason = f"Moderately structured (score {score}/100). Regex will handle main fields; LLM may catch any misses."
    else:
        rec = "llm"
        reason = f"Low structure score ({score}/100) with {len(narrative_lines)} narrative lines and {legal_count} legal phrases. LLM extraction needed for reliable results."

    return {
        "structure_score": score,
        "colon_pairs": len(colon_lines),
        "labelled_fields": len(label_lines),
        "narrative_lines": len(narrative_lines),
        "legal_phrases": legal_count,
        "currency_hits": currency_hits,
        "date_hits": date_hits,
        "likely_type": best_type,
        "type_confidence": round(type_conf, 2),
        "type_scores": type_scores,
        "recommended_extractor": rec,
        "reason": reason,
        "text_length": len(text),
        "line_count": len(lines)
    }

# ── Regex extraction ──────────────────────────────────────────────────────────

TEMPLATES = {
    "financial_contract": [
        {"name":"client_name","label":"Client Legal Name","mandatory":True,"type":"text",
         "patterns":[r"(?:Client|Company|Customer)\s*(?:Name|Legal Name)?\s*[:\-]\s*([A-Z][A-Za-z\s&.,]+(?:Inc|LLC|Ltd|Corp|Limited)?)",
                     r"(?:between|BETWEEN)\s+[A-Z][A-Za-z\s]+(?:Inc|LLC|Ltd|Corp)?\s+(?:and|AND)\s+([A-Z][A-Za-z\s&]+(?:Inc|LLC|Ltd|Corp)?)"]},
        {"name":"acv","label":"Annual Contract Value","mandatory":True,"type":"currency",
         "patterns":[r"(?:Annual Contract Value|ACV|Total Annual Value)\s*[:\-]?\s*(?:USD|\$|INR|Rs\.?)?\s*([\d,]+(?:\.\d{1,2})?)",
                     r"(?:Total Value|Contract Value|Annual Fee)\s*[:\-]?\s*(?:USD|\$|INR)?\s*([\d,]+(?:\.\d{1,2})?)"]},
        {"name":"start_date","label":"Start Date","mandatory":True,"type":"date",
         "patterns":[r"(?:Start Date|Effective Date|Commencement Date)\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
                     r"(?:Start Date|Effective Date|Commencement Date)\s*[:\-]?\s*((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})"]},
        {"name":"end_date","label":"End Date","mandatory":False,"type":"date",
         "patterns":[r"(?:End Date|Expiry Date|Termination Date)\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
                     r"(?:End Date|Expiry Date)\s*[:\-]?\s*((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})"]},
        {"name":"package","label":"Package Name","mandatory":False,"type":"text",
         "patterns":[r"Package\s*[:\-]\s*([A-Za-z][A-Za-z\s]+(?:Plus|Pro|Premium|Basic|Standard|Enterprise|Essential)?)"]},
        {"name":"num_checks","label":"Number of Checks","mandatory":False,"type":"number",
         "patterns":[r"(?:Number of (?:Checks|Screenings)|Total (?:Checks|Screenings))\s*[:\-]?\s*([\d,]+)",
                     r"([\d,]+)\s+(?:background checks|screenings|checks per year)"]},
    ],
    "pricing_sheet": [
        {"name":"product_name","label":"Product Name","mandatory":True,"type":"text",
         "patterns":[r"(?:Product|Service|Item)\s*[:\-]\s*([A-Za-z][A-Za-z\s]+)"]},
        {"name":"unit_price","label":"Unit Price","mandatory":True,"type":"currency",
         "patterns":[r"(?:Unit Price|Price per Unit|Per Unit)\s*[:\-]?\s*(?:USD|\$|INR|Rs\.?)?\s*([\d,]+(?:\.\d{1,2})?)",
                     r"Price\s*[:\-]?\s*(?:USD|\$)?\s*([\d,]+(?:\.\d{1,2})?)"]},
        {"name":"discount","label":"Discount %","mandatory":False,"type":"percentage",
         "patterns":[r"(?:Discount|Rebate)\s*[:\-]?\s*([\d.]+)\s*%"]},
        {"name":"validity","label":"Valid Until","mandatory":False,"type":"text",
         "patterns":[r"(?:Valid Until|Validity|Quote Valid|Expires)\s*[:\-]?\s*(.+?)(?:\n|$)"]},
    ],
    "package_config": [
        {"name":"package_name","label":"Package Name","mandatory":True,"type":"text",
         "patterns":[r"(?:Package Name|Package|Plan Name|Plan)\s*[:\-]\s*([A-Za-z][A-Za-z\s]+)"]},
        {"name":"included_checks","label":"Included Check Types","mandatory":True,"type":"text",
         "patterns":[r"(?:Includes?|Contains?|Check Types?|Components?)\s*[:\-]\s*(.+?)(?:\n[A-Z]|\n\n|$)"]},
        {"name":"tat","label":"Turnaround Time","mandatory":False,"type":"text",
         "patterns":[r"(?:Turnaround Time|TAT|Delivery Time)\s*[:\-]?\s*(\d+\s*(?:business\s*)?days?|\d+\s*hours?)"]},
        {"name":"price_per_check","label":"Price Per Check","mandatory":False,"type":"currency",
         "patterns":[r"(?:Price per Check|Cost per Check|Per Check Price)\s*[:\-]?\s*(?:\$|USD|INR)?\s*([\d,]+(?:\.\d{1,2})?)"]},
    ]
}

def parse_val(raw, ftype):
    if not raw: return None, 0.0
    raw = raw.strip().rstrip('.,;').strip()
    if ftype == "currency":
        c = re.sub(r'[,\s]','',raw); c = re.sub(r'^[^\d]*','',c)
        try: return f"{float(c):,.2f}", 0.95
        except: return raw, 0.60
    if ftype == "date":
        months = {'january':'01','february':'02','march':'03','april':'04',
                  'may':'05','june':'06','july':'07','august':'08',
                  'september':'09','october':'10','november':'11','december':'12'}
        for mn, num in months.items():
            if mn in raw.lower():
                nums = re.findall(r'\d+', raw)
                yr = next((n for n in nums if len(n)==4), None)
                dy = next((n for n in nums if int(n)<=31 and len(n)<=2), None)
                if yr and dy: return f"{yr}-{num}-{dy.zfill(2)}", 0.90
        m = re.match(r'(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})', raw)
        if m:
            d,mo,y = m.groups()
            return f"{'20'+y if len(y)==2 else y}-{mo.zfill(2)}-{d.zfill(2)}", 0.85
        return raw, 0.70
    if ftype == "number":
        try: return str(int(float(raw.replace(',','').strip()))), 0.95
        except: return raw, 0.60
    if ftype == "percentage":
        m = re.search(r'([\d.]+)', raw)
        return (f"{m.group(1)}%", 0.95) if m else (raw, 0.7)
    return raw.replace('\n',' ').strip()[:100], 0.82

def do_regex_extract(text, doc_type):
    tmpl = TEMPLATES.get(doc_type, TEMPLATES["financial_contract"])
    fields = []
    for field in tmpl:
        bv, bc = None, 0.0
        for i, pat in enumerate(field["patterns"]):
            m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
            if m:
                raw = m.group(1) if m.lastindex else m.group(0)
                v, c = parse_val(raw, field["type"])
                adj = c * (1.0 if i==0 else 0.9)
                if v and adj > bc: bv, bc = v, adj
        fields.append({"name": field["name"], "label": field["label"],
                       "value": bv, "confidence": round(bc,3),
                       "mandatory": field["mandatory"], "method": "regex"})
    return fields

def do_llm_extract(text, doc_type, api_key):
    try:
        resp = http_requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1200,
                "system": "Extract document data. Return ONLY valid JSON, no markdown.",
                "messages": [{"role": "user", "content": f"""Extract these fields. JSON only:
{{"document_type":"financial_contract|pricing_sheet|package_config",
"fields":{{"client_name":{{"value":"or null","quote":"source text","confidence":0.9}},
"acv":{{"value":"number string or null","quote":"...","confidence":0.9}},
"start_date":{{"value":"YYYY-MM-DD or null","quote":"...","confidence":0.9}},
"end_date":{{"value":"YYYY-MM-DD or null","quote":"...","confidence":0.9}},
"package":{{"value":"or null","quote":"...","confidence":0.85}},
"num_checks":{{"value":"integer string or null","quote":"...","confidence":0.9}}}}}}

Document:
{text[:4000]}"""}]
            }, timeout=40)
        raw = resp.json()["content"][0]["text"].replace("```json","").replace("```","").strip()
        parsed = json.loads(raw)
        label_map = {"client_name":"Client Legal Name","acv":"Annual Contract Value",
                     "start_date":"Start Date","end_date":"End Date",
                     "package":"Package Name","num_checks":"Number of Checks"}
        fields = []
        for k, label in label_map.items():
            fd = parsed.get("fields",{}).get(k,{})
            fields.append({"name":k,"label":label,"value":fd.get("value"),
                          "quote":fd.get("quote"),"confidence":fd.get("confidence",0),
                          "mandatory":k in ["client_name","acv","start_date"],"method":"llm"})
        return fields, parsed.get("document_type", doc_type)
    except Exception as e:
        return [], doc_type

def compute_routing(fields):
    mand = [f for f in fields if f.get("mandatory")]
    miss = [f for f in mand if not f.get("value")]
    if miss:
        return {"routing":"full_review","score":0,"band":"low",
                "reason":f"Missing mandatory fields: {[f['label'] for f in miss]}"}
    scores = ([f["confidence"] for f in mand]*2 +
              [f["confidence"] for f in fields if not f.get("mandatory") and f.get("value")])
    comp = sum(scores)/len(scores) if scores else 0
    r = round(comp,3)
    if comp >= 0.88: return {"routing":"auto_accept","score":r,"band":"high",
                              "reason":f"All fields high confidence ({round(comp*100)}%)"}
    if comp >= 0.70: return {"routing":"partial_review","score":r,"band":"medium",
                              "reason":f"Composite {round(comp*100)}% — some fields need check"}
    return {"routing":"full_review","score":r,"band":"low",
            "reason":f"Low confidence {round(comp*100)}% — manual review required"}

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('static_agent', 'index.html')

@app.route('/api/upload', methods=['POST'])
def upload():
    """Single file upload — analyse + extract"""
    api_key = request.headers.get('X-API-Key','')
    file = request.files.get('file')
    if not file: return jsonify({"error":"No file provided"}), 400

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return jsonify({"error":f"File type {ext} not supported. Use: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

    # Check size
    file.seek(0, 2)
    size_mb = file.tell() / (1024*1024)
    file.seek(0)
    if size_mb > MAX_FILE_SIZE_MB:
        return jsonify({"error":f"File too large ({size_mb:.1f}MB). Max: {MAX_FILE_SIZE_MB}MB"}), 400

    # Save temp
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    file.save(tmp.name); tmp.close()

    try:
        # Extract text
        if ext == '.pdf':
            text, pages, is_scanned = extract_text_from_pdf(tmp.name)
            meta = {"pages": pages, "is_scanned": is_scanned}
            if is_scanned:
                return jsonify({"error":"This PDF appears to be a scanned image. Text extraction requires OCR which needs the full server setup. Please use a text-based PDF or copy-paste the text directly."}), 422
        elif ext in ['.xlsx','.xls','.csv']:
            text, sheets = extract_text_from_excel(tmp.name)
            meta = {"sheets": sheets}
        else:
            text = read_text_file(tmp.name)
            meta = {}

        if not text or text.startswith(('PDF_ERROR','EXCEL_ERROR')):
            return jsonify({"error":f"Could not extract text: {text}"}), 422

        # Analyse structure — Claude will use this to decide path
        analysis = analyse_structure(text, file.filename)
        doc_type = analysis["likely_type"]
        rec = analysis["recommended_extractor"]

        # Extract based on recommendation
        quote_map = {}
        if rec == "regex" or not api_key:
            fields = do_regex_extract(text, doc_type)
            method_used = "regex"
        else:
            fields, doc_type = do_llm_extract(text, doc_type, api_key)
            method_used = "llm"
            if not fields:  # LLM failed, fallback to regex
                fields = do_regex_extract(text, doc_type)
                method_used = "regex_fallback"

        routing = compute_routing(fields)

        return jsonify({
            "doc_id": f"DOC-{uuid.uuid4().hex[:8].upper()}",
            "filename": file.filename,
            "file_type": ext.lstrip('.').upper(),
            "size_mb": round(size_mb, 2),
            "meta": meta,
            "text_preview": text[:300] + "..." if len(text) > 300 else text,
            "structure_analysis": analysis,
            "method_used": method_used,
            "doc_type": doc_type,
            "doc_type_display": doc_type.replace("_"," ").title(),
            "fields": fields,
            "routing": routing,
            "processed_at": datetime.datetime.now().isoformat()
        })
    finally:
        os.unlink(tmp.name)

@app.route('/api/bulk', methods=['POST'])
def bulk_upload():
    """Bulk upload — process multiple files, return summary"""
    api_key = request.headers.get('X-API-Key','')
    files = request.files.getlist('files')
    if not files: return jsonify({"error":"No files provided"}), 400
    if len(files) > 20: return jsonify({"error":"Max 20 files per batch"}), 400

    results = []
    for file in files:
        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            results.append({"filename":file.filename,"status":"skipped",
                            "error":f"Unsupported type: {ext}"})
            continue

        file.seek(0,2); size_mb = file.tell()/(1024*1024); file.seek(0)
        if size_mb > MAX_FILE_SIZE_MB:
            results.append({"filename":file.filename,"status":"skipped",
                            "error":f"Too large: {size_mb:.1f}MB"})
            continue

        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        file.save(tmp.name); tmp.close()

        try:
            if ext == '.pdf':
                text, pages, is_scanned = extract_text_from_pdf(tmp.name)
                if is_scanned:
                    results.append({"filename":file.filename,"status":"skipped",
                                   "error":"Scanned PDF — OCR required"}); continue
            elif ext in ['.xlsx','.xls','.csv']:
                text, _ = extract_text_from_excel(tmp.name)
            else:
                text = read_text_file(tmp.name)

            if not text or text.startswith(('PDF_ERROR','EXCEL_ERROR')):
                results.append({"filename":file.filename,"status":"error",
                               "error":text}); continue

            analysis = analyse_structure(text, file.filename)
            doc_type = analysis["likely_type"]

            if analysis["recommended_extractor"] == "regex" or not api_key:
                fields = do_regex_extract(text, doc_type)
                method = "regex"
            else:
                fields, doc_type = do_llm_extract(text, doc_type, api_key)
                method = "llm"
                if not fields:
                    fields = do_regex_extract(text, doc_type)
                    method = "regex_fallback"

            routing = compute_routing(fields)
            results.append({
                "filename": file.filename,
                "status": "processed",
                "doc_type": doc_type.replace("_"," ").title(),
                "method_used": method,
                "structure_score": analysis["structure_score"],
                "fields_found": sum(1 for f in fields if f.get("value")),
                "fields_total": len(fields),
                "routing": routing["routing"],
                "composite_score": routing["score"],
                "size_mb": round(size_mb,2),
                "fields": fields
            })
        except Exception as e:
            results.append({"filename":file.filename,"status":"error","error":str(e)})
        finally:
            os.unlink(tmp.name)

    processed = [r for r in results if r.get("status")=="processed"]
    auto = [r for r in processed if r.get("routing")=="auto_accept"]
    review = [r for r in processed if r.get("routing")!="auto_accept"]

    return jsonify({
        "batch_id": f"BATCH-{uuid.uuid4().hex[:6].upper()}",
        "total_files": len(files),
        "processed": len(processed),
        "skipped": len([r for r in results if r.get("status")=="skipped"]),
        "errors": len([r for r in results if r.get("status")=="error"]),
        "auto_accepted": len(auto),
        "needs_review": len(review),
        "results": results,
        "processed_at": datetime.datetime.now().isoformat()
    })

@app.route('/api/limits', methods=['GET'])
def limits():
    return jsonify({
        "max_file_size_mb": MAX_FILE_SIZE_MB,
        "max_bulk_files": 20,
        "supported_formats": list(ALLOWED_EXTENSIONS),
        "pdf_support": PDF_OK,
        "excel_support": XL_OK,
        "note": "Scanned PDFs require OCR setup (Tesseract). Text-based PDFs work directly."
    })

if __name__ == '__main__':
    print("\n🤖 HireRight ETL Agent Server")
    print(f"   PDF support:   {'✓' if PDF_OK else '✗ install pypdf'}")
    print(f"   Excel support: {'✓' if XL_OK else '✗ install pandas openpyxl'}")
    print(f"   Max file size: {MAX_FILE_SIZE_MB}MB")
    print(f"   Max bulk:      20 files")
    print("\n   Running at: http://localhost:5052\n")
    app.run(debug=False, port=5052, host='0.0.0.0')
