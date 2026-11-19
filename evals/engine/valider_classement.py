"""Le classement des écarts détecte-t-il les erreurs du moteur ? On en injecte et on compte (développement).

On part d'articles sans erreur moteur et on casse le résultat comme le ferait un moteur fautif :
- deplace : le passage écrit par l'amendement est au mauvais endroit (mauvaise occurrence, mauvais alinéa) ;
- tronque : seule la première moitié du passage est écrite ;
- remplace : le passage écrit n'a rien à voir avec celui de l'amendement.

Une erreur d'un seul mot dans un passage reste une « retouche » : ce classement ne peut pas la voir.

Usage : uv run python -m evals.engine.valider_classement V2-gram+llm
"""

import random
import sys
from collections import Counter

from evals.engine.run import etapes_du_split
from evals.engine.score import (MOTEUR, _passages, apparier, categorie_article, classer_ecarts, lire_resultats,
                                mots_officiels, sans_mentions)


def deplacer(words, flags, beginning, end, alea):
    passage, reste = words[beginning:end], words[:beginning] + words[end:]
    if len(reste) < 10:
        return None
    place = alea.randrange(len(reste))
    while abs(place - beginning) < 8:
        place = alea.randrange(len(reste))
    drapeaux_reste = flags[:beginning] + flags[end:]
    return reste[:place] + passage + reste[place:], drapeaux_reste[:place] + [1] * len(passage) + drapeaux_reste[place:]


def tronquer(words, flags, beginning, end, alea):
    milieu = (beginning + end) // 2
    return words[:milieu] + words[end:], flags[:milieu] + flags[end:]


def replace_with(words, flags, beginning, end, alea):
    autres = [f"intrus{i}" for i in range(end - beginning)]
    return words[:beginning] + autres + words[end:], flags


MUTATIONS = {"deplace": deplacer, "tronque": tronquer, "remplace": replace_with}


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "V2-gram+llm"
    _, resultats = lire_resultats(version, "dev")
    alea = random.Random(822)
    report = {name: Counter() for name in MUTATIONS}
    for step in etapes_du_split("dev"):
        r = resultats[step["id"]]
        pairs, _ = apparier(r["articles"], step["sortie"]["articles"])
        for p in r["articles"]:
            o = pairs.get(p["id"])
            if o is None:
                continue
            predicted, flags = sans_mentions(p["mots"], p["amendement"])
            official = sans_mentions(mots_officiels(o["lignes"]))
            if len(predicted) > 3000 or categorie_article(classer_ecarts(predicted, flags, official)) in MOTEUR:
                continue
            passages = sorted({v for v in _passages(flags).values() if v[1] - v[0] >= 4})
            if not passages:
                continue
            beginning, end = alea.choice(passages)
            size = "4-9 mots" if end - beginning < 10 else "10+ mots"
            for name, muter in MUTATIONS.items():
                casse = muter(predicted, flags, beginning, end, alea)
                if casse is None:
                    continue
                categorie = categorie_article(classer_ecarts(casse[0], casse[1], official))
                report[name][(size, "détecté" if categorie in MOTEUR else "manqué : " + categorie)] += 1
    for name, compte in report.items():
        for size in ("4-9 mots", "10+ mots"):
            total = sum(v for k, v in compte.items() if k[0] == size)
            detecte = compte[(size, "détecté")]
            manques = {k[1]: v for k, v in compte.items() if k[0] == size and k[1] != "détecté"}
            print(f"{name:9s} {size:9s} {detecte}/{total} = {100 * detecte / max(total, 1):.1f} %  {manques}")


if __name__ == "__main__":
    main()
