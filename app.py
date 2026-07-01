import os
import uuid
import base64
import threading
import json
import anthropic
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from build_prompt import build_prompt

app = Flask(__name__, static_folder="frontend", static_url_path="")
CORS(app)

# In-memory job store: id -> {status, html, error}
_jobs = {}
_jobs_lock = threading.Lock()

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _map_form(form, logo_b64, photo_urls):
    prestige_map = {"standard": "standard", "mix": "mid", "bespoke": "high"}
    team_map = {"sole": "sole trader", "small": "small team", "company": "established company"}
    urgency_map = {"emergency": "high", "ahead": "low"}
    commercial = int(form.get("commercial_split", 50))

    data = {
        "business_name": form.get("business_name", ""),
        "trade": form.get("trade", ""),
        "location": form.get("location", ""),
        "coverage_area": form.get("coverage_area", ""),
        "phone": form.get("phone", ""),
        "email": form.get("email", ""),
        "logo_uploaded": bool(logo_b64),
        "portfolio_uploaded": bool(photo_urls),
        "domestic_commercial_split": 100 - commercial,
        "craft_prestige": prestige_map.get(form.get("work_type", ""), "standard"),
        "team_size": team_map.get(form.get("team_size", ""), "sole trader"),
        "large_commercial_contracts": form.get("large_contracts") == "yes",
        "urgency": urgency_map.get(form.get("urgency", ""), "low"),
        "years_trading": form.get("years_trading", ""),
        "claimed_accreditations": form.get("accreditations", ""),
        "claimed_projects": form.get("past_clients", ""),
        "other_notes": form.get("notes", ""),
    }

    if photo_urls:
        data["other_notes"] = (data["other_notes"] + "\n\nPortfolio photos are available at these URLs (embed them as <img> tags in the portfolio section): " + ", ".join(photo_urls)).strip()

    return data


def _run(job_id, prompt, logo_b64, logo_mime):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    try:
        content = []
        if logo_b64 and logo_mime:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": logo_mime, "data": logo_b64},
            })
        content.append({"type": "text", "text": prompt})

        messages = [{"role": "user", "content": content}]
        accumulated_text = ""

        for _ in range(15):
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=16000,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=messages,
            )

            # Collect any text from this turn
            for block in resp.content:
                if hasattr(block, "text"):
                    accumulated_text += block.text

            if resp.stop_reason == "end_turn":
                break

            # Continue conversation for tool_use turns
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = [
                {"type": "tool_result", "tool_use_id": b.id, "content": ""}
                for b in resp.content
                if getattr(b, "type", "") == "tool_use"
            ]
            if tool_results:
                messages.append({"role": "user", "content": tool_results})
            else:
                break

        # Extract HTML block
        lower = accumulated_text.lower()
        idx = lower.find("<!doctype html>")
        html = accumulated_text[idx:] if idx != -1 else accumulated_text

        with _jobs_lock:
            _jobs[job_id] = {"status": "done", "html": html}

    except Exception as exc:
        with _jobs_lock:
            _jobs[job_id] = {"status": "error", "error": str(exc)}


@app.route("/api/generate", methods=["POST"])
def generate():
    form = request.form
    logo_file = request.files.get("logo")
    photo_files = request.files.getlist("photos")

    logo_b64 = None
    logo_mime = None
    if logo_file and logo_file.filename:
        data = logo_file.read()
        logo_b64 = base64.standard_b64encode(data).decode()
        logo_mime = logo_file.content_type or "image/png"

    job_id = uuid.uuid4().hex[:10]
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    # Determine base URL for photo serving
    base_url = request.host_url.rstrip("/")
    photo_urls = []
    for i, pf in enumerate(photo_files):
        if pf and pf.filename:
            ext = os.path.splitext(pf.filename)[1] or ".jpg"
            fname = f"photo_{i}{ext}"
            pf.save(os.path.join(job_dir, fname))
            photo_urls.append(f"{base_url}/api/generate/{job_id}/photos/{fname}")

    build_data = _map_form(form, logo_b64, photo_urls)
    prompt = build_prompt(build_data)

    with _jobs_lock:
        _jobs[job_id] = {"status": "pending"}

    t = threading.Thread(target=_run, args=(job_id, prompt, logo_b64, logo_mime), daemon=True)
    t.start()

    return jsonify({"id": job_id})


@app.route("/api/generate/<job_id>/status")
def job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"status": "not_found"}), 404
    result = {"status": job["status"]}
    if job["status"] == "error":
        result["error"] = job.get("error", "Unknown error")
    return jsonify(result)


@app.route("/api/generate/<job_id>/html")
def job_html(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    if job["status"] != "done":
        return jsonify({"error": "not ready", "status": job["status"]}), 409
    return job["html"], 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/generate/<job_id>/photos/<filename>")
def job_photo(job_id, filename):
    job_dir = os.path.join(UPLOAD_DIR, job_id)
    return send_from_directory(job_dir, filename)


# Serve frontend static files (fallback for local dev)
@app.route("/", defaults={"path": "index.html"})
@app.route("/<path:path>")
def frontend(path):
    frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")
    if os.path.exists(os.path.join(frontend_dir, path)):
        return send_from_directory(frontend_dir, path)
    return send_from_directory(frontend_dir, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
