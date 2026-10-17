import os
import re
import time
import random
import threading
from urllib.parse import urlparse
import pandas as pd
import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# Supports index.html whether placed in 'templates/index.html' or root './index.html'
template_dir = "templates" if os.path.exists("templates/index.html") else "."
app = Flask(__name__, template_folder=template_dir)

# Evolution API v2 Config (Loaded from .env with fallback defaults)
EVO_URL = os.getenv("EVO_URL", "http://localhost:8080").rstrip("/")
API_KEY = os.getenv("AUTHENTICATION_API_KEY", "PulseCastSecretKey2026")
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "91")

HEADERS = {
    "apikey": API_KEY,
    "Content-Type": "application/json"
}

campaign_lock = threading.Lock()
campaign_status = {
    "running": False,
    "total": 0,
    "sent": 0,
    "failed": 0,
    "current_lead": "",
    "logs": []
}


def add_log(message: str):
    timestamp = time.strftime("%H:%M:%S")
    with campaign_lock:
        campaign_status["logs"].append(f"[{timestamp}] {message}")
        if len(campaign_status["logs"]) > 250:
            campaign_status["logs"].pop(0)


def clean_phone(raw_phone: str) -> str:
    """Extracts digits cleanly, avoiding pandas .0 float artifacts."""
    raw_str = str(raw_phone).split(".")[0].strip()
    digits = re.sub(r"\D", "", raw_str)
    if len(digits) == 10:
        return f"{DEFAULT_COUNTRY_CODE}{digits}"
    return digits


def detect_media_type(url: str):
    """Accurately detects media type and extracts filename even when URL has query params."""
    parsed_path = urlparse(url.strip()).path
    lower_path = parsed_path.lower()
    raw_name = os.path.basename(parsed_path)
    clean_name = re.sub(r"[^a-zA-Z0-9._-]", "_", raw_name) if raw_name else ""

    if lower_path.endswith(".pdf"):
        return "document", "application/pdf", clean_name or "Document.pdf"
    elif lower_path.endswith(".png"):
        return "image", "image/png", clean_name or "Image.png"
    elif lower_path.endswith(".mp4"):
        return "video", "video/mp4", clean_name or "Video.mp4"
    return "image", "image/jpeg", clean_name or "Image.jpg"


def render_template_vars(template: str, row_norm: dict) -> str:
    """Replaces any {column_name} placeholder dynamically (case-insensitive) from CSV row."""
    def replacer(match):
        key = match.group(1).strip().lower()
        val = row_norm.get(key)
        if val:
            return val
        if key == "name":
            return "there"
        return match.group(0)
    return re.sub(r"\{([a-zA-Z0-9_-]+)\}", replacer, template)


@app.route('/api/connect/<instance_name>', methods=['GET'])
def connect_instance(instance_name):
    try:
        # 1. Check if instance is already open/connected
        state_res = requests.get(
            f"{EVO_URL}/instance/connectionState/{instance_name}",
            headers=HEADERS,
            timeout=8
        )
        if state_res.status_code == 200:
            state_data = state_res.json()
            state = state_data.get("instance", {}).get("state") or state_data.get("state")
            if state == "open":
                return jsonify({"status": "connected", "state": "open"})

        # 2. Try creating instance
        create_payload = {
            "instanceName": instance_name,
            "qrcode": True,
            "integration": "WHATSAPP-BAILEYS"
        }
        create_res = requests.post(
            f"{EVO_URL}/instance/create",
            json=create_payload,
            headers=HEADERS,
            timeout=12
        )
        create_data = create_res.json()

        if "qrcode" in create_data and isinstance(create_data["qrcode"], dict) and "base64" in create_data["qrcode"]:
            return jsonify({"base64": create_data["qrcode"]["base64"], "state": "connecting"})

        # 3. If instance already exists, fetch fresh QR from connect endpoint
        conn_res = requests.get(
            f"{EVO_URL}/instance/connect/{instance_name}",
            headers=HEADERS,
            timeout=12
        )
        conn_data = conn_res.json()
        if "base64" in conn_data:
            return jsonify({"base64": conn_data["base64"], "state": "connecting"})
        elif "qrcode" in conn_data and isinstance(conn_data["qrcode"], dict) and "base64" in conn_data["qrcode"]:
            return jsonify({"base64": conn_data["qrcode"]["base64"], "state": "connecting"})

        return jsonify(conn_data)
    except Exception as e:
        return jsonify({"error": f"Evolution API unreachable at {EVO_URL}: {str(e)}"}), 500


@app.route('/api/status/<instance_name>', methods=['GET'])
def check_status(instance_name):
    try:
        res = requests.get(
            f"{EVO_URL}/instance/connectionState/{instance_name}",
            headers=HEADERS,
            timeout=6
        )
        data = res.json()
        state = data.get("instance", {}).get("state") or data.get("state", "disconnected")
        return jsonify({"state": state, "raw": data})
    except Exception as e:
        return jsonify({"state": "offline", "error": str(e)}), 200


@app.route('/api/logout/<instance_name>', methods=['DELETE'])
def logout_instance(instance_name):
    try:
        res = requests.delete(
            f"{EVO_URL}/instance/logout/{instance_name}",
            headers=HEADERS,
            timeout=8
        )
        return jsonify(res.json()), res.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def run_broadcast(instance_name, leads, message_template, media_url, min_delay, max_delay):
    global campaign_status
    with campaign_lock:
        campaign_status["running"] = True
        campaign_status["total"] = len(leads)
        campaign_status["sent"] = 0
        campaign_status["failed"] = 0
        campaign_status["current_lead"] = ""
        campaign_status["logs"] = []

    add_log(f"🚀 PulseCast campaign initialized for {len(leads)} contacts on '{instance_name}'")

    for idx, row in enumerate(leads, start=1):
        if not campaign_status["running"]:
            add_log("🛑 Campaign manually stopped by user.")
            break

        # Case-insensitive column lookup for universal CSV support
        row_norm = {str(k).strip().lower(): str(v).strip() for k, v in row.items() if k is not None}
        name = row_norm.get("name") or "Contact"
        raw_phone = (
            row_norm.get("phone")
            or row_norm.get("mobile")
            or row_norm.get("number")
            or row_norm.get("whatsapp")
            or ""
        )

        phone = clean_phone(raw_phone)
        if not phone or len(phone) < 10:
            with campaign_lock:
                campaign_status["failed"] += 1
            add_log(f"⚠️ Row {idx}: Skipped invalid phone '{raw_phone}' ({name})")
            continue

        row_norm["phone"] = phone

        with campaign_lock:
            campaign_status["current_lead"] = f"{name} (+{phone})"

        custom_msg = render_template_vars(message_template, row_norm)

        try:
            if media_url and media_url.strip():
                mtype, mimetype, file_name = detect_media_type(media_url)
                payload = {
                    "number": phone,
                    "mediatype": mtype,
                    "mimetype": mimetype,
                    "caption": custom_msg,
                    "media": media_url.strip(),
                    "fileName": file_name,
                    "delay": 2500
                }
                endpoint = f"{EVO_URL}/message/sendMedia/{instance_name}"
            else:
                payload = {
                    "number": phone,
                    "text": custom_msg,
                    "delay": 2500,
                    "linkPreview": True
                }
                endpoint = f"{EVO_URL}/message/sendText/{instance_name}"

            res = requests.post(endpoint, json=payload, headers=HEADERS, timeout=20)

            if res.status_code in [200, 201]:
                with campaign_lock:
                    campaign_status["sent"] += 1
                add_log(f"✅ [{idx}/{len(leads)}] Sent to {name} (+{phone})")
            else:
                with campaign_lock:
                    campaign_status["failed"] += 1
                add_log(f"❌ [{idx}/{len(leads)}] Failed +{phone}: {res.text[:90]}")

        except Exception as e:
            with campaign_lock:
                campaign_status["failed"] += 1
            add_log(f"⚠️ [{idx}/{len(leads)}] Error +{phone}: {str(e)}")

        # Wait anti-ban delay only if there are more contacts remaining
        if idx < len(leads) and campaign_status["running"]:
            sleep_time = random.randint(min_delay, max_delay)
            for _ in range(sleep_time):
                if not campaign_status["running"]:
                    break
                time.sleep(1)

    with campaign_lock:
        campaign_status["running"] = False
        campaign_status["current_lead"] = ""
    add_log("🎉 Campaign Completed!")


@app.route('/api/start-campaign', methods=['POST'])
def start_campaign():
    if campaign_status["running"]:
        return jsonify({"error": "A campaign is already running!"}), 400

    instance_name = request.form.get("instance_name", "PulseCast_Main").strip()
    message_template = request.form.get("message", "").strip()
    media_url = request.form.get("media_url", "").strip()
    min_delay = int(request.form.get("min_delay", 8))
    max_delay = int(request.form.get("max_delay", 16))
    csv_file = request.files.get("file")

    if min_delay > max_delay:
        min_delay, max_delay = max_delay, min_delay

    if not csv_file or not message_template:
        return jsonify({"error": "Both a CSV file and message template are required."}), 400

    try:
        df = pd.read_csv(csv_file, dtype=str).fillna("")
    except Exception as e:
        return jsonify({"error": f"Failed to parse CSV file: {str(e)}"}), 400

    leads = df.to_dict(orient="records")
    if not leads:
        return jsonify({"error": "The uploaded CSV file contains no rows."}), 400

    thread = threading.Thread(
        target=run_broadcast,
        args=(instance_name, leads, message_template, media_url, min_delay, max_delay),
        daemon=True
    )
    thread.start()

    return jsonify({"message": "Campaign started!", "total_leads": len(leads)})


@app.route('/api/stop-campaign', methods=['POST'])
def stop_campaign():
    if not campaign_status["running"]:
        return jsonify({"message": "No active campaign to stop."})
    with campaign_lock:
        campaign_status["running"] = False
    return jsonify({"message": "Stopping campaign after current message..."})


@app.route('/api/campaign-progress', methods=['GET'])
def get_progress():
    with campaign_lock:
        return jsonify(campaign_status)


@app.route('/')
def home():
    return render_template('index.html')


if __name__ == '__main__':
    port = int(os.getenv("FLASK_PORT", 5000))
    debug_mode = os.getenv("FLASK_DEBUG", "true").lower() == "true"
    app.run(host='0.0.0.0', port=port, debug=debug_mode)