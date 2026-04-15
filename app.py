import os
import base64
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import psycopg2
import anthropic
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify
)
from werkzeug.security import generate_password_hash
from PIL import Image

# ---------- Flask setup ----------
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')

# ---------- In-memory generation status ----------
generation_status = {}

# ---------- DB helper ----------
def get_db():
    return psycopg2.connect(os.environ['DATABASE_URL'])

# ---------- Table setup ----------
def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            business_name TEXT,
            location TEXT,
            trade_type TEXT,
            logo_filename TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            plan TEXT DEFAULT NULL,
            verified BOOLEAN DEFAULT FALSE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS preview_requests (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id),
            business_name TEXT,
            location TEXT,
            email TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

with app.app_context():
    try:
        init_db()
        print("[DB] Tables initialised")
    except Exception as e:
        print(f"[DB] Init error: {e}")

# ---------- Image compression ----------
def compress_image(filepath, max_size=(800, 400), quality=60):
    img = Image.open(filepath)
    img.thumbnail(max_size, Image.LANCZOS)
    buffer = BytesIO()
    if img.mode == 'RGBA':
        img = img.convert('RGB')
    buffer = BytesIO()
    img.save(buffer, format='JPEG', quality=quality)
    buffer.seek(0)
    return buffer.read(), 'image/jpeg'

# ---------- Logo colour extraction ----------
def extract_logo_colours(ai_client, logo_bytes, media_type):
    try:
        import json
        response = ai_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(logo_bytes).decode()
                        }
                    },
                    {
                        "type": "text",
                        "text": 'Analyse this logo. Reply with JSON only, no other text, no markdown: {"bg": "#hex", "accent": "#hex", "text": "#hex", "theme": "dark" or "light", "font_style": "refined" or "editorial" or "clean" or "industrial"}'
                    }
                ]
            }]
        )
        raw = response.content[0].text.strip()
        raw = raw.replace('```json', '').replace('```', '').strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[COLOURS] Failed: {e}")
        return {"bg": "#ffffff", "accent": "#D4820A", "text": "#1a1a1a", "theme": "light", "font_style": "clean"}

# ---------- Web search ----------
def run_single_search(ai_client, query):
    response = ai_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": f"Search for: {query}. Return a summary of what you find."}]
    )
    results = []
    for block in response.content:
        if hasattr(block, 'text'):
            results.append(block.text)
    return ' '.join(results)

def search_business(ai_client, business_name, location):
    queries = [
        f"{business_name} {location}",
        f"{business_name} {location} site:companieshouse.gov.uk",
        f"{business_name} {location} accreditation trade body",
    ]
    results = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(run_single_search, ai_client, q): q for q in queries}
        for i, future in enumerate(as_completed(futures)):
            try:
                results.append(future.result())
                print(f"[PREVIEW] Search {i + 1} complete")
            except Exception as e:
                print(f"[PREVIEW] Search {i + 1} failed: {e}")
    return ' '.join(results)

# ---------- System prompt ----------
SYSTEM_PROMPT = """
CRITICAL: Default to a LIGHT theme (#ffffff or off-white background, #1a1a1a dark text) unless the logo analysis clearly indicates a dark theme. For dark themes, ALWAYS set explicit color values on body, p, h1-h6, li and nav elements — never rely solely on CSS variables for text colour. Dark theme text must be #e8e8e8 or lighter. Failure to do this makes the site completely invisible.

CRITICAL CONTRAST RULE: Text colour must always contrast with background. Never dark text on dark background. Never light text on light background. Always set color explicitly on body and all text elements.

You are a professional web designer generating a bespoke preview website for a trade business. You will be given form data, web search results about the business, and brand colour information extracted from their logo.

Your output is a single, complete, self-contained HTML file. Nothing else. No explanation. No preamble. No markdown. No code fences. The file must work when opened directly in a browser with no external dependencies except Google Fonts.

---

STEP 1 — COLOUR SCHEME

You will be given brand colours extracted from the logo as JSON. Use these to build the entire site palette. Every colour decision — nav background, hero, buttons, borders, accents — must feel like it was designed around this specific logo.

If no logo colours are provided, default to: light cream/off-white background, dark navy text, amber (#D4820A) accent.

---

STEP 2 — RESEARCH THE BUSINESS

From the search results provided, extract every useful fact:
- Phone number(s)
- Email address
- Registered company number (Companies House)
- Year established or years trading
- Director/owner name (use only if helpful for about section)
- Full service list
- Coverage areas
- Trade body memberships and accreditation grades (e.g. LCA grade, Gas Safe, NICEIC, FMB, TrustMark, Checkatrade, Which? Trusted Trader)
- Any notable project types or sectors
- Any customer review quotes (max 1)

STRICT RULES:
- NEVER include a street address or postcode. Town/county only.
- NEVER invent facts. Only include what is in the search results or form data.
- NEVER include accreditations unless found in search results.
- Company registration number is fine to include if found.

---

STEP 3 — WRITE THE CONTENT

Hero headline: 4-8 words. Punchy, confident, specific to their trade. Never generic.

Body copy: Professional, third-person. No clichés. Short paragraphs.

Services: 4-6 service cells numbered 01-06. Each has a name (short, uppercase) and 2-sentence description.

About section: Lead with a strong fact. Personal approach. Bullet list of 4-5 verified facts.

---

STEP 4 — PORTFOLIO SECTION

Always include a portfolio section. Use this exact placeholder — do not vary it:
- Section heading: "Your work, front and centre"
- 3-column grid of placeholder boxes (background: #e0e0e0 for light themes, #2a2a2a for dark themes), each 280px tall, with a subtle camera icon or "📷" centred inside
- Below the grid, centred muted text: "Your portfolio will showcase your best projects here — add photos when you activate your account."
- No fake captions. No invented project names.

---

STEP 5 — BUILD THE HTML

FIXED STRUCTURE — always use this exact section order:

1. NAV — Logo img tag with src="LOGO_SRC", business name, trade tagline, links: Services · Accreditations (if found) · About · Portfolio · Contact, CTA button "Request a Quote" → #contact

2. HERO — Left: eyebrow + H1 + body + two CTAs. Right: accreditation panel or service list panel. Stats bar below: 2-3 stats that elevate (years trading, regions, accreditation grade). NEVER team size if small, never contract limits.

3. SERVICES (id="services") — numbered grid

4. ACCREDITATIONS (id="accreditations") — ONLY if accreditations found in search

5. ABOUT (id="about") — always present

6. PORTFOLIO (id="portfolio") — always present, use placeholder as described in Step 4

7. CONTACT (id="contact") — Left: H2 + contact details (NO address). Right: enquiry form. Fields: Name, Email, Telephone, Nature of enquiry (dropdown matching their services), Project details. Button: "Submit Enquiry"

8. FOOTER — Logo img with src="LOGO_SRC" (smaller, muted opacity), nav links, company reg if found, copyright year

9. GROUNDWORK BADGE (fixed bottom right) — <a class="gw-badge" href="https://groundwork.co.uk">⚡ Built by Groundwork</a> — small, unobtrusive

---

TYPOGRAPHY

Choose ONE pairing:
- REFINED/HERITAGE (leadwork, restoration, joinery): Cinzel + Barlow Condensed
- EDITORIAL/PREMIUM (bespoke carpentry, luxury fit-out): Cormorant Garamond + Barlow
- CLEAN/PROFESSIONAL (roofing, building, electrical, plumbing): Barlow Condensed 700 + Barlow 300/400
- BOLD/INDUSTRIAL (groundwork, demolition, scaffolding): Bebas Neue + Barlow

Body: 0.85-0.92rem, line-height 1.8-1.92, weight 300.

---

CSS RULES

- CSS custom properties for all colours in :root
- CSS Grid or Flexbox only
- Nav: fixed, 80px min height, backdrop-filter blur
- Hero: min-height 100vh
- Sections: padding 6-7rem top and bottom
- Responsive at 960px breakpoint
- Scroll animations: IntersectionObserver, fade-up
- ALWAYS set explicit color on: body, h1, h2, h3, h4, h5, h6, p, li, nav a, footer a — do not rely only on CSS variables

---

QUALITY CHECKLIST

☐ No street address or postcode
☐ No invented facts
☐ No accreditations unless found in search
☐ Hero headline specific to trade
☐ Enquiry form dropdown matches actual services
☐ LOGO_SRC used as src on nav and footer logo img tags
☐ Groundwork badge present
☐ Fully self-contained — no external images
☐ Google Fonts in head
☐ All section IDs correct
☐ Mobile responsive at 960px
☐ Scroll animations with IntersectionObserver
☐ Copyright year correct
☐ Text colour explicitly set on all text elements — not just via CSS variables
☐ Dark theme: body color #e8e8e8, headings #ffffff, paragraphs #d0d0d0

---

OUTPUT FORMAT

Return the complete HTML file and nothing else.
Start with <!DOCTYPE html>
End with </html>
No explanation. No commentary. No markdown.
"""

# ---------- User message builder ----------
def build_user_message(form_data, search_results, colour_hint=None):
    if colour_hint:
        logo_info = (
            f"\nLOGO: Provided. Use LOGO_SRC as the src attribute on BOTH the nav logo img tag AND the footer logo img tag. "
            f"Brand colours extracted from logo: bg={colour_hint.get('bg')}, accent={colour_hint.get('accent')}, "
            f"text={colour_hint.get('text')}, theme={colour_hint.get('theme')}, font_style={colour_hint.get('font_style')}"
        )
    else:
        logo_info = "\nLOGO: Not provided. Generate a text-based logo mark using the business initials in a coloured square."

    content = [{
        "type": "text",
        "text": (
            f"FORM DATA:\n"
            f"Business name: {form_data['business_name']}\n"
            f"Location: {form_data['location']}\n"
            f"{logo_info}\n\n"
            f"SEARCH RESULTS:\n{search_results}\n\n"
            f"CURRENT YEAR: 2026\n\n"
            f"Now generate the complete HTML file for this business. Return HTML only — no explanation, no markdown, start with <!DOCTYPE html>."
        )
    }]
    return content

# ---------- Background generation ----------
def run_generation(user_id, business_name, location, logo_path):
    import time
    start = time.time()
    print(f"[PREVIEW] Starting generation for user {user_id}")

    def on_timeout():
        if generation_status.get(user_id) != 'done':
            print(f"[PREVIEW] TIMEOUT - killed after 420s for user {user_id}")
            generation_status[user_id] = 'error'

    timer = threading.Timer(420, on_timeout)
    timer.start()

    try:
        ai_client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

        print(f"[PREVIEW] Starting web searches... ({time.time() - start:.1f}s)")
        search_results = search_business(ai_client, business_name, location)
        print(f"[PREVIEW] All searches complete ({time.time() - start:.1f}s)")

        colour_hint = None
        logo_bytes = None
        logo_media_type = None

        if logo_path and os.path.exists(logo_path):
            print(f"[PREVIEW] Logo found, extracting colours... ({time.time() - start:.1f}s)")
            logo_bytes, logo_media_type = compress_image(logo_path)
            colour_hint = extract_logo_colours(ai_client, logo_bytes, logo_media_type)
            print(f"[PREVIEW] Colours extracted: {colour_hint} ({time.time() - start:.1f}s)")
        else:
            print(f"[PREVIEW] No logo provided")

        form_data = {'business_name': business_name, 'location': location}
        messages = build_user_message(form_data, search_results, colour_hint)

        print(f"[PREVIEW] Calling Claude API... ({time.time() - start:.1f}s)")
        response = ai_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": messages}]
        )

        html = response.content[0].text
        html = html.replace('```html', '').replace('```', '').strip()
        if '<!DOCTYPE' in html:
            html = html[html.index('<!DOCTYPE'):]

        print(f"[PREVIEW] Claude response received, length: {len(html)} ({time.time() - start:.1f}s)")

        # Inject logo via post-processing
        if logo_bytes:
            data_uri = f"data:{logo_media_type};base64,{base64.b64encode(logo_bytes).decode()}"
            html = html.replace('LOGO_SRC', data_uri)
            print(f"[PREVIEW] Logo injected")

        # Contrast safety net
        dark_indicators = [
            '#0a0', '#0b0', '#0c0', '#0d0', '#0e0', '#0f0',
            '#1a1', '#1b1', '#1c1', '#0a0a0a', '#0d0d0d', '#111',
            '#0d1b', '#12', '#13', '#14', '#15', '#16'
        ]
        is_dark = (
            any(d in html.lower() for d in dark_indicators)
            or (colour_hint and colour_hint.get('theme') == 'dark')
        )

        if is_dark:
            contrast_fix = """<style>
:root {
    --text: #e8e8e8 !important;
    --color-text: #e8e8e8 !important;
    --body-color: #e8e8e8 !important;
    --foreground: #e8e8e8 !important;
}
body { color: #e8e8e8 !important; }
h1, h2, h3, h4, h5, h6 { color: #ffffff !important; }
p, li, td, th { color: #d0d0d0 !important; }
nav a { color: #c0c0c0 !important; }
footer { color: #a0a0a0 !important; }
</style>"""
        else:
            contrast_fix = "<style>body { color: inherit; }</style>"

        html = html.replace('</body>', contrast_fix + '</body>')

        os.makedirs('static/previews', exist_ok=True)
        preview_path = f'static/previews/{user_id}.html'
        with open(preview_path, 'w', encoding='utf-8') as f:
            f.write(html)

        print(f"[PREVIEW] Saving file... ({time.time() - start:.1f}s)")
        generation_status[user_id] = 'done'
        print(f"[PREVIEW] Done - user {user_id} ({time.time() - start:.1f}s total)")

    except Exception as e:
        print(f"[PREVIEW] ERROR for user {user_id} at {time.time() - start:.1f}s: {e}")
        if generation_status.get(user_id) != 'done':
            generation_status[user_id] = 'error'
    finally:
        timer.cancel()

# ---------- Routes ----------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/submit-preview', methods=['POST'])
def submit_preview():
    business_name = request.form.get('business_name', '').strip()
    location = request.form.get('location', '').strip()
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '').strip()

    if not all([business_name, location, email, password]):
        return render_template('index.html', form_error="Please fill in all required fields.")

    password_hash = generate_password_hash(password)

    logo_filename = None
    logo_file = request.files.get('logo')
    if logo_file and logo_file.filename:
        os.makedirs('uploads', exist_ok=True)
        ext = os.path.splitext(logo_file.filename)[1].lower()
        logo_filename = f"logo_{email.replace('@', '_').replace('.', '_')}{ext}"
        logo_file.save(os.path.join('uploads', logo_filename))

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (email, password_hash, business_name, location, logo_filename)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (email, password_hash, business_name, location, logo_filename))
        user_id = cur.fetchone()[0]
        cur.execute("""
            INSERT INTO preview_requests (user_id, business_name, location, email)
            VALUES (%s, %s, %s, %s)
        """, (user_id, business_name, location, email))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        if 'unique' in str(e).lower():
            return render_template('index.html', form_error="An account with that email already exists.")
        return render_template('index.html', form_error="Something went wrong. Please try again.")

    session['user_id'] = user_id
    return redirect(url_for('preview', user_id=user_id))

@app.route('/preview/<int:user_id>')
def preview(user_id):
    has_logo = False
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT logo_filename FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0]:
            has_logo = True
    except Exception:
        pass
    return render_template('preview.html', user_id=user_id, has_logo=has_logo)

@app.route('/generate-preview/<int:user_id>', methods=['POST'])
def generate_preview(user_id):
    if generation_status.get(user_id) in ['pending', 'done']:
        return jsonify({'status': generation_status.get(user_id)})

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT business_name, location, logo_filename FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

    if not row:
        return jsonify({'status': 'error', 'message': 'User not found'})

    business_name, location, logo_filename = row
    logo_path = os.path.join('uploads', logo_filename) if logo_filename else None

    generation_status[user_id] = 'pending'
    thread = threading.Thread(
        target=run_generation,
        args=(user_id, business_name, location, logo_path)
    )
    thread.daemon = True
    thread.start()

    return jsonify({'status': 'pending'})

@app.route('/preview-status/<int:user_id>')
def preview_status(user_id):
    status = generation_status.get(user_id, 'pending')
    if status == 'done':
        return jsonify({'status': 'done', 'preview_url': f'/static/previews/{user_id}.html'})
    return jsonify({'status': status})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
