import os
import base64
import threading
import secrets
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
            photo_filenames TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            plan TEXT DEFAULT NULL,
            verified BOOLEAN DEFAULT FALSE
        )
    """)
    cur.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS photo_filenames TEXT
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
def compress_image(filepath, max_size=(800, 600), quality=70):
    img = Image.open(filepath)
    img.thumbnail(max_size, Image.LANCZOS)
    buffer = BytesIO()
    if img.mode == 'RGBA':
        img = img.convert('RGB')
    buffer = BytesIO()
    img.save(buffer, format='JPEG', quality=quality)
    buffer.seek(0)
    return buffer.read(), 'image/jpeg'

def compress_image_bytes(img_bytes, max_size=(800, 600), quality=70):
    img = Image.open(BytesIO(img_bytes))
    img.thumbnail(max_size, Image.LANCZOS)
    buffer = BytesIO()
    if img.mode == 'RGBA':
        img = img.convert('RGB')
    img.save(buffer, format='JPEG', quality=quality)
    buffer.seek(0)
    return buffer.read(), 'image/jpeg'

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
SYSTEM_PROMPT = """You are a professional web designer generating a bespoke preview website for a
trade business. You will be given form data, web search results about the business,
and optionally their logo and portfolio photos as base64 images.

Your output is a single, complete, self-contained HTML file. Nothing else.
No explanation. No preamble. No markdown. No code fences.
The file must work when opened directly in a browser with no external dependencies
except Google Fonts.

---

STEP 1 — ANALYSE THE LOGO

If a logo is provided:
- Identify the dominant colour and secondary colour from the logo.
- If the logo is light or white on a dark background → use a DARK theme.
- If the logo has strong colours on a light/white background → use a LIGHT theme
  with those colours as accents.
- If no logo is provided → default to a light cream/off-white background with
  dark navy text and amber (#D4820A) as the accent colour.
- Build the entire site palette from the logo. Every colour decision — nav
  background, hero, buttons, borders, accents — must feel like it was designed
  around this specific logo.

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
- Trade body memberships and accreditation grades
  (e.g. LCA grade, Gas Safe registration, NICEIC, FMB, TrustMark,
  Checkatrade, Which? Trusted Trader, Houzz listing)
- Any notable project types or sectors mentioned
- Any customer review quotes (use sparingly, max 1)

STRICT RULES on search data:
- NEVER include a street address or postcode. Town/county only.
- NEVER mention contract value limits, capacity caps, or anything that makes
  the business look smaller than it is.
- NEVER invent facts. Only include what is verifiably found in the search results
  or explicitly provided in the form.
- NEVER include accreditations or trade body memberships unless specifically
  found in the search results. Do not assume any trade body for any trade.
- Company registration number is fine to include if found — it adds credibility.

---

STEP 3 — WRITE THE CONTENT

Hero headline:
- 4–8 words. Punchy, confident, specific to their trade.
- Never generic. Never "Quality work at competitive prices."
- Think about what makes their trade distinctive and lead with that.
- Examples by trade:
  - Roofer: "Roofing done properly. First time."
  - Carpenter: "Crafted to last. Built to impress."
  - Electrician: "Safe, certified, and done on time."
  - Plasterer: "A perfect finish, every room, every time."
  - Plumber: "Expert plumbing and heating. No messing about."
  - Builder: "Built right. On budget. On time."

Body copy:
- Professional, third-person voice throughout.
- No clichés ("competitive prices", "no job too small", "fully insured and reliable").
- Write as if describing a business you respect.
- Keep paragraphs short — these sites are read on phones.

Services:
- Write 4–6 service cells based on what the search results and trade type suggest.
- Number them 01 through 06.
- Each service has a name (short, uppercase) and a 2-sentence description.
- Descriptions should be specific and professional — not generic filler.

About section:
- Lead with a strong fact (years trading, family business, region).
- Mention the personal approach — clients deal directly with the business owner.
- Include a bullet list of 4–5 verified facts (accreditations, coverage, company
  registration if found, years trading).

Enquiry form dropdown:
- Options must be specific to their actual services.
- Never generic options like "Other work" as the only option.
- Always include "Other" as a final option.

---

STEP 4 — PORTFOLIO SECTION

If 1–3 portfolio images are provided:
- Include a portfolio section between the about section and contact section.
- For each image, use Claude Vision to write a short, professional caption
  (project type, location if inferable, materials/technique if visible).
- Display as a 3-column grid (or 2-column if only 2 images, 1-column if 1).
- Each image has a project title and a single-line description below it.
- Section heading: "Recent work" or "Selected projects".
- If no images are provided, omit this section entirely.

---

STEP 5 — BUILD THE HTML

FIXED STRUCTURE — always use this exact section order:

1. NAV
   - Logo (prominent — height 56–64px minimum)
   - Business name and trade tagline next to logo
   - Links: Services · About · [Portfolio if images provided] · Contact
   - CTA button: "Request a Quote" — links to #contact

2. HERO
   - Left column: eyebrow rule + text, H1, body paragraph, two CTAs
     (primary: "Request a quote" → #contact, ghost: "View services" → #services)
   - Right column: either an accreditation panel (if accreditations found)
     OR a service list panel (if no accreditations found)
   - Stats bar below left column: 2–3 stats that ELEVATE
     (years trading, regions covered, accreditation grade, domestic & commercial)
     NEVER: contract limits, team size if small, anything that caps the business

3. SERVICES (id="services")
   - Eyebrow + H2 + intro paragraph (right column)
   - 2-column or 3-column numbered grid of service cells
   - Each cell: number, name (uppercase), 2-sentence description

4. ACCREDITATIONS (id="accreditations") — ONLY if accreditations found in search
   - Left: list of accreditation rows with name, detail, and status pill
   - Right: explanatory copy about the most significant body found
   - If NO accreditations found → replace with ABOUT section (see below)

5. ABOUT (id="about") — always present
   - If accreditations section exists: left column body copy + bullet list,
     right column aside box with contact details
   - If no accreditations section: this becomes the combined about + contact
     details section

6. PORTFOLIO (id="portfolio") — ONLY if photos provided
   - Image grid with captions
   - Sits between about and contact

7. CONTACT (id="contact")
   - Left: H2 + body + contact details table (phone, email, coverage — NO address)
   - Right: enquiry form panel
     Fields: Name, Email, Telephone, Nature of enquiry (dropdown), Project details
   - Form submit button text: "Submit Enquiry"

8. FOOTER
   - Logo (smaller, muted opacity)
   - Nav links
   - Company registration number if found
   - "© [year] [Business Name]"

9. GROUNDWORK BADGE (fixed, bottom right)
   - "⚡ Built by Groundwork"
   - Links to https://groundwork.co.uk
   - Small, unobtrusive, always present

---

TYPOGRAPHY RULES

Always load from Google Fonts. Choose ONE of these pairings based on the
business type and aesthetic direction:

- REFINED / HERITAGE (leadwork, restoration, joinery, period property):
  Cinzel (headings) + Barlow / Barlow Condensed (body)

- EDITORIAL / PREMIUM (bespoke carpentry, luxury fit-out, architects):
  Cormorant Garamond (headings) + Barlow / Barlow Condensed (body)

- CLEAN / PROFESSIONAL (roofing, building, electrical, plumbing, general trade):
  Barlow Condensed 700 (headings, uppercase) + Barlow 300/400 (body)

- BOLD / INDUSTRIAL (groundwork, demolition, plant hire, scaffolding):
  Bebas Neue (headings) + Barlow (body)

Body font size: 0.85–0.92rem. Line height: 1.8–1.92. Font weight: 300.
All heading sizes should scale with clamp() for responsiveness.

---

CSS RULES

- Use CSS custom properties (variables) for all colours — defined in :root.
- All layout uses CSS Grid or Flexbox — no floats, no tables.
- Nav is fixed, height 80px minimum, background matches theme with backdrop-filter blur.
- Hero is min-height: 100vh, grid layout, padding-top equals nav height.
- All sections have padding: 6–7rem top and bottom on desktop.
- Responsive breakpoint at max-width: 960px — single column, hidden nav links.
- Scroll animations: IntersectionObserver, fade-up class, threshold 0.1.
- All interactive elements (buttons, links, nav items) have transition: 0.2s.
- NEVER use inline styles except for one-off overrides.
- NEVER use !important except on nav CTA hover states.
- No external CSS frameworks. No Tailwind. No Bootstrap.

---

LOGO RULES

- If a logo image is provided in this message: embed it as a data URI in the HTML.
  Height in nav: 56–64px. In footer: 32–40px with reduced opacity.
- If no logo: generate a text-based logo mark using the business initials
  in a coloured square/circle, paired with the business name in the nav font.
- NEVER display a broken image. If no logo provided, use the text mark.

---

GROUNDWORK BADGE

Always include this fixed badge in the bottom right corner:

<a class="gw-badge" href="https://groundwork.co.uk">⚡ Built by Groundwork</a>

Style: small, unobtrusive, matches the site theme. Dark sites get a light badge,
light sites get a dark badge. Always present — this is Groundwork's marketing.

---

QUALITY CHECKLIST (verify before outputting)

Before returning the HTML, check:
☐ No street address or postcode anywhere in the output
☐ No contract value limits or capacity caps
☐ No invented facts — everything is from search results or form data
☐ No trade body accreditations unless found in search
☐ Hero headline is specific to this trade — not generic
☐ Enquiry form dropdown options match their actual services
☐ Logo renders correctly (embedded data URI, height correct)
☐ Groundwork badge present and links to https://groundwork.co.uk
☐ File is fully self-contained — no external images, no external CSS
☐ Google Fonts link is in <head>
☐ All sections have correct IDs for nav anchor links
☐ Nav links match sections that actually exist in the page
☐ Mobile responsive — single column at 960px breakpoint
☐ Scroll animations use IntersectionObserver
☐ Company registration number included if found in search
☐ Copyright year is current

---

OUTPUT FORMAT

Return the complete HTML file and nothing else.
Start with <!DOCTYPE html> on the first line.
End with </html> on the last line.
No explanation. No commentary. No markdown formatting."""

# ---------- User message builder ----------
def build_user_message(form_data, search_results, logo_bytes=None, logo_media_type=None, photos=None):
    text = (
        f"FORM DATA:\n"
        f"Business name: {form_data['business_name']}\n"
        f"Location: {form_data['location']}\n\n"
        f"SEARCH RESULTS:\n{search_results}\n\n"
        f"CURRENT YEAR: 2026\n"
    )

    content = [{"type": "text", "text": text}]

    # Logo image
    if logo_bytes and logo_media_type:
        content.append({
            "type": "text",
            "text": "LOGO IMAGE (analyse colours, embed as data URI in the final HTML):"
        })
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": logo_media_type,
                "data": base64.b64encode(logo_bytes).decode()
            }
        })
    else:
        content.append({
            "type": "text",
            "text": "LOGO: Not provided. Generate a text-based logo mark using the business initials."
        })

    # Portfolio photos
    if photos:
        content.append({
            "type": "text",
            "text": f"PORTFOLIO PHOTOS ({len(photos)} provided — include a portfolio section with these images embedded as data URIs):"
        })
        for i, (photo_bytes, photo_media_type) in enumerate(photos):
            content.append({
                "type": "text",
                "text": f"Photo {i + 1} of {len(photos)}:"
            })
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": photo_media_type,
                    "data": base64.b64encode(photo_bytes).decode()
                }
            })
    else:
        content.append({
            "type": "text",
            "text": "PORTFOLIO PHOTOS: None provided. Omit the portfolio section."
        })

    content.append({
        "type": "text",
        "text": "Now generate the complete HTML file for this business. Return HTML only — no explanation, no markdown, start with <!DOCTYPE html>."
    })

    return content

# ---------- Background generation ----------
def run_generation(user_id, business_name, location, logo_path, photo_paths):
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

        logo_bytes = None
        logo_media_type = None

        if logo_path and os.path.exists(logo_path):
            print(f"[PREVIEW] Logo found, compressing... ({time.time() - start:.1f}s)")
            logo_bytes, logo_media_type = compress_image(logo_path)
            print(f"[PREVIEW] Logo ready ({time.time() - start:.1f}s)")
        else:
            print(f"[PREVIEW] No logo provided")

        # Load and compress portfolio photos
        photos = []
        for path in (photo_paths or []):
            if path and os.path.exists(path):
                try:
                    photo_bytes, photo_media_type = compress_image(path, max_size=(1200, 900), quality=75)
                    photos.append((photo_bytes, photo_media_type))
                    print(f"[PREVIEW] Photo loaded: {path}")
                except Exception as e:
                    print(f"[PREVIEW] Photo load failed {path}: {e}")

        form_data = {'business_name': business_name, 'location': location}
        messages = build_user_message(form_data, search_results, logo_bytes, logo_media_type, photos or None)

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

        os.makedirs('static/previews', exist_ok=True)
        preview_path = f'static/previews/{user_id}.html'
        with open(preview_path, 'w', encoding='utf-8') as f:
            f.write(html)

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

    if not all([business_name, location, email]):
        return render_template('index.html', form_error="Please fill in all required fields.")

    # Auto-generate a password — user sets their own on claim
    password_hash = generate_password_hash(secrets.token_hex(16))

    logo_filename = None
    logo_file = request.files.get('logo')
    if logo_file and logo_file.filename:
        os.makedirs('uploads', exist_ok=True)
        ext = os.path.splitext(logo_file.filename)[1].lower()
        logo_filename = f"logo_{email.replace('@', '_').replace('.', '_')}{ext}"
        logo_file.save(os.path.join('uploads', logo_filename))

    # Accept up to 3 portfolio photos
    photo_filenames = []
    photos_files = request.files.getlist('photos')
    for i, photo_file in enumerate(photos_files[:3]):
        if photo_file and photo_file.filename:
            os.makedirs('uploads', exist_ok=True)
            ext = os.path.splitext(photo_file.filename)[1].lower()
            photo_filename = f"photo_{email.replace('@', '_').replace('.', '_')}_{i}{ext}"
            photo_file.save(os.path.join('uploads', photo_filename))
            photo_filenames.append(photo_filename)

    photo_filenames_str = ','.join(photo_filenames) if photo_filenames else None

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO users (email, password_hash, business_name, location, logo_filename, photo_filenames)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """, (email, password_hash, business_name, location, logo_filename, photo_filenames_str))
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
    has_photos = False
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT logo_filename, photo_filenames FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            has_logo = bool(row[0])
            has_photos = bool(row[1])
    except Exception:
        pass
    return render_template('preview.html', user_id=user_id, has_logo=has_logo, has_photos=has_photos)

@app.route('/generate-preview/<int:user_id>', methods=['POST'])
def generate_preview(user_id):
    if generation_status.get(user_id) in ['pending', 'done']:
        return jsonify({'status': generation_status.get(user_id)})

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT business_name, location, logo_filename, photo_filenames FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

    if not row:
        return jsonify({'status': 'error', 'message': 'User not found'})

    business_name, location, logo_filename, photo_filenames_str = row
    logo_path = os.path.join('uploads', logo_filename) if logo_filename else None

    photo_paths = []
    if photo_filenames_str:
        for fname in photo_filenames_str.split(','):
            fname = fname.strip()
            if fname:
                photo_paths.append(os.path.join('uploads', fname))

    generation_status[user_id] = 'pending'
    thread = threading.Thread(
        target=run_generation,
        args=(user_id, business_name, location, logo_path, photo_paths)
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
