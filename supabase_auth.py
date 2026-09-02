"""
supabase_auth.py
Resolves the logged-in user for a request, for the one endpoint that
needs to know WHO is publishing when nothing else in the request implies
it: /import-trades. (The daily sync pipeline never needs this -- it
already knows the user from the broker_accounts row it's processing.)

The site's other pages never send a user id to chart_service.py at all --
they query Supabase directly from the browser with the anon key, and
Row Level Security scopes every read/write to auth.uid() automatically
(see web-service/auth.js's header comment). That works fine client-side,
but chart_service.py publishes with the SERVICE ROLE key (it has to, to
write into other users' rows during the daily sync), which bypasses RLS
entirely -- so for any write it does, it must know the target user_id
itself rather than relying on RLS to sort it out.

The fix: the browser already holds a Supabase access token once someone's
logged in (window.sb.auth.getSession()). Have it send that as
`Authorization: Bearer <token>` on the /import-trades request, and verify
it here against Supabase's own /auth/v1/user endpoint (using the
service-role key as `apikey`, which is allowed to look up any valid
user's token) to recover their user_id server-side. A forged/expired/
missing token gets rejected with 401 before anything is parsed or
matched -- so a request can't publish trades under a user_id it doesn't
have a valid token for.

Reuses SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY from daily_sync.py /
publish.py -- no new env vars.
"""

import os
import logging

import requests

log = logging.getLogger("chart_service.supabase_auth")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def resolve_user_id(auth_header: str | None) -> str | None:
    """auth_header is the raw 'Authorization' header value, expected as
    'Bearer <supabase access token>'. Returns the user's id (a uuid
    string) on success, or None if the header is missing/malformed or
    the token doesn't check out."""
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return None
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        log.error("resolve_user_id: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
        return None

    try:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_SERVICE_ROLE_KEY},
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning("resolve_user_id: Supabase auth check failed: %s", e)
        return None

    if resp.status_code != 200:
        return None
    return (resp.json() or {}).get("id")
