"""Integration tests: desktop layout regression (F3.7 visual bugfix).

The F3.7 shell shipped with all 8 views visible at once: the author rule
`.view { display: flex }` overrode the UA stylesheet's `[hidden] { display:
none }`, so every view rendered side-by-side in the flex row #main
(overlapping panels, cut labels, multiple scrollbars).

These tests lock the fix in two layers:
  1. Static CSS assertions on the real index.html — the exact bug class the
     stub-DOM harness cannot see (no layout engine under Node).
  2. Behavioral harness test — the JS view machine shows exactly one view
     at a time across a full navigation cycle.

No pixel assertions; structure and behavior only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tests.integration.test_f37_ux import _run_page

INDEX_HTML = (
    Path(__file__).resolve().parents[2] / "src" / "jarvis" / "api" / "static" / "index.html"
)

VIEWS = ["home", "sessions", "tasks", "memory", "activity", "audit", "nexus", "system"]


def _read_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _style_block(html: str) -> str:
    m = re.search(r"<style>([\s\S]*?)</style>", html)
    assert m, "index.html must contain an inline <style> block"
    return m.group(1)


def _script_block(html: str) -> str:
    m = re.search(r"<script>([\s\S]*?)</script>", html)
    assert m, "index.html must contain an inline <script> block"
    return m.group(1)


# -- the core regression: hidden views must not render ------------------------


def test_hidden_views_are_display_none() -> None:
    # The author stylesheet must restore the UA [hidden] behavior that
    # ".view { display: flex }" defeats. Without this rule all 8 views
    # render side-by-side (the reported overlap bug).
    css = _style_block(_read_html())
    assert re.search(r"\.view\[hidden\]\s*\{[^}]*display\s*:\s*none", css), (
        "missing CSS guard: .view[hidden] { display: none }"
    )


def test_exactly_one_view_visible_initially() -> None:
    html = _read_html()
    for v in VIEWS:
        tag = re.search(rf'<section[^>]*id="view-{v}"[^>]*>', html)
        assert tag, f"missing section #view-{v}"
        if v == "home":
            assert "hidden" not in tag.group(0), "view-home must start visible"
        else:
            assert "hidden" in tag.group(0), f"view-{v} must start hidden"


def test_view_machine_never_uses_style_display() -> None:
    # The hidden attribute is the single source of truth for view
    # visibility; ad-hoc style.display overrides would reintroduce
    # competing mechanisms.
    js = _script_block(_read_html())
    assert "style.display" not in js, "view switching must use the hidden attribute only"


# -- flex/grid scroll correctness ----------------------------------------------


def test_view_can_shrink_for_internal_scroll() -> None:
    # .view is a flex item in the row-flex #main; without min-height: 0 the
    # automatic minimum keeps it at content height and overflow-y never
    # engages correctly.
    css = _style_block(_read_html())
    m = re.search(r"\.view\s*\{([^}]*)\}", css)
    assert m, "missing .view rule"
    assert re.search(r"min-height\s*:\s*0", m.group(1)), ".view needs min-height: 0"


# -- responsive strategy --------------------------------------------------------


def test_responsive_breakpoints_present() -> None:
    # Desktop-first collapse stages: 3-col -> tighter 3-col -> 2-col
    # (syspanel hidden) -> icon rail -> single column with top nav strip.
    css = _style_block(_read_html())
    for bp in ("1500px", "1240px", "900px", "760px"):
        assert f"(max-width: {bp})" in css, f"missing {bp} breakpoint"


def test_syspanel_hidden_before_sidebar_collapses() -> None:
    # Visual Reset: the supplementary right panel was dissolved entirely —
    # its content lives in the System view. There is no #syspanel to hide.
    # The 1240px stage still yields gracefully instead: the statusline
    # telemetry dissolves and views keep usable width at 1024x768.
    html = _read_html()
    assert "syspanel" not in html
    css = _style_block(html)
    m1240 = re.search(r"@media\s*\(max-width:\s*1240px\)\s*\{([\s\S]*?)\n\}", css)
    assert m1240 and "#statusline" in m1240.group(1) and "display: none" in m1240.group(1)


# -- behavioral: one view at a time --------------------------------------------


def test_full_navigation_cycle_shows_one_view_at_a_time(tmp_path: Path) -> None:
    steps: list[dict[str, Any]] = [{"fire": [f"nav-{v}", "click"]} for v in VIEWS[1:] + ["home"]]
    result = _run_page(
        {"storedSession": "sess-1", "history": [], "memories": [], "steps": steps},
        tmp_path,
    )
    # Every navigation made exactly one view visible, in order.
    assert result["viewHistory"] == ["home"] + VIEWS[1:] + ["home"]
    assert result["views"] == ["home"]


def test_composer_and_sidebar_survive_view_changes(tmp_path: Path) -> None:
    # After visiting every view, home still sends messages (no state leaks).
    steps: list[dict[str, Any]] = [{"fire": [f"nav-{v}", "click"]} for v in VIEWS[1:]]
    steps += [
        {"fire": ["nav-home", "click"]},
        {"set": ["input", "olá"]},
        {"fire": ["composer", "submit"]},
    ]
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [],
            "memories": [],
            "postMessage": {"status": "ok", "message": "oi", "source": "fake", "task_id": "t-9"},
            "steps": steps,
        },
        tmp_path,
    )
    assert result["views"] == ["home"]
    assert result["viewHistory"][-1] == "home"
    kinds = [b["kind"] for b in result["bubbles"]]
    assert "user" in kinds and "assistant" in kinds


# -- Phase B: visual elevation, still honest ------------------------------------


def test_home_status_strip_reflects_real_boot_state(tmp_path: Path) -> None:
    # The Home system strip shows only real app state: backend health from
    # the /health check, the actual session id, the real turn count.
    result = _run_page({"storedSession": "sess-1", "history": [], "memories": []}, tmp_path)
    hs = result["homeStatus"]
    assert hs["backend"] == "online"
    assert hs["session"] == "sess-1"
    assert hs["turns"] == "0"
    assert hs["task"] == "—"


def test_home_status_strip_updates_after_turn(tmp_path: Path) -> None:
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [],
            "memories": [],
            "postMessage": {"status": "ok", "message": "oi", "source": "fake", "task_id": "t-9"},
            "steps": [{"set": ["input", "olá"]}, {"fire": ["composer", "submit"]}],
        },
        tmp_path,
    )
    hs = result["homeStatus"]
    assert hs["turns"] == "1"
    assert hs["task"] == "t-9 · ok"


def test_nexus_warning_status_maps_honestly(tmp_path: Path) -> None:
    # The backend ResponseStatus enum is surfaced verbatim — "warning"
    # becomes "atenção", never upgraded to "online" nor invented.
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [],
            "memories": [],
            "postMessage": {
                "status": "warning",
                "message": "O NEXUS está em atenção.",
                "source": "nexus",
                "task_id": "t-1",
                "fact_ids": ["FACT_NEXUS_AVAILABILITY"],
                "observed_at": "2026-09-30T15:00:00+00:00",
            },
            "steps": [{"fire": ["nexus-query", "click"]}],
        },
        tmp_path,
    )
    assert result["nexusStatus"] == "atenção"
    prov = result["nexusProv"]
    assert "status: warning" in prov
    assert "fonte: nexus" in prov
    assert "evidências: 1" in prov


def test_nexus_unavailable_maps_down(tmp_path: Path) -> None:
    result = _run_page(
        {
            "storedSession": "sess-1",
            "history": [],
            "memories": [],
            "postMessage": {
                "status": "unavailable",
                "message": "Não consigo consultar o NEXUS.",
                "source": "orchestrator",
                "task_id": "t-2",
            },
            "steps": [{"fire": ["nexus-query", "click"]}],
        },
        tmp_path,
    )
    assert result["nexusStatus"] == "indisponível"


def test_nexus_never_queried_shows_unknown(tmp_path: Path) -> None:
    # Before any real query the NEXUS view must not claim a state.
    result = _run_page({"storedSession": "sess-1", "history": [], "memories": []}, tmp_path)
    assert result["nexusStatus"] == "não consultado"
    assert result["nexusProv"] == ""
