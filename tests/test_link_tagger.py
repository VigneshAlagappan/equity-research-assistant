from types import SimpleNamespace

from research import link_tagger
from research.link_tagger import parse_tags, tag_links


def test_parse_tags_validates_range_types_and_missing():
    text = '{"tags": [{"item": 0, "link": 1}, {"item": 1, "link": null}, {"item": 2, "link": 9}, {"item": 7, "link": 0}, {"item": 3, "link": true}]}'
    assert parse_tags(text, 4, 2) == [1, None, None, None]
    assert parse_tags("no json", 2, 2) == [None, None]
    assert parse_tags('{"tags": "bad"}', 2, 2) == [None, None]


def test_tag_links_skips_when_nothing_to_tag_and_survives_unavailability(monkeypatch):
    assert tag_links(["only one"], [{"label": "x"}]) == ([None], None)
    assert tag_links(["a", "b"], []) == ([], None)

    def boom(**kw):
        raise link_tagger.AllProvidersUnavailableError("down")

    monkeypatch.setattr(link_tagger, "route_explicit_chain", boom)
    assert tag_links(["a", "b"], [{"label": "x"}]) == ([None], None)


def test_tag_links_uses_the_chain_and_parses(monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return SimpleNamespace(response=SimpleNamespace(text='{"tags": [{"item": 0, "link": 0}, {"item": 1, "link": 1}]}'))

    monkeypatch.setattr(link_tagger, "route_explicit_chain", fake)
    tags, result = tag_links(["a", "b", "c"], [{"label": "x"}, {"label": "y", "value": "v"}], model_chain=["m1"])
    assert tags == [0, 1] and result is not None and seen["model_chain"] == ["m1"]
    assert "0: a" in seen["user_message"] and "links 0..1" in seen["user_message"]
