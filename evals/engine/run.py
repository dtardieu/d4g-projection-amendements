"""Lot 4 : fait tourner une version du moteur sur le corpus et écrit un résultat par étape (JSONL).

Chaque résultat garde le texte produit, le statut de chaque amendement et, pour chaque mot produit, s'il vient d'un
amendement : assez pour tout scorer ensuite sans relancer le moteur.

Usage : uv run python -m evals.engine.run --version V1-gram --split dev
"""

import argparse
import dataclasses
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from evals.engine.corpus import CORPUS, MANIFESTE, NOM
from evals.engine.versions import APPEL_LLM, MODELE_LLM, VERSIONS
from engine.analysis import RawAmendment
from engine.text import create_version, is_blank, normalize, numbers

RESULTATS = Path(__file__).parent / "resultats" / NOM
CHAMPS = {f.name for f in dataclasses.fields(RawAmendment)} | {"numero_ordre"}
MAX_PAIRES_COMMUTATION = 300


@dataclasses.dataclass
class AmendementEval(RawAmendment):
    numero_ordre: int | None = None


def amendements_de(step):
    """Sans ceux du titre : le texte du titre n'est pas dans le corpus, il n'y a rien à quoi les appliquer."""
    return [AmendementEval(**{k: v for k, v in a.items() if k in CHAMPS})
            for a in step["amendements"] if not (a["article_vise"] or "").upper().startswith("TITRE")]


def version_d_entree(step):
    version = create_version([(a["numero"], "\n".join(a["lignes"])) for a in step["entree"]["articles"]])
    flags = {a["numero"]: a["supprime"] for a in step["entree"]["articles"]}
    return dataclasses.replace(version, articles=tuple(
        dataclasses.replace(a, supprime=flags[a.numero]) for a in version.articles))


def article_sortie(article, view):
    """Le texte produit, en mots, avec pour chaque mot s'il vient d'un amendement (¶ sépare les alinéas)."""
    words, authors = [], []
    for rank, alinea in enumerate(article.paragraphs):
        if rank:
            words.append("¶")
            authors.append(0)
        for token in alinea.tokens:
            if not is_blank(token):
                words.append(normalize(token.text))
                authors.append(0 if token.origin == "T0" else 1)
    return {"id": article.id, "numero": view[article.id], "numero_depart": article.numero, "supprime": article.supprime,
            "mots": words, "amendement": authors,
            "origines": sorted({j.origin for a in article.paragraphs for j in a.tokens} - {"T0"})}


def executer(step, name):
    initial = version_d_entree(step)
    amendments = amendements_de(step)
    beginning = time.time()
    result = VERSIONS[name](initial, amendments)
    view = numbers(result.version)
    return {
        "id": step["id"],
        "duree": round(time.time() - beginning, 3),
        "articles": [article_sortie(a, view) for a in result.version.articles],
        "statuts": {u: {"numero": s.numero, "sort": s.sort, "etat": s.etat, "detail": s.detail[:160]}
                    for u, s in result.statuses.items()},
    }


def etapes_du_split(split):
    with CORPUS.open(encoding="utf-8") as f:
        for line in f:
            step = json.loads(line)
            if split == "tout" or step["split"] == split:
                yield step


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
    with provisoire.open("w", encoding="utf-8") as output:
        output.write(json.dumps({"entete": True, "version": args.version, "split": args.split, "commit": commit(),
                                 "date": datetime.now().isoformat(timespec="seconds"),
                                 "corpus": NOM, "corpus_sha256": manifeste["sha256"],
                                 "modele": MODELE_LLM if "llm" in args.version else None}) + "\n")
        for step in etapes_du_split(args.split):
            output.write(json.dumps(executer(step, args.version), ensure_ascii=False) + "\n")
            n += 1
    provisoire.replace(chemin)
    print(f"{args.version} {args.split} : {n} étapes → {chemin}")
    if "llm" in args.version:
        a = APPEL_LLM
        print(f"LLM {MODELE_LLM} : {a.calls} appels, {a.en_cache} lus en cache, entrée {a.entree}, sortie {a.output} "
              f"(réflexion {a.reflexion}), invalides {a.invalides}, coût {a.cout:.3f} $")


if __name__ == "__main__":
    main()
