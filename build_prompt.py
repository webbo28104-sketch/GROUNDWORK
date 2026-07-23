"""
Groundwork — production site-generation prompt.

This is the actual prompt sent to Claude when a client's form is submitted.
It is the distilled version of SITE_GENERATION_SPEC.md — instructions only,
no commentary or history. Update this file when the spec doc changes;
don't send the spec doc itself to the API.
"""
from datetime import datetime

def build_prompt(form_data: dict) -> str:
    """
    form_data keys expected:
    business_name, trade, location, coverage_area, phone, email,
    logo_uploaded (bool), portfolio_uploaded (bool),
    work_split (plain-language string, e.g. "30% domestic / 70% commercial"),
    craft_prestige, team_size,
    large_commercial_contracts (bool), urgency,
    years_trading, claimed_accreditations, claimed_projects, other_notes

    Optional media-reference keys (not printed in the generic facts list —
    rendered in their own MEDIA REFERENCES section instead):
    logo_src_token (str) — literal placeholder string to use verbatim as the
        logo <img> src; substituted for the real data URI after generation.
    photo_src_tokens (list[str]) — literal placeholder strings, one per
        portfolio photo, in display order; substituted the same way.
    """
    MEDIA_KEYS = {"logo_src_token", "photo_src_tokens", "logo_bg_hex", "logo_accent_hex", "commercial_lean"}

    facts = "\n".join(f"- {k}: {v}" for k, v in form_data.items() if v and k not in MEDIA_KEYS)
    current_year = datetime.now().year

    logo_src_token = form_data.get("logo_src_token")
    photo_src_tokens = form_data.get("photo_src_tokens") or []
    logo_bg_hex = form_data.get("logo_bg_hex")
    logo_accent_hex = form_data.get("logo_accent_hex")
    commercial_lean = form_data.get("commercial_lean")

    media_lines = []
    if logo_src_token:
        media_lines.append(
            f'- Logo: use exactly this literal string as the logo <img> src attribute: {logo_src_token} '
            f'— copy it verbatim, character for character. Do not modify it, do not invent a different path, '
            f'filename, or data URI of your own.'
        )
    if logo_bg_hex:
        media_lines.append(
            f'- The logo image already has a solid background baked in at exactly {logo_bg_hex} (it could not be '
            f'cleanly cut out — the original had a busy/gradient backdrop, so it was placed on a matching flat '
            f'chip instead). You MUST set the nav bar\'s own background colour to this exact hex, {logo_bg_hex}, '
            f'so the logo\'s background blends invisibly into the nav rather than appearing as a mismatched box. '
            f'Build the rest of the palette to work with {logo_bg_hex} as the nav surface colour — do not pick a '
            f'different nav background just because it seems like a better palette fit.'
        )
    if logo_accent_hex:
        media_lines.append(
            f'- The logo also contains a distinct secondary colour, exactly {logo_accent_hex}. Use this exact hex '
            f'as the site\'s accent colour (link/button hover states, small UI highlights, secondary CTAs) rather '
            f'than choosing your own accent — it should read as pulled directly from the brand\'s own logo.'
        )
    if photo_src_tokens:
        tokens_list = ", ".join(photo_src_tokens)
        media_lines.append(
            f'- Portfolio photos: use exactly these literal strings as the src attribute for the portfolio images, '
            f'one per card, in this exact order: {tokens_list} — copy each one verbatim, character for character. '
            f'Do not modify them, do not invent filenames or paths of your own, and do not add extra photo cards '
            f'beyond the {len(photo_src_tokens)} tokens given.'
        )
    media_references_section = ""
    if media_lines:
        media_references_section = "\n=== MEDIA REFERENCES ===\n" + "\n".join(media_lines) + "\n"

    return f"""You are generating a single-page marketing website for a UK trades/construction business, from their sign-up form data below.

FORM DATA:
{facts}

=== STEP 1: VERIFY ===
Use web search (2-4 targeted searches max) to:
1. Confirm this is the correct business — disambiguate from any similarly-named businesses before using anything found online.
2. Check accreditation bodies relevant to their specific trade (only relevant ones, not a blind sweep).
3. If they listed any claimed accreditations/projects/clients, verify those specific claims.
4. Look for a verified public email address for the business (their own site, official listing, etc.). If no verified public email address is found, omit the email field from the contact section entirely — do not display placeholder text, example addresses, or any invented email. Phone number only is acceptable.
Do not search beyond this. Do not go looking for extra material if nothing was claimed.

=== STEP 2: LIST YOUR SOURCES ===
Before writing any HTML, write a short bullet list of every factual claim you plan to put on the site, and where it came from (form field, or search result with URL). If a claim has no source, delete it or rephrase it generically. This includes operational claims like response times, turnaround, availability — not just credentials. This step is mandatory and must be visible in your output before the HTML.

=== STEP 3: SET THE DESIGN ===
Three dials come directly from the form — do not re-derive them:
- PRESTIGE (from craft_prestige) — controls type distinctiveness and ornamentation level.
- SCALE (from team_size + large_commercial_contracts) — controls layout density and how much "evidence" structure (stat bars, capability grids) is shown.
- URGENCY (from urgency field) — controls CTA aggressiveness and contact form complexity.
These are independent — a low-prestige business can still be large-scale (dense layout, plain type); a high-prestige business can still be small (simple layout, distinctive type).
PRESTIGE NUDGE — a commercial-majority client should read as a touch more polished/premium *within its PRESTIGE tier* than an otherwise-identical domestic-majority client (e.g. the crisper/more restrained option among that tier's type pairings, tighter grid, slightly more restrained saturation) — commercial clients are typically more design-conscious. This never moves the PRESTIGE tier itself (that's fixed by craft_prestige, not by this) — it's a same-tier lean only, and never overrides craft_prestige. Current lean: {commercial_lean}.
{media_references_section}
TYPE — pick one pairing matching the PRESTIGE level, rotate, don't reuse the same pairing as last time:
- High prestige, heritage/institutional register: Cinzel + EB Garamond + Barlow Condensed
- High prestige, contemporary/design-led register: Fraunces + Newsreader + Inter
- High prestige, alternates: Playfair Display + Source Serif 4 + Work Sans / Cormorant + Crimson Pro + Jost
- Mid prestige: Archivo + Inter / Space Grotesk + IBM Plex Sans / Sora + Inter / Libre Franklin + Source Sans 3
- Low prestige: Inter / DM Sans / Manrope / Plus Jakarta Sans (display+body same family)

PALETTE:
- If a logo was uploaded: extract its real colours as the base. IMPORTANT — distinguish the logo's mark/lettering colour from an incidental dark backdrop (a black square behind a single light mark is usually just a presentation crop, not a brand colour). Default to a light-surface theme built from the mark's actual colour unless the dark tone is clearly woven into the lettering/composition itself.
- If no logo, use the trade's real material vocabulary (lead=charcoal/grey-blue, render/plaster=warm putty tones, timber=warm wood tones, structural metal/roofing=zinc grey/graphite, scaffolding/steel=steel grey-blue+safety amber, masonry=brick/sandstone). If the trade has no obvious material (electrician, plumber, locksmith), pick one confident accent not overused, paired with neutral grey/white.
- Modulate saturation by PRESTIGE (high=restrained/near-monochrome with one sparing accent; low=bolder, more saturated, accent used often).
- Modulate structural colour count by SCALE (sole trader=one accent only; large=accent + secondary category colour + neutral structural greys).
- Before finalizing, ask: "is this palette specific to this business, or would it be my generic answer for any business like this?" If generic, revise.
- Every body/paragraph text colour must reach at least a 4.5:1 contrast ratio against the surface it sits on (WCAG AA). Never use a light/pastel tone for readable body copy on a white or light surface — reserve light tints for large decorative elements (backgrounds, dividers, icons), not text meant to be read.

LAYOUT DENSITY (by SCALE):
- Sole trader: 1-2 column grids, generous whitespace, no stat bar.
- Small/established team: 2-3 columns, stat bar only if ≥2 real verified stats exist.
- Large/corporate: 3-4 columns, numbered capability grid, stat bar.

URGENCY OVERRIDES:
- High urgency: phone number visible at all times (nav, hero, and a persistent sticky call bar), CTA repeated multiple times, short copy, minimal contact form (or no form, just call/email) — friction is the enemy here.
- Low urgency: normal CTA frequency, longer copy is fine, fuller enquiry form is fine.

=== STEP 4: BUILD ===
Fixed page structure — always in this order, sections can be omitted but never reordered:
1. Nav — logo (if a logo src token is given in MEDIA REFERENCES, embed it as an <img> using that exact token as the src; otherwise a typographic wordmark) + scroll-anchor links + primary contact CTA (phone if given, otherwise email/contact-anchor — never invent a phone number).
2. Hero — value proposition, 1-2 CTAs, stat row ONLY if real verified stats exist.
3. About — credibility section using only verified facts. If research surfaces a specific named building, landmark, or notable project the company has worked on (e.g. a listed building, an award entry, a named institution), include it by name in the About body copy. Do not fabricate project names — only include if found in research. Named references are strongly preferred over generic descriptions.
4. Services — from form input, generic safe categories unless something specific was found.
5. Accreditations — OMIT ENTIRELY if nothing was verified. Do not pad with vague reassurance copy instead.
6. Portfolio — if photo src tokens are given in MEDIA REFERENCES, use them as the src for the portfolio image cards, one token per card, in the order given. If no photo tokens are given, render the portfolio grid with a single tasteful placeholder note rather than repeating "Photos Coming Soon" per card — wording along the lines of: "Portfolio photography in preparation. Contact us to discuss examples of work relevant to your project type." Do not repeat placeholder text across multiple cards. Never invent project names.
6a. Testimonials — use web_search to look up this business's real Google Business Profile reviews (search "[business name] [location] google reviews"). If genuine reviews are found, include a Testimonials section with up to 3 real quotes verbatim (trimmed for length is fine, paraphrasing the meaning is not), each attributed with the reviewer's first name/initial and star rating exactly as shown, plus the aggregate rating and review count if visible (e.g. "4.9★ from 32 Google reviews") linking out to the business's Google listing if a URL was found. OMIT THIS SECTION ENTIRELY if no real reviews are found — never fabricate a quote, name, or rating, and never invent an aggregate score.
7. Contact — real contact details only. For low/normal urgency: include an enquiry form that submits via JavaScript fetch() to GW_CONTACT_URL (use this literal string as the fetch URL — it will be substituted at deploy time). The form must include exactly these fields:
   - `<input type="hidden" name="site_id" value="GW_SITE_ID">` (use the literal string GW_SITE_ID as the value — substituted at deploy time)
   - `<input type="text" name="website" tabindex="-1" autocomplete="off" style="position:absolute;left:-9999px;opacity:0;pointer-events:none;" aria-hidden="true">` (spam honeypot, hidden off-screen — must be present but invisible)
   - name (required text input), email (required email input), phone (optional tel input), message (required textarea)
   - A submit button that is disabled during submission
   After a successful response (`json.ok === true`): hide the form and show an inline "Thanks — we'll be in touch shortly." confirmation. On failure: show the `json.error` message inline beneath the form and re-enable the submit button. Never reload or redirect the page on submit.
   For high urgency: omit the form entirely — phone and email link only.
8. Footer — copyright line must read exactly "© {current_year} [business name]." using {current_year} as the literal year (do not use 2025, 2024, or any other year — this is the actual current year, computed at generation time). Do not invent a different year. Below or beside the copyright line, include a small, unobtrusive builder credit: "Website by Groundwork" as a real hyperlink to https://groundworkbuild.com — styled small and low-contrast like a typical site-builder credit line, not a prominent CTA. Never use placeholder text like "Your Agency" or any other agency name.

=== HARD RULES ===
- Never state who specifically performs the work (e.g. "our team of two") — work may be subcontracted. Describe quality/standard instead of staffing.
- Never publish the work_split percentage or contract value figures, even though they were given — these are for tone-setting only. If reflected at all, do it indirectly (e.g. "primarily serving commercial clients" for a commercial-majority split, or omit entirely for a domestic-majority split) with no number.
- Never fabricate reviews, projects, credentials, years-trading, or operational claims (response times, turnaround) — if it's not in the form or verified by search, it doesn't go on the page. Real Google reviews found via web_search (Step 4.6a) are the one exception to "if it's not in the form" — they still must be verified by search, quoted verbatim, and never invented.
- If multiple businesses share a similar name, never conflate them.

=== STEP 5: MARK EDITABLE TEXT ===
After building the full HTML, add the attribute data-gw-text="[id]" to every element whose sole purpose is displaying visible text. This enables customers to edit their site text post-generation.

Mark: headings (h1–h4), body paragraphs (p), button labels, CTA link text, nav anchor text, list items (li) with descriptive copy, and any standalone span containing visible copy.
Do NOT mark: structural containers (div, section, article, header, footer, nav, ul, ol, figure), elements that only wrap other marked elements, SVG/img/input elements, or elements with no visible text content.

ID format — data-gw-text="[section]-[descriptor]-[n]":
- section: the page section (hero, nav, about, services, portfolio, accreditations, testimonials, contact, footer)
- descriptor: what the text is (headline, subheadline, body, cta, title, desc, item, caption, copyright, credit)
- n: 1-based counter when multiple of the same kind exist in a section (omit if only one)

Every ID must be unique across the entire page, lowercase, and use only letters, digits, and hyphens.

Examples: data-gw-text="hero-headline", data-gw-text="hero-subheadline", data-gw-text="hero-cta-1", data-gw-text="about-heading", data-gw-text="about-body-1", data-gw-text="services-card-1-title", data-gw-text="services-card-1-desc", data-gw-text="contact-heading", data-gw-text="footer-copyright".

Output: your Step 2 source list, then the complete single-file HTML starting with <!DOCTYPE html>. No other commentary."""
