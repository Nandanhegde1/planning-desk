"""Open-Meteo traffic can be routed through a proxy.

On Render's free plan the outbound address is shared, and Open-Meteo answered
every call with "Daily API request limit exceeded" because other tenants had
spent the per-IP allowance. OPEN_METEO_PROXY points the app at a pass-through
instead; these tests pin how the URLs are built and that none bypass it.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import http  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def test_without_a_proxy_the_url_is_unchanged(monkeypatch):
    monkeypatch.setattr(http, "OPEN_METEO_PROXY", "")
    url = "https://api.open-meteo.com/v1/forecast"
    assert http.open_meteo(url) == url


def test_with_a_proxy_the_path_moves_to_the_proxy(monkeypatch):
    monkeypatch.setattr(http, "OPEN_METEO_PROXY", "https://edge.example.workers.dev")
    assert (
        http.open_meteo("https://archive-api.open-meteo.com/v1/archive")
        == "https://edge.example.workers.dev/v1/archive"
    )


def test_the_four_endpoints_have_distinct_paths():
    """The proxy routes on the path alone, so two endpoints sharing one would
    send one of them to the wrong upstream."""
    source = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "core").glob("*.py"))
    urls = set(re.findall(r'"(https://[a-z-]*api\.open-meteo\.com(/v1/[a-z-]+))"', source))
    paths = [path for _, path in urls]
    assert len(paths) == len(set(paths))
    assert set(paths) == {"/v1/forecast", "/v1/archive", "/v1/air-quality", "/v1/search"}


def test_no_open_meteo_url_bypasses_the_proxy():
    for path in (ROOT / "core").glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "open-meteo.com" in line and not line.lstrip().startswith("#"):
                assert "open_meteo(" in line, f"{path.name}: {line.strip()}"


def test_the_edge_worker_serves_exactly_those_paths():
    worker = (ROOT / "edge" / "src" / "index.js").read_text(encoding="utf-8")
    routes = set(re.findall(r'"(/v1/[a-z-]+)":', worker))
    assert routes == {"/v1/forecast", "/v1/archive", "/v1/air-quality", "/v1/search"}
