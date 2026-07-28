"""Shared fixtures: synthetic HTML + xhtml only. No book text."""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# All fixture text below is invented placeholder content. It intentionally
# does NOT come from Pact or its Russian translation.
# ---------------------------------------------------------------------------

EN_FIXTURE_HTML = """\
<html><body>
<h1>Chapter Fixture</h1>
<p>The sample opens with two dogs and a cat in the yard.</p>
<p>"Not today," she said. "Maybe tomorrow at three."</p>
<p>He walked <em>slowly</em> toward the <strong>gate</strong>.</p>
<p>They talked for hours about geometry and weather.</p>
<blockquote>Nothing is ever certain, though it should be.</blockquote>
</body></html>
"""

# 5 blocks in the EN fixture. RU fixture mirrors the block count so the
# structural alignment produces 1:1 pairs with high confidence.
RU_FIXTURE_XHTML = """\
<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<body>
<h1>Глава-заглушка</h1>
<p>Пример открывается двумя собаками и кошкой во дворе.</p>
<p>— Не сегодня, — сказала она. — Может быть, завтра в три.</p>
<p>Он медленно шёл к воротам.</p>
<p>Они часами обсуждали геометрию и погоду.</p>
<blockquote>Ничто никогда не бывает наверняка, хотя должно бы.</blockquote>
</body>
</html>
"""

RU_FIXTURE_UNEVEN = """\
<html><body>
<p>Пример открывается двумя собаками и кошкой во дворе.</p>
<p>Они часами обсуждали геометрию и погоду.</p>
</body></html>
"""


@pytest.fixture
def en_html() -> str:
    return EN_FIXTURE_HTML


@pytest.fixture
def ru_xhtml() -> str:
    return RU_FIXTURE_XHTML


@pytest.fixture
def ru_xhtml_uneven() -> str:
    return RU_FIXTURE_UNEVEN
