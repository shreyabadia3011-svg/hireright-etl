import sqlite3, json, datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "hireright_etl.db"

def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL,
            file_type TEXT, size_mb REAL,
            doc_type TEXT, method_used TEXT,
            structure_score INTEGER, routing TEXT,
            composite_score REAL, status TEXT DEFAULT 'extracted',
            processed_at TEXT, approved_at TEXT, approved_by TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS extracted_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT NOT NULL, field_name TEXT NOT NULL,
            field_label TEXT, value TEXT, confidence REAL,
            method TEXT, verbatim_quote TEXT,
            mandatory INTEGER DEFAULT 0, was_corrected INTEGER DEFAULT 0,
            corrected_value TEXT, created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (doc_id) REFERENCES documents(doc_id)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT, action TEXT,
            performed_by TEXT DEFAULT 'system',
            details TEXT, created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_fields_doc ON extracted_fields(doc_id);
        CREATE INDEX IF NOT EXISTS idx_docs_status ON documents(status);
        CREATE INDEX IF NOT EXISTS idx_docs_type ON documents(doc_type);
    """)
    conn.commit()
    conn.close()

def save_document(result):
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO documents
              (doc_id,filename,file_type,size_mb,doc_type,method_used,
               structure_score,routing,composite_score,status,processed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(doc_id) DO UPDATE SET
              status=excluded.status, processed_at=excluded.processed_at
        """, (result["doc_id"],result["filename"],result.get("file_type",""),
              result.get("size_mb",0),result.get("doc_type",""),
              result.get("method_used",""),
              result.get("structure_analysis",{}).get("structure_score",0),
              result.get("routing",{}).get("routing",""),
              result.get("routing",{}).get("score",0),
              "extracted",result.get("processed_at",datetime.datetime.now().isoformat())))
        conn.execute("DELETE FROM extracted_fields WHERE doc_id=?", (result["doc_id"],))
        for f in result.get("fields",[]):
            conn.execute("""
                INSERT INTO extracted_fields
                  (doc_id,field_name,field_label,value,confidence,
                   method,verbatim_quote,mandatory)
                VALUES (?,?,?,?,?,?,?,?)
            """, (result["doc_id"],f.get("name",""),f.get("label",""),
                  f.get("value"),f.get("confidence",0),f.get("method",""),
                  f.get("quote") or f.get("verbatim_quote"),
                  1 if f.get("mandatory") else 0))
        conn.execute("INSERT INTO audit_log (doc_id,action,details) VALUES (?,?,?)",
                     (result["doc_id"],"extracted",
                      f"method={result.get('method_used')},routing={result.get('routing',{}).get('routing')}"))
        conn.commit()
        return True
    except Exception as e:
        conn.rollback(); print(f"DB error: {e}"); return False
    finally:
        conn.close()

def approve_document(doc_id, corrections=None):
    conn = get_conn()
    try:
        conn.execute("""UPDATE documents SET status='approved',
            approved_at=datetime('now'),approved_by='PSO_Reviewer'
            WHERE doc_id=?""", (doc_id,))
        if corrections:
            for field_name, new_value in corrections.items():
                conn.execute("""UPDATE extracted_fields
                    SET was_corrected=1,corrected_value=?
                    WHERE doc_id=? AND field_name=?""", (new_value,doc_id,field_name))
        conn.execute("INSERT INTO audit_log (doc_id,action,details) VALUES (?,?,?)",
                     (doc_id,"approved",json.dumps(corrections or {})))
        conn.commit(); return True
    finally:
        conn.close()

def get_all_documents(status=None, doc_type=None, limit=100):
    conn = get_conn()
    q = "SELECT * FROM documents WHERE 1=1"
    params = []
    if status:   q += " AND status=?";   params.append(status)
    if doc_type: q += " AND doc_type=?"; params.append(doc_type)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_document_fields(doc_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM extracted_fields WHERE doc_id=? ORDER BY id",
        (doc_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_stats():
    conn = get_conn()
    stats = {}
    stats["total"] = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    stats["approved"] = conn.execute("SELECT COUNT(*) FROM documents WHERE status='approved'").fetchone()[0]
    stats["auto_accept"] = conn.execute("SELECT COUNT(*) FROM documents WHERE routing='auto_accept'").fetchone()[0]
    stats["needs_review"] = conn.execute("SELECT COUNT(*) FROM documents WHERE routing!='auto_accept'").fetchone()[0]
    stats["by_type"] = dict(conn.execute("SELECT doc_type,COUNT(*) FROM documents GROUP BY doc_type").fetchall())
    stats["by_method"] = dict(conn.execute("SELECT method_used,COUNT(*) FROM documents GROUP BY method_used").fetchall())
    stats["recent"] = [dict(r) for r in conn.execute(
        "SELECT doc_id,filename,doc_type,routing,status,created_at FROM documents ORDER BY created_at DESC LIMIT 5").fetchall()]
    conn.close()
    return stats

def search_documents(query):
    conn = get_conn()
    q = f"%{query}%"
    docs = conn.execute("""
        SELECT DISTINCT d.* FROM documents d
        LEFT JOIN extracted_fields f ON d.doc_id=f.doc_id
        WHERE d.filename LIKE ? OR f.value LIKE ?
        ORDER BY d.created_at DESC LIMIT 50
    """, (q,q)).fetchall()
    conn.close()
    return [dict(r) for r in docs]

init_db()
