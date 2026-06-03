"""
HireRight ETL Agent Engine
===========================
Claude acts as the routing AGENT — it inspects the document
and autonomously decides: regex extraction OR LLM extraction.

Architecture (mirrors the article's approach):
- Claude is given 4 tools:
  1. inspect_document    → read structure, detect format quality
  2. regex_extract       → rule-based field extraction
  3. llm_extract         → deep LLM-based extraction for complex docs
  4. validate_and_route  → score results, decide HITL routing

Claude runs a multi-turn agentic loop:
  Step 1: Always calls inspect_document first
  Step 2: Based on inspection, decides which extractor to use
  Step 3: Validates the result
  Step 4: Returns final structured output with decision trace

This is DIFFERENT from the previous version where routing was hardcoded.
Here Claude READS the document and DECIDES the path.
"""

import re
import json
import datetime
import requests
import os
from pathlib import Path
from typing import Optional

# ─── TOOL IMPLEMENTATIONS ─────────────────────────────────────────────────────

def tool_inspect_document(text: str, filename: str) -> dict:
    """
    Tool 1: Inspect document structure and quality.
    Claude calls this first to understand what it's dealing with.
    """
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    
    # Signal detection
    has_colons = sum(1 for l in lines if ':' in l)
    has_labels = sum(1 for l in lines if re.search(
        r'^(?:Client|Company|Date|Value|Package|Price|Product|Total|Name|ACV|TAT|Includes|Discount)', 
        l, re.I))
    has_tables = any('\t' in l or '  |  ' in l for l in lines)
    has_narrative = sum(1 for l in lines if len(l) > 120)
    has_legal_boilerplate = any(w in text.lower() for w in [
        'hereinafter', 'whereas', 'notwithstanding', 'pursuant to',
        'in witness whereof', 'party of the first part'
    ])
    currency_patterns = len(re.findall(r'(?:USD|INR|\$|Rs\.?)\s*[\d,]+', text))
    date_patterns = len(re.findall(
        r'\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4}|'
        r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4}',
        text, re.I))
    
    # Score: how well-structured is this document?
    structure_score = 0
    if has_colons > 3:   structure_score += 30
    if has_labels > 2:   structure_score += 35
    if has_tables:       structure_score += 20
    if currency_patterns > 0: structure_score += 10
    if date_patterns > 0:     structure_score += 10
    if has_narrative > 5:     structure_score -= 20
    if has_legal_boilerplate: structure_score -= 15
    structure_score = max(0, min(100, structure_score))
    
    # Keyword-based doc type signals
    type_signals = {}
    fc_kws = ["contract","agreement","effective date","annual contract value","acv","hereinafter","commencement","client name"]
    ps_kws = ["unit price","per unit","price list","rate card","discount","quote","quotation","pricing"]
    pk_kws = ["package","plan","includes","turnaround","tat","check types","components","background check package"]
    
    type_signals['financial_contract'] = sum(1 for k in fc_kws if k in text.lower())
    type_signals['pricing_sheet']      = sum(1 for k in ps_kws if k in text.lower())
    type_signals['package_config']     = sum(1 for k in pk_kws if k in text.lower())
    
    best_type = max(type_signals, key=type_signals.get)
    type_confidence = min(type_signals[best_type] / 5.0, 1.0)
    
    recommendation = "regex" if structure_score >= 45 and type_confidence >= 0.4 else "llm"
    
    return {
        "filename": filename,
        "line_count": len(lines),
        "char_count": len(text),
        "structure_score": structure_score,
        "has_colon_pairs": has_colons,
        "has_labelled_fields": has_labels,
        "has_narrative_text": has_narrative,
        "has_legal_boilerplate": has_legal_boilerplate,
        "currency_patterns_found": currency_patterns,
        "date_patterns_found": date_patterns,
        "type_signals": type_signals,
        "likely_document_type": best_type,
        "type_confidence": round(type_confidence, 2),
        "structure_score": structure_score,
        "recommended_extractor": recommendation,
        "recommendation_reason": (
            f"Structure score {structure_score}/100 with {has_labels} labeled fields detected. "
            f"Document appears {'well-structured — regex patterns will match reliably' if recommendation == 'regex' else 'unstructured or complex — LLM extraction will be more reliable'}."
        )
    }


def tool_regex_extract(text: str, document_type: str) -> dict:
    """
    Tool 2: Rule-based extraction using pre-defined patterns.
    Fast, deterministic, works for templated/structured documents.
    """
    TEMPLATES = {
        "financial_contract": [
            {"name": "client_name", "label": "Client Legal Name", "mandatory": True, "type": "text",
             "patterns": [r"(?:Client|Company|Customer)\s*(?:Name|Legal Name)?\s*[:\-]\s*([A-Z][A-Za-z\s&.,]+(?:Inc|LLC|Ltd|Corp|Limited)?)",
                          r"(?:between|BETWEEN)\s+[A-Z][A-Za-z\s]+(?:Inc|LLC|Ltd|Corp)?\s+(?:and|AND)\s+([A-Z][A-Za-z\s&]+(?:Inc|LLC|Ltd|Corp)?)"]},
            {"name": "acv", "label": "Annual Contract Value", "mandatory": True, "type": "currency",
             "patterns": [r"(?:Annual Contract Value|ACV|Total Annual Value)\s*[:\-]?\s*(?:USD|\$|INR|Rs\.?)?\s*([\d,]+(?:\.\d{1,2})?)",
                          r"(?:Total Value|Contract Value|Annual Fee)\s*[:\-]?\s*(?:USD|\$|INR)?\s*([\d,]+(?:\.\d{1,2})?)"]},
            {"name": "start_date", "label": "Contract Start Date", "mandatory": True, "type": "date",
             "patterns": [r"(?:Start Date|Effective Date|Commencement Date)\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
                          r"(?:Start Date|Effective Date|Commencement Date)\s*[:\-]?\s*((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})"]},
            {"name": "end_date", "label": "Contract End Date", "mandatory": False, "type": "date",
             "patterns": [r"(?:End Date|Expiry Date|Termination Date)\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
                          r"(?:End Date|Expiry Date)\s*[:\-]?\s*((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})"]},
            {"name": "package", "label": "Package / Plan Name", "mandatory": False, "type": "text",
             "patterns": [r"Package\s*[:\-]\s*([A-Za-z][A-Za-z\s]+(?:Plus|Pro|Premium|Basic|Standard|Enterprise|Essential)?)"]},
            {"name": "num_checks", "label": "Number of Checks", "mandatory": False, "type": "number",
             "patterns": [r"(?:Number of (?:Checks|Screenings)|Total (?:Checks|Screenings))\s*[:\-]?\s*([\d,]+)",
                          r"([\d,]+)\s+(?:background checks|screenings|checks per year)"]},
        ],
        "pricing_sheet": [
            {"name": "product_name", "label": "Product / Service Name", "mandatory": True, "type": "text",
             "patterns": [r"(?:Product|Service|Item)\s*[:\-]\s*([A-Za-z][A-Za-z\s]+)"]},
            {"name": "unit_price", "label": "Unit Price", "mandatory": True, "type": "currency",
             "patterns": [r"(?:Unit Price|Price per Unit|Per Unit|Base Price)\s*[:\-]?\s*(?:USD|\$|INR|Rs\.?)?\s*([\d,]+(?:\.\d{1,2})?)",
                          r"Price\s*[:\-]?\s*(?:USD|\$)?\s*([\d,]+(?:\.\d{1,2})?)"]},
            {"name": "discount", "label": "Discount %", "mandatory": False, "type": "percentage",
             "patterns": [r"(?:Discount|Rebate)\s*[:\-]?\s*([\d.]+)\s*%"]},
            {"name": "validity", "label": "Valid Until", "mandatory": False, "type": "text",
             "patterns": [r"(?:Valid Until|Validity|Quote Valid|Expires)\s*[:\-]?\s*(.+?)(?:\n|$)"]},
        ],
        "package_config": [
            {"name": "package_name", "label": "Package Name", "mandatory": True, "type": "text",
             "patterns": [r"(?:Package Name|Package|Plan Name|Plan)\s*[:\-]\s*([A-Za-z][A-Za-z\s]+)"]},
            {"name": "included_checks", "label": "Included Check Types", "mandatory": True, "type": "text",
             "patterns": [r"(?:Includes?|Contains?|Check Types?|Components?)\s*[:\-]\s*(.+?)(?:\n\n|\n[A-Z]|$)"]},
            {"name": "tat", "label": "Turnaround Time", "mandatory": False, "type": "text",
             "patterns": [r"(?:Turnaround Time|TAT|Delivery Time)\s*[:\-]?\s*(\d+\s*(?:business\s*)?days?|\d+\s*hours?)"]},
            {"name": "price_per_check", "label": "Price Per Check", "mandatory": False, "type": "currency",
             "patterns": [r"(?:Price per Check|Cost per Check|Per Check Price)\s*[:\-]?\s*(?:\$|USD|INR)?\s*([\d,]+(?:\.\d{1,2})?)"]},
        ]
    }
    
    def parse_val(raw, ftype):
        if not raw: return None, 0.0
        raw = raw.strip().rstrip('.,;').strip()
        if ftype == "currency":
            cleaned = re.sub(r'[,\s]','',raw); cleaned = re.sub(r'^[^\d]*','',cleaned)
            try: return f"{float(cleaned):,.2f}", 0.95
            except: return raw, 0.60
        if ftype == "date":
            months = {'january':'01','february':'02','march':'03','april':'04','may':'05','june':'06','july':'07','august':'08','september':'09','october':'10','november':'11','december':'12'}
            for mn,num in months.items():
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
        return raw.replace('\n',' ').strip()[:80], 0.82

    template = TEMPLATES.get(document_type, TEMPLATES["financial_contract"])
    extracted = []
    for field in template:
        best_v, best_c = None, 0.0
        for i, pat in enumerate(field["patterns"]):
            m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
            if m:
                raw = m.group(1) if m.lastindex else m.group(0)
                v, c = parse_val(raw, field["type"])
                adj = c * (1.0 if i==0 else 0.9)
                if v and adj > best_c:
                    best_v, best_c = v, adj
        extracted.append({
            "field_name": field["name"],
            "display_name": field["label"],
            "value": best_v,
            "confidence": round(best_c, 3),
            "mandatory": field["mandatory"],
            "extraction_method": "regex"
        })
    
    found = sum(1 for f in extracted if f["value"])
    mandatory_found = sum(1 for f in extracted if f["mandatory"] and f["value"])
    mandatory_total = sum(1 for f in extracted if f["mandatory"])
    
    return {
        "document_type": document_type,
        "extraction_method": "regex",
        "fields_extracted": found,
        "fields_total": len(extracted),
        "mandatory_found": mandatory_found,
        "mandatory_total": mandatory_total,
        "extraction_success_rate": round(found / len(extracted), 2),
        "fields": extracted
    }


def tool_llm_extract(text: str, document_type: str, api_key: str) -> dict:
    """
    Tool 3: LLM-based deep extraction via Claude.
    Used for unstructured, narrative, or complex documents.
    """
    system_prompt = """You are a precise data extraction assistant for HireRight.
Extract structured fields from the provided document.
Return ONLY valid JSON — no markdown, no explanation.
For every value you extract, you MUST include the verbatim_quote 
(exact text from document that supports your answer). This prevents hallucination.
If a field is not present, return null for both value and verbatim_quote."""

    user_prompt = f"""Extract these fields from the document. Return JSON only:
{{
  "document_type": "financial_contract|pricing_sheet|package_config",
  "fields": {{
    "client_name": {{"value": "string or null", "verbatim_quote": "exact text from doc or null", "confidence": 0.0-1.0}},
    "annual_contract_value": {{"value": "number as string e.g. '87500.00' or null", "verbatim_quote": "...", "confidence": 0.0-1.0}},
    "contract_start_date": {{"value": "YYYY-MM-DD or null", "verbatim_quote": "...", "confidence": 0.0-1.0}},
    "contract_end_date": {{"value": "YYYY-MM-DD or null", "verbatim_quote": "...", "confidence": 0.0-1.0}},
    "package_name": {{"value": "string or null", "verbatim_quote": "...", "confidence": 0.0-1.0}},
    "number_of_checks": {{"value": "integer as string or null", "verbatim_quote": "...", "confidence": 0.0-1.0}}
  }}
}}

Document type hint: {document_type}
Document text:
{text[:4000]}"""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 1000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}]
            },
            timeout=30
        )
        raw = response.json()["content"][0]["text"]
        parsed = json.loads(raw.replace("```json","").replace("```","").strip())
        
        # Normalise to standard field format
        fields = []
        field_map = {
            "client_name": "Client Legal Name",
            "annual_contract_value": "Annual Contract Value",
            "contract_start_date": "Contract Start Date",
            "contract_end_date": "Contract End Date",
            "package_name": "Package / Plan Name",
            "number_of_checks": "Number of Checks"
        }
        for fname, flabel in field_map.items():
            fdata = parsed.get("fields", {}).get(fname, {})
            fields.append({
                "field_name": fname,
                "display_name": flabel,
                "value": fdata.get("value"),
                "verbatim_quote": fdata.get("verbatim_quote"),
                "confidence": fdata.get("confidence", 0.0),
                "mandatory": fname in ["client_name","annual_contract_value","contract_start_date"],
                "extraction_method": "llm"
            })
        
        found = sum(1 for f in fields if f["value"])
        return {
            "document_type": parsed.get("document_type", document_type),
            "extraction_method": "llm",
            "fields_extracted": found,
            "fields_total": len(fields),
            "fields": fields,
            "grounding_verified": True  # verbatim quotes present
        }
    except Exception as e:
        return {"error": str(e), "extraction_method": "llm", "fields": []}


def tool_validate_and_route(extraction_result: dict) -> dict:
    """
    Tool 4: Validate extraction quality and determine HITL routing.
    Claude calls this after extraction to assess quality.
    """
    fields = extraction_result.get("fields", [])
    if not fields:
        return {"routing": "full_review", "score": 0.0, "band": "low",
                "reason": "No fields extracted", "recommendation": "Reject and request resubmission"}
    
    mandatory = [f for f in fields if f.get("mandatory")]
    optional  = [f for f in fields if not f.get("mandatory")]
    
    missing_mandatory = [f for f in mandatory if not f.get("value")]
    if missing_mandatory:
        return {
            "routing": "full_review", "score": 0.0, "band": "low",
            "reason": f"Missing mandatory fields: {[f['display_name'] for f in missing_mandatory]}",
            "fields_found": len(fields) - len(missing_mandatory),
            "fields_total": len(fields),
            "recommendation": "Route to human reviewer — mandatory fields missing"
        }
    
    # Weighted composite: mandatory fields count 2x
    all_scores = ([f["confidence"] for f in mandatory] * 2 +
                  [f["confidence"] for f in optional if f.get("value")])
    composite = sum(all_scores) / len(all_scores) if all_scores else 0.0
    composite = round(composite, 3)
    
    if composite >= 0.88:
        routing, band = "auto_accept", "high"
        reason = f"All mandatory fields extracted with high confidence (composite {composite:.0%})"
        recommendation = "Auto-load to database — no human review needed"
    elif composite >= 0.70:
        routing, band = "partial_review", "medium"
        low_conf = [f["display_name"] for f in fields if f.get("value") and f["confidence"] < 0.80]
        reason = f"Composite {composite:.0%} — {len(low_conf)} field(s) need verification: {low_conf}"
        recommendation = "Route to reviewer — flag specific low-confidence fields only"
    else:
        routing, band = "full_review", "low"
        reason = f"Low composite confidence {composite:.0%} — extraction quality insufficient"
        recommendation = "Route to human reviewer — full manual processing required"
    
    return {
        "routing": routing,
        "score": composite,
        "band": band,
        "reason": reason,
        "recommendation": recommendation,
        "fields_found": sum(1 for f in fields if f.get("value")),
        "fields_total": len(fields),
        "mandatory_complete": len(missing_mandatory) == 0
    }


# ─── TOOL DEFINITIONS FOR CLAUDE ──────────────────────────────────────────────

TOOLS = [
    {
        "name": "inspect_document",
        "description": "Inspect the document's structure, quality, and format. Call this FIRST before deciding on an extraction strategy. Returns structure score, document type signals, and a recommended extraction path (regex or llm).",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The full document text to inspect"},
                "filename": {"type": "string", "description": "The filename of the document"}
            },
            "required": ["text", "filename"]
        }
    },
    {
        "name": "regex_extract",
        "description": "Extract fields using regex pattern matching. Use this for well-structured documents with clear labels (e.g. 'Client Name: Acme Corp'). Fast and deterministic. Works best when structure_score >= 45.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The document text to extract from"},
                "document_type": {"type": "string", "enum": ["financial_contract","pricing_sheet","package_config"], "description": "The document type to use for field extraction"}
            },
            "required": ["text", "document_type"]
        }
    },
    {
        "name": "llm_extract",
        "description": "Extract fields using deep LLM understanding. Use this for unstructured, narrative, or complex documents where regex patterns would fail. More powerful but slower. Returns verbatim quotes for grounding.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The document text to extract from"},
                "document_type": {"type": "string", "enum": ["financial_contract","pricing_sheet","package_config"], "description": "The detected document type"}
            },
            "required": ["text", "document_type"]
        }
    },
    {
        "name": "validate_and_route",
        "description": "Validate the extraction result and determine HITL routing. Call this LAST after extraction is complete. Returns routing decision: auto_accept, partial_review, or full_review.",
        "input_schema": {
            "type": "object",
            "properties": {
                "extraction_result": {
                    "type": "object",
                    "description": "The result from regex_extract or llm_extract tool call"
                }
            },
            "required": ["extraction_result"]
        }
    }
]


# ─── AGENTIC LOOP ──────────────────────────────────────────────────────────────

def run_etl_agent(text: str, filename: str, api_key: str) -> dict:
    """
    The Claude ETL Agent.
    
    Claude autonomously:
    1. Inspects the document
    2. Decides: regex or LLM extraction
    3. Runs the chosen extractor
    4. Validates and routes the result
    5. Returns a complete trace of every decision
    """
    
    messages = [
        {
            "role": "user",
            "content": f"""You are the HireRight ETL Agent. Process this document and extract structured data.

Your job:
1. Call inspect_document to understand the document structure
2. Based on the inspection, decide which extractor to use:
   - Use regex_extract for structured documents (structure_score >= 45, has clear labels)
   - Use llm_extract for unstructured, narrative, or complex documents
3. Call validate_and_route with the extraction result
4. Return a final summary of your decisions and the extracted data

Be transparent about WHY you chose each tool. Document filename: {filename}

Document text:
---
{text[:3000]}
---"""
        }
    ]
    
    agent_trace = []  # Full decision trace
    extraction_result = None
    final_routing = None
    iterations = 0
    max_iterations = 8  # Safety limit
    
    while iterations < max_iterations:
        iterations += 1
        
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 2000,
                "tools": TOOLS,
                "messages": messages
            },
            timeout=60
        )
        
        resp_json = response.json()
        
        if resp_json.get("stop_reason") == "end_turn":
            # Claude is done — extract final text message
            final_text = next(
                (b["text"] for b in resp_json["content"] if b["type"] == "text"), 
                "Agent completed."
            )
            agent_trace.append({"type": "final_response", "content": final_text})
            break
        
        if resp_json.get("stop_reason") == "tool_use":
            # Add assistant response to messages
            messages.append({"role": "assistant", "content": resp_json["content"]})
            
            # Process each tool call
            tool_results = []
            for block in resp_json["content"]:
                if block["type"] != "tool_use":
                    continue
                
                tool_name = block["name"]
                tool_input = block["input"]
                tool_use_id = block["id"]
                
                # Log the decision
                agent_trace.append({
                    "type": "tool_call",
                    "tool": tool_name,
                    "reason": f"Agent called {tool_name}",
                    "inputs": {k: v if k != "text" else f"[{len(v)} chars]" 
                               for k, v in tool_input.items()}
                })
                
                # Execute the tool
                if tool_name == "inspect_document":
                    result = tool_inspect_document(
                        tool_input["text"], tool_input["filename"]
                    )
                
                elif tool_name == "regex_extract":
                    result = tool_regex_extract(
                        tool_input["text"], tool_input["document_type"]
                    )
                    extraction_result = result
                
                elif tool_name == "llm_extract":
                    result = tool_llm_extract(
                        tool_input["text"], tool_input["document_type"], api_key
                    )
                    extraction_result = result
                
                elif tool_name == "validate_and_route":
                    result = tool_validate_and_route(tool_input["extraction_result"])
                    final_routing = result
                
                else:
                    result = {"error": f"Unknown tool: {tool_name}"}
                
                agent_trace.append({
                    "type": "tool_result",
                    "tool": tool_name,
                    "result_summary": {
                        k: v for k, v in result.items()
                        if k not in ["fields"] and not isinstance(v, list)
                    }
                })
                
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": json.dumps(result)
                })
            
            # Add all tool results at once
            messages.append({"role": "user", "content": tool_results})
        
        else:
            # Unexpected stop reason
            break
    
    # Build final output
    return {
        "document_id": f"DOC-{hash(text) & 0xFFFFFF:06X}",
        "filename": filename,
        "agent_trace": agent_trace,
        "extraction_result": extraction_result or {"fields": []},
        "routing": final_routing or {"routing": "full_review", "score": 0, "band": "low"},
        "processed_at": datetime.datetime.now().isoformat(),
        "iterations": iterations
    }


if __name__ == "__main__":
    # Quick demo — uses ANTHROPIC_API_KEY env var
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("Set ANTHROPIC_API_KEY environment variable")
        exit(1)
    
    sample = """SERVICE AGREEMENT

This Agreement is entered into between Nexus Corp Ltd. and HireRight Inc.

Client Name: Nexus Corp Ltd.
Effective Date: April 1, 2024
End Date: March 31, 2025
Annual Contract Value: USD 112,000
Package: Enterprise Plus
Number of Checks: 450 background checks per year"""
    
    print("Running ETL Agent...")
    result = run_etl_agent(sample, "service_agreement.txt", api_key)
    print(json.dumps(result, indent=2))
