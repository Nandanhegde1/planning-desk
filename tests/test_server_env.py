"""Every knob a server process reads must be forwarded to it.

A stdio MCP server starts with a minimal environment. A key missing from
SERVER_ENV_KEYS is invisible to the server that reads it, however carefully it was
set. Locally each server calls load_dotenv itself so this went unnoticed, but
.dockerignore excludes .env, so in the container the value was simply absent.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
READS_ENV = re.compile(r"os\.environ\.get\(\s*[\"']([A-Z0-9_]+)[\"']")

# Set by the platform or read only in the host process, so not forwarded.
NOT_FORWARDED = {"PORT", "PATH", "HOME", "PYTHONPATH", "MPLCONFIGDIR", "WEBSITE_HOSTNAME"}


def test_every_env_key_read_inside_a_server_is_forwarded():
    read_in_child: dict[str, str] = {}
    for path in list((ROOT / "core").glob("*.py")) + list((ROOT / "servers").glob("*.py")):
        for name in READS_ENV.findall(path.read_text(encoding="utf-8")):
            read_in_child.setdefault(name, path.name)

    missing = {
        name: where
        for name, where in read_in_child.items()
        if name not in config.SERVER_ENV_KEYS and name not in NOT_FORWARDED
    }

    assert not missing, (
        "these are read in a child process but never forwarded to it: "
        + ", ".join(f"{k} ({v})" for k, v in sorted(missing.items()))
    )
