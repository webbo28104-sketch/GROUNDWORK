import os
import re
import uuid
import base64
import threading
from datetime import datetime, timedelta
from functools import wraps

import anthropic
from flask import Flask, request, jsonify, send_from_directory, session, redirect, url_for, render_template_string
from flask_cors import CORS
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from build_prompt import build_prompt
from models import SessionLocal, Lead, Generation, init_db
from emails import send_verification_email, send_resend_email

app = Flask(__name__, static_folder="frontend", static_url_path="")
CORS(app)
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-secret-change-me")
serializer = URLSafeTimedSerializer(app.secret_key)

TOKEN_MAX_AGE = 24 * 3600  # 24h magic-link expiry
IP_RATE_LIMIT_PER_HOUR = int(os.environ.get("IP_RATE_LIMIT_PER_HOUR", "5"))

init_db()

# In-memory job store for in-flight generations: id -> {status, html, error}
# This is a live-progress cache only — completed generations are persisted to
# the `generations` table and served from there once this entry ages out
# (e.g. after a process restart).
_jobs = {}
_jobs_lock = threading.Lock()

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _map_form(form, logo_present, photo_urls):
    prestige_map = {"standard": "standard", "mix": "mid", "bespoke": "high"}
    team_map = {"sole": "sole trader", "small": "small team", "company": "established company"}
    urgency_map = {"emergency": "high", "ahead": "low"}
    commercial = int(form.get("commercial_split", 50))
    domestic = 100 - commercial

    data = {
        "business_name": form.get("business_name", ""),
        "trade": form.get("trade", ""),
        "location": form.get("location", ""),
        "coverage_area": form.get("coverage_area", ""),
        "phone": form.get("phone", ""),
        "email": form.get("email", ""),
        "logo_uploaded": bool(logo_present),
        "portfolio_uploaded": bool(photo_urls),
        "work_split": f"{domestic}% domestic / {commercial}% commercial",
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


def _run_and_persist(job_id, lead_id, email, business_name, prompt, logo_b64, logo_mime):
    _run(job_id, prompt, logo_b64, logo_mime)
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job.get("status") != "done":
        return
    db = SessionLocal()
    try:
        db.add(Generation(
            lead_id=lead_id,
            email=email,
            business_name=business_name,
            html_content=job["html"],
            status="draft",
        ))
        db.commit()
    finally:
        db.close()


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


@app.route("/api/generate", methods=["POST"])
def generate():
    form = request.form
    email = (form.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "invalid_email", "message": "A valid email address is required."}), 400

    ip = _client_ip()
    base_url = request.host_url.rstrip("/")

    db = SessionLocal()
    try:
        # Block repeat NEW generations from an email that already has one.
        existing_gen = db.query(Generation).filter(Generation.email == email).first()
        if existing_gen:
            return jsonify({
                "error": "already_generated",
                "message": "You've already generated a site with this email. Check your inbox for the link, or use the resend page to get a new one.",
            }), 409

        # Per-IP rate limit.
        if ip:
            window_start = datetime.utcnow() - timedelta(hours=1)
            recent_from_ip = db.query(Lead).filter(Lead.ip == ip, Lead.created_at >= window_start).count()
            if recent_from_ip >= IP_RATE_LIMIT_PER_HOUR:
                return jsonify({
                    "error": "rate_limited",
                    "message": "Too many submissions from this network recently. Please try again later.",
                }), 429

        # Reuse a still-pending lead for this email instead of creating a duplicate.
        pending_window = datetime.utcnow() - timedelta(hours=24)
        lead = (
            db.query(Lead)
            .filter(Lead.email == email, Lead.status == "pending_verification", Lead.created_at >= pending_window)
            .order_by(Lead.created_at.desc())
            .first()
        )
        if lead is None:
            lead = Lead(public_id=uuid.uuid4().hex[:10], email=email, ip=ip, status="pending_verification", form_data={})
            db.add(lead)
            db.flush()

        job_dir = os.path.join(UPLOAD_DIR, lead.public_id)
        os.makedirs(job_dir, exist_ok=True)

        logo_file = request.files.get("logo")
        logo_path, logo_mime = lead.logo_path, lead.logo_mime
        if logo_file and logo_file.filename:
            ext = os.path.splitext(logo_file.filename)[1] or ".png"
            fname = f"logo{ext}"
            logo_file.save(os.path.join(job_dir, fname))
            logo_path = fname
            logo_mime = logo_file.content_type or "image/png"

        for i, pf in enumerate(request.files.getlist("photos")):
            if pf and pf.filename:
                ext = os.path.splitext(pf.filename)[1] or ".jpg"
                pf.save(os.path.join(job_dir, f"photo_{i}{ext}"))

        photo_urls = [
            f"{base_url}/api/generate/{lead.public_id}/photos/{fname}"
            for fname in sorted(os.listdir(job_dir))
            if fname.startswith("photo_")
        ]

        build_data = _map_form(form, logo_path, photo_urls)

        lead.email = email
        lead.ip = ip
        lead.form_data = build_data
        lead.logo_path = logo_path
        lead.logo_mime = logo_mime
        db.commit()

        token = serializer.dumps({"lead_id": lead.id})
        verify_url = f"{base_url}/verify/{token}"
        send_verification_email(email, verify_url, build_data.get("business_name", ""))

        return jsonify({"status": "check_email", "email": email})
    finally:
        db.close()


@app.route("/verify/<token>")
def verify(token):
    try:
        data = serializer.loads(token, max_age=TOKEN_MAX_AGE)
    except SignatureExpired:
        return redirect("/verify-error.html?reason=expired")
    except BadSignature:
        return redirect("/verify-error.html?reason=invalid")

    db = SessionLocal()
    try:
        lead = db.get(Lead, data.get("lead_id"))
        if not lead:
            return redirect("/verify-error.html?reason=invalid")

        # Idempotent: if this lead already has a generation, just send them to it.
        existing_gen = db.query(Generation).filter(Generation.lead_id == lead.id).first()
        if existing_gen:
            with _jobs_lock:
                _jobs[lead.public_id] = {"status": "done", "html": existing_gen.html_content}
            return redirect(f"/preview.html?id={lead.public_id}")

        lead.status = "verified"
        db.commit()

        build_data = dict(lead.form_data)
        prompt = build_prompt(build_data)

        logo_b64 = None
        if lead.logo_path:
            logo_file_path = os.path.join(UPLOAD_DIR, lead.public_id, lead.logo_path)
            if os.path.exists(logo_file_path):
                with open(logo_file_path, "rb") as f:
                    logo_b64 = base64.standard_b64encode(f.read()).decode()

        with _jobs_lock:
            _jobs[lead.public_id] = {"status": "pending"}

        t = threading.Thread(
            target=_run_and_persist,
            args=(lead.public_id, lead.id, lead.email, build_data.get("business_name", ""), prompt, logo_b64, lead.logo_mime),
            daemon=True,
        )
        t.start()

        return redirect(f"/loading.html?id={lead.public_id}")
    finally:
        db.close()


@app.route("/api/resend", methods=["POST"])
def resend():
    if request.is_json:
        email = (request.json or {}).get("email", "")
    else:
        email = request.form.get("email", "")
    email = (email or "").strip().lower()
    if email:
        db = SessionLocal()
        try:
            has_sites = db.query(Generation).filter(Generation.email == email).first() is not None
            if has_sites:
                token = serializer.dumps({"resend_email": email})
                my_sites_url = f"{request.host_url.rstrip('/')}/my-sites/{token}"
                send_resend_email(email, my_sites_url)
        finally:
            db.close()
    # Always respond the same way regardless of whether the email has sites,
    # so this endpoint can't be used to enumerate which addresses have generated one.
    return jsonify({"status": "sent"})


_PAGE_STYLE = """
*{box-sizing:border-box;}
body{margin:0;background:#F5F3EE;font-family:Inter,Arial,sans-serif;color:#1C1C1C;min-height:100vh;}
.wrap{max-width:640px;margin:0 auto;padding:56px 24px;}
h1{font-size:26px;font-weight:800;letter-spacing:-.02em;margin:0 0 16px;}
a.btn{display:inline-block;background:#3B82F6;color:#fff;font-weight:700;text-decoration:none;padding:12px 22px;border-radius:8px;margin-top:8px;}
.card{background:#fff;border:1px solid #E2E0DA;border-radius:12px;padding:20px 24px;margin-bottom:14px;}
.muted{color:#5C5A56;font-size:14px;}
table{width:100%;border-collapse:collapse;font-size:14px;}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #E2E0DA;}
th{color:#5C5A56;font-weight:600;font-size:12.5px;text-transform:uppercase;letter-spacing:.04em;}
input[type=text],input[type=password],input[type=email]{width:100%;padding:11px 14px;border:1px solid #D8D5CE;border-radius:8px;font-size:15px;margin-bottom:12px;}
button{background:#3B82F6;color:#fff;border:0;font-weight:700;padding:12px 22px;border-radius:8px;font-size:15px;cursor:pointer;}
.err{color:#B42318;font-size:14px;margin-bottom:12px;}
"""


@app.route("/my-sites/<token>")
def my_sites(token):
    try:
        data = serializer.loads(token, max_age=TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return redirect("/verify-error.html?reason=invalid")

    email = data.get("resend_email")
    db = SessionLocal()
    try:
        gens = db.query(Generation).filter(Generation.email == email).order_by(Generation.created_at.desc()).all()
        rows = "".join(
            f'<div class="card"><strong>{g.business_name or "Untitled site"}</strong><br>'
            f'<span class="muted">Generated {g.created_at:%d %b %Y}</span><br>'
            f'<a class="btn" href="/api/generate/{g.lead.public_id}/html" target="_blank" rel="noopener">View site →</a></div>'
            for g in gens
        ) or '<p class="muted">No sites found for this email yet.</p>'
        return render_template_string(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Your Groundwork sites</title><style>{_PAGE_STYLE}</style></head>
<body><div class="wrap"><h1>Your Groundwork website(s)</h1>{rows}</div></body></html>""")
    finally:
        db.close()


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        admin_user = os.environ.get("ADMIN_USERNAME")
        admin_pass = os.environ.get("ADMIN_PASSWORD")
        if admin_user and admin_pass and u == admin_user and p == admin_pass:
            session["is_admin"] = True
            return redirect(request.args.get("next") or url_for("admin_generations"))
        error = "Invalid credentials."
    error_html = f'<p class="err">{error}</p>' if error else ""
    return render_template_string(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Admin login</title><style>{_PAGE_STYLE}</style></head>
<body><div class="wrap" style="max-width:360px;"><h1>Admin login</h1>{error_html}
<form method="post">
<input type="text" name="username" placeholder="Username" autofocus>
<input type="password" name="password" placeholder="Password">
<button type="submit">Log in</button>
</form></div></body></html>""")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin/generations")
@admin_required
def admin_generations():
    db = SessionLocal()
    try:
        gens = db.query(Generation).order_by(Generation.created_at.desc()).all()
        rows = "".join(
            f"<tr><td>{g.business_name or ''}</td><td>{g.email}</td>"
            f"<td>{g.created_at:%d %b %Y %H:%M}</td><td>{g.status}</td>"
            f'<td><a href="/admin/generations/{g.id}/html" target="_blank" rel="noopener">View HTML</a> · '
            f'<a href="/admin/generations/{g.id}/form-data" target="_blank" rel="noopener">Form data</a></td></tr>'
            for g in gens
        )
        return render_template_string(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Admin — generations</title><style>{_PAGE_STYLE}</style></head>
<body><div class="wrap" style="max-width:1100px;">
<h1>All generations ({len(gens)}) <a href="/admin/logout" style="float:right;font-size:13px;">Log out</a></h1>
<table><thead><tr><th>Business</th><th>Email</th><th>Created</th><th>Status</th><th>Links</th></tr></thead>
<tbody>{rows}</tbody></table></div></body></html>""")
    finally:
        db.close()


@app.route("/admin/generations/<int:gen_id>/html")
@admin_required
def admin_generation_html(gen_id):
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_id)
        if not gen:
            return "Not found", 404
        return gen.html_content, 200, {"Content-Type": "text/html; charset=utf-8"}
    finally:
        db.close()


@app.route("/admin/generations/<int:gen_id>/form-data")
@admin_required
def admin_generation_form_data(gen_id):
    db = SessionLocal()
    try:
        gen = db.get(Generation, gen_id)
        if not gen:
            return jsonify({"error": "not found"}), 404
        return jsonify(gen.lead.form_data if gen.lead else {})
    finally:
        db.close()


@app.route("/api/generate/<job_id>/status")
def job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job:
        result = {"status": job["status"]}
        if job["status"] == "error":
            result["error"] = job.get("error", "Unknown error")
        return jsonify(result)

    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if gen:
            return jsonify({"status": "done"})
    finally:
        db.close()
    return jsonify({"status": "not_found"}), 404


@app.route("/api/generate/<job_id>/html")
def job_html(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job:
        if job["status"] != "done":
            return jsonify({"error": "not ready", "status": job["status"]}), 409
        return _inject_watermark(job["html"], job_id), 200, {"Content-Type": "text/html; charset=utf-8"}

    db = SessionLocal()
    try:
        gen = db.query(Generation).join(Lead).filter(Lead.public_id == job_id).first()
        if gen:
            return _inject_watermark(gen.html_content, job_id), 200, {"Content-Type": "text/html; charset=utf-8"}
    finally:
        db.close()
    return jsonify({"error": "not found"}), 404


def _inject_watermark(html: str, job_id: str) -> str:
    checkout_url = f"/checkout.html?id={job_id}"

    watermark_bar = f"""<div id="gw-preview-bar" style="position:fixed;top:0;left:0;right:0;z-index:99999;background:#1C2630;color:#fff;font-family:sans-serif;font-size:13px;display:flex;align-items:center;justify-content:space-between;padding:10px 20px;box-shadow:0 2px 8px rgba(0,0,0,0.3);">
  <span>⚠ Preview — this site is unpublished and watermarked</span>
  <a href="{checkout_url}" style="background:#B8976A;color:#fff;padding:6px 16px;border-radius:4px;text-decoration:none;font-weight:600;">Go live — £99 + £24.99/mo →</a>
</div>
<div style="height:44px;"></div>"""

    robots_meta = '<meta name="robots" content="noindex, nofollow">'

    body_open = re.search(r"<body[^>]*>", html, re.IGNORECASE)
    if body_open:
        insert_at = body_open.end()
        html = html[:insert_at] + watermark_bar + html[insert_at:]

    head_open = re.search(r"<head[^>]*>", html, re.IGNORECASE)
    if head_open:
        insert_at = head_open.end()
        html = html[:insert_at] + robots_meta + html[insert_at:]

    return html


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
