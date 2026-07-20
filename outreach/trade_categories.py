"""
Trade categories and UK search areas for the outreach sourcer.

TRADE_CATEGORIES is the canonical list of (search_term, canonical_name, tier)
tuples. The search_term is what we actually send to the Places API; the
canonical_name is the tidy label we store/display; the tier drives the scorer's
trade-tier points (high=20, medium=12, low=5).

UK_AREAS is the list of population centres we search each trade in. The sourcer
builds one SearchCell per (area, trade) combination and works through them
oldest-first, so coverage spreads evenly rather than exhausting one city.

AREA_INCOME_TIER tags each area with a rough regional income band (high/medium/
low), from ONS regional gross disposable household income (GDHI) per head —
London & the South East highest, North East/Wales/Yorkshire/Northern Ireland
lowest, everywhere else medium (ons.gov.uk/economy/regionalaccounts/
grossdisposablehouseholdincome). This is a *region-level* grouping, not a
per-city ranking — city-level disposable income after housing costs doesn't
track this cleanly (e.g. London residents have high gross income but low
disposable income after rent), and area-level noise isn't worth chasing for
what this is actually used for: making sure sourcing doesn't get stuck in one
economic bracket for weeks (see get_pending_cells' shuffle) and letting
click-through eventually get analysed against a real economic signal instead
of just "London" vs "not London".
"""

# List of (search_term, canonical_name, tier)
TRADE_CATEGORIES = [
    # High tier (20 pts) — consumer search-driven
    ("plumber", "Plumber", "high"),
    ("electrician", "Electrician", "high"),
    ("heating engineer", "Heating Engineer", "high"),
    ("roofer", "Roofer", "high"),
    ("landscaper", "Landscaper", "high"),
    ("gardener", "Gardener", "high"),
    ("painter decorator", "Painter/Decorator", "high"),
    ("locksmith", "Locksmith", "high"),
    ("domestic cleaner", "Domestic Cleaner", "high"),
    ("pest control", "Pest Control", "high"),
    ("tree surgeon", "Tree Surgeon", "high"),
    ("driveway contractor", "Driveway/Paving", "high"),
    ("fencing contractor", "Fencing", "high"),
    ("guttering specialist", "Guttering", "high"),
    ("handyman", "Handyman", "high"),
    ("kitchen fitter", "Kitchen Fitter", "high"),
    ("bathroom fitter", "Bathroom Fitter", "high"),
    ("tiler", "Tiler", "high"),
    ("flooring fitter", "Flooring Fitter", "high"),
    ("glazier", "Glazier", "high"),
    ("garage door repair", "Garage Door Fitter", "high"),
    ("appliance repair", "Appliance Repair", "high"),
    ("chimney sweep", "Chimney Sweep", "high"),
    # Medium tier (12 pts)
    ("builder", "Builder", "medium"),
    ("carpenter", "Carpenter", "medium"),
    ("plasterer", "Plasterer", "medium"),
    ("bricklayer", "Bricklayer", "medium"),
    # Low tier (5 pts)
    ("scaffolding company", "Scaffolding", "low"),
    ("groundworks contractor", "Groundworks", "low"),
    ("demolition contractor", "Demolition", "low"),
]

TRADE_TIER_MAP = {term: tier for term, _, tier in TRADE_CATEGORIES}
TRADE_CANONICAL_MAP = {term: name for term, name, _ in TRADE_CATEGORIES}

# UK search areas — major population centres, grouped by ONS region. Each
# region is tagged with a rough income tier (see AREA_INCOME_TIER note above);
# UK_AREAS itself stays a flat list of names (unchanged shape — sourcer.py
# iterates/len()s it directly) so day-to-day sourcing diversity comes from
# get_pending_cells' shuffle, not from reordering this list.
_AREAS_BY_REGION = {
    ("London", "high"): [
        "East London", "North London", "South London", "West London", "Central London",
        "Croydon", "Bromley", "Kingston upon Thames", "Sutton", "Enfield", "Barnet",
        "Harrow", "Ealing", "Hounslow", "Lewisham", "Greenwich",
    ],
    ("South East", "high"): [
        "Brighton", "Hove", "Guildford", "Canterbury", "Maidstone", "Tunbridge Wells",
        "Oxford", "Reading", "Southampton", "Portsmouth", "Basingstoke", "Worthing",
        "Eastbourne", "Hastings", "Folkestone",
    ],
    ("South West", "medium"): [
        "Bristol", "Bath", "Exeter", "Plymouth", "Swindon", "Bournemouth",
        "Poole", "Cheltenham", "Gloucester", "Taunton", "Torquay",
    ],
    ("East of England", "medium"): [
        "Cambridge", "Norwich", "Ipswich", "Chelmsford", "Luton",
        "Milton Keynes", "Peterborough", "Colchester", "Southend-on-Sea",
    ],
    ("East Midlands", "medium"): [
        "Birmingham", "Coventry", "Leicester", "Nottingham", "Derby",
        "Wolverhampton", "Stoke-on-Trent", "Walsall", "Dudley",
    ],
    ("West Midlands / Central", "medium"): [
        "Northampton", "Lincoln", "Shrewsbury", "Worcester", "Hereford",
    ],
    ("North West", "medium"): [
        "Manchester", "Liverpool", "Preston", "Blackpool", "Bolton",
        "Wigan", "Chester", "Warrington", "Stockport", "Salford", "Oldham",
    ],
    ("Yorkshire", "low"): [
        "Leeds", "Sheffield", "Bradford", "Hull", "York", "Wakefield",
        "Harrogate", "Huddersfield", "Doncaster", "Rotherham",
    ],
    ("North East", "low"): [
        "Newcastle", "Sunderland", "Middlesbrough", "Durham", "Gateshead",
    ],
    ("Scotland", "medium"): [
        "Glasgow", "Edinburgh", "Aberdeen", "Dundee", "Inverness", "Stirling",
    ],
    ("Wales", "low"): [
        "Cardiff", "Swansea", "Newport", "Wrexham",
    ],
    ("Northern Ireland", "low"): [
        "Belfast",
    ],
}

UK_AREAS = [area for areas in _AREAS_BY_REGION.values() for area in areas]

AREA_REGION = {area: region for (region, _tier), areas in _AREAS_BY_REGION.items() for area in areas}
AREA_INCOME_TIER = {area: tier for (_region, tier), areas in _AREAS_BY_REGION.items() for area in areas}


def get_trade_by_term(term):
    """Return (canonical_name, tier) for a raw search term, or (term, 'low')
    as a safe fallback if the term isn't in the table (shouldn't happen, but
    keeps the sourcer from crashing on an unexpected value)."""
    name = TRADE_CANONICAL_MAP.get(term, term)
    tier = TRADE_TIER_MAP.get(term, "low")
    return name, tier
