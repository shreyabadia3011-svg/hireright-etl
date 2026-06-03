"""
HireRight ETL Engine - Core Extraction Logic
Rule-based + LLM-based hybrid extraction
"""
import re
import json
import os
import hashlib
import datetime
from pathlib import Path
from typing import Optional

# PDF extraction
try:
    from pypdf import PdfReader
    PDF_AVAILABLE = True
except:
    PDF_AVAILABLE = False

# Excel extraction
try:
    import openpyxl
    import pandas as pd
    EXCEL_AVAILABLE = True
except:
    EXCEL_AVAILABLE = False


TEMPLATES_DIR = Path("/home/claude/etl_prototype/templates")
UPLOADS_DIR = Path("/home/claude/etl_prototype/uploads")
EXTRACTED_DIR = Path("/home/claude/etl_prototype/extracted")


def load_templates():
    templates = {}
    for f in TEMPLATES_DIR.glob("*.json"):
        with open(f) as fp:
            t = json.load(fp)
            templates[t["document_type"]] = t
    return templates


def classify_document(text: str, filename: str) -> str:
    """Simple keyword-based document classifier"""
    text_lower = text.lower()
    filename_lower = filename.lower()

    scores = {
        "financial_contract": 0,
        "pricing_sheet": 0,
        "package_config": 0
    }

    # Financial contract signals
    for kw in ["contract", "agreement", "effective date", "commencement", "annual contract value", "acv", "terms and conditions", "hereinafter"]:
        if kw in text_lower: scores["financial_contract"] += 2
    if any(kw in filename_lower for kw in ["contract", "agreement", "msa", "sow"]): scores["financial_contract"] += 5

    # Pricing sheet signals
    for kw in ["unit price", "per unit", "price list", "rate card", "discount", "quote", "quotation", "pricing"]:
        if kw in text_lower: scores["pricing_sheet"] += 2
    if any(kw in filename_lower for kw in ["pricing", "price", "quote", "rate"]): scores["pricing_sheet"] += 5

    # Package config signals
    for kw in ["package", "plan", "includes", "turnaround", "tat", "check types", "components", "background check package"]:
        if kw in text_lower: scores["package_config"] += 2
    if any(kw in filename_lower for kw in ["package", "plan", "config"]): scores["package_config"] += 5

    best = max(scores, key=scores.get)
    confidence = min(scores[best] / 10.0, 1.0)
    return best, round(confidence, 2)


def extract_text_from_pdf(filepath: str) -> str:
    """Extract text from PDF using pypdf"""
    if not PDF_AVAILABLE:
        return ""
    try:
        reader = PdfReader(filepath)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"
        return text
    except Exception as e:
        return f"ERROR: {str(e)}"


def extract_text_from_excel(filepath: str) -> str:
    """Extract text from Excel using openpyxl + pandas"""
    if not EXCEL_AVAILABLE:
        return ""
    try:
        xl = pd.ExcelFile(filepath)
        all_text = []
        for sheet_name in xl.sheet_names:
            df = pd.read_excel(filepath, sheet_name=sheet_name)
            all_text.append(f"Sheet: {sheet_name}")
            for idx, row in df.iterrows():
                row_parts = []
                for col, val in row.items():
                    if pd.notna(val) and str(val).strip():
                        row_parts.append(f"{col}: {val}")
                if row_parts:
                    all_text.append(" | ".join(row_parts))
        return "\n".join(all_text)
    except Exception as e:
        return f"ERROR: {str(e)}"


def parse_value(raw: str, field_type: str) -> tuple[str, float]:
    """Parse and normalise extracted value, return (value, confidence)"""
    if not raw:
        return None, 0.0

    raw = raw.strip().rstrip('.,;')

    if field_type == "currency":
        # Remove commas, currency symbols
        cleaned = re.sub(r'[,\s]', '', raw)
        cleaned = re.sub(r'^[^\d]*', '', cleaned)
        try:
            val = float(cleaned)
            return f"{val:,.2f}", 0.95
        except:
            return raw, 0.6

    elif field_type == "date":
        # Try to standardise date
        months = {
            'january': '01', 'february': '02', 'march': '03', 'april': '04',
            'may': '05', 'june': '06', 'july': '07', 'august': '08',
            'september': '09', 'october': '10', 'november': '11', 'december': '12'
        }
        raw_lower = raw.lower()
        for month_name, month_num in months.items():
            if month_name in raw_lower:
                # Extract day and year
                nums = re.findall(r'\d+', raw)
                if len(nums) >= 2:
                    day = nums[0].zfill(2) if int(nums[0]) <= 31 else nums[1].zfill(2)
                    year = [n for n in nums if len(n) == 4]
                    if year:
                        return f"{year[0]}-{month_num}-{day}", 0.90
        # Try numeric formats
        m = re.match(r'(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})', raw)
        if m:
            d, mo, y = m.groups()
            if len(y) == 2:
                y = '20' + y
            return f"{y}-{mo.zfill(2)}-{d.zfill(2)}", 0.85
        m2 = re.match(r'(\d{4})-(\d{2})-(\d{2})', raw)
        if m2:
            return raw, 0.95
        return raw, 0.70

    elif field_type == "number":
        cleaned = re.sub(r'[,\s]', '', raw)
        try:
            return str(int(float(cleaned))), 0.95
        except:
            return raw, 0.6

    elif field_type == "percentage":
        m = re.search(r'([\d.]+)', raw)
        if m:
            return f"{m.group(1)}%", 0.95
        return raw, 0.7

    else:  # text
        # Clean up text value
        val = re.sub(r'\s+', ' ', raw).strip()
        if len(val) < 100:
            return val, 0.85
        return val[:100], 0.7


def extract_field(text: str, field: dict) -> dict:
    """Try all patterns for a field, return best match"""
    best_value = None
    best_confidence = 0.0
    best_pattern = None

    for i, pattern in enumerate(field["patterns"]):
        try:
            match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            if match:
                raw = match.group(1) if match.lastindex else match.group(0)
                value, conf = parse_value(raw, field["type"])
                # Primary pattern gets slight boost
                conf = conf * (1.0 if i == 0 else 0.9)
                if value and conf > best_confidence:
                    best_value = value
                    best_confidence = conf
                    best_pattern = pattern
        except re.error:
            continue

    return {
        "field_name": field["field_name"],
        "display_name": field["display_name"],
        "value": best_value,
        "confidence": round(best_confidence, 3),
        "type": field["type"],
        "mandatory": field["mandatory"],
        "pattern_matched": best_pattern is not None,
        "extraction_method": "rule_engine"
    }


def compute_composite_score(extracted_fields: list) -> dict:
    """Compute overall confidence and routing decision"""
    mandatory_fields = [f for f in extracted_fields if f["mandatory"]]
    optional_fields = [f for f in extracted_fields if not f["mandatory"]]

    # Check mandatory fields
    missing_mandatory = [f for f in mandatory_fields if not f["value"]]
    if missing_mandatory:
        return {
            "composite_score": 0.0,
            "routing": "full_review",
            "reason": f"Missing mandatory fields: {[f['display_name'] for f in missing_mandatory]}",
            "confidence_band": "low"
        }

    # Calculate weighted composite
    mandatory_scores = [f["confidence"] for f in mandatory_fields if f["value"]]
    optional_scores = [f["confidence"] for f in optional_fields if f["value"]]

    all_scores = mandatory_scores * 2 + optional_scores  # mandatory weighted 2x
    composite = sum(all_scores) / len(all_scores) if all_scores else 0.0

    if composite >= 0.88:
        routing = "auto_accept"
        band = "high"
    elif composite >= 0.70:
        routing = "partial_review"
        band = "medium"
    else:
        routing = "full_review"
        band = "low"

    return {
        "composite_score": round(composite, 3),
        "routing": routing,
        "reason": f"Composite confidence from {len(mandatory_scores)} mandatory + {len(optional_scores)} optional fields",
        "confidence_band": band
    }


def process_document(filepath: str, filename: str) -> dict:
    """Main extraction pipeline"""
    file_ext = Path(filename).suffix.lower()

    # Step 1: Extract raw text
    if file_ext == '.pdf':
        raw_text = extract_text_from_pdf(filepath)
        file_type = "pdf"
    elif file_ext in ['.xlsx', '.xls']:
        raw_text = extract_text_from_excel(filepath)
        file_type = "excel"
    elif file_ext in ['.txt']:
        with open(filepath) as f:
            raw_text = f.read()
        file_type = "text"
    else:
        return {"error": f"Unsupported file type: {file_ext}"}

    if raw_text.startswith("ERROR:"):
        return {"error": raw_text}

    # Step 2: Classify document
    doc_type, classification_confidence = classify_document(raw_text, filename)

    # Step 3: Load template
    templates = load_templates()
    template = templates.get(doc_type)
    if not template:
        return {"error": f"No template found for document type: {doc_type}"}

    # Step 4: Extract fields
    extracted_fields = []
    for field in template["fields"]:
        result = extract_field(raw_text, field)
        extracted_fields.append(result)

    # Step 5: Compute composite score
    routing_info = compute_composite_score(extracted_fields)

    # Step 6: Build result
    doc_hash = hashlib.sha256(raw_text.encode()).hexdigest()[:12]

    result = {
        "document_id": f"DOC-{doc_hash.upper()}",
        "filename": filename,
        "file_type": file_type,
        "document_type": doc_type,
        "document_type_display": template["display_name"],
        "classification_confidence": classification_confidence,
        "extracted_fields": extracted_fields,
        "routing": routing_info,
        "raw_text_preview": raw_text[:500] + "..." if len(raw_text) > 500 else raw_text,
        "processed_at": datetime.datetime.now().isoformat(),
        "text_length": len(raw_text)
    }

    # Save result
    output_path = EXTRACTED_DIR / f"{doc_hash}_result.json"
    with open(output_path, 'w') as f:
        json.dump(result, f, indent=2)

    return result


def process_text_input(text: str, doc_type_override: str = None) -> dict:
    """Process raw text input directly"""
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write(text)
        tmp_path = f.name
    result = process_document(tmp_path, "manual_input.txt")
    if doc_type_override:
        # Re-run with forced document type
        templates = load_templates()
        template = templates.get(doc_type_override)
        if template:
            result["document_type"] = doc_type_override
            result["document_type_display"] = template["display_name"]
            extracted_fields = []
            for field in template["fields"]:
                r = extract_field(text, field)
                extracted_fields.append(r)
            result["extracted_fields"] = extracted_fields
            result["routing"] = compute_composite_score(extracted_fields)
    os.unlink(tmp_path)
    return result


if __name__ == "__main__":
    # Quick test
    sample_text = """
    SERVICE AGREEMENT

    This Agreement is entered into between TechCorp Solutions Inc. and HireRight Inc.

    Client Name: TechCorp Solutions Inc.
    Effective Date: January 15, 2024
    End Date: January 14, 2025
    Annual Contract Value: USD 125,000
    Package: Enterprise Premium
    Number of Checks: 500 background checks per year

    Terms and conditions apply as per the master services agreement.
    """
    result = process_text_input(sample_text)
    print(json.dumps(result, indent=2))
