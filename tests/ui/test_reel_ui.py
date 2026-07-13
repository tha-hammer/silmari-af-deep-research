"""B3 (Create Reel UI) + B5 (retire deep-link) — structural checks on index.html.

B3 is LEAF (frontend state machine; its behavioral contract is closed by the B2
dispatch + B6 poll API tests). B5 is a removal. With no JS test harness in this
repo, these assert the wiring is present/absent in the served page.
"""

from __future__ import annotations

from pathlib import Path

_INDEX = Path(__file__).resolve().parents[2] / "ui" / "index.html"


def _html() -> str:
    return _INDEX.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# B5 — the ${REELS_BASE}/create-from-research deep-link is gone
# --------------------------------------------------------------------------- #
def test_b5_create_from_research_deeplink_retired():
    assert "create-from-research" not in _html()


# --------------------------------------------------------------------------- #
# B3 — Create Reel button + selection + poll wiring
# --------------------------------------------------------------------------- #
def test_b3_create_reel_button_present():
    assert 'id="createReel"' in _html()


def test_b3_posts_create_reel_and_polls_reel_status():
    html = _html()
    assert "/api/create-reel" in html      # B2 dispatch
    assert "/api/reel-status" in html      # B6 poll


def test_b3_per_paragraph_selection_with_stable_ids():
    html = _html()
    assert "para-check" in html            # per-paragraph checkbox
    assert "data-para-id" in html          # stable {sectionIndex}-{paragraphIndex}
    assert "selectedParagraphs" in html    # assembled selection payload
