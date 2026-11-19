"""Pilote du juge d'équivalence, sur le développement seulement.

lancer : tire 100 écarts (80 envoyés au modèle, 20 tranchés « effet différent » par les règles, pour contrôler les
règles), écrit la feuille d'annotation à l'aveugle, puis mesure le juge sur des erreurs injectées.
bilan : lit la feuille annotée et donne, par seuil, le taux de faux « même effet ».

Usage : uv run python -m evals.engine.judge_pilot lancer
        uv run python -m evals.engine.judge_pilot bilan
        uv run python -m evals.engine.judge_pilot phrases
"""

import argparse
import csv
import json
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from evals.engine.judge import (SENTENCE_GUIDELINES, CONTEXT, DecisionsJudge, SENTENCE_QUESTION, a_first, message, same_effect_probability, rule)
from evals.engine.run import RESULTS, split_steps
from evals.engine.score import (_passages, match_articles, classify_gaps, read_results, official_words, without_accents,
                                without_mentions)
from evals.engine.validate_classification import move, replace_with, truncate
from evals.engine.viewer.app import pieces

VERSION = "V2-gram+llm"
DIRECTORY = RESULTS / "juge_pilote"
SHEET = DIRECTORY / "feuille_annotation.csv"
KEY_FILE = DIRECTORY / "cle_et_reponses.jsonl"
INJECTIONS = DIRECTORY / "erreurs_injectees.json"
SENTENCES = DIRECTORY / "phrases_entieres.json"
MAX_SIZE = 600  # mots : au-delà, un écart est une réécriture entière, à examiner autrement
MAX_SENTENCE = 80  # mots de chaque côté
THRESHOLDS = (0.5, 0.7, 0.8, 0.9, 0.95, 0.99)
HUMAN_LABELS = {"forme", "meme", "renumerotation", "different", "indetermine"}
EQUIVALENT_VERDICTS = {"forme", "meme", "renumerotation"}  # une renumérotation ne change pas le fond


def text(words):
    return "".join(pieces(words)).strip()


def sentence_bounds(words, i1, i2):
    """Début et fin de la phrase autour de [i1, i2[ : un point après une lettre seule (« L. ») n'en termine pas une."""
    def cut(k):
        return words[k] in (";", ":", "¶") or (words[k] == "." and not (k > 0 and len(words[k - 1]) == 1))
    beginning = i1
    while beginning > max(0, i1 - MAX_SENTENCE) and not cut(beginning - 1):
        beginning -= 1
    end = i2
    while end < min(len(words), i2 + MAX_SENTENCE) and not cut(end):
        end += 1
    return beginning, min(end + 1, len(words))


def difference(identifier, predicted, official, i1, i2, j1, j2):
    """Un écart prêt pour le juge : le moteur est A ou B selon l'identifiant, le contexte vient du texte du moteur."""
    engine, reference = predicted[i1:i2], official[j1:j2]
    a, b = (engine, reference) if a_first(identifier) else (reference, engine)
    beginning, end = sentence_bounds(predicted, i1, i2)
    sentences = [text(predicted[beginning:i1] + side + predicted[i2:end]) for side in (a, b)]
    return {"id": identifier, "phrase_a": sentences[0], "phrase_b": sentences[1], "contexte_avant": text(predicted[max(0, i1 - CONTEXT):i1]), "passage_a": text(a),
            "passage_b": text(b), "contexte_apres": text(predicted[i2:i2 + CONTEXT]), "regle": rule(engine, reference),
            "taille": max(i2 - i1, j2 - j1)}


def message_of(d):
    return message(d["contexte_avant"], d["passage_a"], d["passage_b"], d["contexte_apres"])


def articles_of(version):
    _, results = read_results(version, "dev")
    for step in split_steps("dev"):
        r = results[step["id"]]
        pairs, _ = match_articles(r["articles"], step["sortie"]["articles"])
        for p in r["articles"]:
            o = pairs.get(p["id"])
            if o is not None:
                predicted, flags = without_mentions(p["mots"], p["amendement"])
                yield step["id"], p["id"], predicted, flags, without_mentions(official_words(o["lignes"]))


def draw_gaps(rng):
    """Au plus un écart par article, pour ne pas laisser quelques articles très modifiés peser sur l'échantillon."""
    to_model, by_rule, counted = [], [], Counter()
    for step_id, article_id, predicted, flags, official in articles_of(VERSION):
        candidates = {"modele": [], "regle": []}
        for label, i1, i2, j1, j2 in classify_gaps(predicted, flags, official):
            d = difference(f"{step_id}|{article_id}|{i1}", predicted, official, i1, i2, j1, j2)
            d["etiquette"] = label
            too_long = d["taille"] > MAX_SIZE
            counted["trop long" if too_long else d["regle"] or "modèle"] += 1
            if not too_long and d["regle"] != "forme_seule":
                candidates["regle" if d["regle"] else "modele"].append(d)
        if candidates["modele"]:
            to_model.append(rng.choice(candidates["modele"]))
        if candidates["regle"]:
            by_rule.append(rng.choice(candidates["regle"]))
    rng.shuffle(to_model)
    rng.shuffle(by_rule)
    return to_model[:80], by_rule[:20], counted


def mutate_word(words, flags, beginning, end, rng):
    """Un seul mot du passage remplacé par un autre mot du même article : l'erreur que le classement ne voit pas."""
    places = [i for i in range(beginning, end) if len(words[i]) >= 5]
    others = sorted({m for m in words if len(m) >= 5 and m.isalpha()})
    if not places or len(others) < 2:
        return None
    place = rng.choice(places)
    return words[:place] + [rng.choice([m for m in others if m != words[place]])] + words[place + 1:], flags


MUTATIONS = {"mot changé": mutate_word, "tronqué": truncate, "déplacé": move, "remplacé": replace_with}


def inject(rng, per_mutation=30):
    """On casse des articles exacts : chaque écart obtenu est donc une vraie erreur, connue d'avance."""
    bases = []
    for step_id, article_id, predicted, flags, official in articles_of(VERSION):
        if predicted == official and len(predicted) <= 3000:
            passages = sorted({v for v in _passages(flags).values() if v[1] - v[0] >= 4})
            if passages:
                bases.append((step_id, article_id, predicted, flags, official, passages))
    rng.shuffle(bases)
    cases = []
    for name, mutate in MUTATIONS.items():
        n = 0
        for step_id, article_id, predicted, flags, official, passages in bases:
            if n == per_mutation:
                break
            beginning, end = rng.choice(passages)
            broken = mutate(predicted, flags, beginning, end, rng)
            if broken is None:
                continue
            differences = [difference(f"{name}|{step_id}|{article_id}|{i1}", broken[0], official, i1, i2, j1, j2)
                           for _, i1, i2, j1, j2 in classify_gaps(broken[0], broken[1], official)]
            if differences:
                cases.append({"mutation": name, "taille": "4-9 mots" if end - beginning < 10 else "10+ mots", "differences": differences})
                n += 1
    return cases, len(bases)


def assess_all(judge, differences):
    with ThreadPoolExecutor(8) as pool:
        return list(pool.map(lambda d: judge(message_of(d)), differences))


def injection_report(cases):
    """Une erreur échappe au juge si tous ses écarts sont jugés « même effet » au-dessus du seuil."""
    lignes = {}
    for name in MUTATIONS:
        group = [c for c in cases if c["mutation"] == name]
        line = {"cas": len(group)}
        for threshold in THRESHOLDS:
            alone = sum(any(same_effect_probability(d["jugement"]) < threshold for d in c["differences"]) for c in group)
            line[f"modèle seul à {threshold}"] = alone
        line["avec règles à 0.9"] = sum(
            any(d["regle"] == "effet_different" or same_effect_probability(d["jugement"]) < 0.9 for d in c["differences"])
            for c in group)
        lignes[name] = line
    return lignes


def launch(_args):
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    rng = random.Random(822)
    to_model, by_rule, counted = draw_gaps(rng)
    print("écarts du développement :", dict(counted))
    print(f"tirés : {len(to_model)} pour le modèle, {len(by_rule)} tranchés par les règles")
    cases, base_count = inject(rng)
    print(f"erreurs injectées : {len(cases)}, à partir de {base_count} articles exacts")
    judge = DecisionsJudge()
    sample = to_model + by_rule
    for d, judgment in zip(sample, assess_all(judge, sample)):
        d["jugement"] = judgment
    to_judge = [d for c in cases for d in c["differences"] if d["regle"] != "forme_seule"]
    for d, judgment in zip(to_judge, assess_all(judge, to_judge)):
        d["jugement"] = judgment
    for c in cases:
        for d in c["differences"]:
            d.setdefault("jugement", {"choix": "forme_seule", "probabilites": {"forme_seule": 1.0}, "confiance": None})
    print(f"appels {judge.calls}, en cache {judge.cached}, refus {judge.refusals}, jetons {judge.input_}, coût {judge.cost:.3f} $")

    order = sorted(range(len(sample)), key=lambda i: rng.random())
    with SHEET.open("w", encoding="utf-8-sig", newline="") as f:  # -sig : Excel lit alors les accents
        output = csv.writer(f, delimiter=";")
        output.writerow(["numero", "contexte_avant", "passage_a", "passage_b", "contexte_apres", "annotation", "commentaire"])
        for numero, i in enumerate(order, 1):
            d = sample[i]
            d["numero"] = numero
            output.writerow([numero, d["contexte_avant"], d["passage_a"], d["passage_b"], d["contexte_apres"], "", ""])
    with KEY_FILE.open("w", encoding="utf-8") as f:
        for d in sorted(sample, key=lambda x: x["numero"]):
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    report = injection_report(cases)
    INJECTIONS.write_text(json.dumps({"bilan": report, "cas": cases}, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\nchoix du modèle sur l'échantillon, par origine :")
    for name, group in (("modèle", to_model), ("règles", by_rule)):
        print(" ", name, dict(Counter(d["jugement"]["choix"] for d in group)))
    print("probabilité de même effet :", dict(sorted(Counter(round(same_effect_probability(d["jugement"]), 1) for d in to_model).items())))
    print("\nerreurs repérées :")
    for name, line in report.items():
        print(" ", name, line)
    print(f"\nfeuille : {SHEET}")


def read_annotations():
    with SHEET.open(encoding="utf-8-sig", newline="") as f:
        return {int(l["numero"]): without_accents(l["annotation"].strip()) for l in csv.DictReader(f, delimiter=";")
                if l["annotation"].strip()}


def rate_by_threshold(lignes):
    """lignes : (probabilité de même effet, annotation humaine). Faux même effet = accepté, mais humainement différent."""
    result = {}
    slices = [(p, a) for p, a in lignes if a != "indetermine"]
    for threshold in THRESHOLDS:
        accepted = [a for p, a in slices if p >= threshold]
        wrong = sum(a == "different" for a in accepted)
        true_ones = sum(a in EQUIVALENT_VERDICTS for _, a in slices)
        result[threshold] = {"acceptés": len(accepted), "faux même effet": wrong,
                           "taux de faux %": round(100 * wrong / len(accepted), 1) if accepted else None,
                           "vrais même effet retrouvés": sum(a in EQUIVALENT_VERDICTS for a in accepted), "sur": true_ones}
    return result


def report(_args):
    annotations = read_annotations()
    unknowns = set(annotations.values()) - HUMAN_LABELS
    if unknowns:
        raise SystemExit(f"annotations inconnues : {unknowns}. Attendues : {sorted(HUMAN_LABELS)}")
    keys = [json.loads(l) for l in KEY_FILE.open(encoding="utf-8")]
    annotated = [(d, annotations[d["numero"]]) for d in keys if d["numero"] in annotations]
    print(f"{len(annotated)} annotations sur {len(keys)}")
    model = [(d, a) for d, a in annotated if not d["regle"]]
    rules = [(d, a) for d, a in annotated if d["regle"]]
    print("écarts jugés par le modèle, par seuil :")
    for threshold, line in rate_by_threshold([(same_effect_probability(d["jugement"]), a) for d, a in model]).items():
        print(f"  {threshold} : {line}")
    print("accord par choix du modèle :", dict(Counter((d["jugement"]["choix"], a) for d, a in model)))
    print("règles « effet différent » selon l'humain :", dict(Counter(a for _, a in rules)))


def sentence_message(d):
    return f"Phrase A :\n<<<{d['phrase_a']}>>>\n\nPhrase B :\n<<<{d['phrase_b']}>>>"


def auc(positives, negatives):
    return sum((a > b) + 0.5 * (a == b) for a in positives for b in negatives) / (len(positives) * len(negatives))


def auc_of(lignes, key):
    return auc([key(x) for x, equivalent in lignes if equivalent], [key(x) for x, equivalent in lignes if not equivalent])


def sentences(_args):
    """Mêmes tirages que « lancer » (même graine), jugés sur la phrase entière. La feuille annotée n'est pas touchée."""
    rng = random.Random(822)
    to_model, _, _ = draw_gaps(rng)
    cases, _ = inject(rng)
    keys = {d["id"]: d for d in map(json.loads, KEY_FILE.open(encoding="utf-8"))}
    if any(d["id"] not in keys for d in to_model):
        raise SystemExit("le tirage ne correspond plus à la feuille annotée")
    judge = DecisionsJudge(guidelines=SENTENCE_GUIDELINES, question=SENTENCE_QUESTION)
    def assess(differences):
        with ThreadPoolExecutor(8) as pool:
            return list(pool.map(lambda d: judge(sentence_message(d)), differences))
    for d, judgment in zip(to_model, assess(to_model)):
        d["jugement_phrase"] = judgment
    to_judge = [d for c in cases for d in c["differences"] if d["regle"] != "forme_seule"]
    for d, judgment in zip(to_judge, assess(to_judge)):
        d["jugement"] = judgment
    for c in cases:
        for d in c["differences"]:
            d.setdefault("jugement", {"choix": "forme_seule", "probabilites": {"forme_seule": 1.0}, "confiance": None})
    print(f"appels {judge.calls}, en cache {judge.cached}, refus {judge.refusals}, jetons {judge.input_}, coût {judge.cost:.3f} $")
    annotations = read_annotations()
    lignes = []
    for d in to_model:
        annotation = annotations.get(keys[d["id"]]["numero"])
        if annotation:
            d["annotation"], d["jugement_fragment"] = annotation, keys[d["id"]]["jugement"]
            lignes.append(d)
    SENTENCES.write_text(json.dumps({"ecarts": lignes, "injections": injection_report(cases)}, ensure_ascii=False, indent=1), encoding="utf-8")
    slices = [(d, d["annotation"] in EQUIVALENT_VERDICTS) for d in lignes if d["annotation"] != "indetermine"]
    new = lambda d: same_effect_probability(d["jugement_phrase"])
    old = lambda d: same_effect_probability(d["jugement_fragment"])
    size = lambda d: -d["taille"]
    print(f"{len(slices)} écarts, {sum(y for _, y in slices)} équivalents")
    print("choix :", dict(Counter((d["jugement_phrase"]["choix"], d["annotation"]) for d in lignes)))
    print("probabilité moyenne", round(sum(new(d) for d, _ in slices) / len(slices), 3))
    print("AUC phrase", round(auc_of(slices, new), 3), "fragment", round(auc_of(slices, old), 3), "taille", round(auc_of(slices, size), 3))
    rng2, gaps = random.Random(1), []
    for _ in range(2000):
        draw = [rng2.choice(slices) for _ in slices]
        if any(y for _, y in draw) and not all(y for _, y in draw):
            gaps.append((auc_of(draw, new), auc_of(draw, new) - auc_of(draw, size), auc_of(draw, new) - auc_of(draw, old)))
    for rank, name in enumerate(("AUC phrase", "phrase - taille", "phrase - fragment")):
        values = sorted(e[rank] for e in gaps)
        print(f"  {name} : IC95 {values[len(values) // 40]:.3f} à {values[-len(values) // 40]:.3f}")
    for threshold, line in rate_by_threshold([(new(d), d["annotation"]) for d in lignes]).items():
        print(f"  seuil {threshold} : {line}")
    print("par tranche de probabilité :")
    for low, high in ((0, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01)):
        group = [y for d, y in slices if low <= new(d) < high]
        print(f"  [{low}, {high}[ : {len(group)} écarts, {sum(group)} équivalents")
    print("erreurs injectées :")
    for name, line in injection_report(cases).items():
        print(" ", name, {k: v for k, v in line.items() if k in ("cas", "modèle seul à 0.5", "modèle seul à 0.7", "modèle seul à 0.9")})
    print("acceptés à 0.5 :")
    for d, _ in sorted(slices, key=lambda x: -new(x[0])):
        if new(d) >= 0.5:
            print(f"  {new(d):.2f} {d['annotation']:14s} {keys[d['id']]['numero']:>3} A=«{d['passage_a'][:50]}» B=«{d['passage_b'][:50]}»")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commande", choices=["lancer", "bilan", "phrases"])
    args = parser.parse_args()
    {"lancer": launch, "bilan": report, "phrases": sentences}[args.commande](args)


if __name__ == "__main__":
    main()
