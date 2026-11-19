"""Le classement des écarts détecte-t-il les erreurs du moteur ? On en injecte et on compte (développement).

On part d'articles sans erreur moteur et on casse le résultat comme le ferait un moteur fautif :
- deplace : le passage écrit par l'amendement est au mauvais endroit (mauvaise occurrence, mauvais alinéa) ;
- tronque : seule la première moitié du passage est écrite ;
- remplace : le passage écrit n'a rien à voir avec celui de l'amendement.

Une erreur d'un seul mot dans un passage reste une « retouche » : ce classement ne peut pas la voir.

Usage : uv run python -m evals.engine.validate_classification V2-gram+llm
"""

import random
import sys
from collections import Counter

from evals.engine.run import split_steps
from evals.engine.score import (ENGINE, _passages, match_articles, article_category, classify_gaps, read_results,
                                official_words, without_mentions)


def move(words, flags, beginning, end, rng):
    passage, remainder = words[beginning:end], words[:beginning] + words[end:]
    if len(remainder) < 10:
        return None
    place = rng.randrange(len(remainder))
    while abs(place - beginning) < 8:
        place = rng.randrange(len(remainder))
    remaining_flags = flags[:beginning] + flags[end:]
    return remainder[:place] + passage + remainder[place:], remaining_flags[:place] + [1] * len(passage) + remaining_flags[place:]


def truncate(words, flags, beginning, end, rng):
    middle = (beginning + end) // 2
    return words[:middle] + words[end:], flags[:middle] + flags[end:]


def replace_with(words, flags, beginning, end, rng):
    others = [f"intrus{i}" for i in range(end - beginning)]
    return words[:beginning] + others + words[end:], flags


MUTATIONS = {"deplace": move, "tronque": truncate, "remplace": replace_with}


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else "V2-gram+llm"
    _, results = read_results(version, "dev")
    rng = random.Random(822)
    report = {name: Counter() for name in MUTATIONS}
    for step in split_steps("dev"):
        r = results[step["id"]]
        pairs, _ = match_articles(r["articles"], step["sortie"]["articles"])
        for p in r["articles"]:
            o = pairs.get(p["id"])
            if o is None:
                continue
            predicted, flags = without_mentions(p["mots"], p["amendement"])
            official = without_mentions(official_words(o["lignes"]))
            if len(predicted) > 3000 or article_category(classify_gaps(predicted, flags, official)) in ENGINE:
                continue
            passages = sorted({v for v in _passages(flags).values() if v[1] - v[0] >= 4})
            if not passages:
                continue
            beginning, end = rng.choice(passages)
            size = "4-9 mots" if end - beginning < 10 else "10+ mots"
            for name, mutate in MUTATIONS.items():
                broken = mutate(predicted, flags, beginning, end, rng)
                if broken is None:
                    continue
                category = article_category(classify_gaps(broken[0], broken[1], official))
                report[name][(size, "détecté" if category in ENGINE else "manqué : " + category)] += 1
    for name, counted in report.items():
        for size in ("4-9 mots", "10+ mots"):
            total = sum(v for k, v in counted.items() if k[0] == size)
            detected = counted[(size, "détecté")]
            missing_items = {k[1]: v for k, v in counted.items() if k[0] == size and k[1] != "détecté"}
            print(f"{name:9s} {size:9s} {detected}/{total} = {100 * detected / max(total, 1):.1f} %  {missing_items}")


if __name__ == "__main__":
    main()
