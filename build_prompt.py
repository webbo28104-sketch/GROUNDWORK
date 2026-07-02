"""
Groundwork — production site-generation prompt.

This is the actual prompt sent to Claude when a client's form is submitted.
It is the distilled version of SITE_GENERATION_SPEC.md — instructions only,
no commentary or history. Update this file when the spec doc changes;
don't send the spec doc itself to the API.
"""

def build_prompt(form_data: dict) -> str:
    """
    form_data keys expected:
    business_name, trade, location, coverage_area, phone, email,
    logo_uploaded (bool), portfolio_uploaded (bool),
    work_split (plain-language string, e.g. "30% domestic / 70% commercial"),
    craft_prestige, team_size,
    large_commercial_contracts (bool), urgency,
    years_trading, claimed_accreditations, claimed_projects, other_notes
    """

    facts = "\n".join(f"- {k}: {v}" for k, v in form_data.items() if v)

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

LAYOUT DENSITY (by SCALE):
- Sole trader: 1-2 column grids, generous whitespace, no stat bar.
- Small/established team: 2-3 columns, stat bar only if ≥2 real verified stats exist.
- Large/corporate: 3-4 columns, numbered capability grid, stat bar.

URGENCY OVERRIDES:
- High urgency: phone number visible at all times (nav, hero, and a persistent sticky call bar), CTA repeated multiple times, short copy, minimal contact form (or no form, just call/email) — friction is the enemy here.
- Low urgency: normal CTA frequency, longer copy is fine, fuller enquiry form is fine.

=== STEP 4: BUILD ===
Fixed page structure — always in this order, sections can be omitted but never reordered:
1. Nav — logo (embed the real uploaded logo image directly if provided, else a typographic wordmark) + scroll-anchor links + primary contact CTA (phone if given, otherwise email/contact-anchor — never invent a phone number).
2. Hero — value proposition, 1-2 CTAs, stat row ONLY if real verified stats exist.
3. About — credibility section using only verified facts. If research surfaces a specific named building, landmark, or notable project the company has worked on (e.g. a listed building, an award entry, a named institution), include it by name in the About body copy. Do not fabricate project names — only include if found in research. Named references are strongly preferred over generic descriptions.
4. Services — from form input, generic safe categories unless something specific was found.
5. Accreditations — OMIT ENTIRELY if nothing was verified. Do not pad with vague reassurance copy instead.
6. Portfolio — use uploaded photos if provided. If no photos are provided, render the portfolio grid with a single tasteful placeholder note rather than repeating "Photos Coming Soon" per card — wording along the lines of: "Portfolio photography in preparation. Contact us to discuss examples of work relevant to your project type." Do not repeat placeholder text across multiple cards. Never invent project names.
7. Contact — real contact details only, plus a front-end-only enquiry form (or the urgency-driven minimal version above). If no verified public email address was found, omit the email field entirely — no placeholder text or invented addresses. Phone number only is acceptable.
8. Footer.

=== HARD RULES ===
- Never state who specifically performs the work (e.g. "our team of two") — work may be subcontracted. Describe quality/standard instead of staffing.
- Never publish the work_split percentage or contract value figures, even though they were given — these are for tone-setting only. If reflected at all, do it indirectly (e.g. "primarily serving commercial clients" for a commercial-majority split, or omit entirely for a domestic-majority split) with no number.
- Never fabricate reviews, projects, credentials, years-trading, or operational claims (response times, turnaround) — if it's not in the form or verified by search, it doesn't go on the page.
- If multiple businesses share a similar name, never conflate them.

Output: your Step 2 source list, then the complete single-file HTML starting with <!DOCTYPE html>. No other commentary."""
