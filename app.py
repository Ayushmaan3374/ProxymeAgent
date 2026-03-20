from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
import os
import time

app = Flask(__name__)
CORS(app)

# 🔐 Get API key from Render environment
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY not set in environment")

client = genai.Client(api_key=GEMINI_API_KEY)

# 🔹 In-memory store (temporary storage)
meeting_store = {}


@app.route("/upload-context", methods=["POST"])
def upload_context():

    data = request.get_json() or {}

    meeting_id = data.get("meeting_id")
    name = data.get("name", "").lower().strip()
    context = data.get("context", "")

    if not meeting_id or not name or not context:
        return jsonify({"error": "missing fields"}), 400

    key = (meeting_id, name)

    meeting_store[key] = {
        "raw_text": context,
        "summary": None,
        "last_used": None,
        "created_at": time.time()
    }

    return jsonify({"status": "uploaded"})


@app.route("/process", methods=["POST"])
def process():

    data = request.get_json() or {}

    meeting_id = data.get("meetingId")
    speech_text = data.get("speech_text", "")
    name = data.get("representor_name", "").lower().strip()

    key = (meeting_id, name)
    meeting = meeting_store.get(key)

    if not meeting:
        return jsonify({
            "ai_response": f"No context found for {name}"
        })

    # 🔥 Lazy summarization
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
            meeting["summary"] = response.text
            meeting["raw_text"] = None
        except Exception:
            return jsonify({"ai_response": "AI error during summarization"}), 500

    meeting["last_used"] = time.time()

    # 🔥 Main response
    prompt = f"""
You are {name} speaking in a meeting.

Context:
{meeting['summary']}

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
        return jsonify({"ai_response": "AI error during response"}), 500

    cleanup()

    return jsonify({"ai_response": response.text})


# 🔹 Cleanup inactive meetings
def cleanup():
    now = time.time()
    remove = []

    for key, m in meeting_store.items():
        if m["last_used"] and now - m["last_used"] > 3600:
            remove.append(key)

    for k in remove:
        del meeting_store[k]


@app.route("/health")
def health():
    return {"status": "running"}


# 🔥 Render-compatible run
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)