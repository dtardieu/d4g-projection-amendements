"""Pilote du juge d'équivalence, sur le développement seulement.

lancer : tire 100 écarts (80 envoyés au modèle, 20 tranchés « effet différent » par les règles, pour contrôler les
règles), écrit la feuille d'annotation à l'aveugle, puis mesure le juge sur des erreurs injectées.
bilan : lit la feuille annotée et donne, par seuil, le taux de faux « même effet ».

Usage : uv run python -m evals.engine.pilote_juge lancer
        uv run python -m evals.engine.pilote_juge bilan
        uv run python -m evals.engine.pilote_juge phrases
"""

import argparse
import csv
import json
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from evals.engine.judge import (CONSIGNES_PHRASE, CONTEXT, JugeDecisions, QUESTION_PHRASE, a_d_abord, message, probabilite_meme_effet, regle)
from evals.engine.run import RESULTATS, etapes_du_split
from evals.engine.score import (_passages, apparier, classer_ecarts, lire_resultats, mots_officiels, sans_accents,
                                sans_mentions)
from evals.engine.valider_classement import deplacer, replace_with, tronquer
from evals.engine.visu.app import pieces

VERSION = "V2-gram+llm"
DOSSIER = RESULTATS / "juge_pilote"
FEUILLE = DOSSIER / "feuille_annotation.csv"
CLE = DOSSIER / "cle_et_reponses.jsonl"
INJECTIONS = DOSSIER / "erreurs_injectees.json"
PHRASES = DOSSIER / "phrases_entieres.json"
TAILLE_MAX = 600  # mots : au-delà, un écart est une réécriture entière, à examiner autrement
MAX_PHRASE = 80  # mots de chaque côté
SEUILS = (0.5, 0.7, 0.8, 0.9, 0.95, 0.99)
ETIQUETTES_HUMAINES = {"forme", "meme", "renumerotation", "different", "indetermine"}
EQUIVALENTES = {"forme", "meme", "renumerotation"}  # une renumérotation ne change pas le fond


def text(words):
    return "".join(pieces(words)).strip()


def bornes_phrase(words, i1, i2):
    """Début et fin de la phrase autour de [i1, i2[ : un point après une lettre seule (« L. ») n'en termine pas une."""
    def coupe(k):
        return words[k] in (";", ":", "¶") or (words[k] == "." and not (k > 0 and len(words[k - 1]) == 1))
    beginning = i1
    while beginning > max(0, i1 - MAX_PHRASE) and not coupe(beginning - 1):
        beginning -= 1
    end = i2
    while end < min(len(words), i2 + MAX_PHRASE) and not coupe(end):
        end += 1
    return beginning, min(end + 1, len(words))


def difference(identifiant, predicted, official, i1, i2, j1, j2):
    """Un écart prêt pour le juge : le moteur est A ou B selon l'identifiant, le contexte vient du texte du moteur."""
    engine, reference = predicted[i1:i2], official[j1:j2]
    a, b = (engine, reference) if a_d_abord(identifiant) else (reference, engine)
    beginning, end = bornes_phrase(predicted, i1, i2)
    sentences = [text(predicted[beginning:i1] + cote + predicted[i2:end]) for cote in (a, b)]
    return {"id": identifiant, "phrase_a": sentences[0], "phrase_b": sentences[1], "contexte_avant": text(predicted[max(0, i1 - CONTEXT):i1]), "passage_a": text(a),
            "passage_b": text(b), "contexte_apres": text(predicted[i2:i2 + CONTEXT]), "regle": regle(engine, reference),
            "taille": max(i2 - i1, j2 - j1)}


def message_de(d):
    return message(d["contexte_avant"], d["passage_a"], d["passage_b"], d["contexte_apres"])


def articles_de(version):
    _, resultats = lire_resultats(version, "dev")
    for step in etapes_du_split("dev"):
        r = resultats[step["id"]]
        pairs, _ = apparier(r["articles"], step["sortie"]["articles"])
        for p in r["articles"]:
            o = pairs.get(p["id"])
            if o is not None:
                predicted, flags = sans_mentions(p["mots"], p["amendement"])
                yield step["id"], p["id"], predicted, flags, sans_mentions(mots_officiels(o["lignes"]))


def tirer_ecarts(alea):
    """Au plus un écart par article, pour ne pas laisser quelques articles très modifiés peser sur l'échantillon."""
    au_modele, par_regle, compte = [], [], Counter()
    for etape_id, article_id, predicted, flags, official in articles_de(VERSION):
        candidates = {"modele": [], "regle": []}
        for etiquette, i1, i2, j1, j2 in classer_ecarts(predicted, flags, official):
            d = difference(f"{etape_id}|{article_id}|{i1}", predicted, official, i1, i2, j1, j2)
            d["etiquette"] = etiquette
            trop_long = d["taille"] > TAILLE_MAX
            compte["trop long" if trop_long else d["regle"] or "modèle"] += 1
            if not trop_long and d["regle"] != "forme_seule":
                candidates["regle" if d["regle"] else "modele"].append(d)
        if candidates["modele"]:
            au_modele.append(alea.choice(candidates["modele"]))
        if candidates["regle"]:
            par_regle.append(alea.choice(candidates["regle"]))
    alea.shuffle(au_modele)
    alea.shuffle(par_regle)
    return au_modele[:80], par_regle[:20], compte


def muter_mot(words, flags, beginning, end, alea):
    """Un seul mot du passage remplacé par un autre mot du même article : l'erreur que le classement ne voit pas."""
    places = [i for i in range(beginning, end) if len(words[i]) >= 5]
    autres = sorted({m for m in words if len(m) >= 5 and m.isalpha()})
    if not places or len(autres) < 2:
        return None
    place = alea.choice(places)
    return words[:place] + [alea.choice([m for m in autres if m != words[place]])] + words[place + 1:], flags


MUTATIONS = {"mot changé": muter_mot, "tronqué": tronquer, "déplacé": deplacer, "remplacé": replace_with}


def injecter(alea, par_mutation=30):
    """On casse des articles exacts : chaque écart obtenu est donc une vraie erreur, connue d'avance."""
    bases = []
    for etape_id, article_id, predicted, flags, official in articles_de(VERSION):
        if predicted == official and len(predicted) <= 3000:
            passages = sorted({v for v in _passages(flags).values() if v[1] - v[0] >= 4})
            if passages:
                bases.append((etape_id, article_id, predicted, flags, official, passages))
    alea.shuffle(bases)
    cas = []
    for name, muter in MUTATIONS.items():
        n = 0
        for etape_id, article_id, predicted, flags, official, passages in bases:
            if n == par_mutation:
                break
            beginning, end = alea.choice(passages)
            casse = muter(predicted, flags, beginning, end, alea)
            if casse is None:
                continue
            differences = [difference(f"{name}|{etape_id}|{article_id}|{i1}", casse[0], official, i1, i2, j1, j2)
                           for _, i1, i2, j1, j2 in classer_ecarts(casse[0], casse[1], official)]
            if differences:
                cas.append({"mutation": name, "taille": "4-9 mots" if end - beginning < 10 else "10+ mots", "differences": differences})
                n += 1
    return cas, len(bases)


def juger_tout(judge, differences):
    with ThreadPoolExecutor(8) as pool:
        return list(pool.map(lambda d: judge(message_de(d)), differences))


def bilan_injections(cas):
    """Une erreur échappe au juge si tous ses écarts sont jugés « même effet » au-dessus du seuil."""
    lignes = {}
    for name in MUTATIONS:
        groupe = [c for c in cas if c["mutation"] == name]
        line = {"cas": len(groupe)}
        for seuil in SEUILS:
            seul = sum(any(probabilite_meme_effet(d["jugement"]) < seuil for d in c["differences"]) for c in groupe)
            line[f"modèle seul à {seuil}"] = seul
        line["avec règles à 0.9"] = sum(
            any(d["regle"] == "effet_different" or probabilite_meme_effet(d["jugement"]) < 0.9 for d in c["differences"])
            for c in groupe)
        lignes[name] = line
    return lignes


def lancer(_args):
    DOSSIER.mkdir(parents=True, exist_ok=True)
    alea = random.Random(822)
    au_modele, par_regle, compte = tirer_ecarts(alea)
    print("écarts du développement :", dict(compte))
    print(f"tirés : {len(au_modele)} pour le modèle, {len(par_regle)} tranchés par les règles")
    cas, nb_bases = injecter(alea)
    print(f"erreurs injectées : {len(cas)}, à partir de {nb_bases} articles exacts")
    judge = JugeDecisions()
    echantillon = au_modele + par_regle
    for d, judgment in zip(echantillon, juger_tout(judge, echantillon)):
        d["jugement"] = judgment
    a_juger = [d for c in cas for d in c["differences"] if d["regle"] != "forme_seule"]
    for d, judgment in zip(a_juger, juger_tout(judge, a_juger)):
        d["jugement"] = judgment
    for c in cas:
        for d in c["differences"]:
            d.setdefault("jugement", {"choix": "forme_seule", "probabilites": {"forme_seule": 1.0}, "confiance": None})
    print(f"appels {judge.calls}, en cache {judge.en_cache}, refus {judge.refus}, jetons {judge.entree}, coût {judge.cout:.3f} $")

    order = sorted(range(len(echantillon)), key=lambda i: alea.random())
    with FEUILLE.open("w", encoding="utf-8-sig", newline="") as f:  # -sig : Excel lit alors les accents
        output = csv.writer(f, delimiter=";")
        output.writerow(["numero", "contexte_avant", "passage_a", "passage_b", "contexte_apres", "annotation", "commentaire"])
        for numero, i in enumerate(order, 1):
            d = echantillon[i]
            d["numero"] = numero
            output.writerow([numero, d["contexte_avant"], d["passage_a"], d["passage_b"], d["contexte_apres"], "", ""])
    with CLE.open("w", encoding="utf-8") as f:
        for d in sorted(echantillon, key=lambda x: x["numero"]):
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    report = bilan_injections(cas)
    INJECTIONS.write_text(json.dumps({"bilan": report, "cas": cas}, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\nchoix du modèle sur l'échantillon, par origine :")
    for name, groupe in (("modèle", au_modele), ("règles", par_regle)):
        print(" ", name, dict(Counter(d["jugement"]["choix"] for d in groupe)))
    print("probabilité de même effet :", dict(sorted(Counter(round(probabilite_meme_effet(d["jugement"]), 1) for d in au_modele).items())))
    print("\nerreurs repérées :")
    for name, line in report.items():
        print(" ", name, line)
    print(f"\nfeuille : {FEUILLE}")


def lire_annotations():
    with FEUILLE.open(encoding="utf-8-sig", newline="") as f:
        return {int(l["numero"]): sans_accents(l["annotation"].strip()) for l in csv.DictReader(f, delimiter=";")
                if l["annotation"].strip()}


def taux_par_seuil(lignes):
    """lignes : (probabilité de même effet, annotation humaine). Faux même effet = accepté, mais humainement différent."""
    result = {}
    tranches = [(p, a) for p, a in lignes if a != "indetermine"]
    for seuil in SEUILS:
        acceptes = [a for p, a in tranches if p >= seuil]
        faux = sum(a == "different" for a in acceptes)
        vrais = sum(a in EQUIVALENTES for _, a in tranches)
        result[seuil] = {"acceptés": len(acceptes), "faux même effet": faux,
                           "taux de faux %": round(100 * faux / len(acceptes), 1) if acceptes else None,
                           "vrais même effet retrouvés": sum(a in EQUIVALENTES for a in acceptes), "sur": vrais}
    return result


def report(_args):
    annotations = lire_annotations()
    inconnues = set(annotations.values()) - ETIQUETTES_HUMAINES
    if inconnues:
        raise SystemExit(f"annotations inconnues : {inconnues}. Attendues : {sorted(ETIQUETTES_HUMAINES)}")
    keys = [json.loads(l) for l in CLE.open(encoding="utf-8")]
    annotes = [(d, annotations[d["numero"]]) for d in keys if d["numero"] in annotations]
    print(f"{len(annotes)} annotations sur {len(keys)}")
    model = [(d, a) for d, a in annotes if not d["regle"]]
    regles = [(d, a) for d, a in annotes if d["regle"]]
    print("écarts jugés par le modèle, par seuil :")
    for seuil, line in taux_par_seuil([(probabilite_meme_effet(d["jugement"]), a) for d, a in model]).items():
        print(f"  {seuil} : {line}")
    print("accord par choix du modèle :", dict(Counter((d["jugement"]["choix"], a) for d, a in model)))
    print("règles « effet différent » selon l'humain :", dict(Counter(a for _, a in regles)))


def message_phrase(d):
    return f"Phrase A :\n<<<{d['phrase_a']}>>>\n\nPhrase B :\n<<<{d['phrase_b']}>>>"


def auc(positifs, negatifs):
    return sum((a > b) + 0.5 * (a == b) for a in positifs for b in negatifs) / (len(positifs) * len(negatifs))


def auc_de(lignes, key):
    return auc([key(x) for x, equivalent in lignes if equivalent], [key(x) for x, equivalent in lignes if not equivalent])


def sentences(_args):
    """Mêmes tirages que « lancer » (même graine), jugés sur la phrase entière. La feuille annotée n'est pas touchée."""
    alea = random.Random(822)
    au_modele, _, _ = tirer_ecarts(alea)
    cas, _ = injecter(alea)
    keys = {d["id"]: d for d in map(json.loads, CLE.open(encoding="utf-8"))}
    if any(d["id"] not in keys for d in au_modele):
        raise SystemExit("le tirage ne correspond plus à la feuille annotée")
    judge = JugeDecisions(guidelines=CONSIGNES_PHRASE, question=QUESTION_PHRASE)
    def juger(differences):
        with ThreadPoolExecutor(8) as pool:
            return list(pool.map(lambda d: judge(message_phrase(d)), differences))
    for d, judgment in zip(au_modele, juger(au_modele)):
        d["jugement_phrase"] = judgment
    a_juger = [d for c in cas for d in c["differences"] if d["regle"] != "forme_seule"]
    for d, judgment in zip(a_juger, juger(a_juger)):
        d["jugement"] = judgment
    for c in cas:
        for d in c["differences"]:
            d.setdefault("jugement", {"choix": "forme_seule", "probabilites": {"forme_seule": 1.0}, "confiance": None})
    print(f"appels {judge.calls}, en cache {judge.en_cache}, refus {judge.refus}, jetons {judge.entree}, coût {judge.cout:.3f} $")
    annotations = lire_annotations()
    lignes = []
    for d in au_modele:
        annotation = annotations.get(keys[d["id"]]["numero"])
        if annotation:
            d["annotation"], d["jugement_fragment"] = annotation, keys[d["id"]]["jugement"]
            lignes.append(d)
    PHRASES.write_text(json.dumps({"ecarts": lignes, "injections": bilan_injections(cas)}, ensure_ascii=False, indent=1), encoding="utf-8")
    tranches = [(d, d["annotation"] in EQUIVALENTES) for d in lignes if d["annotation"] != "indetermine"]
    new = lambda d: probabilite_meme_effet(d["jugement_phrase"])
    old = lambda d: probabilite_meme_effet(d["jugement_fragment"])
    size = lambda d: -d["taille"]
    print(f"{len(tranches)} écarts, {sum(y for _, y in tranches)} équivalents")
    print("choix :", dict(Counter((d["jugement_phrase"]["choix"], d["annotation"]) for d in lignes)))
    print("probabilité moyenne", round(sum(new(d) for d, _ in tranches) / len(tranches), 3))
    print("AUC phrase", round(auc_de(tranches, new), 3), "fragment", round(auc_de(tranches, old), 3), "taille", round(auc_de(tranches, size), 3))
    alea2, ecarts = random.Random(1), []
    for _ in range(2000):
        tirage = [alea2.choice(tranches) for _ in tranches]
        if any(y for _, y in tirage) and not all(y for _, y in tirage):
            ecarts.append((auc_de(tirage, new), auc_de(tirage, new) - auc_de(tirage, size), auc_de(tirage, new) - auc_de(tirage, old)))
    for rank, name in enumerate(("AUC phrase", "phrase - taille", "phrase - fragment")):
        valeurs = sorted(e[rank] for e in ecarts)
        print(f"  {name} : IC95 {valeurs[len(valeurs) // 40]:.3f} à {valeurs[-len(valeurs) // 40]:.3f}")
    for seuil, line in taux_par_seuil([(new(d), d["annotation"]) for d in lignes]).items():
        print(f"  seuil {seuil} : {line}")
    print("par tranche de probabilité :")
    for bas, haut in ((0, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01)):
        groupe = [y for d, y in tranches if bas <= new(d) < haut]
        print(f"  [{bas}, {haut}[ : {len(groupe)} écarts, {sum(groupe)} équivalents")
    print("erreurs injectées :")
    for name, line in bilan_injections(cas).items():
        print(" ", name, {k: v for k, v in line.items() if k in ("cas", "modèle seul à 0.5", "modèle seul à 0.7", "modèle seul à 0.9")})
    print("acceptés à 0.5 :")
    for d, _ in sorted(tranches, key=lambda x: -new(x[0])):
        if new(d) >= 0.5:
            print(f"  {new(d):.2f} {d['annotation']:14s} {keys[d['id']]['numero']:>3} A=«{d['passage_a'][:50]}» B=«{d['passage_b'][:50]}»")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commande", choices=["lancer", "bilan", "phrases"])
    args = parser.parse_args()
    {"lancer": lancer, "bilan": report, "phrases": sentences}[args.commande](args)


if __name__ == "__main__":
    main()
