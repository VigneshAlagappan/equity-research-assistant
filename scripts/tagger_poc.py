"""Proof of concept: run the link tagger (research/link_tagger.py) over an
investigation already on file and print, per evidence item, the evaluator's own
tag next to the tagger's -- for a human spot-check of accuracy.

  python -m scripts.tagger_poc INVESTIGATION_ID [--out tagger_poc.json]

Read-only on the database; one cheap LLM call per hypothesis (Jev's model chain).
"""

from __future__ import annotations

import argparse
import json

import storage.backend_bootstrap

storage.backend_bootstrap.install()

from research.link_tagger import tag_links  # noqa: E402
from storage.backend_bootstrap import open_db  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("investigation_id")
    parser.add_argument("--out", default="tagger_poc.json")
    args = parser.parse_args()
    conn = open_db()
    cur = conn.cursor()
    cur.execute("SELECT hypothesis_id, statement, chain_steps FROM investigation_hypotheses WHERE investigation_id = %s ORDER BY generation_order", (args.investigation_id,))
    hypotheses = cur.fetchall()
    rows, models = [], set()
    tokens_in = tokens_out = 0
    for h in hypotheses:
        steps = json.loads(h["chain_steps"] or "[]")
        cur.execute(
            "SELECT id, stance, label, value, chain_step FROM investigation_hypothesis_evidence "
            "WHERE hypothesis_id = %s AND stance <> 'missing' ORDER BY id", (h["hypothesis_id"],))
        items = [dict(r) for r in cur.fetchall()]
        tags, result = tag_links(steps, items)
        if result is not None:
            models.add(result.response.model)
            tokens_in += result.response.input_tokens
            tokens_out += result.response.output_tokens
        for it, tag in zip(items, tags):
            rows.append({"hypothesis_id": h["hypothesis_id"], "steps": steps, "stance": it["stance"], "label": it["label"],
                         "value": (it["value"] or "")[:140], "evaluator_link": it["chain_step"], "tagger_link": tag})
    n = len(rows)
    ev = sum(1 for r in rows if r["evaluator_link"] is not None)
    tg = sum(1 for r in rows if r["tagger_link"] is not None)
    both = [r for r in rows if r["evaluator_link"] is not None and r["tagger_link"] is not None]
    agree = sum(1 for r in both if r["evaluator_link"] == r["tagger_link"])
    print(f"items={n} evaluator_tagged={ev} ({ev / n:.0%}) tagger_tagged={tg} ({tg / n:.0%}) "
          f"both_tagged={len(both)} agree={agree}" if n else "no evidence rows", flush=True)
    print("tagger models:", sorted(models), f"tokens in={tokens_in} out={tokens_out}", flush=True)
    with open(args.out, "w") as f:
        json.dump(rows, f, indent=1)
    print("wrote", args.out)


if __name__ == "__main__":
    main()
