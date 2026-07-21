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
# Region lists mix big cities with a much larger set of smaller market
# towns — added 2026-07-20 after noticing the original list was almost
# entirely cities/commuter suburbs, which skews away from where trades
# outreach likely performs best (less competition, weaker existing web
# presence than a city-centre business already fighting for search rank).
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
        "Woking", "Horsham", "Crawley", "Chichester", "Winchester", "Andover",
        "Newbury", "Slough", "Windsor", "Maidenhead", "Dartford", "Gravesend",
        "Sevenoaks", "Ashford", "Dover", "Margate", "Ramsgate", "Rochester",
        "Chatham", "Gillingham", "Aldershot", "Farnborough", "Fareham", "Gosport",
        "Petersfield", "Haywards Heath", "Crowborough", "Horley", "Redhill",
        "Epsom", "Leatherhead", "Dorking", "Camberley", "Bracknell", "Wokingham",
        "Thame", "Wallingford", "Bicester", "Banbury", "Aylesbury", "High Wycombe",
        "Amersham", "Chesham", "Marlow",
    ],
    ("South West", "medium"): [
        "Bristol", "Bath", "Exeter", "Plymouth", "Swindon", "Bournemouth",
        "Poole", "Cheltenham", "Gloucester", "Taunton", "Torquay",
        "Weston-super-Mare", "Yeovil", "Trowbridge", "Chippenham", "Salisbury",
        "Frome", "Weymouth", "Dorchester", "Bridgwater", "Wells", "Glastonbury",
        "Tiverton", "Barnstaple", "Bideford", "Truro", "Falmouth", "Penzance",
        "St Austell", "Newquay", "Camborne", "Redruth", "Launceston", "Bodmin",
        "Street", "Shepton Mallet", "Melksham", "Devizes", "Marlborough",
        "Tavistock", "Newton Abbot", "Paignton", "Totnes",
    ],
    ("East of England", "medium"): [
        "Cambridge", "Norwich", "Ipswich", "Chelmsford", "Luton",
        "Milton Keynes", "Peterborough", "Colchester", "Southend-on-Sea",
        "St Albans", "Watford", "Stevenage", "Hitchin", "Letchworth",
        "Welwyn Garden City", "Hemel Hempstead", "Bishop's Stortford", "Harlow",
        "Braintree", "Witham", "Maldon", "Great Yarmouth", "King's Lynn",
        "Bury St Edmunds", "Newmarket", "Ely", "Huntingdon", "St Neots",
        "Wisbech", "Thetford", "Diss", "Sudbury", "Saffron Walden", "Royston",
    ],
    ("East Midlands", "medium"): [
        "Birmingham", "Coventry", "Leicester", "Nottingham", "Derby",
        "Wolverhampton", "Stoke-on-Trent", "Walsall", "Dudley",
        "Mansfield", "Chesterfield", "Loughborough", "Kettering", "Wellingborough",
        "Corby", "Rushden", "Grantham", "Newark-on-Trent", "Worksop", "Retford",
        "Melton Mowbray", "Hinckley", "Rugby", "Oakham", "Boston", "Spalding",
        "Ilkeston", "Long Eaton", "Beeston",
    ],
    ("West Midlands / Central", "medium"): [
        "Northampton", "Lincoln", "Shrewsbury", "Worcester", "Hereford",
        "Telford", "Kidderminster", "Redditch", "Bromsgrove", "Solihull",
        "Nuneaton", "Tamworth", "Cannock", "Lichfield", "Stafford", "Leek",
        "Newcastle-under-Lyme", "Market Drayton", "Ludlow", "Bridgnorth",
        "Oswestry", "Bromyard", "Leominster", "Ross-on-Wye", "Malvern", "Evesham",
    ],
    ("North West", "medium"): [
        "Manchester", "Liverpool", "Preston", "Blackpool", "Bolton",
        "Wigan", "Chester", "Warrington", "Stockport", "Salford", "Oldham",
        "Lancaster", "Morecambe", "Kendal", "Barrow-in-Furness", "Southport",
        "Crewe", "Nantwich", "Macclesfield", "Northwich", "Runcorn", "Widnes",
        "St Helens", "Skelmersdale", "Burnley", "Nelson", "Colne", "Accrington",
        "Rawtenstall", "Rochdale", "Bury", "Ashton-under-Lyne", "Altrincham", "Sale",
    ],
    ("Yorkshire", "low"): [
        "Leeds", "Sheffield", "Bradford", "Hull", "York", "Wakefield",
        "Harrogate", "Huddersfield", "Doncaster", "Rotherham",
        "Scarborough", "Whitby", "Ripon", "Skipton", "Keighley", "Barnsley",
        "Pontefract", "Castleford", "Selby", "Goole", "Beverley", "Bridlington",
        "Halifax", "Dewsbury", "Batley", "Pudsey", "Otley", "Ilkley", "Malton",
        "Northallerton", "Thirsk", "Richmond (Yorks)",
    ],
    ("North East", "low"): [
        "Newcastle", "Sunderland", "Middlesbrough", "Durham", "Gateshead",
        "Hexham", "Berwick-upon-Tweed", "Alnwick", "Morpeth", "Blyth",
        "Ashington", "Consett", "Bishop Auckland", "Darlington", "Hartlepool",
        "Stockton-on-Tees", "Peterlee", "Chester-le-Street", "Washington (T&W)",
    ],
    ("Scotland", "medium"): [
        "Glasgow", "Edinburgh", "Aberdeen", "Dundee", "Inverness", "Stirling",
        "Perth", "Ayr", "Kilmarnock", "Paisley", "Falkirk", "Livingston",
        "Dunfermline", "Kirkcaldy", "St Andrews", "Elgin", "Peterhead",
        "Fraserburgh", "Arbroath", "Montrose", "Hamilton", "East Kilbride",
        "Motherwell", "Greenock", "Dumfries", "Galashiels",
    ],
    ("Wales", "low"): [
        "Cardiff", "Swansea", "Newport", "Wrexham",
        "Bangor, Wales", "Llandudno", "Rhyl", "Colwyn Bay", "Aberystwyth",
        "Carmarthen", "Haverfordwest", "Pembroke", "Merthyr Tydfil", "Bridgend",
        "Neath", "Port Talbot", "Llanelli", "Pontypridd", "Caerphilly", "Barry",
        "Monmouth", "Abergavenny", "Brecon",
    ],
    ("Northern Ireland", "low"): [
        "Belfast",
        "Derry", "Lisburn", "Newry", "Bangor (NI)", "Craigavon",
        "Ballymena", "Newtownabbey", "Coleraine", "Omagh", "Enniskillen", "Armagh",
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
