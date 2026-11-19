"""Lot 3 : construit le corpus d'évaluation an_premiere_lecture depuis l'API Tricoteuses et le fige par un manifeste.

Une étape = (texte d'entrée, amendements déposés dessus, texte de sortie officiel), pour la première lecture à
l'Assemblée : commission (texte déposé → texte de la commission) et séance (texte de la commission → texte adopté).
Actes, textes découpés et amendements viennent de l'API Tricoteuses (décision du 13 novembre 2026), via l'adaptateur
datasources/tricoteuses.py. Les réponses sont gardées dans donnees/tricoteuses/ : relancer ne retélécharge rien.

Le découpage développement/test se fait par dossier, par hachage : stable, et les deux étapes d'un dossier restent
ensemble. Le test ne sert qu'à la comparaison finale.

L'ancien corpus (CLAIR et open data de l'AN) et ses résultats sont archivés dans resultats_clair_archive/. Limite
connue : le rapport annexé d'une loi de programmation n'est pas rattaché à l'article qui l'approuve.

Usage : uv run python -m evals.engine.corpus
"""

import dataclasses
import hashlib
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

from engine.analysis import RawAmendment
from datasources import tricoteuses

HERE = Path(__file__).parent
DATA = HERE / "donnees"
NAME = "an_premiere_lecture"  # le périmètre : la première lecture à l'Assemblée
CORPUS = DATA / f"{NAME}.jsonl"
MANIFEST = HERE / f"{NAME}_manifeste.json"
CACHE = DATA / "tricoteuses"
LEGISLATURE = 17
FIELDS = [f.name for f in dataclasses.fields(RawAmendment)] + ["numero_ordre"]
DEV_SHARE = 70
SALT = "moteur-v1"
# Dossiers vus pendant la construction du moteur (exemples, fixtures, tests) : ni développement ni test.
EXCLUDED_DOSSIERS = {"DLR5L17N51328": "PPL n° 822, tombée en test alors qu'elle a servi à construire le moteur"}


def split_de(dossier):
    if dossier in EXCLUDED_DOSSIERS:
        return "exclu"
    return "dev" if int(hashlib.sha256(f"{SALT}:{dossier}".encode()).hexdigest(), 16) % 100 < DEV_SHARE else "test"


def is_finance(titre):
    return bool(re.search(r"finances|financement de la sécurité sociale", titre or "", re.I))


def cached(name, computation):
    """Résultat JSON de calcul(), lu dans le cache s'il y est, sinon calculé puis gardé."""
    path = CACHE / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    value = computation()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return value


def text(ref):
    """{uid, titre, articles} (un tricoteuses.Texte en JSON), ou None quand Tricoteuses n'a pas le texte ou qu'il
    n'a aucun article."""
    def read():
        try:
            return dataclasses.asdict(tricoteuses.read_text(ref))
        except tricoteuses.TextNotFound:
            return None
    return cached(f"textes/{ref}", read)


def amendments(ref):
    """Amendements déposés sur le texte, au format du corpus (sans l'auteur, inutile à l'évaluation)."""
    def read():
        return [{c: getattr(a, c) for c in FIELDS} for a in tricoteuses.text_amendments(ref)]
    return cached(f"amendements/{ref}", read)


def dossier_steps(reading, excluded):
    """Les étapes retenues d'une PremiereLecture ; chaque étape écartée est comptée dans exclus, avec sa raison."""
    steps, dossier = [], reading.dossier
    for name, input_, output in (("commission", reading.depose, reading.commission),
                                ("seance", reading.commission, reading.adopte)):
        if not input_ or not output:
            excluded["acte manquant"] += 1
            continue
        deposited = amendments(input_)
        if not any(a["sort"] == "Adopté" for a in deposited):
            excluded["aucun amendement adopté"] += 1
            continue
        input_text, output_text = text(input_), text(output)
        if input_text is None:
            excluded["texte d'entrée introuvable"] += 1
            continue
        if output_text is None:
            excluded["texte de sortie introuvable"] += 1
            continue
        titre = input_text["titre"]
        steps.append({
            "id": f"{dossier}:{name}", "dossier": dossier, "titre": titre, "etape": name, "split": split_de(dossier),
            "finances": is_finance(titre), "entree": {"ref": input_, "articles": input_text["articles"]},
            "sortie": {"ref": output, "articles": output_text["articles"]}, "amendements": deposited,
        })
    return steps


def build():
    excluded, steps = Counter(), []
    readings = cached(f"premiere_lecture_{LEGISLATURE}", lambda: [
        dataclasses.asdict(reading) for reading in tricoteuses.first_reading_texts(LEGISLATURE)])
    for reading in (tricoteuses.FirstReading(**d) for d in readings):
        steps += dossier_steps(reading, excluded)
        print(f"{reading.dossier} : {len(steps)} étapes retenues", flush=True)
    return steps, excluded


def main():
    steps, excluded = build()
    write(steps, excluded, CORPUS, MANIFEST, source=tricoteuses.URL)


def write(steps, excluded, corpus, manifest, source=None):
    """Écrit le corpus (une étape par ligne, triées) et son manifeste, qui le fige par son empreinte."""
    steps.sort(key=lambda e: e["id"])
    body = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in steps)
    corpus.write_text(body, encoding="utf-8")
    counted = Counter((e["etape"], e["split"]) for e in steps)
    manifest.write_text(json.dumps({
        "nom": NAME,
        **({"source": source} if source else {}),
        "date": date.today().isoformat(),
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "etapes": len(steps),
        "par_etape_et_split": {f"{k[0]}/{k[1]}": v for k, v in sorted(counted.items())},
        "amendements_adoptes": sum(a["sort"] == "Adopté" for e in steps for a in e["amendements"]),
        "exclus": dict(excluded),
        "dossiers_exclus": EXCLUDED_DOSSIERS,
        "sel_split": SALT,
        "ids": {"dev": [e["id"] for e in steps if e["split"] == "dev"],
                "test": [e["id"] for e in steps if e["split"] == "test"]},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(manifest.read_text(encoding="utf-8")[:400])


if __name__ == "__main__":
    main()
