"""
Domain billing maintenance — daily job (added 2026-07-14).

Runnable as a module or script from the project root:
    python outreach/domain_billing.py
    python -m outreach.domain_billing

What it does (see app.py's run_domain_billing_maintenance / _domain_reprice_if_due /
_domain_check_renewal_grace for the actual logic):
  1. For every domain on a yearly renewal subscription (Domain.stripe_subscription_id
     set — grandfathered one-time-payment domains are untouched), checks whether
     the subscription renews within 30 days and, if so, re-fetches live Porkbun
     wholesale pricing and updates the subscription's price for the NEXT invoice
     only if the guaranteed-margin formula now comes out differently. Silent, no
     customer notice, never changes an already-issued invoice.
  2. For any domain whose renewal payment has been failing for more than
     DOMAIN_RENEWAL_GRACE_DAYS, disables Porkbun auto-renewal so Groundwork stops
     paying to renew a domain nobody's paying for — ahead of (not instead of)
     customer.subscription.deleted, since Stripe's own dunning schedule can span
     weeks longer than that grace period.

Needs to run on a real recurring schedule (a Railway Cron service pointed at
this script) — there is no in-process scheduler in this codebase, so nothing
calls this automatically on its own.
"""
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_PROJECT_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
except Exception:
    pass

from app import run_domain_billing_maintenance  # noqa: E402


def main():
    run_domain_billing_maintenance()


if __name__ == "__main__":
    main()
