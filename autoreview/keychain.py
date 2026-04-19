"""API key: VENICE_API_KEY env, else macOS/Linux keychain via keyring."""
from __future__ import annotations

import getpass
import hashlib
import logging
import os
from functools import lru_cache
from pathlib import Path

KEYRING_SERVICE = "autoreview-venice"
KEYRING_USERNAME_GLOBAL = "global"
logger = logging.getLogger(__name__)


@lru_cache(maxsize=512)
def _project_key_username_for_path(resolved_abs: str) -> str:
    return hashlib.sha256(resolved_abs.encode("utf-8")).hexdigest()


def project_key_username(root: Path) -> str:
    """Legacy keyring username for a review target directory (per-folder storage)."""
    return _project_key_username_for_path(str(root.resolve()))


def get_api_key(root: Path) -> str | None:
    """Return API key from env or keychain; None if missing.

    Keychain storage is **one key for the whole app** (service ``autoreview-venice``,
    username ``global``). Older releases stored per-folder; we read that as a fallback and
    **migrate** it to the global entry on first use so other folders see the same key.
    """
    env = os.environ.get("VENICE_API_KEY", "").strip()
    if env:
        return env
    try:
        import keyring  # type: ignore[import-untyped]
    except ImportError:
        return None
    val = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME_GLOBAL)
    if val:
        return val
    legacy = keyring.get_password(KEYRING_SERVICE, project_key_username(root))
    if not legacy:
        return None
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME_GLOBAL, legacy)
        keyring.delete_password(KEYRING_SERVICE, project_key_username(root))
        logger.info("Migrated legacy per-folder API key to global keychain entry")
    except Exception as e:
        logger.warning("Could not migrate legacy API key to global: %s", e)
    return legacy


def set_api_key(root: Path, api_key: str) -> None:
    """Store the Venice API key in the system keychain for all projects.

    ``root`` is only used to remove a legacy per-folder entry for that path after saving
    the global key (migration from older Autoreview versions).
    """
    if not (api_key or "").strip():
        raise ValueError("API key cannot be empty.")
    try:
        import keyring  # type: ignore[import-untyped]
        from keyring import errors as keyring_errors  # type: ignore[import-untyped]
    except ImportError as e:
        raise RuntimeError(
            "keyring is not installed; run: pip install keyring  (or set VENICE_API_KEY in the environment)"
        ) from e
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME_GLOBAL, api_key.strip())
    try:
        keyring.delete_password(KEYRING_SERVICE, project_key_username(root))
    except keyring_errors.PasswordDeleteError:
        pass
    logger.info("Stored API key in keychain (service=%s, global)", KEYRING_SERVICE)


def delete_api_key(root: Path) -> bool:
    """Remove stored keys: the global app key and any legacy per-``root`` entry. Returns True if any was removed."""
    try:
        import keyring  # type: ignore[import-untyped]
        from keyring import errors as keyring_errors  # type: ignore[import-untyped]
    except ImportError:
        return False
    removed = False
    for user in (KEYRING_USERNAME_GLOBAL, project_key_username(root)):
        try:
            keyring.delete_password(KEYRING_SERVICE, user)
        except keyring_errors.PasswordDeleteError:
            continue
        removed = True
    if removed:
        logger.info("Removed API key from keychain (service=%s)", KEYRING_SERVICE)
    return removed


def prompt_and_store_key(root: Path, prompt: str = "Venice API key: ") -> str:
    """Prompt once (masked), store in keychain, return key."""
    key = getpass.getpass(prompt)
    if not key.strip():
        raise ValueError("API key cannot be empty.")
    set_api_key(root, key.strip())
    return key.strip()
