"""Who is calling: checks an Auth0 access token and returns its claims.

The phone signs in with Auth0 and sends a short-lived access token (an hour)
with each request. We check it was signed by our Auth0 tenant, for this API,
for our app, and hasn't expired. A leaked token is useless an hour later, and
signing out or blocking someone in Auth0 stops their refreshes.

Settings (all public values, none of them are secrets):
    AUTH0_DOMAIN     dev-xxxx.us.auth0.com
    AUTH0_AUDIENCE   the API identifier, e.g. https://mumble-azure.vercel.app/api
    AUTH0_CLIENT_ID  the Single Page Application's client id
"""

import json
import os
import urllib.request

import jwt

_jwks: jwt.PyJWKClient | None = None


class AuthError(Exception):
    pass


def settings() -> dict:
    return {
        "domain": os.environ.get("AUTH0_DOMAIN", ""),
        "audience": os.environ.get("AUTH0_AUDIENCE", ""),
        "client_id": os.environ.get("AUTH0_CLIENT_ID", ""),
    }


def _signing_key(token: str):
    global _jwks
    if _jwks is None:
        # Cached across requests while the function stays warm.
        _jwks = jwt.PyJWKClient(f"https://{settings()['domain']}/.well-known/jwks.json",
                                cache_keys=True, lifespan=3600, timeout=5)
    return _jwks.get_signing_key_from_jwt(token).key


def verify(token: str) -> dict:
    s = settings()
    if not all(s.values()):
        raise AuthError("sign-in isn't set up")  # fail closed, never open
    if not token:
        raise AuthError("sign in first")
    try:
        claims = jwt.decode(
            token,
            _signing_key(token),
            algorithms=["RS256"],  # never "none", never HS256 with the public key
            audience=s["audience"],
            issuer=f"https://{s['domain']}/",
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            leeway=30,
        )
    except (jwt.PyJWTError, ValueError) as e:
        raise AuthError("bad or expired sign-in") from e
    # Tokens for this API issued to some other app (or a machine client) don't count.
    if claims.get("azp") != s["client_id"]:
        raise AuthError("token is for another app")
    return claims


def userinfo(token: str) -> dict:
    """Email and whether it's verified. Asked once, when an account is first seen."""
    req = urllib.request.Request(f"https://{settings()['domain']}/userinfo",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except (OSError, ValueError) as e:
        raise AuthError("couldn't check your email") from e
