"""Essai du niveau LLM sur des instructions adoptées que la grammaire rejette (développement seulement).

Mesure les vrais jetons et le coût, et si les opérations produites s'exécutent et reproduisent le texte officiel.

Usage : uv run python -m evals.engine.essai_llm --n 50
"""

import argparse
import random
from collections import Counter

from evals.engine.llm_openai import TARIFS, AppelOpenAI
from evals.engine.run import amendements_de, etapes_du_split, version_d_entree
from evals.engine.score import changes, mots_officiels, sans_mentions
from engine.analysis import analyze_amendment, analyze_scope
from engine.application import apply
from engine.resolution import AmbiguousTarget, TargetError
from engine.text import article_by_id, number_key, find_article

GRAINE = 822


def candidates():
    """Amendements adoptés, hors sous-amendements, sur un article existant, dont une instruction est rejetée."""
    for step in etapes_du_split("dev"):
        for a in amendements_de(step):
            if a.sort != "Adopté" or a.parent_uid or not analyze_amendment(a).errors:
                continue
            try:
                scope = analyze_scope(a.article_vise)
            except ValueError:
                continue
            if scope.node == "article":
                yield step, a, scope


def words(lignes):
    return sans_mentions(mots_officiels(lignes))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--modele", default="gpt-6-luna", choices=sorted(TARIFS))
    parser.add_argument("--plafond", type=float, default=2.0)
    args = parser.parse_args()
    tous = list(candidates())
    random.Random(GRAINE).shuffle(tous)
    print(f"{len(tous)} amendements candidats sur le développement")
    call = AppelOpenAI(args.model, plafond=args.plafond)
    report, verbs, exemples = Counter(), Counter(), []
    vp = predicted_items = reels = 0
    for step, a, scope in tous[:args.n]:
        initial = version_d_entree(step)
        article = find_article(initial, scope.article)
        lignes = [] if article is None else ["".join(j.text for j in al.tokens) for al in article.paragraphs]
        analysis = analyze_amendment(a, "\n".join(lignes), call)
        if analysis.errors or not analysis.operations:
            report["rejeté ou invalide"] += 1
            exemples.append((a.numero, "rejet", a.dispositif[:90]))
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
            exemples.append((a.numero, str(error)[:70], a.dispositif[:90]))
            continue
        report["s'exécute"] += 1
        official = {number_key(x["numero"]): x for x in step["sortie"]["articles"]}.get(number_key(scope.article))
        autres = [b for b in amendements_de(step) if b.sort == "Adopté" and not b.parent_uid and b.uid != a.uid
                  and (b.article_vise or "").strip().upper() == (a.article_vise or "").strip().upper()]
        if official is None or autres:
            continue
        report["seul adopté sur son article"] += 1
        produit = article_by_id(new_value, article.id)
        after = words(["".join(j.text for j in al.tokens) for al in produit.paragraphs])
        target, before = words(official["lignes"]), words(lignes)
        report["exact"] += after == target
        reel, predicted = changes(before, target), changes(before, after)
        vp += sum((reel & predicted).values())
        predicted_items += sum(predicted.values())
        reels += sum(reel.values())
    print("bilan :", dict(report))
    print("verbes :", dict(verbs))
    if predicted_items and reels:
        print(f"mots changés, seul adopté : précision {100 * vp / predicted_items:.1f} %, rappel {100 * vp / reels:.1f} %")
    print(f"appels {call.calls}, en cache {call.en_cache}, entrée {call.entree}, dont cache {call.entree_cache}, "
          f"sortie {call.output}, dont réflexion {call.reflexion}, sorties invalides {call.invalides}")
    print(f"coût : {call.cout:.3f} $")
    if call.calls:
        print(f"moyenne par appel : entrée {call.entree / call.calls:.0f}, sortie {call.output / call.calls:.0f}, "
              f"coût {call.cout / call.calls:.5f} $")
    for exemple in exemples[:12]:
        print("  ", exemple)


if __name__ == "__main__":
    main()
