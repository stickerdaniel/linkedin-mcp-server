"""
LinkedIn Developer app credential storage.

Stores the Client ID and Client Secret for the LinkedIn Developer app
(not the user's OAuth tokens — those live in user-tokens.json).

Credentials are saved to ~/.linkedin-mcp/app-credentials.json so the user
only has to enter them once. They can also be provided via env vars
LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET which take precedence.
"""

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_CREDENTIALS_FILE = Path("~/.linkedin-mcp/app-credentials.json")


@dataclass
class AppCredentials:
    client_id: str
    client_secret: str


def credentials_path() -> Path:
    return _CREDENTIALS_FILE.expanduser()


def load_app_credentials() -> AppCredentials | None:
    path = credentials_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return AppCredentials(**data)
    except Exception:
        logger.debug("Failed to load app credentials", exc_info=True)
        return None


def save_app_credentials(creds: AppCredentials) -> None:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(creds), indent=2))
    path.chmod(0o600)
