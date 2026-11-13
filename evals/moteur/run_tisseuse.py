"""Fait tourner la chaîne Tisseuse + leximpact-legi-ui sur le corpus, au format de run.py, pour la scorer comme les autres.

Leur chaîne analyse un amendement (parseAmendment, Tisseuse) et le projette sur l'article qu'il vise
(projectAmendmentOnPjlArticle, legi-ui) : c'est un aperçu d'un amendement, pas une consolidation. Ce qui suit est donc
de nous, et pèse sur le score : l'ordre des votes, l'enchaînement (la sortie d'un amendement est l'entrée du suivant),
une série d'identiques appliquée une fois, les articles additionnels posés comme articles. Les sous-amendements ne sont
pas pris en charge par leur analyseur, et rien n'est prédit pour les amendements non adoptés (pas de F1 des tombés).

Avant : cd evals/moteur/tisseuse && npm install --ignore-scripts && npm run build   (clone de legi-ui : variable LEGI_UI)
Usage : uv run python -m evals.moteur.run_tisseuse --split dev
"""

import argparse
import difflib
import json
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from evals.moteur import run
from evals.moteur.corpus import MANIFESTE
from evals.moteur.score import mots_officiels
from moteur.analyse import analyser_portee
from moteur.consolidation import SORTS_VOTES, ordre_d_appel
from moteur.texte import cle_numero

VERSION = "V4-tisseuse-legi-ui"
PONT = Path(__file__).parent / "tisseuse"
POSITIONS = {None: "on_article", "apres": "after_article", "avant": "before_article"}


def preparer(etape):
    """(étape pour le pont, statuts déjà connus, {uid d'une série : uid de celui qui est envoyé pour elle})."""
    articles = {cle_numero(a["numero"]): i for i, a in enumerate(etape["entree"]["articles"])}
    envoyes, statuts, series, suiveurs = [], {}, {}, {}

    def statut(a, etat, detail=""):
        statuts[a.uid] = {"numero": a.numero, "sort": a.sort, "etat": etat, "detail": detail}

    for a in ordre_d_appel(run.amendements_de(etape)):
        if a.sort != "Adopté":
            statut(a, "ignore", "non adopté : cette chaîne ne prédit rien" if a.sort in SORTS_VOTES else "")
        elif a.parent_uid:
            statut(a, "non_analyse", "sous-amendement : non pris en charge par l'analyseur de Tisseuse")
        elif a.id_identique and a.id_identique in series:
            suiveurs[a.uid] = series[a.id_identique]
            statut(a, "identique")
        else:
            try:
                portee = analyser_portee(a.article_vise)
            except ValueError as erreur:
                statut(a, "non_analyse", str(erreur))
                continue
            rang = articles.get(cle_numero(portee.article))
            if rang is None:
                statut(a, "pendant", f"article {portee.article} absent du texte d'entrée")
                continue
            numero = re.search(r"\d+", a.alinea or "")
            envoyes.append({
                "uid": a.uid, "numero": a.numero, "article": rang, "article_libelle": f"Article {portee.article}",
                "position": POSITIONS[portee.avant_apres], "alinea": int(numero.group()) if numero else None,
                "alinea_libelle": a.alinea, "dispositif": a.dispositif or "",
            })
            statut(a, "applique")  # remplacé par la réponse du pont
            if a.id_identique:
                series[a.id_identique] = a.uid
    return {"id": etape["id"], "articles": etape["entree"]["articles"], "amendements": envoyes}, statuts, suiveurs


def convertir(etape, sortie, statuts, suiveurs):
    """Réponse du pont → résultat au format de run.executer. Un mot vient d'un amendement s'il n'était pas dans l'entrée."""
    for uid, s in sortie["statuts"].items():
        statuts[uid].update(etat=s["etat"], detail=s["detail"][:160])
    for uid, meneur in suiveurs.items():
        if statuts[meneur]["etat"] != "applique":  # la série a échoué : ses membres aussi
            statuts[uid].update(etat=statuts[meneur]["etat"], detail=statuts[meneur]["detail"])
    articles = []
    for rang, a in enumerate(sortie["articles"]):
        depart = etape["entree"]["articles"][a["depart"]] if a["depart"] is not None else None
        mots = mots_officiels(a["lignes"])
        avant = mots_officiels(depart["lignes"]) if depart else []
        ecrits = [1] * len(mots)
        for tag, _, _, j1, j2 in difflib.SequenceMatcher(None, avant, mots, autojunk=False).get_opcodes():
            if tag == "equal":
                ecrits[j1:j2] = [0] * (j2 - j1)
        articles.append({
            "id": f"V4/{rang}", "numero": depart["numero"] if depart else "+",
            "numero_depart": depart["numero"] if depart else None, "supprime": a["supprime"], "mots": mots,
            "amendement": ecrits, "origines": a["origines"],
        })
    return {"id": etape["id"], "duree": None, "articles": articles, "statuts": statuts}


def appeler_pont(etapes):
    """Un seul appel de Node pour tout le lot : {id d'étape : réponse}."""
    if not (PONT / "dist" / "pont.mjs").exists():
        raise SystemExit(f"pont non construit : cd {PONT} && npm install --ignore-scripts && npm run build")
    with tempfile.TemporaryDirectory() as dossier:
        entree, sortie = Path(dossier) / "entree.jsonl", Path(dossier) / "sortie.jsonl"
        entree.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in etapes), encoding="utf-8")
        subprocess.run(["node", "--no-warnings", str(PONT / "dist" / "pont.mjs"), str(entree), str(sortie)], check=True)
        return {r["id"]: r for r in map(json.loads, sortie.read_text(encoding="utf-8").splitlines())}


def version_git(dossier):
    return subprocess.run(["git", "-C", str(dossier), "rev-parse", "--short", "HEAD"], capture_output=True,
                          text=True).stdout.strip() or None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    args = parser.parse_args()
    etapes = list(run.etapes_du_split(args.split))
    prepares = [preparer(e) for e in etapes]
    reponses = appeler_pont([p[0] for p in prepares])
    dossier = run.RESULTATS / VERSION
    dossier.mkdir(parents=True, exist_ok=True)
    chemin = dossier / f"{args.split}.jsonl"
    provisoire = chemin.with_suffix(".en_cours")
    paquet = json.loads((PONT / "node_modules" / "@tricoteuses" / "tisseuse" / "package.json").read_text(encoding="utf-8"))
    with provisoire.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "entete": True, "version": VERSION, "split": args.split, "commit": run.commit(),
            "date": datetime.now().isoformat(timespec="seconds"),
            "corpus_sha256": json.loads(MANIFESTE.read_text(encoding="utf-8"))["sha256"], "modele": None,
            "tisseuse": paquet["version"], "legi_ui": version_git(PONT.parents[4] / "other_srcs" / "leximpact-legi-ui"),
        }) + "\n")
        for etape, (_, statuts, suiveurs) in zip(etapes, prepares, strict=True):
            f.write(json.dumps(convertir(etape, reponses[etape["id"]], statuts, suiveurs), ensure_ascii=False) + "\n")
    provisoire.replace(chemin)
    print(f"{VERSION} {args.split} : {len(etapes)} étapes → {chemin}")


if __name__ == "__main__":
    main()
