"""Lot 3 : construit le corpus d'évaluation an_premiere_lecture depuis l'API Tricoteuses et le fige par un manifeste.

Une étape = (texte d'entrée, amendements déposés dessus, texte de sortie officiel), pour la première lecture à
l'Assemblée : commission (texte déposé → texte de la commission) et séance (texte de la commission → texte adopté).
Actes, textes découpés et amendements viennent de l'API Tricoteuses (décision du 13 novembre 2026), via l'adaptateur
datasources/tricoteuses.py. Les réponses sont gardées dans donnees/tricoteuses/ : relancer ne retélécharge rien.

Le découpage développement/test se fait par dossier, par hachage : stable, et les deux étapes d'un dossier restent
ensemble. Le test ne sert qu'à la comparaison finale.

L'ancien corpus (CLAIR et open data de l'AN) et ses résultats sont archivés dans resultats_clair_archive/. Limite
connue : le rapport annexé d'une loi de programmation n'est pas rattaché à l'article qui l'approuve.

Usage : uv run python -m evals.moteur.corpus
"""

import dataclasses
import hashlib
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

from moteur.analyse import AmendementBrut
from datasources import tricoteuses

ICI = Path(__file__).parent
DONNEES = ICI / "donnees"
NOM = "an_premiere_lecture"  # le périmètre : la première lecture à l'Assemblée
CORPUS = DONNEES / f"{NOM}.jsonl"
MANIFESTE = ICI / f"{NOM}_manifeste.json"
CACHE = DONNEES / "tricoteuses"
LEGISLATURE = 17
CHAMPS = [f.name for f in dataclasses.fields(AmendementBrut)] + ["numero_ordre"]
PART_DEV = 70
SEL = "moteur-v1"
# Dossiers vus pendant la construction du moteur (exemples, fixtures, tests) : ni développement ni test.
DOSSIERS_EXCLUS = {"DLR5L17N51328": "PPL n° 822, tombée en test alors qu'elle a servi à construire le moteur"}


def split_de(dossier):
    if dossier in DOSSIERS_EXCLUS:
        return "exclu"
    return "dev" if int(hashlib.sha256(f"{SEL}:{dossier}".encode()).hexdigest(), 16) % 100 < PART_DEV else "test"


def est_finances(titre):
    return bool(re.search(r"finances|financement de la sécurité sociale", titre or "", re.I))


def en_cache(nom, calcul):
    """Résultat JSON de calcul(), lu dans le cache s'il y est, sinon calculé puis gardé."""
    chemin = CACHE / f"{nom}.json"
    if chemin.exists():
        return json.loads(chemin.read_text(encoding="utf-8"))
    valeur = calcul()
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(valeur, ensure_ascii=False), encoding="utf-8")
    return valeur


def texte(ref):
    """{uid, titre, articles} (un tricoteuses.Texte en JSON), ou None quand Tricoteuses n'a pas le texte ou qu'il
    n'a aucun article."""
    def lire():
        try:
            return dataclasses.asdict(tricoteuses.lire_texte(ref))
        except tricoteuses.TexteIntrouvable:
            return None
    return en_cache(f"textes/{ref}", lire)


def amendements(ref):
    """Amendements déposés sur le texte, au format du corpus (sans l'auteur, inutile à l'évaluation)."""
    def lire():
        return [{c: getattr(a, c) for c in CHAMPS} for a in tricoteuses.amendements_du_texte(ref)]
    return en_cache(f"amendements/{ref}", lire)


def etapes_du_dossier(lecture, exclus):
    """Les étapes retenues d'une PremiereLecture ; chaque étape écartée est comptée dans exclus, avec sa raison."""
    etapes, dossier = [], lecture.dossier
    for nom, entree, sortie in (("commission", lecture.depose, lecture.commission),
                                ("seance", lecture.commission, lecture.adopte)):
        if not entree or not sortie:
            exclus["acte manquant"] += 1
            continue
        deposes = amendements(entree)
        if not any(a["sort"] == "Adopté" for a in deposes):
            exclus["aucun amendement adopté"] += 1
            continue
        texte_entree, texte_sortie = texte(entree), texte(sortie)
        if texte_entree is None:
            exclus["texte d'entrée introuvable"] += 1
            continue
        if texte_sortie is None:
            exclus["texte de sortie introuvable"] += 1
            continue
        titre = texte_entree["titre"]
        etapes.append({
            "id": f"{dossier}:{nom}", "dossier": dossier, "titre": titre, "etape": nom, "split": split_de(dossier),
            "finances": est_finances(titre), "entree": {"ref": entree, "articles": texte_entree["articles"]},
            "sortie": {"ref": sortie, "articles": texte_sortie["articles"]}, "amendements": deposes,
        })
    return etapes


def construire():
    exclus, etapes = Counter(), []
    lectures = en_cache(f"premiere_lecture_{LEGISLATURE}", lambda: [
        dataclasses.asdict(lecture) for lecture in tricoteuses.textes_de_premiere_lecture(LEGISLATURE)])
    for lecture in (tricoteuses.PremiereLecture(**d) for d in lectures):
        etapes += etapes_du_dossier(lecture, exclus)
        print(f"{lecture.dossier} : {len(etapes)} étapes retenues", flush=True)
    return etapes, exclus


def main():
    etapes, exclus = construire()
    ecrire(etapes, exclus, CORPUS, MANIFESTE, source=tricoteuses.URL)


def ecrire(etapes, exclus, corpus, manifeste, source=None):
    """Écrit le corpus (une étape par ligne, triées) et son manifeste, qui le fige par son empreinte."""
    etapes.sort(key=lambda e: e["id"])
    corps = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in etapes)
    corpus.write_text(corps, encoding="utf-8")
    compte = Counter((e["etape"], e["split"]) for e in etapes)
    manifeste.write_text(json.dumps({
        "nom": NOM,
        **({"source": source} if source else {}),
        "date": date.today().isoformat(),
        "sha256": hashlib.sha256(corps.encode("utf-8")).hexdigest(),
        "etapes": len(etapes),
        "par_etape_et_split": {f"{k[0]}/{k[1]}": v for k, v in sorted(compte.items())},
        "amendements_adoptes": sum(a["sort"] == "Adopté" for e in etapes for a in e["amendements"]),
        "exclus": dict(exclus),
        "dossiers_exclus": DOSSIERS_EXCLUS,
        "sel_split": SEL,
        "ids": {"dev": [e["id"] for e in etapes if e["split"] == "dev"],
                "test": [e["id"] for e in etapes if e["split"] == "test"]},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(manifeste.read_text(encoding="utf-8")[:400])


if __name__ == "__main__":
    main()
