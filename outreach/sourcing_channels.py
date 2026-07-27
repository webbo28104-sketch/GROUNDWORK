"""
Groundwork outreach — sourcing-channel registry (added 2026-07-27, by
request), the single place both the pipeline (where a Prospect.sourcing_
channel value gets set) and the admin UI (where it gets filtered/labeled)
read from, so the two can't drift apart.

Sourcing channel answers "how was this BUSINESS originally found" — fixed
at creation time, independent of outreach method (OutreachTouch.channel:
email/sms/facebook) and independent of email_source (how a specific email
was found, which can itself be socially-sourced on a google_places
prospect without that prospect becoming socials-sourced — see
Prospect.sourcing_channel's docstring in models.py for the full policy).
"""

GOOGLE_PLACES = "google_places"
SOCIALS = "socials"

# Display order + labels for admin UI selects/tabs. Extend this dict (and
# set the new value explicitly wherever that channel's prospects are
# created) when a new sourcing method is added — nothing else needs to
# change to make it show up as a filterable option.
SOURCING_CHANNEL_LABELS = {
    GOOGLE_PLACES: "Google Places",
    SOCIALS: "Socials",
}
