"""
HireRight ETL Prototype - Flask Backend
"""
import os, json, uuid, datetime
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
import tempfile

app = Flask(__name__, static_folder='static')
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB

UPLOADS_DIR = Path("/home/claude/etl_prototype/uploads")
EXTRACTED_DIR = Path("/home/claude/etl_prototype/extracted")

# In-memory store for demo
documents_store = {}

from extractor import process_document, process_text_input, load_templates

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/templates', methods=['GET'])
def get_templates():
    templates = load_templates()
    return jsonify([{
        "id": k,
        "display_name": v["display_name"],
        "field_count": len(v["fields"])
    } for k, v in templates.items()])

@app.route('/api/extract/text', methods=['POST'])
def extract_text():
    """Extract from pasted text"""
    data = request.json
    text = data.get('text', '').strip()
    doc_type = data.get('doc_type')

    if not text:
        return jsonify({"error": "No text provided"}), 400
    if len(text) < 20:
        return jsonify({"error": "Text too short to extract from"}), 400

    result = process_text_input(text, doc_type if doc_type != 'auto' else None)
    doc_id = result.get("document_id", str(uuid.uuid4())[:12])
    documents_store[doc_id] = result
    return jsonify(result)

@app.route('/api/extract/file', methods=['POST'])
def extract_file():
    """Extract from uploaded file"""
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    allowed = {'.pdf', '.xlsx', '.xls', '.txt'}
    ext = Path(file.filename).suffix.lower()
    if ext not in allowed:
        return jsonify({"error": f"File type {ext} not supported. Use PDF, Excel, or TXT."}), 400

    # Save temp file
    tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
    file.save(tmp.name)
    tmp.close()

    try:
        result = process_document(tmp.name, file.filename)
        doc_id = result.get("document_id", str(uuid.uuid4())[:12])
        documents_store[doc_id] = result
        return jsonify(result)
    finally:
        os.unlink(tmp.name)

@app.route('/api/documents', methods=['GET'])
def get_documents():
    """Get all processed documents"""
    docs = []
    # Load from file system
    for f in EXTRACTED_DIR.glob("*_result.json"):
        try:
            with open(f) as fp:
                doc = json.load(fp)
                docs.append({
                    "document_id": doc["document_id"],
                    "filename": doc["filename"],
                    "document_type_display": doc.get("document_type_display", "Unknown"),
                    "routing": doc["routing"]["routing"],
                    "composite_score": doc["routing"]["composite_score"],
                    "processed_at": doc["processed_at"]
                })
        except:
            pass
    # Add in-memory
    for doc_id, doc in documents_store.items():
        if not any(d["document_id"] == doc["document_id"] for d in docs):
            docs.append({
                "document_id": doc["document_id"],
                "filename": doc["filename"],
                "document_type_display": doc.get("document_type_display", "Unknown"),
                "routing": doc["routing"]["routing"],
                "composite_score": doc["routing"]["composite_score"],
                "processed_at": doc["processed_at"]
            })
    return jsonify(sorted(docs, key=lambda x: x["processed_at"], reverse=True))

@app.route('/api/documents/<doc_id>/approve', methods=['POST'])
def approve_field(doc_id):
    """Approve or correct a field value"""
    data = request.json
    field_name = data.get('field_name')
    new_value = data.get('value')
    action = data.get('action', 'approve')  # approve | correct | reject

    # Find doc in store or file
    doc = documents_store.get(doc_id)
    if not doc:
        for f in EXTRACTED_DIR.glob("*_result.json"):
            try:
                with open(f) as fp:
                    d = json.load(fp)
                    if d["document_id"] == doc_id:
                        doc = d
                        break
            except:
                pass

    if not doc:
        return jsonify({"error": "Document not found"}), 404

    # Update field
    for field in doc["extracted_fields"]:
        if field["field_name"] == field_name:
            field["review_action"] = action
            if action == "correct" and new_value:
                field["original_value"] = field["value"]
                field["value"] = new_value
                field["confidence"] = 1.0  # Human-verified
            elif action == "approve":
                field["confidence"] = 1.0
            break

    documents_store[doc_id] = doc
    return jsonify({"status": "updated", "document_id": doc_id})

@app.route('/api/documents/<doc_id>/finalise', methods=['POST'])
def finalise_document(doc_id):
    """Mark document as approved and ready for DB load"""
    doc = documents_store.get(doc_id)
    if not doc:
        return jsonify({"error": "Document not found"}), 404

    doc["final_status"] = "approved"
    doc["finalised_at"] = datetime.datetime.now().isoformat()
    doc["finalised_by"] = "PSO_Reviewer"
    documents_store[doc_id] = doc

    # Simulate DB write
    final_data = {
        field["display_name"]: field["value"]
        for field in doc["extracted_fields"]
        if field["value"]
    }

    return jsonify({
        "status": "success",
        "message": "Document data loaded to database",
        "document_id": doc_id,
        "fields_loaded": len(final_data),
        "data_preview": final_data
    })

if __name__ == '__main__':
    app.run(debug=True, port=5050, host='0.0.0.0')
