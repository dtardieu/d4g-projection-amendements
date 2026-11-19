"""Lot 4 : fait tourner une version du moteur sur le corpus et écrit un résultat par étape (JSONL).

Chaque résultat garde le texte produit, le statut de chaque amendement et, pour chaque mot produit, s'il vient d'un
amendement : assez pour tout scorer ensuite sans relancer le moteur.

Usage : uv run python -m evals.moteur.run --version V1-gram --split dev
"""

import argparse
import dataclasses
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from evals.moteur.corpus import CORPUS, MANIFESTE, NOM
from evals.moteur.versions import APPEL_LLM, MODELE_LLM, VERSIONS
from moteur.analyse import AmendementBrut
from moteur.texte import creer_version, est_blanc, normaliser, numeros

RESULTATS = Path(__file__).parent / "resultats" / NOM
CHAMPS = {f.name for f in dataclasses.fields(AmendementBrut)} | {"numero_ordre"}
MAX_PAIRES_COMMUTATION = 300


@dataclasses.dataclass
class AmendementEval(AmendementBrut):
    numero_ordre: int | None = None


def amendements_de(etape):
    """Sans ceux du titre : le texte du titre n'est pas dans le corpus, il n'y a rien à quoi les appliquer."""
    return [AmendementEval(**{k: v for k, v in a.items() if k in CHAMPS})
            for a in etape["amendements"] if not (a["article_vise"] or "").upper().startswith("TITRE")]


def version_d_entree(etape):
    version = creer_version([(a["numero"], "\n".join(a["lignes"])) for a in etape["entree"]["articles"]])
    drapeaux = {a["numero"]: a["supprime"] for a in etape["entree"]["articles"]}
    return dataclasses.replace(version, articles=tuple(
        dataclasses.replace(a, supprime=drapeaux[a.numero]) for a in version.articles))


def article_sortie(article, view):
    """Le texte produit, en mots, avec pour chaque mot s'il vient d'un amendement (¶ sépare les alinéas)."""
    mots, auteurs = [], []
    for rang, alinea in enumerate(article.alineas):
        if rang:
            mots.append("¶")
            auteurs.append(0)
        for jeton in alinea.jetons:
            if not est_blanc(jeton):
                mots.append(normaliser(jeton.texte))
                auteurs.append(0 if jeton.origine == "T0" else 1)
    return {"id": article.id, "numero": view[article.id], "numero_depart": article.numero, "supprime": article.supprime,
            "mots": mots, "amendement": auteurs,
            "origines": sorted({j.origine for a in article.alineas for j in a.jetons} - {"T0"})}


def executer(etape, nom):
    depart = version_d_entree(etape)
    amendements = amendements_de(etape)
    debut = time.time()
    resultat = VERSIONS[nom](depart, amendements)
    view = numeros(resultat.version)
    return {
        "id": etape["id"],
        "duree": round(time.time() - debut, 3),
        "articles": [article_sortie(a, view) for a in resultat.version.articles],
        "statuts": {u: {"numero": s.numero, "sort": s.sort, "etat": s.etat, "detail": s.detail[:160]}
                    for u, s in resultat.statuts.items()},
    }


def etapes_du_split(split):
    with CORPUS.open(encoding="utf-8") as f:
        for ligne in f:
            etape = json.loads(ligne)
            if split == "tout" or etape["split"] == split:
                yield etape


def commit():
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, choices=sorted(VERSIONS))
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    args = parser.parse_args()
    dossier = RESULTATS / args.version
    dossier.mkdir(parents=True, exist_ok=True)
    manifeste = json.loads(MANIFESTE.read_text(encoding="utf-8"))
    chemin = dossier / f"{args.split}.jsonl"
    provisoire = chemin.with_suffix(".en_cours")
    n = 0
    # Un résultat n'apparaît que complet : un plantage ne laisse jamais un fichier partiel à scorer.
    chemin.unlink(missing_ok=True)
    with provisoire.open("w", encoding="utf-8") as sortie:
        sortie.write(json.dumps({"entete": True, "version": args.version, "split": args.split, "commit": commit(),
                                 "date": datetime.now().isoformat(timespec="seconds"),
                                 "corpus": NOM, "corpus_sha256": manifeste["sha256"],
                                 "modele": MODELE_LLM if "llm" in args.version else None}) + "\n")
        for etape in etapes_du_split(args.split):
            sortie.write(json.dumps(executer(etape, args.version), ensure_ascii=False) + "\n")
            n += 1
    provisoire.replace(chemin)
    print(f"{args.version} {args.split} : {n} étapes → {chemin}")
    if "llm" in args.version:
        a = APPEL_LLM
        print(f"LLM {MODELE_LLM} : {a.appels} appels, {a.en_cache} lus en cache, entrée {a.entree}, sortie {a.sortie} "
              f"(réflexion {a.reflexion}), invalides {a.invalides}, coût {a.cout:.3f} $")


if __name__ == "__main__":
    main()
