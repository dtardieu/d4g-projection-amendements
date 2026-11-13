"""Pilote du juge d'équivalence, sur le développement seulement.

lancer : tire 100 écarts (80 envoyés au modèle, 20 tranchés « effet différent » par les règles, pour contrôler les
règles), écrit la feuille d'annotation à l'aveugle, puis mesure le juge sur des erreurs injectées.
bilan : lit la feuille annotée et donne, par seuil, le taux de faux « même effet ».

Usage : uv run python -m evals.moteur.pilote_juge lancer
        uv run python -m evals.moteur.pilote_juge bilan
        uv run python -m evals.moteur.pilote_juge phrases
"""

import argparse
import csv
import json
import random
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from evals.moteur.juge import (CONSIGNES_PHRASE, CONTEXTE, JugeDecisions, QUESTION_PHRASE, a_d_abord, message, probabilite_meme_effet, regle)
from evals.moteur.run import RESULTATS, etapes_du_split
from evals.moteur.score import (_passages, apparier, classer_ecarts, lire_resultats, mots_officiels, sans_accents,
                                sans_mentions)
from evals.moteur.valider_classement import deplacer, remplacer, tronquer
from evals.moteur.visu.app import morceaux

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


def texte(mots):
    return "".join(morceaux(mots)).strip()


def bornes_phrase(mots, i1, i2):
    """Début et fin de la phrase autour de [i1, i2[ : un point après une lettre seule (« L. ») n'en termine pas une."""
    def coupe(k):
        return mots[k] in (";", ":", "¶") or (mots[k] == "." and not (k > 0 and len(mots[k - 1]) == 1))
    debut = i1
    while debut > max(0, i1 - MAX_PHRASE) and not coupe(debut - 1):
        debut -= 1
    fin = i2
    while fin < min(len(mots), i2 + MAX_PHRASE) and not coupe(fin):
        fin += 1
    return debut, min(fin + 1, len(mots))


def difference(identifiant, predit, officiel, i1, i2, j1, j2):
    """Un écart prêt pour le juge : le moteur est A ou B selon l'identifiant, le contexte vient du texte du moteur."""
    moteur, reference = predit[i1:i2], officiel[j1:j2]
    a, b = (moteur, reference) if a_d_abord(identifiant) else (reference, moteur)
    debut, fin = bornes_phrase(predit, i1, i2)
    phrases = [texte(predit[debut:i1] + cote + predit[i2:fin]) for cote in (a, b)]
    return {"id": identifiant, "phrase_a": phrases[0], "phrase_b": phrases[1], "contexte_avant": texte(predit[max(0, i1 - CONTEXTE):i1]), "passage_a": texte(a),
            "passage_b": texte(b), "contexte_apres": texte(predit[i2:i2 + CONTEXTE]), "regle": regle(moteur, reference),
            "taille": max(i2 - i1, j2 - j1)}


def message_de(d):
    return message(d["contexte_avant"], d["passage_a"], d["passage_b"], d["contexte_apres"])


def articles_de(version):
    _, resultats = lire_resultats(version, "dev")
    for etape in etapes_du_split("dev"):
        r = resultats[etape["id"]]
        paires, _ = apparier(r["articles"], etape["sortie"]["articles"])
        for p in r["articles"]:
            o = paires.get(p["id"])
            if o is not None:
                predit, drapeaux = sans_mentions(p["mots"], p["amendement"])
                yield etape["id"], p["id"], predit, drapeaux, sans_mentions(mots_officiels(o["lignes"]))


def tirer_ecarts(alea):
    """Au plus un écart par article, pour ne pas laisser quelques articles très modifiés peser sur l'échantillon."""
    au_modele, par_regle, compte = [], [], Counter()
    for etape_id, article_id, predit, drapeaux, officiel in articles_de(VERSION):
        candidats = {"modele": [], "regle": []}
        for etiquette, i1, i2, j1, j2 in classer_ecarts(predit, drapeaux, officiel):
            d = difference(f"{etape_id}|{article_id}|{i1}", predit, officiel, i1, i2, j1, j2)
            d["etiquette"] = etiquette
            trop_long = d["taille"] > TAILLE_MAX
            compte["trop long" if trop_long else d["regle"] or "modèle"] += 1
            if not trop_long and d["regle"] != "forme_seule":
                candidats["regle" if d["regle"] else "modele"].append(d)
        if candidats["modele"]:
            au_modele.append(alea.choice(candidats["modele"]))
        if candidats["regle"]:
            par_regle.append(alea.choice(candidats["regle"]))
    alea.shuffle(au_modele)
    alea.shuffle(par_regle)
    return au_modele[:80], par_regle[:20], compte


def muter_mot(mots, drapeaux, debut, fin, alea):
    """Un seul mot du passage remplacé par un autre mot du même article : l'erreur que le classement ne voit pas."""
    places = [i for i in range(debut, fin) if len(mots[i]) >= 5]
    autres = sorted({m for m in mots if len(m) >= 5 and m.isalpha()})
    if not places or len(autres) < 2:
        return None
    place = alea.choice(places)
    return mots[:place] + [alea.choice([m for m in autres if m != mots[place]])] + mots[place + 1:], drapeaux


MUTATIONS = {"mot changé": muter_mot, "tronqué": tronquer, "déplacé": deplacer, "remplacé": remplacer}


def injecter(alea, par_mutation=30):
    """On casse des articles exacts : chaque écart obtenu est donc une vraie erreur, connue d'avance."""
    bases = []
    for etape_id, article_id, predit, drapeaux, officiel in articles_de(VERSION):
        if predit == officiel and len(predit) <= 3000:
            passages = sorted({v for v in _passages(drapeaux).values() if v[1] - v[0] >= 4})
            if passages:
                bases.append((etape_id, article_id, predit, drapeaux, officiel, passages))
    alea.shuffle(bases)
    cas = []
    for nom, muter in MUTATIONS.items():
        n = 0
        for etape_id, article_id, predit, drapeaux, officiel, passages in bases:
            if n == par_mutation:
                break
            debut, fin = alea.choice(passages)
            casse = muter(predit, drapeaux, debut, fin, alea)
            if casse is None:
                continue
            differences = [difference(f"{nom}|{etape_id}|{article_id}|{i1}", casse[0], officiel, i1, i2, j1, j2)
                           for _, i1, i2, j1, j2 in classer_ecarts(casse[0], casse[1], officiel)]
            if differences:
                cas.append({"mutation": nom, "taille": "4-9 mots" if fin - debut < 10 else "10+ mots", "differences": differences})
                n += 1
    return cas, len(bases)


def juger_tout(juge, differences):
    with ThreadPoolExecutor(8) as pool:
        return list(pool.map(lambda d: juge(message_de(d)), differences))


def bilan_injections(cas):
    """Une erreur échappe au juge si tous ses écarts sont jugés « même effet » au-dessus du seuil."""
    lignes = {}
    for nom in MUTATIONS:
        groupe = [c for c in cas if c["mutation"] == nom]
        ligne = {"cas": len(groupe)}
        for seuil in SEUILS:
            seul = sum(any(probabilite_meme_effet(d["jugement"]) < seuil for d in c["differences"]) for c in groupe)
            ligne[f"modèle seul à {seuil}"] = seul
        ligne["avec règles à 0.9"] = sum(
            any(d["regle"] == "effet_different" or probabilite_meme_effet(d["jugement"]) < 0.9 for d in c["differences"])
            for c in groupe)
        lignes[nom] = ligne
    return lignes


def lancer(_args):
    DOSSIER.mkdir(parents=True, exist_ok=True)
    alea = random.Random(822)
    au_modele, par_regle, compte = tirer_ecarts(alea)
    print("écarts du développement :", dict(compte))
    print(f"tirés : {len(au_modele)} pour le modèle, {len(par_regle)} tranchés par les règles")
    cas, nb_bases = injecter(alea)
    print(f"erreurs injectées : {len(cas)}, à partir de {nb_bases} articles exacts")
    juge = JugeDecisions()
    echantillon = au_modele + par_regle
    for d, jugement in zip(echantillon, juger_tout(juge, echantillon)):
        d["jugement"] = jugement
    a_juger = [d for c in cas for d in c["differences"] if d["regle"] != "forme_seule"]
    for d, jugement in zip(a_juger, juger_tout(juge, a_juger)):
        d["jugement"] = jugement
    for c in cas:
        for d in c["differences"]:
            d.setdefault("jugement", {"choix": "forme_seule", "probabilites": {"forme_seule": 1.0}, "confiance": None})
    print(f"appels {juge.appels}, en cache {juge.en_cache}, refus {juge.refus}, jetons {juge.entree}, coût {juge.cout:.3f} $")

    ordre = sorted(range(len(echantillon)), key=lambda i: alea.random())
    with FEUILLE.open("w", encoding="utf-8-sig", newline="") as f:  # -sig : Excel lit alors les accents
        sortie = csv.writer(f, delimiter=";")
        sortie.writerow(["numero", "contexte_avant", "passage_a", "passage_b", "contexte_apres", "annotation", "commentaire"])
        for numero, i in enumerate(ordre, 1):
            d = echantillon[i]
            d["numero"] = numero
            sortie.writerow([numero, d["contexte_avant"], d["passage_a"], d["passage_b"], d["contexte_apres"], "", ""])
    with CLE.open("w", encoding="utf-8") as f:
        for d in sorted(echantillon, key=lambda x: x["numero"]):
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    bilan = bilan_injections(cas)
    INJECTIONS.write_text(json.dumps({"bilan": bilan, "cas": cas}, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\nchoix du modèle sur l'échantillon, par origine :")
    for nom, groupe in (("modèle", au_modele), ("règles", par_regle)):
        print(" ", nom, dict(Counter(d["jugement"]["choix"] for d in groupe)))
    print("probabilité de même effet :", dict(sorted(Counter(round(probabilite_meme_effet(d["jugement"]), 1) for d in au_modele).items())))
    print("\nerreurs repérées :")
    for nom, ligne in bilan.items():
        print(" ", nom, ligne)
    print(f"\nfeuille : {FEUILLE}")


def lire_annotations():
    with FEUILLE.open(encoding="utf-8-sig", newline="") as f:
        return {int(l["numero"]): sans_accents(l["annotation"].strip()) for l in csv.DictReader(f, delimiter=";")
                if l["annotation"].strip()}


def taux_par_seuil(lignes):
    """lignes : (probabilité de même effet, annotation humaine). Faux même effet = accepté, mais humainement différent."""
    resultat = {}
    tranches = [(p, a) for p, a in lignes if a != "indetermine"]
    for seuil in SEUILS:
        acceptes = [a for p, a in tranches if p >= seuil]
        faux = sum(a == "different" for a in acceptes)
        vrais = sum(a in EQUIVALENTES for _, a in tranches)
        resultat[seuil] = {"acceptés": len(acceptes), "faux même effet": faux,
                           "taux de faux %": round(100 * faux / len(acceptes), 1) if acceptes else None,
                           "vrais même effet retrouvés": sum(a in EQUIVALENTES for a in acceptes), "sur": vrais}
    return resultat


def bilan(_args):
    annotations = lire_annotations()
    inconnues = set(annotations.values()) - ETIQUETTES_HUMAINES
    if inconnues:
        raise SystemExit(f"annotations inconnues : {inconnues}. Attendues : {sorted(ETIQUETTES_HUMAINES)}")
    cles = [json.loads(l) for l in CLE.open(encoding="utf-8")]
    annotes = [(d, annotations[d["numero"]]) for d in cles if d["numero"] in annotations]
    print(f"{len(annotes)} annotations sur {len(cles)}")
    modele = [(d, a) for d, a in annotes if not d["regle"]]
    regles = [(d, a) for d, a in annotes if d["regle"]]
    print("écarts jugés par le modèle, par seuil :")
    for seuil, ligne in taux_par_seuil([(probabilite_meme_effet(d["jugement"]), a) for d, a in modele]).items():
        print(f"  {seuil} : {ligne}")
    print("accord par choix du modèle :", dict(Counter((d["jugement"]["choix"], a) for d, a in modele)))
    print("règles « effet différent » selon l'humain :", dict(Counter(a for _, a in regles)))


def message_phrase(d):
    return f"Phrase A :\n<<<{d['phrase_a']}>>>\n\nPhrase B :\n<<<{d['phrase_b']}>>>"


def auc(positifs, negatifs):
    return sum((a > b) + 0.5 * (a == b) for a in positifs for b in negatifs) / (len(positifs) * len(negatifs))


def auc_de(lignes, cle):
    return auc([cle(x) for x, equivalent in lignes if equivalent], [cle(x) for x, equivalent in lignes if not equivalent])


def phrases(_args):
    """Mêmes tirages que « lancer » (même graine), jugés sur la phrase entière. La feuille annotée n'est pas touchée."""
    alea = random.Random(822)
    au_modele, _, _ = tirer_ecarts(alea)
    cas, _ = injecter(alea)
    cles = {d["id"]: d for d in map(json.loads, CLE.open(encoding="utf-8"))}
    if any(d["id"] not in cles for d in au_modele):
        raise SystemExit("le tirage ne correspond plus à la feuille annotée")
    juge = JugeDecisions(consignes=CONSIGNES_PHRASE, question=QUESTION_PHRASE)
    def juger(differences):
        with ThreadPoolExecutor(8) as pool:
            return list(pool.map(lambda d: juge(message_phrase(d)), differences))
    for d, jugement in zip(au_modele, juger(au_modele)):
        d["jugement_phrase"] = jugement
    a_juger = [d for c in cas for d in c["differences"] if d["regle"] != "forme_seule"]
    for d, jugement in zip(a_juger, juger(a_juger)):
        d["jugement"] = jugement
    for c in cas:
        for d in c["differences"]:
            d.setdefault("jugement", {"choix": "forme_seule", "probabilites": {"forme_seule": 1.0}, "confiance": None})
    print(f"appels {juge.appels}, en cache {juge.en_cache}, refus {juge.refus}, jetons {juge.entree}, coût {juge.cout:.3f} $")
    annotations = lire_annotations()
    lignes = []
    for d in au_modele:
        annotation = annotations.get(cles[d["id"]]["numero"])
        if annotation:
            d["annotation"], d["jugement_fragment"] = annotation, cles[d["id"]]["jugement"]
            lignes.append(d)
    PHRASES.write_text(json.dumps({"ecarts": lignes, "injections": bilan_injections(cas)}, ensure_ascii=False, indent=1), encoding="utf-8")
    tranches = [(d, d["annotation"] in EQUIVALENTES) for d in lignes if d["annotation"] != "indetermine"]
    nouveau = lambda d: probabilite_meme_effet(d["jugement_phrase"])
    ancien = lambda d: probabilite_meme_effet(d["jugement_fragment"])
    taille = lambda d: -d["taille"]
    print(f"{len(tranches)} écarts, {sum(y for _, y in tranches)} équivalents")
    print("choix :", dict(Counter((d["jugement_phrase"]["choix"], d["annotation"]) for d in lignes)))
    print("probabilité moyenne", round(sum(nouveau(d) for d, _ in tranches) / len(tranches), 3))
    print("AUC phrase", round(auc_de(tranches, nouveau), 3), "fragment", round(auc_de(tranches, ancien), 3), "taille", round(auc_de(tranches, taille), 3))
    alea2, ecarts = random.Random(1), []
    for _ in range(2000):
        tirage = [alea2.choice(tranches) for _ in tranches]
        if any(y for _, y in tirage) and not all(y for _, y in tirage):
            ecarts.append((auc_de(tirage, nouveau), auc_de(tirage, nouveau) - auc_de(tirage, taille), auc_de(tirage, nouveau) - auc_de(tirage, ancien)))
    for rang, nom in enumerate(("AUC phrase", "phrase - taille", "phrase - fragment")):
        valeurs = sorted(e[rang] for e in ecarts)
        print(f"  {nom} : IC95 {valeurs[len(valeurs) // 40]:.3f} à {valeurs[-len(valeurs) // 40]:.3f}")
    for seuil, ligne in taux_par_seuil([(nouveau(d), d["annotation"]) for d in lignes]).items():
        print(f"  seuil {seuil} : {ligne}")
    print("par tranche de probabilité :")
    for bas, haut in ((0, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01)):
        groupe = [y for d, y in tranches if bas <= nouveau(d) < haut]
        print(f"  [{bas}, {haut}[ : {len(groupe)} écarts, {sum(groupe)} équivalents")
    print("erreurs injectées :")
    for nom, ligne in bilan_injections(cas).items():
        print(" ", nom, {k: v for k, v in ligne.items() if k in ("cas", "modèle seul à 0.5", "modèle seul à 0.7", "modèle seul à 0.9")})
    print("acceptés à 0.5 :")
    for d, _ in sorted(tranches, key=lambda x: -nouveau(x[0])):
        if nouveau(d) >= 0.5:
            print(f"  {nouveau(d):.2f} {d['annotation']:14s} {cles[d['id']]['numero']:>3} A=«{d['passage_a'][:50]}» B=«{d['passage_b'][:50]}»")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("commande", choices=["lancer", "bilan", "phrases"])
    args = parser.parse_args()
    {"lancer": lancer, "bilan": bilan, "phrases": phrases}[args.commande](args)


if __name__ == "__main__":
    main()
