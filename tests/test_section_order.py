from llm_summary.llm import ViewItem, ViewSection
from llm_summary.renderer import order_sections


def _sec(sid, n=1):
    items = [ViewItem(kind="pr", number=i) for i in range(n)]
    return ViewSection(id=sid, title=sid, items=items)


def test_canonical_order():
    # Deliberately scrambled, plus an empty section and an unknown id.
    sections = [
        _sec("merged"),
        _sec("attention"),
        _sec("issues"),
        _sec("new"),
        _sec("empty", n=0),
        _sec("zzz-unknown"),
    ]
    ids = [s.id for s in order_sections(sections)]
    assert ids == ["attention", "merged", "new", "issues", "zzz-unknown"]


def test_empty_sections_dropped():
    assert order_sections([_sec("attention", n=0)]) == []


def test_merged_first_when_no_attention():
    ids = [s.id for s in order_sections([_sec("issues"), _sec("merged")])]
    assert ids == ["merged", "issues"]
