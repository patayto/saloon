import logging
import sys

from django.apps import AppConfig

logger = logging.getLogger(__name__)

# Commands where the spotify_spotifytoken table may not exist yet — skip init.
_SKIP_INIT_COMMANDS = {"migrate", "makemigrations", "sqlmigrate", "showmigrations"}


class SpotifyConfig(AppConfig):
    name = 'spotify'

    def ready(self):
        """Proactively initialize a Spotify token when Django starts.

        Runs for both `runserver` and management commands, so both CLI and web
        operations start with a valid token in place. Failures are logged and
        swallowed — a missing token never prevents Django from starting.
        """
        invoked_command = sys.argv[1] if len(sys.argv) > 1 else ""
        if invoked_command in _SKIP_INIT_COMMANDS:
            return

        try:
            import warnings
            # Django warns about DB access in ready() for multi-process setups.
            # For SQLite/dev this is intentional and safe.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="Accessing the database during app initialization",
                    category=RuntimeWarning,
                )
                from spotify.auth import ensure_valid_token
                ensure_valid_token()
            logger.info("Spotify token ready.")
        except Exception as exc:
            logger.warning("Could not initialize Spotify token at startup: %s", exc)
