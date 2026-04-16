import os
import base64
import threading
import uuid
import random
import psycopg2
import anthropic
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, session
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# ---------- App setup ----------
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key-change-me')
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_FILE_DIR'] = '/tmp/flask_session'
app.config['SESSION_PERMANENT'] = False

os.makedirs('/tmp/flask_session', exist_ok=True)

from flask_session import Session
Session(app)

ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
APP_URL = os.environ.get('APP_URL', 'http://localhost:5000')
SENDGRID_API_KEY = os.environ.get('SENDGRID_API_KEY')

# In-memory failed attempt tracker {email: {'count': N, 'blocked_until': datetime|None}}
verify_attempts = {}

# ---------- DB ----------
def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db()
    cur = conn.cursor()

    if os.environ.get('TEST_MODE', 'false').lower() != 'true':
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                email_verified BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        conn.commit()

        cur.execute('''
            CREATE TABLE IF NOT EXISTS email_verifications (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                code TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        ''')
        conn.commit()

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
    conn.commit()

    migrations = [
        "ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS logo_b64 TEXT",
        "ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS photo_count INTEGER DEFAULT 0",
        "ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending'",
        "ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS preview_html TEXT",
        "ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP",
        "ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id)",
    ]
    for sql in migrations:
        try:
            cur.execute(sql)
            conn.commit()
        except Exception as e:
            print(f"[DB] Migration skipped ({e}): {sql[:60]}")
            conn.rollback()

    cur.close()
    conn.close()
    print("[DB] Tables initialised")

# ---------- System prompt ----------
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

# ---------- Generation helpers ----------
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
            text = getattr(block, 'text', None)
            if text:
                result += text + "\n"
        return result.strip() if result.strip() else f"Business: {business_name}, Location: {location}"
    except Exception as e:
        print(f"Search failed, using fallback: {e}")
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

        if os.environ.get('TEST_MODE', 'false').lower() == 'true':
            # Cheap test generation — real search, simplified HTML, minimal tokens
            test_response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2000,
                messages=[{
                    "role": "user",
                    "content": f"""Generate a simple but complete HTML page for this trade business.
It must be a real working website preview — not a placeholder.

Business: {business_name}
Location: {location}
Search data: {search_results[:500]}

Rules:
- Single self-contained HTML file
- Dark navy and amber colour scheme (#1a1208 background, #D4820A accent)
- Include: nav with business name, hero with a real headline specific to their trade, a services section with 4 services, a contact section with any phone/email found in the search data
- Load Barlow Condensed from Google Fonts
- Mobile responsive
- Fixed bottom right badge: "⚡ Built by Groundwork"
- No external images
- Return HTML only, start with <!DOCTYPE html>, end with </html>"""
                }]
            )
            html = test_response.content[0].text if test_response.content else ""
        else:
            # Full production generation
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_user_message(business_name, location, search_results, logo_b64, photos_b64)}]
            )
            html = response.content[0].text if response.content else ""
        if not html:
            raise Exception("Claude returned empty response")

        html = html.replace('```html', '').replace('```', '').strip()

        if '<!DOCTYPE' in html.upper():
            idx = html.upper().index('<!DOCTYPE')
            html = html[idx:]

        if '</html>' not in html.lower():
            html += '\n</html>'

        if len(html) < 500:
            raise Exception(f"Generated HTML too short (len={len(html)})")

        print(f"[Generation] Request {request_id} HTML length: {len(html)}")
        print(f"[Generation] First 200 chars: {html[:200]}")

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

def start_generation(user_id, email, business_name, location, logo_b64, photos_b64):
    """Insert preview_requests row and kick off background thread. Returns request_id or None."""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO preview_requests (business_name, location, email, logo_b64, photo_count, status, user_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id',
            (business_name, location, email, logo_b64, len(photos_b64) if photos_b64 else 0, 'generating', user_id)
        )
        request_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Failed to create preview request: {e}")
        return None

    thread = threading.Thread(
        target=run_generation,
        args=(request_id, business_name, location, logo_b64, photos_b64 or None),
        daemon=True
    )
    thread.start()
    return request_id

# ---------- Email ----------
def send_verification_email(email, code, token, business_name):
    verify_url = f"{APP_URL}/verify/{token}"
    html_content = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:2rem">
      <h2 style="color:#1a1208">Verify your Groundwork account</h2>
      <p>We're building your preview website for <strong>{business_name}</strong>. First, verify your email.</p>
      <div style="background:#1a1208;border-radius:8px;padding:1.5rem;text-align:center;margin:1.5rem 0">
        <p style="color:#9E8E7A;font-size:0.85rem;margin-bottom:0.5rem;letter-spacing:0.1em">YOUR VERIFICATION CODE</p>
        <p style="color:#F5A623;font-size:2.5rem;font-weight:700;letter-spacing:0.3em;margin:0">{code[:3]} {code[3:]}</p>
      </div>
      <p style="text-align:center;margin:1.5rem 0">
        <a href="{verify_url}" style="background:#D4820A;color:#1a1208;padding:0.9rem 2rem;text-decoration:none;font-weight:700;font-size:0.9rem;border-radius:4px">
          Or click here to verify automatically →
        </a>
      </p>
      <p style="color:#999;font-size:0.8rem">Code expires in 15 minutes. If you didn't request this, ignore this email.</p>
    </div>
    """
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        message = Mail(
            from_email='hello@groundwork.co.uk',
            to_emails=email,
            subject=f'Verify your Groundwork account — {business_name}',
            html_content=html_content
        )
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        sg.send(message)
        print(f"Verification email sent to {email}")
    except Exception as e:
        print(f"Email send failed: {e}")

def create_verification(user_id):
    """Generate a 6-digit code and UUID token, store in DB, return (code, token)."""
    code = ''.join([str(random.randint(0, 9)) for _ in range(6)])
    token = str(uuid.uuid4())
    expires_at = datetime.utcnow() + timedelta(minutes=15)
    conn = get_db()
    cur = conn.cursor()
    # Invalidate any existing unused codes for this user
    cur.execute("UPDATE email_verifications SET used = TRUE WHERE user_id = %s AND used = FALSE", (user_id,))
    cur.execute(
        "INSERT INTO email_verifications (user_id, code, token, expires_at) VALUES (%s, %s, %s, %s)",
        (user_id, code, token, expires_at)
    )
    conn.commit()
    cur.close()
    conn.close()
    return code, token

def complete_verification(user_id):
    """Mark user as verified and start generation. Returns request_id or None."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE users SET email_verified = TRUE WHERE id = %s RETURNING email", (user_id,))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    if not row:
        return None
    email = row[0]

    business_name = session.get('pending_business_name', '')
    location = session.get('pending_location', '')
    logo_b64 = session.get('pending_logo_b64')
    photos_b64 = session.get('pending_photos_b64') or []

    request_id = start_generation(user_id, email, business_name, location, logo_b64, photos_b64)
    session['user_id'] = user_id
    # Clear pending data
    for key in ['pending_business_name', 'pending_location', 'pending_logo_b64', 'pending_photos_b64', 'pending_email']:
        session.pop(key, None)
    return request_id

# ---------- Routes ----------
@app.route('/')
def index():
    test_mode = os.environ.get('TEST_MODE', 'false').lower() == 'true'
    return render_template('index.html', test_mode=test_mode)

@app.route('/mode')
def mode():
    test_mode = os.environ.get('TEST_MODE', 'false').lower() == 'true'
    model = 'claude-haiku-4-5-20251001' if test_mode else 'claude-sonnet-4-6'
    return f"TEST_MODE={'true' if test_mode else 'false'}\nMODEL={model}\n", 200, {'Content-Type': 'text/plain'}

@app.route('/submit-preview', methods=['POST'])
def submit_preview():
    business_name = request.form.get('business_name', '').strip()
    location = request.form.get('location', '').strip()
    email = request.form.get('email', '').strip().lower()
    password = request.form.get('password', '').strip()

    if not business_name or not location or not email:
        return render_template('index.html', form_error="Please fill in all required fields.")

    if not password:
        password = str(uuid.uuid4())  # auto-generate if not provided

    # Process logo
    logo_b64 = None
    logo_file = request.files.get('logo')
    if logo_file and logo_file.filename:
        try:
            logo_data = logo_file.read()
            mt = logo_file.content_type or 'image/png'
            logo_b64 = f"data:{mt};base64,{base64.b64encode(logo_data).decode()}"
        except Exception:
            pass

    # Process photos (up to 3)
    photos_b64 = []
    for key in ['photos', 'photo_1', 'photo_2', 'photo_3']:
        for f in request.files.getlist(key):
            if f and f.filename and len(photos_b64) < 3:
                try:
                    data = f.read()
                    mt = f.content_type or 'image/jpeg'
                    photos_b64.append(f"data:{mt};base64,{base64.b64encode(data).decode()}")
                except Exception:
                    pass

    if os.environ.get('TEST_MODE', 'false').lower() == 'true':
        # Skip account creation, email verification and session entirely
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
        for f in request.files.getlist('photos'):
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
                (business_name, location, email or 'test@test.com', logo_b64, len(photos_b64), 'generating')
            )
            request_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            print(f"TEST_MODE DB insert error: {e}")
            return "Database error. Please try again.", 500

        thread = threading.Thread(
            target=run_generation,
            args=(request_id, business_name, location, logo_b64, photos_b64 or None),
            daemon=True
        )
        thread.start()
        return render_template('generating.html', business_name=business_name, request_id=request_id)

    # Store form data in session for use after verification
    session['pending_business_name'] = business_name
    session['pending_location'] = location
    session['pending_logo_b64'] = logo_b64
    session['pending_photos_b64'] = photos_b64
    session['pending_email'] = email

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, email_verified FROM users WHERE email = %s", (email,))
        existing = cur.fetchone()

        if existing:
            user_id, is_verified = existing
            if is_verified:
                # Already verified — go straight to generation
                cur.close()
                conn.close()
                request_id = start_generation(user_id, email, business_name, location, logo_b64, photos_b64)
                if not request_id:
                    return "Failed to start generation. Please try again.", 500
                session['user_id'] = user_id
                return redirect(url_for('generating', request_id=request_id))
            else:
                # Exists but unverified — resend code
                cur.close()
                conn.close()
                code, token = create_verification(user_id)
                send_verification_email(email, code, token, business_name)
                return redirect(url_for('verify_get', email=email))
        else:
            # New user
            password_hash = generate_password_hash(password)
            cur.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
                (email, password_hash)
            )
            user_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
            conn.close()
            code, token = create_verification(user_id)
            send_verification_email(email, code, token, business_name)
            return redirect(url_for('verify_get', email=email))

    except Exception as e:
        print(f"submit_preview error: {e}")
        return render_template('index.html', form_error="Something went wrong. Please try again.")

@app.route('/verify', methods=['GET'])
def verify_get():
    email = request.args.get('email', session.get('pending_email', ''))
    return render_template('verify.html', email=email, error=None)

@app.route('/verify', methods=['POST'])
def verify_post():
    email = request.form.get('email', '').strip().lower()
    code = request.form.get('code', '').strip().replace(' ', '')

    # Check rate limiting
    now = datetime.utcnow()
    attempt_data = verify_attempts.get(email, {'count': 0, 'blocked_until': None})
    if attempt_data['blocked_until'] and now < attempt_data['blocked_until']:
        remaining = int((attempt_data['blocked_until'] - now).total_seconds() / 60) + 1
        return render_template('verify.html', email=email, error=f"Too many attempts. Try again in {remaining} minutes.")

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT ev.id, ev.user_id, ev.used, ev.expires_at
            FROM email_verifications ev
            JOIN users u ON u.id = ev.user_id
            WHERE u.email = %s AND ev.code = %s AND ev.used = FALSE
            ORDER BY ev.created_at DESC LIMIT 1
        """, (email, code))
        row = cur.fetchone()

        if not row:
            cur.close()
            conn.close()
            # Increment attempt counter
            attempt_data['count'] = attempt_data.get('count', 0) + 1
            if attempt_data['count'] >= 3:
                attempt_data['blocked_until'] = now + timedelta(minutes=15)
                attempt_data['count'] = 0
            verify_attempts[email] = attempt_data
            return render_template('verify.html', email=email, error="Invalid or expired code. Please check and try again.")

        ev_id, user_id, used, expires_at = row

        if expires_at < now:
            cur.close()
            conn.close()
            return render_template('verify.html', email=email, error="That code has expired. Request a new one below.")

        # Mark used
        cur.execute("UPDATE email_verifications SET used = TRUE WHERE id = %s", (ev_id,))
        conn.commit()
        cur.close()
        conn.close()

        # Clear attempt counter
        verify_attempts.pop(email, None)

        request_id = complete_verification(user_id)
        if not request_id:
            return render_template('verify.html', email=email, error="Verification succeeded but we couldn't start your preview. Please try again.")

        return redirect(url_for('generating', request_id=request_id))

    except Exception as e:
        print(f"verify_post error: {e}")
        return render_template('verify.html', email=email, error="Something went wrong. Please try again.")

@app.route('/verify/<token>', methods=['GET'])
def verify_magic_link(token):
    now = datetime.utcnow()
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT ev.id, ev.user_id, ev.used, ev.expires_at, u.email
            FROM email_verifications ev
            JOIN users u ON u.id = ev.user_id
            WHERE ev.token = %s
        """, (token,))
        row = cur.fetchone()

        if not row:
            cur.close()
            conn.close()
            return render_template('verify.html', email='', error="That verification link is invalid. Please request a new code.")

        ev_id, user_id, used, expires_at, email = row

        if used:
            cur.close()
            conn.close()
            return render_template('verify.html', email=email, error="This link has already been used. If you need a new one, re-submit the form.")

        if expires_at < now:
            cur.close()
            conn.close()
            return render_template('verify.html', email=email, error="This link has expired. Please request a new code.")

        # Mark used
        cur.execute("UPDATE email_verifications SET used = TRUE WHERE id = %s", (ev_id,))
        conn.commit()
        cur.close()
        conn.close()

        verify_attempts.pop(email, None)

        request_id = complete_verification(user_id)
        if not request_id:
            return render_template('verify.html', email=email, error="Link verified but we couldn't start your preview. Please try again.")

        return redirect(url_for('generating', request_id=request_id))

    except Exception as e:
        print(f"verify_magic_link error: {e}")
        return render_template('verify.html', email='', error="Something went wrong. Please try again.")

@app.route('/generating/<int:request_id>')
def generating(request_id):
    business_name = ''
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT business_name FROM preview_requests WHERE id = %s", (request_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            business_name = row[0]
    except Exception:
        pass
    return render_template('generating.html', business_name=business_name, request_id=request_id)

@app.route('/preview/<int:request_id>')
def preview_status(request_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT status FROM preview_requests WHERE id = %s", (request_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            return jsonify({'status': 'not_found'}), 404
        status = row[0]
        if status == 'complete':
            return jsonify({'status': 'complete'})
        elif status == 'error':
            return jsonify({'status': 'error'})
        return jsonify({'status': 'generating'})
    except Exception as e:
        print(f"Poll error for request {request_id}: {e}")
        return jsonify({'status': 'error', 'detail': str(e)}), 500

@app.route('/preview/<int:request_id>/view')
def preview_view(request_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT preview_html FROM preview_requests WHERE id = %s AND status = 'complete'", (request_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row or not row[0]:
            return "Preview not ready yet.", 404
        html = row[0]
        print(f"[View] Request {request_id} serving HTML length: {len(html)}")
        response = app.make_response(html)
        response.headers['Content-Type'] = 'text/html; charset=utf-8'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return response
    except Exception as e:
        print(f"View error for request {request_id}: {e}")
        return f"Something went wrong loading your preview: {e}", 500

@app.route('/preview/<int:request_id>/raw')
def preview_raw(request_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT preview_html FROM preview_requests WHERE id = %s AND status = 'complete'", (request_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row or not row[0]:
            return "Preview not ready yet.", 404
        return row[0], 200, {'Content-Type': 'text/html; charset=utf-8'}
    except Exception as e:
        print(f"Raw error for request {request_id}: {e}")
        return "Something went wrong.", 500

@app.route('/dashboard')
def dashboard():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('index'))
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, business_name, created_at
            FROM preview_requests
            WHERE user_id = %s AND status = 'complete'
            ORDER BY created_at DESC LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return render_template('preview_wrapper.html', request_id=row[0], business_name=row[1])
        return render_template('index.html', form_error="No preview found. Generate one below.")
    except Exception as e:
        print(f"Dashboard error: {e}")
        return redirect(url_for('index'))

@app.route('/resend-code', methods=['POST'])
def resend_code():
    email = request.form.get('email', '').strip().lower()
    business_name = session.get('pending_business_name', 'your business')
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = %s AND email_verified = FALSE", (email,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            user_id = row[0]
            code, token = create_verification(user_id)
            send_verification_email(email, code, token, business_name)
    except Exception as e:
        print(f"Resend code error: {e}")
    return redirect(url_for('verify_get', email=email))

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico', mimetype='image/vnd.microsoft.icon')

# ---------- Startup ----------
with app.app_context():
    if DATABASE_URL:
        try:
            init_db()
        except Exception as e:
            print(f"[DB] Init failed: {e}")
    mode = "TEST" if os.environ.get('TEST_MODE', 'false').lower() == 'true' else "PRODUCTION"
    print(f"[Groundwork] Starting in {mode} mode")

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
