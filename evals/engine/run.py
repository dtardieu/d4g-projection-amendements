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

from evals.engine.corpus import CORPUS, MANIFEST, NAME
from evals.engine.versions import LLM_CALL, LLM_MODEL, VERSIONS
from engine.analysis import RawAmendement
from engine.text import create_version, is_blank, normalize, numbers

RESULTS = Path(__file__).parent / "resultats" / NAME
FIELDS = {f.name for f in dataclasses.fields(RawAmendement)} | {"numero_ordre"}
MAX_COMMUTATION_PAIRS = 300


@dataclasses.dataclass
class EvalAmendement(RawAmendement):
    numero_ordre: int | None = None


def amendements_of(step):
    """Sans ceux du titre : le texte du titre n'est pas dans le corpus, il n'y a rien à quoi les appliquer."""
    return [EvalAmendement(**{k: v for k, v in a.items() if k in FIELDS})
            for a in step["amendements"] if not (a["article_vise"] or "").upper().startswith("TITRE")]


def input_version(step):
    version = create_version([(a["numero"], "\n".join(a["lignes"])) for a in step["entree"]["articles"]])
    flags = {a["numero"]: a["supprime"] for a in step["entree"]["articles"]}
    return dataclasses.replace(version, articles=tuple(
        dataclasses.replace(a, supprime=flags[a.numero]) for a in version.articles))


def output_article(article, view):
    """Le texte produit, en mots, avec pour chaque mot s'il vient d'un amendement (¶ sépare les alinéas)."""
    words, authors = [], []
    for rank, alinea in enumerate(article.alineas):
        if rank:
            words.append("¶")
            authors.append(0)
        for token in alinea.tokens:
            if not is_blank(token):
                words.append(normalize(token.text))
                authors.append(0 if token.origin == "T0" else 1)
    return {"id": article.id, "numero": view[article.id], "numero_depart": article.numero, "supprime": article.supprime,
            "mots": words, "amendement": authors,
            "origines": sorted({j.origin for a in article.alineas for j in a.tokens} - {"T0"})}


def execute(step, name):
    initial = input_version(step)
    amendements = amendements_of(step)
    beginning = time.time()
    result = VERSIONS[name](initial, amendements)
    view = numbers(result.version)
    return {
        "id": step["id"],
        "duree": round(time.time() - beginning, 3),
        "articles": [output_article(a, view) for a in result.version.articles],
        "statuts": {u: {"numero": s.numero, "sort": s.sort, "etat": s.etat, "detail": s.detail[:160]}
                    for u, s in result.statuses.items()},
    }


def split_steps(split):
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
    dossier = RESULTS / args.version
    dossier.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    path = dossier / f"{args.split}.jsonl"
    temporary = path.with_suffix(".en_cours")
    n = 0
    # Un résultat n'apparaît que complet : un plantage ne laisse jamais un fichier partiel à scorer.
    path.unlink(missing_ok=True)
    with temporary.open("w", encoding="utf-8") as output:
        output.write(json.dumps({"entete": True, "version": args.version, "split": args.split, "commit": commit(),
                                 "date": datetime.now().isoformat(timespec="seconds"),
                                 "corpus": NAME, "corpus_sha256": manifest["sha256"],
                                 "modele": LLM_MODEL if "llm" in args.version else None}) + "\n")
        for step in split_steps(args.split):
            output.write(json.dumps(execute(step, args.version), ensure_ascii=False) + "\n")
            n += 1
    temporary.replace(path)
    print(f"{args.version} {args.split} : {n} étapes → {path}")
    if "llm" in args.version:
        a = LLM_CALL
        print(f"LLM {LLM_MODEL} : {a.calls} appels, {a.cached} lus en cache, entrée {a.input_}, sortie {a.output} "
              f"(réflexion {a.reasoning}), invalides {a.invalid}, coût {a.cost:.3f} $")


if __name__ == "__main__":
    main()
