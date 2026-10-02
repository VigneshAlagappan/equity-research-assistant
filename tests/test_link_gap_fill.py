from types import SimpleNamespace

from research import link_gap_fill
from research.hypothesis_evaluator import EvidenceItem, HypothesisEvaluation
from research.link_gap_fill import gap_fill_investigation, parse_link_judgement, untested_presented_links


def hyp(steps=("a", "b", "c", "d")):
    return SimpleNamespace(hypothesis_id="h1", chain_steps=list(steps), statement="s", companies=["X"], investigation_id="inv")


def ev(verdict="SUPPORTED", support=(), contra=()):
    return HypothesisEvaluation(hypothesis_id="h1", verdict=verdict, confidence_basis="", supporting_evidence=list(support),
                                contradicting_evidence=list(contra))


def tagged(link):
    return EvidenceItem(kind="FACT", label="x", chain_step=link)


def test_untested_links_only_for_presented_hypotheses():
    assert untested_presented_links(hyp(), ev(support=[tagged(0)], contra=[tagged(2)])) == [1]
    assert untested_presented_links(hyp(), ev("REFUTED")) == []
    assert untested_presented_links(hyp(), ev("INSUFFICIENT_EVIDENCE")) == []
    assert untested_presented_links(hyp(["only one"]), ev()) == []
    assert untested_presented_links(hyp(), None) == []


def test_parse_link_judgement_validates_stance_items_and_tags():
    good = '{"stance": "supporting", "items": [{"kind": "FACT", "label": "L", "value": "v", "citation": "c"}]}'
    stance, items = parse_link_judgement(good, 2)
    assert stance == "supporting" and items[0].chain_step == 2 and items[0].source_tier == "RETRIEVED"
    assert parse_link_judgement('{"stance": "none", "items": []}', 0) == ("none", [])
    assert parse_link_judgement('{"stance": "supporting", "items": []}', 0) == ("none", [])  # no items -> none
    assert parse_link_judgement('{"stance": "maybe", "items": [{"kind": "FACT", "label": "L"}]}', 0) == ("none", [])
    assert parse_link_judgement('{"stance": "supporting", "items": [{"kind": "BOGUS", "label": "L"}]}', 0) == ("none", [])
    assert parse_link_judgement("not json", 0) == ("none", [])


def _run(monkeypatch, verdict_text, *, max_links=4, rendered="evidence", deadline_in=100.0, evaluation=None):
    calls = {"plan": [], "route": 0}
    monkeypatch.setattr(link_gap_fill, "plan_and_gather", lambda conn, h, q, **kw: calls["plan"].append(q) or "plan")
    monkeypatch.setattr(link_gap_fill, "_render_plan", lambda plan: rendered)
    monkeypatch.setattr(link_gap_fill.observability, "record", lambda *a, **k: None)

    def fake_route(**kw):
        calls["route"] += 1
        return SimpleNamespace(response=SimpleNamespace(text=verdict_text))

    monkeypatch.setattr(link_gap_fill, "route", fake_route)
    evaluation = evaluation or ev(support=[tagged(0)])
    investigation = SimpleNamespace(hypotheses=[hyp()], evaluations={"h1": evaluation})
    import time

    added = gap_fill_investigation(None, investigation, "q", capabilities=None, fact_store=None,
                                   deadline=time.monotonic() + deadline_in, max_links=max_links, model="m")
    return added, calls, evaluation


JUDGE = '{"stance": "contradicting", "items": [{"kind": "FACT", "label": "L", "value": "v", "citation": "c"}]}'


def test_fills_untested_links_within_the_cap_and_tags_items(monkeypatch):
    added, calls, evaluation = _run(monkeypatch, JUDGE, max_links=1)
    assert calls["plan"] == ["b c"] and calls["route"] == 1 and added == 1  # link 1 only (cap 1); link 0 already tested
    assert [i.chain_step for i in evaluation.contradicting_evidence] == [1]
    assert evaluation.contradicting_evidence[0].source_tier == "RETRIEVED"


def test_all_untested_links_when_cap_allows(monkeypatch):
    added, calls, _ = _run(monkeypatch, JUDGE, max_links=4)
    assert calls["plan"] == ["b c", "c d"] and added == 2


def test_nothing_retrieved_skips_the_model_call(monkeypatch):
    added, calls, _ = _run(monkeypatch, JUDGE, rendered="No evidence was retrieved for this hypothesis.")
    assert calls["route"] == 0 and added == 0


def test_none_stance_adds_nothing_and_deadline_stops_early(monkeypatch):
    added, calls, evaluation = _run(monkeypatch, '{"stance": "none", "items": []}')
    assert added == 0 and evaluation.contradicting_evidence == [] and calls["route"] == 2
    added, calls, _ = _run(monkeypatch, JUDGE, deadline_in=-1.0)
    assert calls["plan"] == [] and added == 0
