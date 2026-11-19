"""Fait tourner la chaîne Tisseuse + leximpact-legi-ui sur le corpus, au format de run.py, pour la scorer comme les autres.

Leur chaîne analyse un amendement (parseAmendment, Tisseuse) et le projette sur l'article qu'il vise
(projectAmendmentOnPjlArticle, legi-ui) : c'est un aperçu d'un amendement, pas une consolidation. Ce qui suit est donc
de nous, et pèse sur le score : l'ordre des votes, l'enchaînement (la sortie d'un amendement est l'entrée du suivant),
une série d'identiques appliquée une fois, les articles additionnels posés comme articles. Les sous-amendements ne sont
pas pris en charge par leur analyseur, et rien n'est prédit pour les amendements non adoptés (pas de F1 des tombés).

Avant : cd evals/engine/tisseuse && npm install --ignore-scripts && npm run build   (clone de legi-ui : variable LEGI_UI)
Usage : uv run python -m evals.engine.run_tisseuse --split dev
"""

import argparse
import difflib
import json
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from evals.engine import run
from evals.engine.corpus import MANIFEST
from evals.engine.score import official_words
from engine.analysis import analyze_scope
from engine.consolidation import VOTED_OUTCOMES, call_order
from engine.text import number_key

VERSION = "V4-tisseuse-legi-ui"
BRIDGE = Path(__file__).parent / "tisseuse"
POSITIONS = {None: "on_article", "apres": "after_article", "avant": "before_article"}


def prepare(step):
    """(étape pour le pont, statuts déjà connus, {uid d'une série : uid de celui qui est envoyé pour elle})."""
    articles = {number_key(a["numero"]): i for i, a in enumerate(step["entree"]["articles"])}
    sent, statuses, series, followers = [], {}, {}, {}

    def status(a, etat, detail=""):
        statuses[a.uid] = {"numero": a.numero, "sort": a.sort, "etat": etat, "detail": detail}

    for a in call_order(run.amendments_of(step)):
        if a.sort != "Adopté":
            status(a, "ignore", "non adopté : cette chaîne ne prédit rien" if a.sort in VOTED_OUTCOMES else "")
        elif a.parent_uid:
            status(a, "non_analyse", "sous-amendement : non pris en charge par l'analyseur de Tisseuse")
        elif a.id_identique and a.id_identique in series:
            followers[a.uid] = series[a.id_identique]
            status(a, "identique")
        else:
            try:
                scope = analyze_scope(a.article_vise)
            except ValueError as error:
                status(a, "non_analyse", str(error))
                continue
            rank = articles.get(number_key(scope.article))
            if rank is None:
                status(a, "pendant", f"article {scope.article} absent du texte d'entrée")
                continue
            numero = re.search(r"\d+", a.alinea or "")
            sent.append({
                "uid": a.uid, "numero": a.numero, "article": rank, "article_libelle": f"Article {scope.article}",
                "position": POSITIONS[scope.before_after], "alinea": int(numero.group()) if numero else None,
                "alinea_libelle": a.alinea, "dispositif": a.dispositif or "",
            })
            status(a, "applique")  # remplacé par la réponse du pont
            if a.id_identique:
                series[a.id_identique] = a.uid
    return {"id": step["id"], "articles": step["entree"]["articles"], "amendements": sent}, statuses, followers


def convert(step, output, statuses, followers):
    """Réponse du pont → résultat au format de run.executer. Un mot vient d'un amendement s'il n'était pas dans l'entrée."""
    for uid, s in output["statuts"].items():
        statuses[uid].update(etat=s["etat"], detail=s["detail"][:160])
    for uid, leader in followers.items():
        if statuses[leader]["etat"] != "applique":  # la série a échoué : ses membres aussi
            statuses[uid].update(etat=statuses[leader]["etat"], detail=statuses[leader]["detail"])
    articles = []
    for rank, a in enumerate(output["articles"]):
        initial = step["entree"]["articles"][a["depart"]] if a["depart"] is not None else None
        words = official_words(a["lignes"])
        before = official_words(initial["lignes"]) if initial else []
        written_items = [1] * len(words)
        for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, before, words, autojunk=False).get_opcodes():
            if tag == "equal":
                written_items[j1:j2] = [0] * (j2 - j1)
        articles.append({
            "id": f"V4/{rank}", "numero": initial["numero"] if initial else "+",
            "numero_depart": initial["numero"] if initial else None, "supprime": a["supprime"], "mots": words,
            "amendement": written_items, "origines": a["origines"],
        })
    return {"id": step["id"], "duree": None, "articles": articles, "statuts": statuses}


def call_bridge(steps):
    """Un seul appel de Node pour tout le lot : {id d'étape : réponse}."""
    if not (BRIDGE / "dist" / "pont.mjs").exists():
        raise SystemExit(f"pont non construit : cd {BRIDGE} && npm install --ignore-scripts && npm run build")
    with tempfile.TemporaryDirectory() as dossier:
        input_, output = Path(dossier) / "entree.jsonl", Path(dossier) / "sortie.jsonl"
        input_.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in steps), encoding="utf-8")
        subprocess.run(["node", "--no-warnings", str(BRIDGE / "dist" / "pont.mjs"), str(input_), str(output)], check=True)
        return {r["id"]: r for r in map(json.loads, output.read_text(encoding="utf-8").splitlines())}


def git_version(dossier):
    return subprocess.run(["git", "-C", str(dossier), "rev-parse", "--short", "HEAD"], capture_output=True,
                          text=True).stdout.strip() or None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    args = parser.parse_args()
    steps = list(run.split_steps(args.split))
    prepared = [prepare(e) for e in steps]
    responses = call_bridge([p[0] for p in prepared])
    dossier = run.RESULTS / VERSION
    dossier.mkdir(parents=True, exist_ok=True)
    path = dossier / f"{args.split}.jsonl"
    temporary = path.with_suffix(".en_cours")
    batch = json.loads((BRIDGE / "node_modules" / "@tricoteuses" / "tisseuse" / "package.json").read_text(encoding="utf-8"))
    with temporary.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "entete": True, "version": VERSION, "split": args.split, "commit": run.commit(),
            "date": datetime.now().isoformat(timespec="seconds"),
            "corpus_sha256": json.loads(MANIFEST.read_text(encoding="utf-8"))["sha256"], "modele": None,
            "tisseuse": batch["version"], "legi_ui": git_version(BRIDGE.parents[4] / "other_srcs" / "leximpact-legi-ui"),
        }) + "\n")
        for step, (_, statuses, followers) in zip(steps, prepared, strict=True):
            f.write(json.dumps(convert(step, responses[step["id"]], statuses, followers), ensure_ascii=False) + "\n")
    temporary.replace(path)
    print(f"{VERSION} {args.split} : {len(steps)} étapes → {path}")


if __name__ == "__main__":
    main()
