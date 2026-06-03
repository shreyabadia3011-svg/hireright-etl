# HireRight ETL Engine — Prototype

## Two Ways to Run

### Option A: Open directly in browser (no install needed)
Open `static/index.html` directly in Chrome/Safari/Firefox.
Works with text paste and .txt file upload.

### Option B: Full Flask server (supports PDF + Excel upload)
```bash
pip install -r requirements.txt
python app.py
```
Then open http://localhost:5050

## Project Structure
```
etl_prototype/
├── app.py              ← Flask server (PDF/Excel upload)
├── extractor.py        ← Core extraction engine
├── requirements.txt    ← Python dependencies
├── templates/          ← Field extraction templates (JSON)
│   ├── financial_contract.json
│   ├── pricing_sheet.json
│   └── package_config.json
├── static/
│   └── index.html      ← Full UI (works standalone too)
├── uploads/            ← Uploaded files (created automatically)
└── extracted/          ← Extraction results (created automatically)
```

## Document Types Supported
- Financial contracts (client name, ACV, dates, package)
- Pricing sheets (product, unit price, discount, validity)
- Package configurations (name, included checks, TAT, price/check)

## HITL Confidence Thresholds
- ≥ 88% → Auto-accept (no human review needed)
- 70–88% → Partial review (flag specific fields)
- < 70% → Full review (manual processing required)
