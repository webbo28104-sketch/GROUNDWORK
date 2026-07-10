"""
Trade categories and UK search areas for the outreach sourcer.

TRADE_CATEGORIES is the canonical list of (search_term, canonical_name, tier)
tuples. The search_term is what we actually send to the Places API; the
canonical_name is the tidy label we store/display; the tier drives the scorer's
trade-tier points (high=20, medium=12, low=5).

UK_AREAS is the list of population centres we search each trade in. The sourcer
builds one SearchCell per (area, trade) combination and works through them
oldest-first, so coverage spreads evenly rather than exhausting one city.
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

# UK search areas — major population centres
UK_AREAS = [
    # London
    "East London", "North London", "South London", "West London", "Central London",
    "Croydon", "Bromley", "Kingston upon Thames", "Sutton", "Enfield", "Barnet",
    "Harrow", "Ealing", "Hounslow", "Lewisham", "Greenwich",
    # South East
    "Brighton", "Hove", "Guildford", "Canterbury", "Maidstone", "Tunbridge Wells",
    "Oxford", "Reading", "Southampton", "Portsmouth", "Basingstoke", "Worthing",
    "Eastbourne", "Hastings", "Folkestone",
    # South West
    "Bristol", "Bath", "Exeter", "Plymouth", "Swindon", "Bournemouth",
    "Poole", "Cheltenham", "Gloucester", "Taunton", "Torquay",
    # East of England
    "Cambridge", "Norwich", "Ipswich", "Chelmsford", "Luton",
    "Milton Keynes", "Peterborough", "Colchester", "Southend-on-Sea",
    # East Midlands
    "Birmingham", "Coventry", "Leicester", "Nottingham", "Derby",
    "Wolverhampton", "Stoke-on-Trent", "Walsall", "Dudley",
    # West Midlands / Central
    "Northampton", "Lincoln", "Shrewsbury", "Worcester", "Hereford",
    # North West
    "Manchester", "Liverpool", "Preston", "Blackpool", "Bolton",
    "Wigan", "Chester", "Warrington", "Stockport", "Salford", "Oldham",
    # Yorkshire
    "Leeds", "Sheffield", "Bradford", "Hull", "York", "Wakefield",
    "Harrogate", "Huddersfield", "Doncaster", "Rotherham",
    # North East
    "Newcastle", "Sunderland", "Middlesbrough", "Durham", "Gateshead",
    # Scotland
    "Glasgow", "Edinburgh", "Aberdeen", "Dundee", "Inverness", "Stirling",
    # Wales
    "Cardiff", "Swansea", "Newport", "Wrexham",
    # Northern Ireland
    "Belfast",
]


def get_trade_by_term(term):
    """Return (canonical_name, tier) for a raw search term, or (term, 'low')
    as a safe fallback if the term isn't in the table (shouldn't happen, but
    keeps the sourcer from crashing on an unexpected value)."""
    name = TRADE_CANONICAL_MAP.get(term, term)
    tier = TRADE_TIER_MAP.get(term, "low")
    return name, tier
