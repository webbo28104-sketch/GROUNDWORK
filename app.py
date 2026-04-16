import os
import json
import base64
import threading
import psycopg2
import anthropic
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS preview_requests (
            id SERIAL PRIMARY KEY,
            business_name TEXT NOT NULL,
            location TEXT NOT NULL,
            email TEXT NOT NULL,
            logo_b64 TEXT,
            photo_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            preview_html TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            completed_at TIMESTAMP
        )
    ''')
    # Add missing columns to existing tables
    migrations = [
        "ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS logo_b64 TEXT",
        "ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS photo_count INTEGER DEFAULT 0",
        "ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS preview_html TEXT",
        "ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP",
    ]
    for sql in migrations:
        try:
            cur.execute(sql)
        except Exception:
            pass
    conn.commit()
    cur.close()
    conn.close()

SYSTEM_PROMPT = """You are a professional web designer generating a bespoke preview website for a trade business. You will be given form data, web search results about the business, and optionally their logo and portfolio photos as base64 images.

Your output is a single, complete, self-contained HTML file. Nothing else. No explanation. No preamble. No markdown. No code fences. The file must work when opened directly in a browser with no external dependencies except Google Fonts.

STEP 1 — ANALYSE THE LOGO
If a logo is provided:
- Identify the dominant colour and secondary colour from the logo.
- If the logo is light or white on a dark background use a DARK theme.
- If the logo has strong colours on a light/white background use a LIGHT theme with those colours as accents.
- If no logo is provided default to light cream/off-white background with dark navy text and amber (#D4820A) as accent.
- Build the entire site palette from the logo.

STEP 2 — RESEARCH THE BUSINESS
From the search results extract: phone, email, company number, years trading, director name, services, coverage areas, trade body memberships and accreditation grades (LCA, Gas Safe, NICEIC, FMB, TrustMark, Checkatrade, Which? Trusted Trader, Houzz).

STRICT RULES:
- NEVER include a street address or postcode. Town/county only.
- NEVER mention contract value limits or capacity caps.
- NEVER invent facts. Only use what is in search results or form data.
- NEVER include accreditations unless found in search results.
- Company registration number is fine if found.

STEP 3 — WRITE THE CONTENT
Hero headline: 4-8 words. Punchy, confident, specific to their trade. Never generic.
Examples:
- Roofer: "Roofing done properly. First time."
- Carpenter: "Crafted to last. Built to impress."
- Electrician: "Safe, certified, and done on time."
- Plasterer: "A perfect finish, every room, every time."
- Plumber: "Expert plumbing and heating. No messing about."
- Builder: "Built right. On budget. On time."
- Leadwork: "Expert leadwork. Built to last generations."

Body copy: Professional, third-person voice. No cliches. Short paragraphs for mobile.
Services: Write 4-6 cells numbered 01-06. Uppercase name, 2-sentence description. Specific not generic.

STEP 4 — PORTFOLIO SECTION
If portfolio photos provided: include a portfolio section between about and contact. Write a short professional caption per image from what you can see. Grid: 3-col for 3 images, 2-col for 2, 1-col for 1. Section heading: "Recent work". If no images omit entirely.

STEP 5 — BUILD THE HTML
Fixed structure always in this order:
1. NAV: logo 56-64px height, business name + tagline, links Services/About/Portfolio(if photos)/Contact, CTA "Request a Quote" to #contact
2. HERO: left: eyebrow + H1 + body + two CTAs; right: accreditation panel OR service list; stats bar below with elevating stats only (years trading, regions, grade — never limits)
3. SERVICES id="services": eyebrow + H2 + intro + numbered grid
4. ACCREDITATIONS id="accreditations": ONLY if found in search. Left: rows with pills. Right: explanatory copy. If none found replace with ABOUT only.
5. ABOUT id="about": body + bullet list left, contact details aside right
6. PORTFOLIO id="portfolio": ONLY if photos provided
7. CONTACT id="contact": left: contact table (phone, email, coverage — NO address); right: enquiry form with trade-specific dropdown
8. FOOTER: logo muted, nav links, company number if found, copyright
9. GROUNDWORK BADGE: fixed bottom right "⚡ Built by Groundwork" linking to https://groundwork.co.uk

Typography — choose based on trade:
- Refined/heritage (leadwork, restoration, joinery): Cinzel + Barlow Condensed
- Editorial/premium (bespoke carpentry, luxury fit-out): Cormorant Garamond + Barlow Condensed
- Clean/professional (roofing, building, electrical, plumbing): Barlow Condensed 700 + Barlow 300
- Bold/industrial (groundwork, demolition, scaffolding): Bebas Neue + Barlow

CSS rules:
- CSS custom properties for all colours in :root
- CSS Grid/Flexbox only
- Nav fixed 80px height backdrop-filter blur
- Hero min-height 100vh padding-top equals nav height
- Sections 6-7rem padding top/bottom
- Responsive at 960px single column
- IntersectionObserver scroll animations fade-up class
- All transitions 0.2s
- No external frameworks

Quality checklist before returning:
- No street address or postcode
- No contract limits or caps
- No invented facts
- No unverified accreditations
- Hero headline specific to this trade
- Form dropdown matches actual services
- Logo renders at 56px+ height
- Groundwork badge present linking to https://groundwork.co.uk
- Fully self-contained HTML
- Google Fonts in head
- All section IDs correct
- Nav links match existing sections
- Mobile responsive at 960px
- Company registration included if found
- Copyright year correct

Return HTML only. Start with <!DOCTYPE html>. End with </html>."""

def search_business(business_name, location):
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{
                "role": "user",
                "content": f"""Search for information about this trade business and return everything useful you find:
Business: {business_name}
Location: {location}
Search for: contact details, services, accreditations, years trading, company registration, trade body memberships, reviews.
Return a structured summary of everything found. If you find a Companies House listing include the company number."""
            }]
        )
        result = ""
        for block in response.content:
            if hasattr(block, 'text'):
                result += block.text + "\n"
        return result if result.strip() else f"Business: {business_name}, Location: {location}"
    except Exception as e:
        print(f"Search failed for {business_name}: {e}")
        return f"Business: {business_name}, Location: {location}"

def build_user_message(business_name, location, search_results, logo_b64=None, photos_b64=None):
    content = []
    text = f"""FORM DATA:
Business name: {business_name}
Location: {location}

SEARCH RESULTS:
{search_results}

CURRENT YEAR: {datetime.now().year}
GROUNDWORK URL: https://groundwork.co.uk
"""
    content.append({"type": "text", "text": text})
    if logo_b64:
        try:
            header, data = logo_b64.split(',', 1)
            media_type = header.split(':')[1].split(';')[0]
            content.append({"type": "text", "text": "LOGO IMAGE (analyse colours and embed as base64 src in the HTML):"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
        except Exception:
            pass
    if photos_b64:
        content.append({"type": "text", "text": f"PORTFOLIO PHOTOS ({len(photos_b64)} provided — include a portfolio section):"})
        for i, photo in enumerate(photos_b64):
            try:
                header, data = photo.split(',', 1)
                media_type = header.split(':')[1].split(';')[0]
                content.append({"type": "text", "text": f"Photo {i+1} of {len(photos_b64)}:"})
                content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
            except Exception:
                pass
    content.append({"type": "text", "text": "Now generate the complete HTML file. Return HTML only — starting with <!DOCTYPE html>, ending with </html>."})
    return content

def run_generation(request_id, business_name, location, logo_b64, photos_b64):
    try:
        search_results = search_business(business_name, location)
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_message(business_name, location, search_results, logo_b64, photos_b64)}]
        )
        html = response.content[0].text
        html = html.replace('```html', '').replace('```', '').strip()
        if '<!DOCTYPE' in html:
            html = html[html.index('<!DOCTYPE'):]
        if not html or '</html>' not in html.lower():
            raise ValueError(f"Generated HTML is empty or malformed (len={len(html)})")
        conn = get_db()
        try:
            cur = conn.cursor()
            cur.execute(
                'UPDATE preview_requests SET status = %s, preview_html = %s, completed_at = NOW() WHERE id = %s',
                ('complete', html, request_id)
            )
            conn.commit()
            cur.close()
        finally:
            conn.close()
    except Exception as e:
        print(f"Generation error for request {request_id}: {e}")
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("UPDATE preview_requests SET status = 'error' WHERE id = %s", (request_id,))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e2:
            print(f"Failed to set error status for request {request_id}: {e2}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/submit-preview', methods=['POST'])
def submit_preview():
    business_name = request.form.get('business_name', '').strip()
    location = request.form.get('location', '').strip()
    email = request.form.get('email', '').strip()
    if not business_name or not location or not email:
        return render_template('index.html', form_error="Please fill in all required fields.")
    logo_b64 = None
    logo_file = request.files.get('logo')
    if logo_file and logo_file.filename:
        try:
            logo_data = logo_file.read()
            mt = logo_file.content_type or 'image/png'
            logo_b64 = f"data:{mt};base64,{base64.b64encode(logo_data).decode()}"
        except Exception:
            pass
    photos_b64 = []
    for key in ['photos', 'photo_1', 'photo_2', 'photo_3']:
        files = request.files.getlist(key)
        for f in files:
            if f and f.filename and len(photos_b64) < 3:
                try:
                    data = f.read()
                    mt = f.content_type or 'image/jpeg'
                    photos_b64.append(f"data:{mt};base64,{base64.b64encode(data).decode()}")
                except Exception:
                    pass
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO preview_requests (business_name, location, email, logo_b64, photo_count, status) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id',
            (business_name, location, email, logo_b64, len(photos_b64), 'generating')
        )
        request_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB insert error: {e}")
        return "Database error. Please try again.", 500

    thread = threading.Thread(target=run_generation, args=(request_id, business_name, location, logo_b64, photos_b64 or None), daemon=True)
    thread.start()
    return render_template('generating.html', business_name=business_name, request_id=request_id)

@app.route('/preview/<int:request_id>')
def preview_status(request_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT status, preview_html FROM preview_requests WHERE id = %s", (request_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return jsonify({'status': 'not_found'}), 404
        status, html = row
        if status == 'complete' and html:
            return jsonify({'status': 'complete'})
        elif status == 'error':
            return jsonify({'status': 'error'})
        return jsonify({'status': 'generating'})
    except Exception as e:
        print(f"Poll error for request {request_id}: {e}")
        return jsonify({'status': 'generating'})

@app.route('/preview/<int:request_id>/view')
def preview_view(request_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT preview_html, business_name FROM preview_requests WHERE id = %s AND status = 'complete'", (request_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return "Preview not ready yet.", 404
        html, business_name = row
        return html, 200, {'Content-Type': 'text/html'}
    except Exception as e:
        print(f"View error for request {request_id}: {e}")
        return "Something went wrong loading your preview.", 500

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico', mimetype='image/vnd.microsoft.icon')

with app.app_context():
    if DATABASE_URL:
        init_db()

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
