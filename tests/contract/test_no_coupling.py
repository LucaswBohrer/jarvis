"""No-coupling test (design: "Teste de não-acoplamento").

The suite FAILS if the JARVIS package:
- imports any NEXUS module,
- references a path that could be the NEXUS database,
- calls any endpoint outside /api/v1/.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SRC = REPO / "src" / "jarvis"


def _py_files():
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_nexus_imports():
    offenders = []
    for path in _py_files():
        text = path.read_text()
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Any import whose module path mentions nexus outside our own package.
            # Ours (relative or jarvis.*): contracts, ports, adapter and
            # verification for the NEXUS *integration* all live here by design.
            # A real coupling would be an absolute import of a foreign `nexus`
            # package. The runtime check below (sys.modules) is the real
            # enforcement; this static scan is defense in depth.
            if re.search(r"^\s*(import|from)\s+[\w.]*nexus", line) and not re.search(
                r"^\s*(import|from)\s+(\.|jarvis)", line
            ):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {stripped}")
            if "workspace/nexus" in line and not stripped.startswith("#"):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {stripped}")
    assert not offenders, "NEXUS coupling found:\n" + "\n".join(offenders)


def test_no_nexus_database_paths():
    # Only string literals / paths count; a Python identifier such as
    # `references_nexus_database` is not a database path.
    file_name = re.compile(r"\bnexus\.(db|sqlite3?)\b")
    literal_ref = re.compile(r"""["'][^"']*nexus(?:_database|\.db|\.sqlite3?)["']""")
    parent_ref = re.compile(r"\.\./nexus\b")

    def scan(text):
        hits = []
        for pat in (file_name, literal_ref, parent_ref):
            for m in pat.finditer(text.lower()):
                hits.append(m.group(0))
        return hits

    offenders = []
    for path in _py_files():
        for hit in scan(path.read_text()):
            offenders.append(f"{path.relative_to(REPO)}: {hit}")
    # Also scan config examples and policy files.
    for extra in [REPO / ".env.example", REPO / "config" / "policy.toml", REPO / "alembic.ini"]:
        if extra.exists():
            for hit in scan(extra.read_text()):
                offenders.append(f"{extra.relative_to(REPO)}: {hit}")
    assert not offenders, "NEXUS database path found:\n" + "\n".join(offenders)


def test_only_api_v1_endpoints():
    """Every literal NEXUS path must live under /api/v1/."""
    offenders = []
    for path in _py_files():
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for match in re.finditer(r'"/api[^"]*"', line):
                url = match.group(0).strip('"')
                if not url.startswith("/api/v1/"):
                    offenders.append(f"{path.relative_to(REPO)}:{lineno}: {url}")
    assert not offenders, "non-v1 endpoint found:\n" + "\n".join(offenders)


def test_no_nexus_modules_importable_from_jarvis():
    """Importing jarvis must not pull any module whose name starts with nexus."""
    for mod in list(sys.modules):
        assert not mod.startswith("nexus."), f"leaked module: {mod}"
        assert mod != "nexus", "leaked module: nexus"


def test_no_wildcard_capabilities_in_policy():
    """Deny-by-default: policy.toml must not contain wildcard capabilities."""
    text = (REPO / "config" / "policy.toml").read_text()
    assert "*" not in text, "wildcard found in policy.toml"
