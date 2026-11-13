"""Le classement des écarts détecte-t-il les erreurs du moteur ? On en injecte et on compte (développement).

On part d'articles sans erreur moteur et on casse le résultat comme le ferait un moteur fautif :
- deplace : le passage écrit par l'amendement est au mauvais endroit (mauvaise occurrence, mauvais alinéa) ;
- tronque : seule la première moitié du passage est écrite ;
- remplace : le passage écrit n'a rien à voir avec celui de l'amendement.

Une erreur d'un seul mot dans un passage reste une « retouche » : ce classement ne peut pas la voir.

Usage : uv run python -m evals.moteur.valider_classement V2-gram+llm
"""

import random
import sys
from collections import Counter

from evals.moteur.run import etapes_du_split
from evals.moteur.score import (MOTEUR, _passages, apparier, categorie_article, classer_ecarts, lire_resultats,
                                mots_officiels, sans_mentions)


def deplacer(mots, drapeaux, debut, fin, alea):
    passage, reste = mots[debut:fin], mots[:debut] + mots[fin:]
    if len(reste) < 10:
        return None
    place = alea.randrange(len(reste))
    while abs(place - debut) < 8:
        place = alea.randrange(len(reste))
    drapeaux_reste = drapeaux[:debut] + drapeaux[fin:]
    return reste[:place] + passage + reste[place:], drapeaux_reste[:place] + [1] * len(passage) + drapeaux_reste[place:]


def tronquer(mots, drapeaux, debut, fin, alea):
    milieu = (debut + fin) // 2
    return mots[:milieu] + mots[fin:], drapeaux[:milieu] + drapeaux[fin:]


def remplacer(mots, drapeaux, debut, fin, alea):
    autres = [f"intrus{i}" for i in range(fin - debut)]
    return mots[:debut] + autres + mots[fin:], drapeaux


MUTATIONS = {"deplace": deplacer, "tronque": tronquer, "remplace": remplacer}


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "V2-gram+llm"
    _, resultats = lire_resultats(version, "dev")
    alea = random.Random(822)
    bilan = {nom: Counter() for nom in MUTATIONS}
    for etape in etapes_du_split("dev"):
        r = resultats[etape["id"]]
        paires, _ = apparier(r["articles"], etape["sortie"]["articles"])
        for p in r["articles"]:
            o = paires.get(p["id"])
            if o is None:
                continue
            predit, drapeaux = sans_mentions(p["mots"], p["amendement"])
            officiel = sans_mentions(mots_officiels(o["lignes"]))
            if len(predit) > 3000 or categorie_article(classer_ecarts(predit, drapeaux, officiel)) in MOTEUR:
                continue
            passages = sorted({v for v in _passages(drapeaux).values() if v[1] - v[0] >= 4})
            if not passages:
                continue
            debut, fin = alea.choice(passages)
            taille = "4-9 mots" if fin - debut < 10 else "10+ mots"
            for nom, muter in MUTATIONS.items():
                casse = muter(predit, drapeaux, debut, fin, alea)
                if casse is None:
                    continue
                categorie = categorie_article(classer_ecarts(casse[0], casse[1], officiel))
                bilan[nom][(taille, "détecté" if categorie in MOTEUR else "manqué : " + categorie)] += 1
    for nom, compte in bilan.items():
        for taille in ("4-9 mots", "10+ mots"):
            total = sum(v for k, v in compte.items() if k[0] == taille)
            detecte = compte[(taille, "détecté")]
            manques = {k[1]: v for k, v in compte.items() if k[0] == taille and k[1] != "détecté"}
            print(f"{nom:9s} {taille:9s} {detecte}/{total} = {100 * detecte / max(total, 1):.1f} %  {manques}")


if __name__ == "__main__":
    main()
