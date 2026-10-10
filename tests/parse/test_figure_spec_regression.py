# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Regression contract against the real reference dataset.

The reference file (4.2.1_传动系.md from the imported menus tree) is the agreed
figure-form standard: ``src`` keeps the authoring reference, ``vikingsrc``
carries the stored viking:// URI, and ``<figcaption>`` holds the description.
Authored captions are unreliable and discarded on ingest, so a pre-existing
body is stripped and refilled by the backfill stage. These tests pin the
read-side and backfill behavior to that reality.
"""

from pathlib import Path

from openviking.indexing.image_context import (
    _find_vikingsrc_nearby,
    _reference_matches,
    extract_occurrences,
)
from openviking.parse.figure_caption import _backfill_content

_REF_DIR = Path(__file__).resolve().parents[2] / "data" / "viking" / "e2etest" / "resources" / "raw" / "menus" / "807908103991924"
_REF_MD = _REF_DIR / "4_结构与功能" / "4.2_传动系" / "4.2.1_传动系.md"

source_uri = (
    "viking://resources/raw/menus/807908103991924/"
    "4_结构与功能/4.2_传动系/4.2.1_传动系.md"
)
image_uri = (
    "viking://resources/raw/menus/807908103991924/"
    "4_结构与功能/4.2_传动系/images/843884777710219.jpg"
)

pytestmark = pytestmark = __import__("pytest").mark.skipif(
    not _REF_MD.exists(), reason="reference dataset not present on this machine"
)


def _ref_content() -> str:
    return _REF_MD.read_text(encoding="utf-8")


class TestReferenceFigureForm:
    """The reference file is the normative figure form."""

    def test_reference_file_has_figure_with_vikingsrc(self):
        content = _ref_content()
        assert "<figure>" in content and "</figure>" in content
        assert 'vikingsrc="viking://resources/' in content

    def test_occurrence_found_via_vikingsrc_fallback(self):
        # The authoring src uses a non-viking scheme and never resolves; the
        # storage pointer attribute is what must drive the match.
        content = _ref_content()
        occurrences = extract_occurrences(content, source_uri, image_uri)
        assert len(occurrences) == 1
        assert occurrences[0]["context_uri"] == source_uri
        assert occurrences[0]["heading_path"]

    def test_vikingsrc_pointer_extraction(self):
        content = _ref_content()
        pointer = _find_vikingsrc_nearby("kb://images/843884777710219", content.splitlines(keepends=True), (20, 46))
        assert pointer == image_uri


class TestReferenceBackfill:
    def test_backfill_into_existing_reference_form(self):
        # The reference figcaption carries an authored caption, which is not
        # trusted. Once the ingest strip removes its body, the backfill stage
        # must fill the emptied block with the regenerated description.
        from openviking.parse.image_rewrite import strip_figcaption_bodies

        content = _ref_content()
        stripped = strip_figcaption_bodies(content)
        assert stripped != content
        new_content, filled = _backfill_content(stripped, image_uri, ["新的描述"])
        assert filled == 1
        assert "新的描述" in new_content
        # The authored text is gone for good.
        assert "轨道式挖掘机内部结构示意图" not in new_content

    def test_backfill_into_stripped_copy(self):
        # Strip the reference caption body (keeping structure) then verify a
        # backfill lands inside the same <figure> block with attrs intact.
        content = _ref_content()
        start = content.index("<figcaption>")
        end = content.index("</figcaption>") + len("</figcaption>")
        stripped = content[:start] + "<figcaption>\n        </figcaption>" + content[end:]

        new_content, filled = _backfill_content(stripped, image_uri, ["回填描述"])
        assert filled == 1
        block = new_content[new_content.index("<figure>"): new_content.index("</figure>") + len("</figure>")]
        assert 'src="kb://images/843884777710219"' in block
        assert 'vikingsrc="' + image_uri in block
        assert "回填描述" in block
