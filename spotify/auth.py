"""
Spotify authentication helpers.

There are two token types, stored as separate rows in SpotifyToken:

  'user'   — Authorization Code flow. Requires a one-time browser redirect
              (/spotify/login/). Grants access to user-specific endpoints such
              as /me/tracks. Refresh tokens are long-lived; the system rotates
              access tokens automatically.

  'client' — Client Credentials flow. Fully automatic; no user interaction
              needed. Grants access to public Spotify data (tracks, audio
              features, etc.) but not user-specific endpoints.

get_access_token() is the single entry point for all operations. It:
  - Prefers the user token (broader access) when available.
  - Falls back to client credentials automatically.
  - Refreshes whichever token it uses if it is near expiry.

Nothing outside this module needs to know which flow is active.
"""

import base64
import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

from spotify.models import SpotifyToken

logger = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.spotify.com/api/token"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    """Return a valid access token, refreshing or fetching one as needed.

    Prefers a stored user token (Authorization Code flow) for maximum access.
    Falls back to a client credentials token for app-only access.
    """
    token = _best_stored_token()
    if token is None:
        token = _fetch_client_credentials()
    elif _near_expiry(token):
        token = _refresh(token)
    return token.access_token


def ensure_valid_token() -> None:
    """Proactively ensure a valid token is stored.

    Called at startup so the first real operation never has to wait for a
    fresh fetch. Safe to call even if the DB is empty.
    """
    get_access_token()


def exchange_code(code: str) -> SpotifyToken:
    """Exchange an OAuth authorization code for user tokens (one-time setup).

    Stores (or replaces) the 'user' SpotifyToken row. After this, all
    user-specific Spotify endpoints become available automatically.
    """
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.SPOTIFY_REDIRECT_URI,
        },
        headers={"Authorization": _auth_header()},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token, _ = SpotifyToken.objects.update_or_create(
        token_type=SpotifyToken.TOKEN_TYPE_USER,
        defaults={
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": timezone.now() + timedelta(seconds=data["expires_in"]),
        },
    )
    logger.info("Stored user token (expires %s)", token.expires_at)
    return token


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _best_stored_token() -> SpotifyToken | None:
    """Return the best available stored token (user preferred over client)."""
    return (
        SpotifyToken.objects.filter(token_type=SpotifyToken.TOKEN_TYPE_USER).first()
        or SpotifyToken.objects.filter(token_type=SpotifyToken.TOKEN_TYPE_CLIENT).first()
    )


def _near_expiry(token: SpotifyToken) -> bool:
    return timezone.now() >= token.expires_at - timedelta(seconds=60)


def _refresh(token: SpotifyToken) -> SpotifyToken:
    """Refresh the given token in-place (or re-fetch for client credentials)."""
    if token.token_type == SpotifyToken.TOKEN_TYPE_CLIENT:
        return _fetch_client_credentials()

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        },
        headers={"Authorization": _auth_header()},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token.access_token = data["access_token"]
    token.expires_at = timezone.now() + timedelta(seconds=data["expires_in"])
    if "refresh_token" in data:
        token.refresh_token = data["refresh_token"]
    token.save()
    logger.debug("Refreshed user token (expires %s)", token.expires_at)
    return token


def _fetch_client_credentials() -> SpotifyToken:
    """Obtain an app-only access token via the Client Credentials flow."""
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        headers={"Authorization": _auth_header()},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token, _ = SpotifyToken.objects.update_or_create(
        token_type=SpotifyToken.TOKEN_TYPE_CLIENT,
        defaults={
            "access_token": data["access_token"],
            "refresh_token": "",
            "expires_at": timezone.now() + timedelta(seconds=data["expires_in"]),
        },
    )
    logger.info("Fetched client credentials token (expires %s)", token.expires_at)
    return token


def _auth_header() -> str:
    credentials = f"{settings.SPOTIFY_CLIENT_ID}:{settings.SPOTIFY_CLIENT_SECRET}"
    return "Basic " + base64.b64encode(credentials.encode()).decode()
