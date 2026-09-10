"""Browser-level interaction tests for a generated map, driven with Playwright
against real Cytoscape: search, filtering, the inspector, the timeline and Audit
mode are exercised through actual clicks, drags and key presses.

The page under test is a real `build_map.py` output with a local Cytoscape
bundle inlined, so the browser needs no network at all.

Run (see README "Tests"):
    python3 -m unittest discover -s tests -p "test_browser_ui.py" -v

Cytoscape bundle, in order:
    1. $CYTOSCAPE_JS — an official cytoscape.min.js you already have,
    2. tests/.cache/cytoscape.min.js — kept from an earlier run,
    3. one checksum-verified download of the pinned CDN URL into that cache.

Playwright is the only optional dependency here; when it (or its Chromium) is
missing these tests skip with an install hint, and the rest of the suite still
runs. Everything else is a hard failure.
"""
import hashlib
import importlib.util
import os
import tempfile
import unittest
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError as exc:                      # optional dependency
    sync_playwright = PlaywrightError = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_map.py"
SPEC = importlib.util.spec_from_file_location("build_map", SCRIPT)
build_map = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_map)

INSTALL_HINT = ("pip install -r requirements-test.txt && "
                "python3 -m playwright install chromium")
CACHE = ROOT / "tests" / ".cache" / "cytoscape.min.js"
# sha256 of the pinned CDN bundle (cytoscape 3.30.2) — downloads are verified,
# a bundle you point $CYTOSCAPE_JS at is not (it may be any official build).
BUNDLE_SHA256 = "83e8c54a6bec655bfd81df07df605649c268af69aeca67a5ea2da54ea42dac81"

AS_OF = "2026-06-15"
# Seven notes with known themes, types, tags, dates and links, so every visible
# count in the UI is predictable. Ages are measured against AS_OF.
VAULT = {
    "Work/Hub.md": (                                        # fresh, 1 day, deg 4
        "---\ncreated: 2025-01-10\nupdated: 2026-06-14\ntags: [index, core]\n---\n"
        "# Hub\n## Summary\nThe hub of this fixture vault.\n"),
    "Work/Field Log.md": (                                  # recent, tag-only search target
        "---\ncreated: 2025-02-11\nupdated: 2026-06-02\ntags: [quokka, fieldwork]\n---\n"
        "# Field Log\n## Summary\nNotes from the field.\n\n[[Hub]]\n"),
    "Work/Photosynthesis.md": (                             # settled, title search target
        "---\ncreated: 2025-03-12\nupdated: 2026-04-20\n---\n"
        "# Photosynthesis\n## Summary\nLight into sugar.\n\n[[Hub]]\n"),
    "Study/Gamma Rays.md": (                                # dormant, type idea
        "---\ncreated: 2025-04-13\nupdated: 2026-02-01\ntype: idea\n---\n"
        "# Gamma Rays\n## Summary\nHard radiation.\n\n[[Hub]]\n"),
    "Study/Old Lecture.md": (                               # oxidized, still linked
        "---\ncreated: 2024-05-14\nupdated: 2024-06-01\n---\n"
        "# Old Lecture\n## Summary\nAn old lecture.\n\n[[Hub]]\n"),
    "Life/Lone Journal.md": (                               # recent orphan
        "---\ncreated: 2026-05-05\n---\n"
        "# Lone Journal\n## Summary\nA page nothing links to.\n"),
    "Life/Rusty Recipe.md": (                               # oxidized AND orphan
        "---\ncreated: 2023-06-06\nupdated: 2023-07-01\ntype: pattern\n---\n"
        "# Rusty Recipe\n## Summary\nAn ancient recipe.\n"),
}
ALL_NOTES = ["Field Log", "Gamma Rays", "Hub", "Lone Journal", "Old Lecture",
             "Photosynthesis", "Rusty Recipe"]
WORK_NOTES = ["Field Log", "Hub", "Photosynthesis"]
FLAGGED = ["Lone Journal", "Old Lecture", "Rusty Recipe"]   # oxidized ∪ orphan
# months of `created`, in the order the timeline buckets them
MONTHS = ["2023-06", "2024-05", "2025-01", "2025-02", "2025-03", "2025-04", "2026-05"]

STATE = {}


def verified_cached_source(path):
    """Read a cache entry only after proving it is the pinned bundle."""
    blob = path.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    if digest != BUNDLE_SHA256:
        raise RuntimeError(
            f"cached Cytoscape bundle {path} has sha256 {digest}, expected "
            f"{BUNDLE_SHA256} — refusing to execute an altered cache")
    return build_map.read_local_runtime(str(path))


def cytoscape_source():
    """The JS of a real Cytoscape browser bundle, without installing anything."""
    given = (os.environ.get("CYTOSCAPE_JS") or "").strip()
    if given:
        return build_map.read_local_runtime(given)
    if CACHE.exists():
        return verified_cached_source(CACHE)
    try:
        with urllib.request.urlopen(build_map.CYTOSCAPE_CDN, timeout=60) as response:
            blob = response.read()
    except Exception as exc:                    # offline, proxied, blocked…
        raise RuntimeError(
            f"cannot fetch {build_map.CYTOSCAPE_CDN} ({exc}). Point CYTOSCAPE_JS at an "
            "official cytoscape.min.js you already have, or place one at "
            f"{CACHE.relative_to(ROOT)}, and re-run.") from exc
    digest = hashlib.sha256(blob).hexdigest()
    if digest != BUNDLE_SHA256:
        raise RuntimeError(f"{build_map.CYTOSCAPE_CDN} returned sha256 {digest}, expected "
                           f"{BUNDLE_SHA256} — refusing to test against an unexpected bundle")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_bytes(blob)
    return verified_cached_source(CACHE)


def build_fixture_map(folder):
    """Write a real generated map with Cytoscape inlined; returns its file:// URL."""
    vault = Path(folder, "vault")
    for rel, text in VAULT.items():
        target = Path(vault, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    payload = build_map.build(str(vault), "Fixture Brain", source_label="fixture", as_of=AS_OF)
    html = build_map.render(payload, runtime=cytoscape_source(),
                            runtime_path="cytoscape.min.js")
    out = Path(folder, "map.html")
    out.write_text(html, encoding="utf-8")
    return out.as_uri()


def setUpModule():
    if sync_playwright is None:
        raise unittest.SkipTest(
            f"Playwright is not installed ({IMPORT_ERROR}) — browser tests skipped. "
            f"Install it with: {INSTALL_HINT}")
    STATE["tmp"] = tempfile.TemporaryDirectory()
    STATE["url"] = build_fixture_map(STATE["tmp"].name)
    STATE["pw"] = sync_playwright().start()
    try:
        STATE["browser"] = STATE["pw"].chromium.launch()
    except PlaywrightError as exc:
        tearDownModule()
        if "playwright install" in str(exc) or "xecutable doesn't exist" in str(exc):
            raise unittest.SkipTest(
                "Playwright's Chromium is not installed — browser tests skipped. "
                "Install it with: python3 -m playwright install chromium") from exc
        raise


def tearDownModule():
    browser = STATE.pop("browser", None)
    if browser:
        browser.close()
    pw = STATE.pop("pw", None)
    if pw:
        pw.stop()
    tmp = STATE.pop("tmp", None)
    if tmp:
        tmp.cleanup()


class MapPageTest(unittest.TestCase):
    """Opens the generated map in a fresh page and fails on any console error."""

    def setUp(self):
        self.problems = []
        self.context = STATE["browser"].new_context(viewport={"width": 1280, "height": 860})
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda err: self.problems.append("pageerror: " + str(err)))
        self.page.on("console", self.record_console)
        self.addCleanup(self.context.close)
        self.addCleanup(self.assert_page_stayed_clean)
        self.page.goto(STATE["url"])
        # cy.ready() has run once the header stats are filled in
        self.page.wait_for_function(
            "() => document.getElementById('cShown').textContent.includes('/')")

    def record_console(self, message):
        if message.type == "error":
            self.problems.append("console: " + message.text)

    def assert_page_stayed_clean(self):
        self.assertEqual(self.problems, [], "the page reported errors")

    # ── reading the live graph ───────────────────────────────────────────────
    def visible_ids(self):
        return self.page.evaluate(
            "() => cy.nodes().filter(n => !n.hasClass('hidden')).map(n => n.id()).sort()")

    def classes_of(self, node_id):
        return self.page.evaluate("id => cy.$id(id).classes()", node_id)

    def text(self, selector):
        return self.page.locator(selector).inner_text()

    # ── acting on the page ───────────────────────────────────────────────────
    def click_node(self, node_id):
        """A real mouse click on the node at the map's fitted starting view."""
        point = self.page.evaluate(
            "id => { const p = cy.$id(id).renderedPosition(); return {x: p.x, y: p.y}; }", node_id)
        box = self.page.locator("#cy").bounding_box()
        self.page.mouse.click(box["x"] + point["x"], box["y"] + point["y"])
        self.page.wait_for_selector("#insp", state="visible")
        self.assertEqual(self.text("#iTitle"), node_id,
                         "the click landed on a different node")

    def filter_row(self, group, label):
        return self.page.locator("#" + group + " .filter").filter(
            has=self.page.get_by_text(label, exact=True))

    def toggle_filter(self, group, label):
        self.filter_row(group, label).click()

    def click_only(self, group, label):
        self.filter_row(group, label).locator("button.only").click()


class MapLoadsTests(MapPageTest):
    def test_real_cytoscape_renders_the_whole_graph(self):
        self.assertEqual(self.page.evaluate("() => typeof cytoscape"), "function")
        self.assertEqual(self.page.evaluate("() => cy.nodes().length"), 7)
        self.assertEqual(self.page.evaluate("() => cy.edges().length"), 4)
        self.assertEqual(self.visible_ids(), ALL_NOTES)
        self.assertEqual(self.text("#cShown"), "7 / 7")
        self.assertEqual(self.text("#cLinks"), "4 / 4")
        self.assertEqual(self.text("#rtLabel"), "Local runtime")

    def test_an_altered_cached_runtime_is_never_executed(self):
        with tempfile.TemporaryDirectory() as folder:
            poisoned = Path(folder, "cytoscape.min.js")
            poisoned.write_bytes(b"/* altered cache */ function cytoscape(){}")
            with self.assertRaisesRegex(RuntimeError, "refusing to execute an altered cache"):
                verified_cached_source(poisoned)


class ResponsiveAndMotionTests(MapPageTest):
    def test_narrow_viewport_keeps_controls_and_inspector_reachable(self):
        self.page.set_viewport_size({"width": 640, "height": 700})
        self.page.wait_for_timeout(50)
        self.assertTrue(self.page.locator("#dock .src").is_hidden())
        self.assertTrue(self.page.locator("#dock .stat.opt").first.is_hidden())
        self.page.evaluate("() => focus(cy.$id('Hub'))")
        for selector in ("#dock", "#panel", "#insp", "#tl"):
            box = self.page.locator(selector).bounding_box()
            with self.subTest(selector=selector):
                self.assertIsNotNone(box)
                self.assertGreaterEqual(box["x"], 0)
                self.assertGreaterEqual(box["y"], 0)
                self.assertLessEqual(box["x"] + box["width"], 640)
                self.assertLessEqual(box["y"] + box["height"], 700)
        self.page.locator("#inspClose").click()
        self.assertTrue(self.page.locator("#insp").is_hidden())

    def test_reduced_motion_media_query_collapses_css_motion(self):
        context = STATE["browser"].new_context(
            viewport={"width": 1280, "height": 860}, reduced_motion="reduce")
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(STATE["url"])
        page.wait_for_function("() => document.getElementById('cShown').textContent.includes('/')")
        page.evaluate("() => focus(cy.$id('Hub'))")
        result = page.evaluate("""() => ({
            media: matchMedia('(prefers-reduced-motion: reduce)').matches,
            inspector: getComputedStyle(document.getElementById('insp')).animationDuration,
            transition: getComputedStyle(document.querySelector('.minbtn svg')).transitionDuration
        })""")
        self.assertTrue(result["media"])
        self.assertIn(result["inspector"], ("0s", "1e-05s"))
        self.assertIn(result["transition"], ("0s", "1e-05s"))


class SearchTests(MapPageTest):
    def test_tag_only_match_is_highlighted_and_the_rest_recede(self):
        self.page.fill("#search", "quokka")
        self.assertEqual(self.text("#searchInfo"), "1 match — Esc clears")
        self.assertIn("search", self.classes_of("Field Log"))
        self.assertNotIn("sdim", self.classes_of("Field Log"))
        for other in ["Hub", "Photosynthesis", "Rusty Recipe"]:
            with self.subTest(node=other):
                self.assertIn("sdim", self.classes_of(other))
                self.assertNotIn("search", self.classes_of(other))

    def test_title_match_is_case_insensitive_and_partial(self):
        self.page.fill("#search", "PHOTO")
        self.assertEqual(self.text("#searchInfo"), "1 match — Esc clears")
        self.assertIn("search", self.classes_of("Photosynthesis"))

    def test_search_never_hides_nodes_it_only_dims_them(self):
        self.page.fill("#search", "quokka")
        self.assertEqual(self.visible_ids(), ALL_NOTES)
        self.assertEqual(self.text("#cShown"), "7 / 7")

    def test_a_miss_says_so_and_marks_nothing(self):
        self.page.fill("#search", "zzzznotanote")
        self.assertEqual(self.text("#searchInfo"), "No matches in the current view")
        self.assertEqual(self.page.evaluate("() => cy.nodes('.search').length"), 0)

    def test_escape_clears_the_search_and_its_marks(self):
        self.page.fill("#search", "quokka")
        self.page.locator("#search").press("Escape")
        self.page.wait_for_function("() => document.getElementById('searchInfo').hidden")
        self.assertEqual(self.page.input_value("#search"), "")
        self.assertEqual(self.page.evaluate("() => cy.nodes('.search, .sdim').length"), 0)

    def test_ctrl_k_focuses_the_search_box(self):
        self.page.locator("body").press("Control+k")
        self.assertEqual(self.page.evaluate("() => document.activeElement.id"), "search")

    def test_search_only_matches_within_the_current_filters(self):
        self.toggle_filter("themeFilters", "Work")
        self.page.fill("#search", "quokka")
        self.assertEqual(self.text("#searchInfo"), "No matches in the current view")
        self.assertNotIn("search", self.classes_of("Field Log"))


class FilterTests(MapPageTest):
    def test_toggling_a_theme_hides_its_notes_and_updates_the_counts(self):
        self.toggle_filter("themeFilters", "Study")
        self.assertEqual(self.visible_ids(),
                         [n for n in ALL_NOTES if n not in ("Gamma Rays", "Old Lecture")])
        self.assertEqual(self.text("#cShown"), "5 / 7")
        self.assertEqual(self.text("#ctThemes"), "2/3")
        self.assertEqual(self.filter_row("themeFilters", "Study").get_attribute("aria-checked"),
                         "false")

    def test_toggling_a_theme_back_on_restores_it(self):
        self.toggle_filter("themeFilters", "Study")
        self.toggle_filter("themeFilters", "Study")
        self.assertEqual(self.visible_ids(), ALL_NOTES)
        self.assertEqual(self.text("#ctThemes"), "3/3")

    def test_only_solos_a_theme_and_a_second_press_restores_the_group(self):
        self.click_only("themeFilters", "Work")
        self.assertEqual(self.visible_ids(), WORK_NOTES)
        self.assertEqual(self.text("#ctThemes"), "1/3")
        self.click_only("themeFilters", "Work")
        self.assertEqual(self.visible_ids(), ALL_NOTES)
        self.assertEqual(self.text("#ctThemes"), "3/3")

    def test_type_and_freshness_filters_hide_their_own_notes(self):
        self.page.locator("#grpTypes > summary").click()
        self.toggle_filter("typeFilters", "idea")
        self.assertNotIn("Gamma Rays", self.visible_ids())
        self.assertEqual(self.text("#ctTypes"), "3/4")
        self.toggle_filter("freshFilters", "Oxidized")
        self.assertEqual(self.visible_ids(), ["Field Log", "Hub", "Lone Journal", "Photosynthesis"])
        self.assertEqual(self.text("#ctFresh"), "4/5")

    def test_filters_from_different_groups_intersect(self):
        self.page.locator("#grpTypes > summary").click()
        self.toggle_filter("themeFilters", "Work")
        self.toggle_filter("typeFilters", "idea")
        self.assertEqual(self.visible_ids(), ["Lone Journal", "Old Lecture", "Rusty Recipe"])
        self.assertEqual(self.text("#cShown"), "3 / 7")

    def test_hidden_notes_take_their_links_with_them(self):
        self.click_only("themeFilters", "Life")     # both Life notes are orphans
        self.assertEqual(self.text("#cLinks"), "0 / 4")

    def test_the_links_toggle_hides_and_restores_every_edge(self):
        self.page.locator("#toggleEdges").click()
        self.assertEqual(self.page.locator("#toggleEdges").get_attribute("aria-pressed"), "false")
        self.assertEqual(self.text("#cLinks"), "0 / 4")
        self.assertEqual(self.page.evaluate("() => cy.edges('.hidden').length"), 4)
        self.page.locator("#toggleEdges").click()
        self.assertEqual(self.page.locator("#toggleEdges").get_attribute("aria-pressed"), "true")
        self.assertEqual(self.text("#cLinks"), "4 / 4")
        self.assertEqual(self.page.evaluate("() => cy.edges('.hidden').length"), 0)


class InspectorTests(MapPageTest):
    def facts(self):
        return self.page.evaluate("""() => {
            const kids = [...document.getElementById('iFacts').children];
            const out = {};
            for (let i = 0; i < kids.length; i += 2) out[kids[i].textContent] = kids[i + 1].textContent;
            return out;
        }""")

    def test_clicking_a_node_states_its_freshness_facts(self):
        self.click_node("Hub")
        self.assertEqual(self.page.locator("#iChips .chip").all_text_contents(),
                         ["Work", "index", "Fresh"])
        self.assertEqual(self.facts(), {
            "Freshness": "Fresh — updated within 7 days",
            "Age": "1 day",
            "Updated": "2026-06-14",
            "Source": "frontmatter updated",
            "Created": "2025-01-10",
            "Links": "4",
            "Status": "Connected",
            "File": "Work/Hub.md",
        })
        self.assertEqual(self.text("#iSummary"), "The hub of this fixture vault.")
        self.assertEqual(self.page.locator("#iTags .tag").all_text_contents(), ["index", "core"])
        width = self.page.locator("#iMeter").evaluate("el => el.style.width")
        self.assertGreaterEqual(float(width.rstrip("%")), 90.0)

    def test_the_selected_node_is_lit_and_the_rest_dimmed(self):
        self.click_node("Hub")
        self.assertIn("sel", self.classes_of("Hub"))
        self.assertIn("hl", self.classes_of("Old Lecture"))          # a neighbour
        self.assertNotIn("dim", self.classes_of("Old Lecture"))
        self.assertIn("dim", self.classes_of("Rusty Recipe"))        # unrelated

    def test_connected_notes_are_listed_and_navigable(self):
        self.click_node("Hub")
        self.assertEqual(self.text("#iLinksHead"), "CONNECTED (4)")
        self.assertEqual(sorted(self.page.locator("#iLinks a").all_text_contents()),
                         ["Field Log", "Gamma Rays", "Old Lecture", "Photosynthesis"])
        self.page.locator("#iLinks a", has_text="Old Lecture").click()
        self.page.wait_for_function(
            "() => document.getElementById('iTitle').textContent === 'Old Lecture'")
        self.assertEqual(self.facts()["Freshness"], "Oxidized — over a year old")
        self.assertIn("sel", self.classes_of("Old Lecture"))

    def test_an_orphan_is_named_as_one_and_lists_no_links(self):
        self.click_node("Rusty Recipe")
        self.assertIn("orphan", self.page.locator("#iChips .chip").all_text_contents())
        self.assertEqual(self.facts()["Status"], "Orphan — no resolved links")
        self.assertEqual(self.facts()["Links"], "0")
        self.assertTrue(self.page.locator("#iLinksSec").is_hidden())

    def test_a_note_dated_only_by_created_says_where_the_date_came_from(self):
        self.click_node("Lone Journal")
        self.assertEqual(self.facts()["Source"], "frontmatter created (no update field)")
        self.assertEqual(self.facts()["Created"], "2026-05-05")

    def test_closing_the_inspector_releases_the_graph(self):
        self.click_node("Hub")
        self.page.locator("#inspClose").click()
        self.assertTrue(self.page.locator("#insp").is_hidden())
        self.assertEqual(self.page.evaluate("() => cy.elements('.dim, .hl, .sel').length"), 0)

    def test_escape_closes_the_inspector(self):
        self.click_node("Hub")
        self.page.locator("body").press("Escape")
        self.assertTrue(self.page.locator("#insp").is_hidden())


class TimelineTests(MapPageTest):
    def playhead_x(self, cutoff):
        """Viewport x of a timeline fraction, using the chart's own padding."""
        box = self.page.locator("#tlsvg").bounding_box()
        inner = box["width"] - 28          # PAD.l + PAD.r in the template
        return box["x"] + 14 + cutoff * inner, box["y"] + box["height"] / 2

    def drag_to(self, cutoff):
        start_x, y = self.playhead_x(1.0)
        end_x, _ = self.playhead_x(cutoff)
        self.page.mouse.move(start_x, y)
        self.page.mouse.down()
        self.page.mouse.move(end_x, y, steps=8)
        self.page.mouse.up()

    def test_the_timeline_starts_at_the_last_month_with_every_note(self):
        self.assertEqual(self.text("#now"), MONTHS[-1])
        self.assertEqual(self.text("#tot"), "7 notes created")
        self.assertGreater(self.page.locator("#tlsvg path").count(), 0)

    def test_dragging_the_playhead_travels_back_in_time(self):
        self.drag_to(0.5)
        self.assertEqual(self.text("#now"), "2025-02")
        self.assertEqual(self.text("#tot"), "4 notes created")
        self.assertEqual(self.text("#cShown"), "4 / 7")
        self.assertEqual(self.visible_ids(),
                         ["Field Log", "Hub", "Old Lecture", "Rusty Recipe"])

    def test_dragging_back_to_the_end_restores_every_note(self):
        self.drag_to(0.2)
        self.assertNotEqual(self.text("#cShown"), "7 / 7")
        self.drag_to(1.0)
        self.assertEqual(self.text("#now"), MONTHS[-1])
        self.assertEqual(self.visible_ids(), ALL_NOTES)

    def test_hovering_a_month_shows_what_was_created_in_it(self):
        x, y = self.playhead_x(2 / 6)               # third of seven month ticks
        self.page.mouse.move(x, y)
        self.page.wait_for_selector("#tltip", state="visible")
        tip = self.text("#tltip")
        self.assertIn("Jan '25", tip)
        self.assertIn("Work", tip)

    def test_play_growth_replays_the_whole_span_and_resets_itself(self):
        self.page.locator("#play").click()
        self.assertEqual(self.page.locator("#play").get_attribute("aria-pressed"), "true")
        self.assertEqual(self.text("#playLab"), "Pause")
        # the playhead jumps back to the start of the span and sweeps forward
        self.page.wait_for_function(
            "() => document.getElementById('now').textContent !== '2026-05'")
        self.page.wait_for_function(
            "() => document.getElementById('play').getAttribute('aria-pressed') === 'false'",
            timeout=20000)
        self.assertEqual(self.text("#playLab"), "Play growth")
        self.assertEqual(self.text("#now"), MONTHS[-1])
        self.assertEqual(self.visible_ids(), ALL_NOTES)

    def test_play_can_be_paused_and_leaves_the_timeline_where_it_stopped(self):
        self.page.locator("#play").click()
        self.page.locator("#play").click()
        self.assertEqual(self.page.locator("#play").get_attribute("aria-pressed"), "false")
        self.assertEqual(self.text("#playLab"), "Play growth")
        paused_at = self.text("#now")
        self.page.wait_for_timeout(400)
        self.assertEqual(self.text("#now"), paused_at)


class AuditTests(MapPageTest):
    def test_the_badge_counts_flagged_notes_before_the_lens_is_on(self):
        self.assertEqual(self.text("#auditCt"), "3")
        self.assertTrue(self.page.locator("#auditNote").is_hidden())
        self.assertEqual(self.page.evaluate("() => cy.nodes('.flag').length"), 0)

    def test_audit_isolates_oxidized_and_orphan_notes(self):
        self.page.locator("#audit").click()
        self.assertEqual(self.page.locator("#audit").get_attribute("aria-pressed"), "true")
        self.assertEqual(self.visible_ids(), FLAGGED)
        self.assertEqual(self.text("#cShown"), "3 / 7")
        note = self.text("#auditNote")
        self.assertIn("3 flagged", note)
        self.assertIn("2 oxidized", note)
        self.assertIn("2 orphan", note)

    def test_the_lens_marks_why_each_note_is_flagged(self):
        self.page.locator("#audit").click()
        for oxidized in ("Old Lecture", "Rusty Recipe"):
            with self.subTest(node=oxidized):
                self.assertIn("flag", self.classes_of(oxidized))
                self.assertIn("oxid", self.classes_of(oxidized))
        self.assertIn("flag", self.classes_of("Lone Journal"))      # orphan, not oxidized
        self.assertNotIn("oxid", self.classes_of("Lone Journal"))

    def test_turning_audit_off_restores_the_whole_map(self):
        self.page.locator("#audit").click()
        self.page.locator("#audit").click()
        self.assertEqual(self.page.locator("#audit").get_attribute("aria-pressed"), "false")
        self.assertEqual(self.visible_ids(), ALL_NOTES)
        self.assertTrue(self.page.locator("#auditNote").is_hidden())
        self.assertEqual(self.page.evaluate("() => cy.nodes('.flag').length"), 0)

    def test_audit_re_enables_the_oxidized_band_it_needs(self):
        self.toggle_filter("freshFilters", "Oxidized")
        self.assertEqual(self.text("#ctFresh"), "4/5")
        self.page.locator("#audit").click()
        self.assertEqual(self.text("#ctFresh"), "5/5")
        self.assertEqual(self.filter_row("freshFilters", "Oxidized").get_attribute("aria-checked"),
                         "true")
        self.assertEqual(self.visible_ids(), FLAGGED)

    def test_other_filters_still_apply_under_the_lens(self):
        self.page.locator("#audit").click()
        self.toggle_filter("themeFilters", "Life")
        self.assertEqual(self.visible_ids(), ["Old Lecture"])
        self.assertEqual(self.text("#cShown"), "1 / 7")


if __name__ == "__main__":
    unittest.main()
