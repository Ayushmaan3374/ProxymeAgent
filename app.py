from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
import os
import time
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
CORS(app)

# 🔐 ENV VARIABLES
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY not set")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL not set")

client = genai.Client(api_key=GEMINI_API_KEY)


# 🔹 DB CONNECTION
def get_conn():
    return psycopg2.connect(DATABASE_URL)


# 🔹 INIT TABLE
def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS meetings (
        id SERIAL PRIMARY KEY,
        meeting_id TEXT,
        name TEXT,
        raw_text TEXT,
        summary TEXT,
        last_used DOUBLE PRECISION,
        created_at DOUBLE PRECISION
    )
    """)

    conn.commit()
    cur.close()
    conn.close()


init_db()


# =========================
# 📥 UPLOAD CONTEXT
# =========================
@app.route("/upload-context", methods=["POST"])
def upload_context():

    data = request.get_json() or {}

    meeting_id = data.get("meeting_id")
    name = data.get("name", "").lower().strip()
    context = data.get("context", "")

    if not meeting_id or not name or not context:
        return jsonify({"error": "missing fields"}), 400

    conn = get_conn()
    cur = conn.cursor()

    # overwrite if exists
    cur.execute("""
    DELETE FROM meetings WHERE meeting_id=%s AND name=%s
    """, (meeting_id, name))

    cur.execute("""
    INSERT INTO meetings (meeting_id, name, raw_text, summary, last_used, created_at)
    VALUES (%s, %s, %s, %s, %s, %s)
    """, (meeting_id, name, context, None, None, time.time()))

    conn.commit()

    print(f"✅ UPLOAD STORED | meeting_id={meeting_id} | name={name}")

    # 🔥 FIFO cleanup after insert
    fifo_cleanup(conn)

    cur.close()
    conn.close()

    return jsonify({"status": "uploaded"})

# =========================
# 📋 GET REPRESENTORS
# =========================
@app.route("/get-representors", methods=["GET"])
def get_representors():
    try:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
        SELECT meeting_id, name, summary
        FROM meetings
        ORDER BY created_at ASC
        """)

        rows = cur.fetchall()

        names = []
        for row in rows:
            names.append({
                "meeting_id": row[0],
                "name": row[1],
                "used": row[2] is not None   # ✅ summary used or not
            })

        cur.close()
        conn.close()

        return jsonify({"names": names})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    

# =========================
# 🔍 DEBUG DB (TEMPORARY)
# =========================
@app.route("/debug-db", methods=["GET"])
def debug_db():
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("""
        SELECT id, meeting_id, name, summary, created_at
        FROM meetings
        ORDER BY id DESC
        LIMIT 10
        """)

        rows = cur.fetchall()

        cur.close()
        conn.close()

        return jsonify({
            "count": len(rows),
            "data": rows
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
# =========================
# 🎤 PROCESS SPEECH
# =========================
@app.route("/process", methods=["POST"])
def process():

    data = request.get_json() or {}

    meeting_id = data.get("meetingId")
    speech_text = data.get("speech_text", "")
    name = data.get("representor_name", "").lower().strip()

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
    SELECT * FROM meetings
    WHERE meeting_id=%s AND name=%s
    """, (meeting_id, name))

    meeting = cur.fetchone()

    if not meeting:
        cur.close()
        conn.close()
        return jsonify({
            "ai_response": f"No context found for {name}"
        })

    # 🔥 Lazy summarization (FIRST USE)
    if meeting["summary"] is None:

        raw_text = meeting["raw_text"]

        prompt = f"""
Analyze this meeting agenda:

- main topic
- key points
- speaker tone

Agenda:
{raw_text}
"""

        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )

            summary = response.text

            # 🔥 Remove raw_text after use (your requirement)
            cur.execute("""
            UPDATE meetings
            SET summary=%s, raw_text=NULL
            WHERE meeting_id=%s AND name=%s
            """, (summary, meeting_id, name))

            conn.commit()

        except Exception:
            cur.close()
            conn.close()
            return jsonify({"ai_response": "AI error during summarization"}), 500

    else:
        summary = meeting["summary"]

    # 🔥 Generate response
    prompt = f"""
You are {name} speaking in a meeting.

Context:
{summary}

Question:
{speech_text}

Answer naturally and concisely.
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
    except Exception:
        cur.close()
        conn.close()
        return jsonify({"ai_response": "AI error during response"}), 500

    # 🔥 update last_used (marks session active)
    cur.execute("""
    UPDATE meetings
    SET last_used=%s
    WHERE meeting_id=%s AND name=%s
    """, (time.time(), meeting_id, name))

    conn.commit()

    # 🔥 cleanup (time-based)
    cleanup(conn)

    cur.close()
    conn.close()

    return jsonify({"ai_response": response.text})


# =========================
# 🧹 TIME-BASED CLEANUP
# =========================
def cleanup(conn):

    cur = conn.cursor()

    one_hour_ago = time.time() - 3600

    cur.execute("""
    DELETE FROM meetings
    WHERE last_used IS NOT NULL
    AND last_used < %s
    """, (one_hour_ago,))

    conn.commit()
    cur.close()


# =========================
# 🧹 FIFO CLEANUP (NEW)
# =========================
def fifo_cleanup(conn):

    cur = conn.cursor()

    # 🔥 max rows allowed (you can tune this)
    MAX_ROWS = 100

    cur.execute("SELECT COUNT(*) FROM meetings")
    count = cur.fetchone()[0]

    if count > MAX_ROWS:

        # delete oldest entries
        delete_count = count - MAX_ROWS

        cur.execute("""
        DELETE FROM meetings
        WHERE id IN (
            SELECT id FROM meetings
            ORDER BY created_at ASC
            LIMIT %s
        )
        """, (delete_count,))

        print(f"FIFO CLEANUP: removed {delete_count} old records")

    conn.commit()
    cur.close()


# =========================
# ❤️ HEALTH CHECK
# =========================
@app.route("/health")
def health():
    return {"status": "running"}


# =========================
# 🚀 RUN
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)