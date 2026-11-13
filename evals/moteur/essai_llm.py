"""Essai du niveau LLM sur des instructions adoptées que la grammaire rejette (développement seulement).

Mesure les vrais jetons et le coût, et si les opérations produites s'exécutent et reproduisent le texte officiel.

Usage : uv run python -m evals.moteur.essai_llm --n 50
"""

import argparse
import random
from collections import Counter

from evals.moteur.llm_openai import TARIFS, AppelOpenAI
from evals.moteur.run import amendements_de, etapes_du_split, version_d_entree
from evals.moteur.score import changements, mots_officiels, sans_mentions
from moteur.analyse import analyser_amendement, analyser_portee
from moteur.application import appliquer
from moteur.resolution import CibleAmbigue, ErreurCible
from moteur.texte import article_par_id, cle_numero, trouver_article

GRAINE = 822


def candidats():
    """Amendements adoptés, hors sous-amendements, sur un article existant, dont une instruction est rejetée."""
    for etape in etapes_du_split("dev"):
        for a in amendements_de(etape):
            if a.sort != "Adopté" or a.parent_uid or not analyser_amendement(a).erreurs:
                continue
            try:
                portee = analyser_portee(a.article_vise)
            except ValueError:
                continue
            if portee.noeud == "article":
                yield etape, a, portee


def mots(lignes):
    return sans_mentions(mots_officiels(lignes))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--modele", default="gpt-6-luna", choices=sorted(TARIFS))
    parser.add_argument("--plafond", type=float, default=2.0)
    args = parser.parse_args()
    tous = list(candidats())
    random.Random(GRAINE).shuffle(tous)
    print(f"{len(tous)} amendements candidats sur le développement")
    appel = AppelOpenAI(args.modele, plafond=args.plafond)
    bilan, verbes, exemples = Counter(), Counter(), []
    vp = predits = reels = 0
    for etape, a, portee in tous[:args.n]:
        depart = version_d_entree(etape)
        article = trouver_article(depart, portee.article)
        lignes = [] if article is None else ["".join(j.texte for j in al.jetons) for al in article.alineas]
        analyse = analyser_amendement(a, "\n".join(lignes), appel)
        if analyse.erreurs or not analyse.operations:
            bilan["rejeté ou invalide"] += 1
            exemples.append((a.numero, "rejet", a.dispositif[:90]))
            continue
        bilan["opérations produites"] += 1
        verbes.update(op.verbe for op in analyse.operations)
        try:
            nouvelle, _ = appliquer(analyse.operations, portee, depart, depart, a.uid)
        except CibleAmbigue:
            bilan["cible ambiguë"] += 1
            continue
        except ErreurCible as erreur:
            bilan["cible introuvable"] += 1
            exemples.append((a.numero, str(erreur)[:70], a.dispositif[:90]))
            continue
        bilan["s'exécute"] += 1
        officiel = {cle_numero(x["numero"]): x for x in etape["sortie"]["articles"]}.get(cle_numero(portee.article))
        autres = [b for b in amendements_de(etape) if b.sort == "Adopté" and not b.parent_uid and b.uid != a.uid
                  and (b.article_vise or "").strip().upper() == (a.article_vise or "").strip().upper()]
        if officiel is None or autres:
            continue
        bilan["seul adopté sur son article"] += 1
        produit = article_par_id(nouvelle, article.id)
        apres = mots(["".join(j.texte for j in al.jetons) for al in produit.alineas])
        cible, avant = mots(officiel["lignes"]), mots(lignes)
        bilan["exact"] += apres == cible
        reel, predit = changements(avant, cible), changements(avant, apres)
        vp += sum((reel & predit).values())
        predits += sum(predit.values())
        reels += sum(reel.values())
    print("bilan :", dict(bilan))
    print("verbes :", dict(verbes))
    if predits and reels:
        print(f"mots changés, seul adopté : précision {100 * vp / predits:.1f} %, rappel {100 * vp / reels:.1f} %")
    print(f"appels {appel.appels}, en cache {appel.en_cache}, entrée {appel.entree}, dont cache {appel.entree_cache}, "
          f"sortie {appel.sortie}, dont réflexion {appel.reflexion}, sorties invalides {appel.invalides}")
    print(f"coût : {appel.cout:.3f} $")
    if appel.appels:
        print(f"moyenne par appel : entrée {appel.entree / appel.appels:.0f}, sortie {appel.sortie / appel.appels:.0f}, "
              f"coût {appel.cout / appel.appels:.5f} $")
    for exemple in exemples[:12]:
        print("  ", exemple)


if __name__ == "__main__":
    main()
