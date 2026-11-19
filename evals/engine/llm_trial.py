"""Essai du niveau LLM sur des instructions adoptées que la grammaire rejette (développement seulement).

Mesure les vrais jetons et le coût, et si les opérations produites s'exécutent et reproduisent le texte officiel.

Usage : uv run python -m evals.engine.llm_trial --n 50
"""

import argparse
import random
from collections import Counter

from evals.engine.llm_openai import PRICES, OpenAICall
from evals.engine.run import amendements_of, split_steps, input_version
from evals.engine.score import changes, official_words, without_mentions
from engine.analysis import analyze_amendement, analyze_scope
from engine.application import apply
from engine.resolution import AmbiguousTarget, TargetError
from engine.text import article_by_id, number_key, find_article

SEED = 822


def candidates():
    """Amendements adoptés, hors sous-amendements, sur un article existant, dont une instruction est rejetée."""
    for step in split_steps("dev"):
        for a in amendements_of(step):
            if a.sort != "Adopté" or a.parent_uid or not analyze_amendement(a).errors:
                continue
            try:
                scope = analyze_scope(a.article_vise)
            except ValueError:
                continue
            if scope.node == "article":
                yield step, a, scope


def words(lignes):
    return without_mentions(official_words(lignes))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--modele", default="gpt-6-luna", choices=sorted(PRICES))
    parser.add_argument("--plafond", type=float, default=2.0)
    args = parser.parse_args()
    all_items = list(candidates())
    random.Random(SEED).shuffle(all_items)
    print(f"{len(all_items)} amendements candidats sur le développement")
    call = OpenAICall(args.model, budget=args.budget)
    report, verbs, examples = Counter(), Counter(), []
    vp = predicted_items = actual_items = 0
    for step, a, scope in all_items[:args.n]:
        initial = input_version(step)
        article = find_article(initial, scope.article)
        lignes = [] if article is None else ["".join(j.text for j in al.tokens) for al in article.alineas]
        analysis = analyze_amendement(a, "\n".join(lignes), call)
        if analysis.errors or not analysis.operations:
            report["rejeté ou invalide"] += 1
            examples.append((a.numero, "rejet", a.dispositif[:90]))
            continue
        report["opérations produites"] += 1
        verbs.update(op.verb for op in analysis.operations)
        try:
            new_value, _ = apply(analysis.operations, scope, initial, initial, a.uid)
        except AmbiguousTarget:
            report["cible ambiguë"] += 1
            continue
        except TargetError as error:
            report["cible introuvable"] += 1
            examples.append((a.numero, str(error)[:70], a.dispositif[:90]))
            continue
        report["s'exécute"] += 1
        official = {number_key(x["numero"]): x for x in step["sortie"]["articles"]}.get(number_key(scope.article))
        others = [b for b in amendements_of(step) if b.sort == "Adopté" and not b.parent_uid and b.uid != a.uid
                  and (b.article_vise or "").strip().upper() == (a.article_vise or "").strip().upper()]
        if official is None or others:
            continue
        report["seul adopté sur son article"] += 1
        product = article_by_id(new_value, article.id)
        after = words(["".join(j.text for j in al.tokens) for al in product.alineas])
        target, before = words(official["lignes"]), words(lignes)
        report["exact"] += after == target
        actual, predicted = changes(before, target), changes(before, after)
        vp += sum((actual & predicted).values())
        predicted_items += sum(predicted.values())
        actual_items += sum(actual.values())
    print("bilan :", dict(report))
    print("verbes :", dict(verbs))
    if predicted_items and actual_items:
        print(f"mots changés, seul adopté : précision {100 * vp / predicted_items:.1f} %, rappel {100 * vp / actual_items:.1f} %")
    print(f"appels {call.calls}, en cache {call.cached}, entrée {call.input_}, dont cache {call.cache_input}, "
          f"sortie {call.output}, dont réflexion {call.reasoning}, sorties invalides {call.invalid}")
    print(f"coût : {call.cost:.3f} $")
    if call.calls:
        print(f"moyenne par appel : entrée {call.input_ / call.calls:.0f}, sortie {call.output / call.calls:.0f}, "
              f"coût {call.cost / call.calls:.5f} $")
    for example in examples[:12]:
        print("  ", example)


if __name__ == "__main__":
    main()
