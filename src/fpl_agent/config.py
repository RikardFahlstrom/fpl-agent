"""Optional ini file for local settings, including credentials.

Every setting the project reads comes from an environment variable. This module lets a
local `fpl-agent.ini` supply them instead, so credentials do not have to be exported by
hand in every shell.

Environment wins. A variable already set is never overwritten, so a scheduled run or a
secret store can override the file without editing it.

The file holds a password in plaintext, so two things are enforced rather than
documented: it is gitignored (`*.ini`, with `!*.ini.example`), and loading warns if the
file is readable by anyone but its owner.
"""

import configparser
import logging
import os
import stat
from pathlib import Path
from typing import Optional

logger = logging.getLogger("fpl_config")

DEFAULT_CONFIG_PATH = Path("fpl-agent.ini")

# (section, option) -> environment variable. The env var stays the interface; the file is
# only another way to populate it, so nothing downstream needs to know this exists.
MAPPING: dict[tuple[str, str], str] = {
    ("auth", "auto_login"): "FPL_AUTO_LOGIN",
    ("auth", "email"): "FPL_EMAIL",
    ("auth", "password"): "FPL_PASSWORD",
    ("auth", "read_only"): "FPL_READ_ONLY",
    ("auth", "token_cache"): "FPL_TOKEN_CACHE",
    ("auth", "token_endpoint"): "FPL_TOKEN_ENDPOINT",
    ("auth", "client_id"): "FPL_OAUTH_CLIENT_ID",
    ("rivals", "leagues"): "FPL_RIVAL_LEAGUES",
    ("server", "transport"): "FPL_MCP_TRANSPORT",
    ("server", "host"): "FPL_MCP_HOST",
    ("server", "port"): "FPL_MCP_PORT",
    ("server", "auth_port"): "FPL_AUTH_PORT",
    ("server", "auth_base_url"): "FPL_AUTH_BASE_URL",
}

SECRET_ENV = {"FPL_PASSWORD", "FPL_EMAIL"}


def _warn_if_world_readable(path: Path) -> None:
    """A file containing a password should not be readable by other accounts."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        logger.warning(
            "%s is readable beyond its owner; it holds a password. "
            "Run: chmod 600 %s", path, path)


def load(path: Path = DEFAULT_CONFIG_PATH) -> dict[str, str]:
    """Populate os.environ from the ini file, without overriding what is already set.

    Returns the variables this call actually set, by name. Values are never returned or
    logged: the point of the file is to keep the password out of sight.
    """
    if not path.exists():
        return {}

    _warn_if_world_readable(path)

    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except configparser.Error as e:
        logger.warning("could not parse %s: %s", path, e)
        return {}

    applied: dict[str, str] = {}
    for (section, option), env_name in MAPPING.items():
        if not parser.has_option(section, option):
            continue
        value = parser.get(section, option).strip()
        if not value:
            continue
        if os.environ.get(env_name, "").strip():
            continue                     # environment wins
        os.environ[env_name] = value
        applied[env_name] = "***" if env_name in SECRET_ENV else value

    if applied:
        logger.info("loaded %s from %s: %s", len(applied), path,
                    ", ".join(sorted(applied)))
    unknown = [
        f"{section}.{option}"
        for section in parser.sections()
        for option in parser.options(section)
        if (section, option) not in MAPPING
    ]
    if unknown:
        logger.warning("ignoring unrecognised settings in %s: %s",
                       path, ", ".join(sorted(unknown)))
    return applied
