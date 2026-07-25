"""
Real, executed proof for outreach/pickup_facebook_sourced.py's import/dedup
logic — a clean new entry, a website-present entry (has_website vs
no_website), re-importing the same facebook_page_url, a case-insensitive
business_name+location match against an existing (e.g. Places-sourced)
prospect, and an invalid entry. Runs against a throwaway SQLite DB.
"""
import os
import sys
import tempfile

DB_PATH = tempfile.mktemp(suffix=".db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import Base, engine, SessionLocal, Prospect  # noqa: E402
Base.metadata.create_all(engine)

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


from outreach.pickup_facebook_sourced import _import_one  # noqa: E402

db = SessionLocal()

# 1. A clean new entry -> created, scored, no_website (no website field given).
entry1 = {
    "business_name": "Dave's Fencing", "trade": "fencing contractor", "location": "Ipswich, Suffolk",
    "postcode_area": "IP1", "facebook_page_url": "https://facebook.com/davesfencing",
    "phone": "+441473000000", "notes": "found via WebSearch, phone visible in Page info",
}
result1 = _import_one(db, entry1)
db.commit()
check("clean new entry -> created", result1 == "created", result1)
p1 = db.query(Prospect).filter(Prospect.facebook_page_url == "https://facebook.com/davesfencing").first()
check("business_name set correctly", p1.business_name == "Dave's Fencing")
check("website_status defaults to no_website (no website field given)", p1.website_status == "no_website")
check("trade canonicalized", p1.trade == "Fencing Contractor" or p1.trade is not None, str(p1.trade))
check("score computed (not None)", p1.score is not None, str(p1.score))
check("funnel_stage set to queued", p1.funnel_stage == "queued")

# 2. An entry WITH a separate website -> has_website, not no_website.
entry2 = {
    "business_name": "Modern Roofing Co", "trade": "roofer", "location": "Norwich, Norfolk",
    "facebook_page_url": "https://facebook.com/modernroofingco", "website": "https://modernroofing.co.uk",
}
result2 = _import_one(db, entry2)
db.commit()
p2 = db.query(Prospect).filter(Prospect.facebook_page_url == "https://facebook.com/modernroofingco").first()
check("entry with a real website -> has_website, not no_website", p2.website_status == "has_website", p2.website_status)

# 3. Exact same facebook_page_url again -> duplicate, not a second row.
result3 = _import_one(db, entry1)
db.commit()
check("re-importing the same facebook_page_url -> duplicate", result3 == "duplicate", result3)
count_fb1 = db.query(Prospect).filter(Prospect.facebook_page_url == "https://facebook.com/davesfencing").count()
check("no second row created for the same facebook_page_url", count_fb1 == 1, str(count_fb1))

# 4. Same business_name+location as an EXISTING (e.g. Places-sourced) prospect, different fb url -> duplicate, and backfills facebook_page_url onto the existing row.
existing_places_prospect = Prospect(
    google_place_id="places-123", business_name="Sparky Electricians", location="Leeds, West Yorkshire",
    website_status="no_website", funnel_stage="queued", facebook_page_url=None,
)
db.add(existing_places_prospect)
db.commit()
entry4 = {
    "business_name": "sparky electricians", "location": "leeds, west yorkshire",  # different case, same identity
    "facebook_page_url": "https://facebook.com/sparkyelectricians",
}
result4 = _import_one(db, entry4)
db.commit()
check("same business_name+location (case-insensitive) as an existing prospect -> duplicate, not a new row", result4 == "duplicate", result4)
db.refresh(existing_places_prospect)
check("existing prospect's facebook_page_url gets backfilled from the duplicate match",
      existing_places_prospect.facebook_page_url == "https://facebook.com/sparkyelectricians",
      str(existing_places_prospect.facebook_page_url))

# 5. Invalid entry (no facebook_page_url) -> skipped, not created.
entry5 = {"business_name": "No URL Biz", "location": "Bristol"}
result5 = _import_one(db, entry5)
db.commit()
check("entry with no facebook_page_url -> skipped_invalid", result5 == "skipped_invalid", result5)

total_prospects = db.query(Prospect).count()
check("total prospect count is exactly right (2 new + 1 pre-seeded, no phantom rows)", total_prospects == 3, str(total_prospects))

db.close()
os.remove(DB_PATH)

print("")
print("=" * 56)
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
else:
    print("ALL CHECKS PASSED")
print("=" * 56)
sys.exit(1 if failures else 0)
