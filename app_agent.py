"""
HireRight ETL Agent — Flask Server
Serves the agent-powered UI
"""
import os, json, datetime, tempfile
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__, static_folder='static_agent')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

try:
    from pypdf import PdfReader
    PDF_OK = True
except: PDF_OK = False

try:
    import pandas as pd
    XL_OK = True
except: XL_OK = False

from agent_engine import run_etl_agent

def get_api_key(req):
    return req.headers.get('X-API-Key') or os.environ.get('ANTHROPIC_API_KEY','')

@app.route('/')
def index():
    return send_from_directory('static_agent', 'index.html')

@app.route('/api/agent/process', methods=['POST'])
def process():
    api_key = get_api_key(request)
    if not api_key:
        return jsonify({"error": "API key required. Pass X-API-Key header or set ANTHROPIC_API_KEY env var."}), 401
    
    data = request.json or {}
    text = data.get('text','').strip()
    filename = data.get('filename','document.txt')
    
    if not text or len(text) < 20:
        return jsonify({"error": "Document text too short"}), 400
    
    result = run_etl_agent(text, filename, api_key)
    return jsonify(result)

@app.route('/api/agent/upload', methods=['POST'])
def upload():
    api_key = get_api_key(request)
    if not api_key:
        return jsonify({"error": "API key required"}), 401
    
    file = request.files.get('file')
    if not file: return jsonify({"error": "No file"}), 400
    
    ext = Path(file.filename).suffix.lower()
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    file.save(tmp.name); tmp.close()
    
    try:
        if ext == '.pdf' and PDF_OK:
            from pypdf import PdfReader
            r = PdfReader(tmp.name)
            text = '\n'.join(p.extract_text() for p in r.pages)
        elif ext in ['.xlsx','.xls'] and XL_OK:
            df = pd.read_excel(tmp.name)
            rows = []
            for _, row in df.iterrows():
                parts = [f"{col}: {val}" for col,val in row.items() if pd.notna(val) and str(val).strip()]
                if parts: rows.append(' | '.join(parts))
            text = '\n'.join(rows)
        elif ext == '.txt':
            with open(tmp.name) as f: text = f.read()
        else:
            return jsonify({"error": f"Unsupported: {ext}"}), 400
        
        result = run_etl_agent(text, file.filename, api_key)
        return jsonify(result)
    finally:
        os.unlink(tmp.name)

if __name__ == '__main__':
    app.run(debug=True, port=5051, host='0.0.0.0')
