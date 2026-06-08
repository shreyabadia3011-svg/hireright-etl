"""
HireRight ETL Agent — Full Server v2
Now with SQLite persistence, search, stats, and approve endpoint.
"""
import os, json, re, uuid, datetime, tempfile
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
import requests as http_requests

try:
    from pypdf import PdfReader
    PDF_OK = True
except: PDF_OK = False

try:
    import pandas as pd
    import openpyxl
    XL_OK = True
except: XL_OK = False

from database import (save_document, approve_document, get_all_documents,
                      get_document_fields, get_stats, search_documents, DB_PATH)

app = Flask(__name__, static_folder='static_agent')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

ALLOWED = {'.pdf', '.xlsx', '.xls', '.csv', '.txt'}
MAX_MB  = 50

# ── Text extraction ───────────────────────────────────────────────
def extract_text(filepath, ext):
    if ext == '.pdf':
        if not PDF_OK: return "", 0, False
        try:
            r = PdfReader(filepath)
            pages = [p.extract_text() or "" for p in r.pages]
            text = "\n".join(pages)
            return text, len(r.pages), len(text.strip()) < 100
        except Exception as e:
            return f"PDF_ERROR:{e}", 0, False
    elif ext in ['.xlsx','.xls','.csv']:
        if not XL_OK: return "", 0, False
        try:
            if ext == '.csv':
                df = pd.read_csv(filepath)
                rows = [" | ".join(f"{c}: {v}" for c,v in row.items() if pd.notna(v) and str(v).strip())
                        for _, row in df.iterrows()]
                return "\n".join(rows), 1, False
            xl = pd.ExcelFile(filepath)
            parts = []
            for sh in xl.sheet_names:
                df = pd.read_excel(filepath, sheet_name=sh)
                parts.append(f"=== Sheet: {sh} ===")
                for _, row in df.iterrows():
                    r = [f"{c}: {v}" for c,v in row.items() if pd.notna(v) and str(v).strip()]
                    if r: parts.append(" | ".join(r))
            return "\n".join(parts), len(xl.sheet_names), False
        except Exception as e:
            return f"EXCEL_ERROR:{e}", 0, False
    else:
        for enc in ['utf-8','latin-1','cp1252']:
            try:
                with open(filepath, encoding=enc) as f: return f.read(), 1, False
            except: pass
        return "", 0, False

# ── Structure analysis ────────────────────────────────────────────
def analyse_structure(text, filename):
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    tl = text.lower()
    colon_lines = [l for l in lines if ':' in l and len(l)<200]
    label_lines = [l for l in lines if re.match(
        r'^(?:Client|Company|Customer|Effective|Start|End|Date|Value|Package|'
        r'Price|Product|Total|Name|ACV|TAT|Includes|Discount|Annual|Contract|'
        r'Number|Checks|Turnaround|Unit|Validity|Valid)', l, re.I)]
    narrative_lines = [l for l in lines if len(l)>150]
    legal = ['hereinafter','whereas','notwithstanding','pursuant to',
             'party of the first part','further to our','i am pleased to confirm']
    legal_count = sum(1 for p in legal if p in tl)
    curr = len(re.findall(r'(?:USD|INR|\$|Rs\.?|EUR|GBP)\s*[\d,]+', text))
    dates = len(re.findall(
        r'\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|'
        r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}',
        text, re.I))

    score = 0
    score += min(len(colon_lines)*6, 30)
    score += min(len(label_lines)*8, 35)
    score += min(curr*5, 10)
    score += min(dates*5, 10)
    score -= min(len(narrative_lines)*4, 20)
    score -= min(legal_count*5, 15)
    score = max(0, min(100, score))

    fc = sum(1 for k in ["contract","agreement","effective date","acv","annual contract value","client name","commencement"] if k in tl)
    ps = sum(1 for k in ["unit price","per unit","discount","quote","pricing","rate card"] if k in tl)
    pk = sum(1 for k in ["package","plan","includes","turnaround","tat","check types","components"] if k in tl)
    fn = filename.lower()
    if any(k in fn for k in ["contract","agreement","msa"]): fc+=5
    if any(k in fn for k in ["pricing","price","quote"]): ps+=5
    if any(k in fn for k in ["package","plan","config"]): pk+=5

    type_scores = {"financial_contract":fc,"pricing_sheet":ps,"package_config":pk}
    best_type = max(type_scores, key=type_scores.get)
    type_conf = min(type_scores[best_type]/6.0, 1.0)

    if score>=50 and type_conf>=0.4:
        rec="regex"; reason=f"Well-structured (score {score}/100) — regex will be reliable"
    elif score>=30 and type_conf>=0.3:
        rec="regex"; reason=f"Moderately structured (score {score}/100) — regex with fallback"
    else:
        rec="llm"; reason=f"Low structure (score {score}/100), {len(narrative_lines)} narrative lines — LLM needed"

    return {"structure_score":score,"labelled_fields":len(label_lines),
            "narrative_lines":len(narrative_lines),"legal_phrases":legal_count,
            "currency_hits":curr,"date_hits":dates,"likely_type":best_type,
            "type_confidence":round(type_conf,2),"type_scores":type_scores,
            "recommended_extractor":rec,"reason":reason}

# ── Regex extraction ──────────────────────────────────────────────
TEMPLATES = {
    "financial_contract": [
        {"name":"client_name","label":"Client Legal Name","mandatory":True,"type":"text",
         "patterns":[r"(?:Client|Company|Customer)\s*(?:Name|Legal Name)?\s*[:\-]\s*([A-Z][A-Za-z\s&.,]+(?:Inc|LLC|Ltd|Corp|Limited)?)",
                     r"(?:Organization|Organisation)\s*[:\-]\s*([A-Z][A-Za-z\s&.,]+)",
                     r"(?:between|BETWEEN)\s+[A-Z][A-Za-z\s]+(?:Inc|LLC|Ltd|Corp)?\s+(?:and|AND)\s+([A-Z][A-Za-z\s&]+(?:Inc|LLC|Ltd|Corp)?)"]},
        {"name":"acv","label":"Annual Contract Value","mandatory":True,"type":"currency",
         "patterns":[r"(?:Annual Contract Value|ACV|Total Annual Value|Annual Value|Annual Fee|Total Value)\s*[:\-]?\s*(?:USD|\$|INR|Rs\.?)?\s*([\d,]+(?:\.\d{1,2})?)",
                     r"(?:Contract Value|Annual Fee)\s*[:\-]?\s*(?:USD|\$|INR)?\s*([\d,]+(?:\.\d{1,2})?)"]},
        {"name":"start_date","label":"Start Date","mandatory":True,"type":"date",
         "patterns":[r"(?:Start Date|Effective Date|Commencement Date|Start)\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
                     r"(?:Start Date|Effective Date|Commencement Date)\s*[:\-]?\s*((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})"]},
        {"name":"end_date","label":"End Date","mandatory":False,"type":"date",
         "patterns":[r"(?:End Date|Expiry Date|Termination Date)\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
                     r"(?:End Date|Expiry Date)\s*[:\-]?\s*((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})"]},
        {"name":"package","label":"Package Name","mandatory":False,"type":"text",
         "patterns":[r"(?:Package|Service Package|Plan)\s*[:\-]\s*([A-Za-z][A-Za-z\s]+(?:Plus|Pro|Premium|Basic|Standard|Enterprise|Essential|Complete)?)"]},
        {"name":"num_checks","label":"Number of Checks","mandatory":False,"type":"number",
         "patterns":[r"(?:Number of (?:Checks|Screenings)|Total (?:Checks|Screenings)|Checks Included)\s*[:\-]?\s*([\d,]+)",
                     r"([\d,]+)\s+(?:background checks|screenings|checks per year)"]},
    ],
    "pricing_sheet": [
        {"name":"product_name","label":"Product Name","mandatory":True,"type":"text",
         "patterns":[r"(?:Product|Service|Item)\s*[:\-]\s*([A-Za-z][A-Za-z\s]+)"]},
        {"name":"unit_price","label":"Unit Price","mandatory":True,"type":"currency",
         "patterns":[r"(?:Unit Price|Price per Unit|Per Unit|Base Price|Price)\s*[:\-]?\s*(?:USD|\$|INR|Rs\.?)?\s*([\d,]+(?:\.\d{1,2})?)"]},
        {"name":"discount","label":"Discount %","mandatory":False,"type":"percentage",
         "patterns":[r"(?:Discount|Rebate)\s*[:\-]?\s*([\d.]+)\s*%"]},
        {"name":"validity","label":"Valid Until","mandatory":False,"type":"text",
         "patterns":[r"(?:Valid Until|Validity|Quote Valid|Expires|Valid)\s*[:\-]?\s*(.+?)(?:\n|$)"]},
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
    if ftype=="currency":
        c=re.sub(r'[,\s]','',raw); c=re.sub(r'^[^\d]*','',c)
        try: return f"{float(c):,.2f}", 0.95
        except: return raw, 0.60
    if ftype=="date":
        months={'january':'01','february':'02','march':'03','april':'04','may':'05','june':'06',
                'july':'07','august':'08','september':'09','october':'10','november':'11','december':'12'}
        for mn,num in months.items():
            if mn in raw.lower():
                nums=re.findall(r'\d+',raw); yr=next((n for n in nums if len(n)==4),None)
                dy=next((n for n in nums if int(n)<=31 and len(n)<=2),None)
                if yr and dy: return f"{yr}-{num}-{dy.zfill(2)}", 0.90
        m=re.match(r'(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})',raw)
        if m:
            d,mo,y=m.groups()
            return f"{'20'+y if len(y)==2 else y}-{mo.zfill(2)}-{d.zfill(2)}", 0.85
        return raw, 0.70
    if ftype=="number":
        try: return str(int(float(raw.replace(',','').strip()))), 0.95
        except: return raw, 0.60
    if ftype=="percentage":
        m=re.search(r'([\d.]+)',raw); return (f"{m.group(1)}%",0.95) if m else (raw,0.7)
    return raw.replace('\n',' ').strip()[:100], 0.82

def do_regex(text, doc_type):
    tmpl=TEMPLATES.get(doc_type,TEMPLATES["financial_contract"])
    fields=[]
    for field in tmpl:
        bv,bc=None,0.0
        for i,pat in enumerate(field["patterns"]):
            m=re.search(pat,text,re.IGNORECASE|re.MULTILINE)
            if m:
                raw=m.group(1) if m.lastindex else m.group(0)
                v,c=parse_val(raw,field["type"]); adj=c*(1.0 if i==0 else 0.9)
                if v and adj>bc: bv,bc=v,adj
        fields.append({"name":field["name"],"label":field["label"],
                       "value":bv,"confidence":round(bc,3),
                       "mandatory":field["mandatory"],"method":"regex"})
    return fields

def do_llm(text, doc_type, api_key):
    try:
        resp=http_requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":api_key,"anthropic-version":"2023-06-01","content-type":"application/json"},
            json={"model":"claude-sonnet-4-20250514","max_tokens":1200,
                  "system":"Extract document data. Return ONLY valid JSON, no markdown.",
                  "messages":[{"role":"user","content":f"""Extract fields. JSON only:
{{"document_type":"financial_contract|pricing_sheet|package_config",
"fields":{{"client_name":{{"value":"or null","quote":"source text","confidence":0.9}},
"acv":{{"value":"number string or null","quote":"...","confidence":0.9}},
"start_date":{{"value":"YYYY-MM-DD or null","quote":"...","confidence":0.9}},
"end_date":{{"value":"YYYY-MM-DD or null","quote":"...","confidence":0.9}},
"package":{{"value":"or null","quote":"...","confidence":0.85}},
"num_checks":{{"value":"integer string or null","quote":"...","confidence":0.9}}}}}}

Document:\n{text[:4000]}"""}]}, timeout=40)
        raw=resp.json()["content"][0]["text"].replace("```json","").replace("```","").strip()
        parsed=json.loads(raw)
        lm={"client_name":"Client Legal Name","acv":"Annual Contract Value",
            "start_date":"Start Date","end_date":"End Date",
            "package":"Package Name","num_checks":"Number of Checks"}
        fields=[{"name":k,"label":label,"value":(parsed.get("fields",{}).get(k,{}) or {}).get("value"),
                 "quote":(parsed.get("fields",{}).get(k,{}) or {}).get("quote"),
                 "confidence":(parsed.get("fields",{}).get(k,{}) or {}).get("confidence",0),
                 "mandatory":k in ["client_name","acv","start_date"],"method":"llm"}
                for k,label in lm.items()]
        return fields, parsed.get("document_type",doc_type)
    except Exception as e:
        print(f"LLM error: {e}"); return [], doc_type

def compute_routing(fields):
    mand=[f for f in fields if f.get("mandatory")]; miss=[f for f in mand if not f.get("value")]
    if miss: return {"routing":"full_review","score":0,"band":"low",
                     "reason":f"Missing: {[f['label'] for f in miss]}"}
    sc=([f["confidence"] for f in mand]*2+
        [f["confidence"] for f in fields if not f.get("mandatory") and f.get("value")])
    comp=sum(sc)/len(sc) if sc else 0; r=round(comp,3)
    if comp>=0.88: return {"routing":"auto_accept","score":r,"band":"high",
                            "reason":f"All fields high confidence ({round(comp*100)}%)"}
    if comp>=0.70: return {"routing":"partial_review","score":r,"band":"medium",
                            "reason":f"Composite {round(comp*100)}% — some fields need check"}
    return {"routing":"full_review","score":r,"band":"low",
            "reason":f"Low confidence {round(comp*100)}%"}

def process_file(filepath, filename, api_key):
    ext=Path(filename).suffix.lower()
    text, pages, is_scanned = extract_text(filepath, ext)
    if is_scanned: return {"error":"Scanned PDF — text extraction requires OCR setup"}
    if not text or str(text).startswith(('PDF_ERROR','EXCEL_ERROR')):
        return {"error":str(text)}
    analysis=analyse_structure(text,filename)
    doc_type=analysis["likely_type"]
    rec=analysis["recommended_extractor"]
    if rec=="regex" or not api_key:
        fields=do_regex(text,doc_type); method="regex"
    else:
        fields,doc_type=do_llm(text,doc_type,api_key); method="llm"
        if not fields: fields=do_regex(text,doc_type); method="regex_fallback"
    routing=compute_routing(fields)
    import hashlib
    doc_id="DOC-"+hashlib.md5((filename+text[:100]).encode()).hexdigest()[:8].upper()
    result={"doc_id":doc_id,"filename":filename,"file_type":ext.lstrip('.').upper(),
            "size_mb":round(os.path.getsize(filepath)/1024/1024,2),
            "doc_type":doc_type,"doc_type_display":doc_type.replace("_"," ").title(),
            "structure_analysis":analysis,"method_used":method,"fields":fields,
            "routing":routing,"text_preview":text[:300]+"..." if len(text)>300 else text,
            "processed_at":datetime.datetime.now().isoformat()}
    save_document(result)
    return result

# ── Routes ────────────────────────────────────────────────────────

@app.route('/')
def index(): return send_from_directory('static_agent','index.html')

@app.route('/api/upload', methods=['POST'])
def upload():
    api_key=request.headers.get('X-API-Key','')
    file=request.files.get('file')
    if not file: return jsonify({"error":"No file"}),400
    ext=Path(file.filename).suffix.lower()
    if ext not in ALLOWED: return jsonify({"error":f"Unsupported: {ext}"}),400
    file.seek(0,2); mb=file.tell()/1024/1024; file.seek(0)
    if mb>MAX_MB: return jsonify({"error":f"Too large: {mb:.1f}MB (max {MAX_MB}MB)"}),400
    tmp=tempfile.NamedTemporaryFile(suffix=ext,delete=False)
    file.save(tmp.name); tmp.close()
    try:
        result=process_file(tmp.name,file.filename,api_key)
        return jsonify(result)
    finally: os.unlink(tmp.name)

@app.route('/api/bulk', methods=['POST'])
def bulk():
    api_key=request.headers.get('X-API-Key','')
    files=request.files.getlist('files')
    if not files: return jsonify({"error":"No files"}),400
    if len(files)>20: return jsonify({"error":"Max 20 files"}),400
    results=[]
    for file in files:
        ext=Path(file.filename).suffix.lower()
        if ext not in ALLOWED:
            results.append({"filename":file.filename,"status":"skipped","error":f"Unsupported: {ext}"}); continue
        file.seek(0,2); mb=file.tell()/1024/1024; file.seek(0)
        if mb>MAX_MB:
            results.append({"filename":file.filename,"status":"skipped","error":f"Too large"}); continue
        tmp=tempfile.NamedTemporaryFile(suffix=ext,delete=False)
        file.save(tmp.name); tmp.close()
        try:
            r=process_file(tmp.name,file.filename,api_key)
            if "error" in r: results.append({"filename":file.filename,"status":"error","error":r["error"]}); continue
            results.append({**r,"status":"processed","fields_found":sum(1 for f in r["fields"] if f.get("value")),"fields_total":len(r["fields"]),"structure_score":r["structure_analysis"]["structure_score"]})
        except Exception as e: results.append({"filename":file.filename,"status":"error","error":str(e)})
        finally: os.unlink(tmp.name)
    proc=[r for r in results if r.get("status")=="processed"]
    return jsonify({"batch_id":"BATCH-"+uuid.uuid4().hex[:6].upper(),"total_files":len(files),
                    "processed":len(proc),"skipped":len([r for r in results if r.get("status")=="skipped"]),
                    "errors":len([r for r in results if r.get("status")=="error"]),
                    "auto_accepted":len([r for r in proc if r.get("routing",{}).get("routing")=="auto_accept"]),
                    "needs_review":len([r for r in proc if r.get("routing",{}).get("routing")!="auto_accept"]),
                    "results":results,"processed_at":datetime.datetime.now().isoformat()})

@app.route('/api/documents', methods=['GET'])
def documents():
    status=request.args.get('status'); doc_type=request.args.get('type')
    docs=get_all_documents(status=status,doc_type=doc_type)
    for d in docs:
        d['fields']=get_document_fields(d['doc_id'])
    return jsonify(docs)

@app.route('/api/documents/<doc_id>/approve', methods=['POST'])
def approve(doc_id):
    corrections=request.json.get('corrections',{}) if request.json else {}
    approve_document(doc_id,corrections)
    return jsonify({"status":"approved","doc_id":doc_id,"message":"Saved to database"})

@app.route('/api/stats', methods=['GET'])
def stats(): return jsonify(get_stats())

@app.route('/api/search', methods=['GET'])
def search():
    q=request.args.get('q','')
    if not q: return jsonify([])
    docs=search_documents(q)
    return jsonify(docs)

@app.route('/api/export', methods=['GET'])
def export():
    """Export all approved data as JSON"""
    docs=get_all_documents(status='approved')
    for d in docs: d['fields']=get_document_fields(d['doc_id'])
    return jsonify({"exported_at":datetime.datetime.now().isoformat(),"count":len(docs),"documents":docs})

@app.route('/health')
def health(): return 'ok', 200

@app.route('/api/limits', methods=['GET'])
def limits():
    return jsonify({"max_file_size_mb":MAX_MB,"max_bulk_files":20,
                    "supported_formats":list(ALLOWED),"pdf_support":PDF_OK,
                    "excel_support":XL_OK,"database":str(DB_PATH)})

if __name__=='__main__':
    print(f"\n🤖 HireRight ETL Agent Server v2")
    print(f"   PDF support:   {'✓' if PDF_OK else '✗'}")
    print(f"   Excel support: {'✓' if XL_OK else '✗'}")
    print(f"   Database:      {DB_PATH}")
    print(f"   Max file:      {MAX_MB}MB | Max bulk: 20 files")
    print(f"\n   Running at: http://localhost:5052\n")
    port = int(os.environ.get('PORT', 5052))
    app.run(debug=False,port=port,host='0.0.0.0')
