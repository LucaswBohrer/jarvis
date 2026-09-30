#!/usr/bin/env python3
"""Read-only smoke test against the real NEXUS HTTP API (§25 of the audit).

Hits the three endpoints the JARVIS NexusHttpAdapter uses, in order, with
short timeouts. READ-ONLY: only GET requests, never POST/PUT/DELETE, never
touches the NEXUS database or files.

Usage:
    cd ~/workspace/jarvis
    .venv/bin/python scripts/smoke_nexus_status.py
    # or against another base URL:
    JARVIS_NEXUS_BASE_URL=http://127.0.0.1:8000 .venv/bin/python scripts/smoke_nexus_status.py

Exit codes:
    0 - all three endpoints answered 2xx with JSON
    1 - NEXUS unreachable / any endpoint failed (expected when the NEXUS
        server is not running; the JARVIS adapter treats this as
        "NEXUS indisponivel" and answers from its deterministic fallback)
    2 - configuration error

Requires the NEXUS server to be running independently. This script never
starts, stops, or modifies the NEXUS in any way.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

BASE_URL = os.environ.get("JARVIS_NEXUS_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
EQUIPMENT_CODE = os.environ.get("JARVIS_NEXUS_EQUIPMENT_CODE", "DEFAULT")
TIMEOUT_S = 5.0

ENDPOINTS = [
    ("equipment", "/api/v1/equipment"),
    ("summary", f"/api/v1/equipment/{EQUIPMENT_CODE}/summary"),
    ("simulation", "/api/v1/simulation/status"),
]


def check(name: str, path: str) -> tuple[bool, str]:
    url = BASE_URL + path
    # S310: only http(s) URLs built from a scheme-validated BASE_URL reach urlopen.
    req = urllib.request.Request(url, method="GET")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # noqa: S310
            body = resp.read(65536)
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return False, f"{name}: HTTP {resp.status} but body is not JSON"
            keys = ",".join(sorted(payload.keys())) if isinstance(payload, dict) else "non-object"
            return True, f"{name}: HTTP {resp.status} OK (keys: {keys})"
    except urllib.error.HTTPError as exc:
        return False, f"{name}: HTTP {exc.code} {exc.reason}"
    except urllib.error.URLError as exc:
        return False, f"{name}: unreachable ({exc.reason})"
    except OSError as exc:
        return False, f"{name}: unreachable ({exc})"


def main() -> int:
    scheme = urlsplit(BASE_URL).scheme.lower()
    if scheme not in ("http", "https"):
        print(
            f"CONFIG ERROR: JARVIS_NEXUS_BASE_URL must use http/https, got {BASE_URL!r}",
            file=sys.stderr,
        )
        return 2
    print(f"smoke: NEXUS base_url={BASE_URL} equipment={EQUIPMENT_CODE}")
    failures = 0
    for name, path in ENDPOINTS:
        ok, detail = check(name, path)
        print(("  PASS " if ok else "  FAIL ") + detail)
        failures += 0 if ok else 1
    if failures:
        print(
            f"smoke: {failures}/{len(ENDPOINTS)} endpoints failed. "
            "If the NEXUS server is not running, this is expected: JARVIS "
            "answers 'NEXUS indisponivel' from its deterministic fallback "
            "and never fabricates data."
        )
        return 1
    print("smoke: all endpoints reachable")
    return 0


if __name__ == "__main__":
    sys.exit(main())
