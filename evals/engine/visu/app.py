"""Visionneuse des résultats : ce que le moteur a changé, comparé au texte officiel. Jeu de développement seulement.

Usage : uv run uvicorn evals.engine.visu.app:app --port 8766
"""

import difflib
import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from evals.engine.corpus import MANIFESTE
from evals.engine.llm_openai import AppelOpenAI, PlafondDepasse
from evals.engine.run import RESULTATS, AmendementEval, CHAMPS, article_sortie, etapes_du_split, version_d_entree
from evals.engine.score import (adoptes_par_article, apparier, classer_ecarts, diagnostiquer_article, echecs_etape,
                                lire_resultats, mots_officiels, sans_mentions)
from evals.engine.versions import MODELE_LLM
from engine.analysis import (Instruction, analyze_amendment, analyze_with_grammar, analyze_with_llm, analyze_scope,
                            split_instructions, clean_dispositif)
from engine.application import apply, integrate_subamendments
from engine.resolution import TargetError, resolve
from engine.text import article_by_id, number_key, lines_of, numbers, text_of, find_article

SPLIT = "dev"  # le test ne sert qu'à la comparaison finale : on ne le regarde pas ici
SANS_ESPACE_AVANT = {",", ".", ")", "]", "…", "-", "‑", "'", "’", "°"}
SANS_ESPACE_APRES = {"(", "[", "-", "‑", "'", "’"}

ARTICLE_ABSENT = {"entree": [], "officiel": [], "uids": []}  # amendement rattaché à aucun article : article additionnel, titre

# Plafond nul : la visionneuse lit les réponses déjà payées, elle n'appelle jamais le modèle.
LLM_CACHE_SEUL = AppelOpenAI(MODELE_LLM, plafond=0)

app = FastAPI(title="Visionneuse du moteur")


@lru_cache(maxsize=1)
def steps():
    return {e["id"]: e for e in etapes_du_split(SPLIT)}


@lru_cache(maxsize=8)
def resultats(version):
    if not (RESULTATS / version / f"{SPLIT}.jsonl").exists():
        raise HTTPException(404, f"version inconnue : {version}")
    return lire_resultats(version, SPLIT)[1]


def pieces(words):
    """Mots → texte avec ses espaces, un morceau par mot, en tenant compte du mot précédent. ¶ devient un saut de ligne."""
    output, previous = [], None
    for word in words:
        if word == "¶":
            output.append("\n")
        elif previous in (None, "¶") or word in SANS_ESPACE_AVANT or previous in SANS_ESPACE_APRES:
            output.append(word)
        else:
            output.append(" " + word)
        previous = word
    return output


def segments_diff(before, after, flags=None):
    """Diff mot à mot : [{t: "=" | "+" | "-", s: texte}]. drapeaux marque les mots d'apres écrits par un amendement."""
    continuation = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag == "equal":
            continuation += [("=", m, bool(flags and flags[j1 + k])) for k, m in enumerate(after[j1:j2])]
        else:
            continuation += [("-", m, False) for m in before[i1:i2]]
            continuation += [("+", m, False) for m in after[j1:j2]]
    return _regrouper(continuation)


def _regrouper(continuation):
    texts = pieces([m for _, m, _ in continuation])
    segments = []
    for (t, _, mark), text in zip(continuation, texts):
        if segments and segments[-1]["t"] == t and segments[-1].get("amdt") == mark:
            segments[-1]["s"] += text
        else:
            segments.append({"t": t, "s": text, "amdt": mark})
    return segments


def segments_ecarts(predicted, official, flags, ecarts):
    """Texte produit → texte officiel, chaque écart portant son étiquette."""
    continuation, position = [], 0
    for etiquette, i1, i2, j1, j2 in ecarts:
        continuation += [("=", m, None) for m in predicted[position:i1]]
        continuation += [("-", m, etiquette) for m in predicted[i1:i2]]
        continuation += [("+", m, etiquette) for m in official[j1:j2]]
        position = i2
    continuation += [("=", m, None) for m in predicted[position:]]
    texts = pieces([m for _, m, _ in continuation])
    segments = []
    for (t, _, etiquette), text in zip(continuation, texts):
        if segments and segments[-1]["t"] == t and segments[-1].get("cat") == etiquette:
            segments[-1]["s"] += text
        else:
            segments.append({"t": t, "s": text, "cat": etiquette})
    return segments


@lru_cache(maxsize=64)
def analyser_etape(version, etape_id):
    step = steps().get(etape_id)
    if step is None:
        raise HTTPException(404, "étape inconnue (seul le développement est visible)")
    r = resultats(version)[etape_id]
    pairs, missing = apparier(r["articles"], step["sortie"]["articles"])
    entrees = {number_key(a["numero"]): a for a in step["entree"]["articles"]}
    adopted = adoptes_par_article(step)
    articles = {}
    for p in r["articles"]:
        o = pairs.get(p["id"])
        predicted, flags = sans_mentions(p["mots"], p["amendement"])
        entree_art = entrees.get(number_key(p["numero_depart"] or "\0"))
        entree = sans_mentions(mots_officiels(entree_art["lignes"])) if entree_art else []
        official = sans_mentions(mots_officiels(o["lignes"])) if o is not None else []
        if entree_art is None and o is None and not predicted:
            continue
        vises, ecarts, categorie = diagnostiquer_article(p, o, predicted, flags, official, r["statuts"], adopted)
        articles[p["id"]] = {
            "numero": p["numero"], "numero_officiel": o["numero"] if o else None, "categorie": categorie,
            "exact": predicted == official, "touche": entree != official or entree != predicted, "supprime": p["supprime"],
            "officiel_supprime": bool(o and o["supprime"]), "uids": [a["uid"] for a in vises],
            "entree": entree, "predit": predicted, "drapeaux": flags, "officiel": official, "ecarts": ecarts,
        }
    return articles, [o["numero"] for o in missing], r["statuts"]


@app.get("/")
def page():
    return FileResponse(Path(__file__).with_name("index.html"))


@app.get("/revue")
def page_revue():
    chemin = RESULTATS / "revue_amendements" / "revue.html"
    if not chemin.exists():
        raise HTTPException(404, "lancez d'abord : python -m evals.engine.revue_amendements lancer")
    return FileResponse(chemin)


@app.get("/api/versions")
def versions():
    return sorted(d.name for d in RESULTATS.iterdir() if (d / f"{SPLIT}.jsonl").exists())


ETATS_COMPARES = ("applique", "pendant", "non_analyse", "ambigu")


def comparer_versions(dossier, corpus_actuel=None):
    """Une ligne par version qui a un résultat : la date du run et les scores déjà calculés par score.py.

    Un run d'avant l'ajout de la date n'en porte pas : on donne celle du fichier, marquée « date_fichier ».
    Sans score (score.py pas encore lancé), la ligne reste, avec des scores vides.
    """
    lignes = []
    for d in sorted(dossier.iterdir()):
        result, score = d / f"{SPLIT}.jsonl", d / f"score_{SPLIT}.json"
        if not result.exists():
            continue
        with result.open(encoding="utf-8") as f:
            header_line = json.loads(f.readline())
        s = json.loads(score.read_text(encoding="utf-8")) if score.exists() else {}
        date_fichier = datetime.fromtimestamp(result.stat().st_mtime).isoformat(timespec="seconds")
        lignes.append({
            "version": d.name, "date": header_line.get("date") or date_fichier, "date_fichier": "date" not in header_line,
            "commit": header_line.get("commit"), "modele": header_line.get("modele"),
            # Un run fait sur un autre corpus (textes relus depuis) n'est pas comparable aux autres.
            "corpus_ancien": corpus_actuel is not None and header_line.get("corpus_sha256") != corpus_actuel,
            # Le score d'un run plus ancien que le résultat ne décrit plus ce résultat.
            "score_perime": bool(s) and score.stat().st_mtime < result.stat().st_mtime,
            **s.get("consolidation", {}), "f1_tombes_%": s.get("tombes", {}).get("f1_%"),
            **{etat: s.get("etats", {}).get(etat) for etat in ETATS_COMPARES},
        })
    return lignes


@app.get("/api/comparaison")
def comparison():
    return comparer_versions(RESULTATS, json.loads(MANIFESTE.read_text(encoding="utf-8"))["sha256"])


@app.get("/api/etapes")
def liste_etapes(version: str):
    chemin = RESULTATS / version / f"score_{SPLIT}.json"
    scores = {t["id"]: t for t in json.loads(chemin.read_text(encoding="utf-8"))["textes"]} if chemin.exists() else {}
    resultats(version)
    return [{"id": i, "titre": e["titre"], "etape": e["etape"], "finances": e["finances"],
             "adoptes": sum(a["sort"] == "Adopté" for a in e["amendements"]), **{
                 k: scores.get(i, {}).get(k) for k in ("touches", "exacts_%")}}
            for i, e in steps().items()]


@app.get("/api/etape")
def detail_etape(version: str, step: str):
    articles, missing, _ = analyser_etape(version, step)
    return {
        "titre": steps()[step]["titre"], "entree": steps()[step]["entree"]["ref"],
        "sortie": steps()[step]["sortie"]["ref"], "manquants": missing,
        "articles": [{"id": i, **{k: a[k] for k in ("numero", "categorie", "exact", "touche", "supprime")},
                      "nb_amendements": len(a["uids"])} for i, a in articles.items()],
    }


@lru_cache(maxsize=8)
def echecs_corpus(version):
    """Tout le jeu de développement : long au premier appel, puis gardé en mémoire."""
    output = []
    for etape_id, step in steps().items():
        articles, _, statuses = analyser_etape(version, etape_id)
        for echec in echecs_etape(step["amendements"], articles, statuses):
            article = articles.get(echec["article"])
            output.append({**echec, "etape": etape_id, "titre": step["titre"], "stade": step["etape"],
                           "numero_article": article["numero"] if article else None})
    return output


@app.get("/api/echecs")
def liste_echecs(version: str):
    resultats(version)
    return echecs_corpus(version)


@app.get("/api/article")
def detail_article(version: str, step: str, article: str):
    articles, _, statuses = analyser_etape(version, step)
    a = articles.get(article)
    if a is None:
        raise HTTPException(404, "article inconnu")
    par_uid = {x["uid"]: x for x in steps()[step]["amendements"]}
    return {
        **{k: a[k] for k in ("numero", "numero_officiel", "categorie", "exact", "supprime", "officiel_supprime")},
        "effet": segments_diff(a["entree"], a["predit"], a["drapeaux"]),
        "reel": segments_diff(a["entree"], a["officiel"]),
        "ecart": segments_ecarts(a["predit"], a["officiel"], a["drapeaux"], a["ecarts"]),
        "amendements": _amendements(a, par_uid, statuses),
        "autres_adoptes": len(a["uids"]),
    }


@app.get("/api/amendement")
def detail_amendement(version: str, step: str, uid: str, article: str | None = None):
    articles, _, statuses = analyser_etape(version, step)
    a = articles.get(article) if article else ARTICLE_ABSENT
    if a is None:
        raise HTTPException(404, "article inconnu")
    return effet_amendement(steps()[step], uid, a, statuses, "llm" in version)


def effet_amendement(step, uid, article, statuses, avec_llm=False):
    """Ce que produit cet amendement seul sur le texte d'entrée, comparé au texte officiel de l'article.

    Avec ses sous-amendements adoptés ; un sous-amendement est montré à travers son parent. Marche aussi pour un
    amendement rejeté ou tombé : c'est le texte qu'on aurait obtenu.
    """
    par_uid = {x["uid"]: x for x in step["amendements"]}
    raw = par_uid.get(uid)
    if raw is None:
        raise HTTPException(404, "amendement inconnu")
    status = statuses.get(uid, {})
    response = {
        "uid": uid, "numero": raw["numero"], "sort": raw["sort"], "etat": status.get("etat"),
        "detail": status.get("detail"), "dispositif": raw["dispositif"], "article_vise": raw["article_vise"],
        "parent": par_uid[raw["parent_uid"]]["numero"] if raw["parent_uid"] in par_uid else None,
        "erreur": None, "article_produit": None, "effet": [], "ecart": [],
        "reel": segments_diff(article["entree"], article["officiel"]),
        "autres_adoptes": len([u for u in article["uids"] if u != uid]),
    }
    initial = version_d_entree(step)
    response["analyse"] = analyse_detaillee(raw, initial, avec_llm)
    try:
        operations, scope, auteur = _operations(step, raw, initial, par_uid, avec_llm)
        produit = apply(operations, scope, initial, initial, auteur)[0]
    except (TargetError, ValueError, PlafondDepasse) as error:
        response["erreur"] = str(error)
        return response
    if scope.node == "article_additionnel":
        nouvel = next(a for a in produit.articles if a.numero is None)
        entree, target = [], nouvel
    else:
        targeted_one = find_article(initial, scope.article)
        target = next(a for a in produit.articles if a.id == targeted_one.id)
        entree = sans_mentions(mots_officiels(lines_of(targeted_one))) if not targeted_one.supprime else []
    output = article_sortie(target, numbers(produit))
    after, flags = sans_mentions(output["mots"], output["amendement"])
    response["article_produit"] = output["numero"]
    response["effet"] = segments_diff(entree, after, flags)
    response["ecart"] = segments_ecarts(after, article["officiel"], flags, classer_ecarts(after, flags, article["officiel"]))
    return response


def analyse_detaillee(raw, initial, avec_llm=False):
    """Pour chaque instruction : qui l'a comprise (grammaire, LLM ou personne), ses opérations, ce qu'elles visent."""
    try:
        scope = analyze_scope(raw["article_vise"])
    except ValueError as error:
        return [{"texte": raw["article_vise"] or "", "source": "non reconnu", "operations": [], "erreur": str(error)}]
    paragraphs = clean_dispositif(raw["dispositif"] or "")
    if scope.node == "article_additionnel":
        instructions = [Instruction(None, paragraphs[0], paragraphs[1:])] if paragraphs else []
    else:
        instructions = split_instructions(paragraphs)
    article = None if scope.node == "titre" else find_article(initial, scope.article)
    contexte = "\n".join(text_of(a) for a in article.paragraphs) if article else None
    # Le LLM ne sert qu'aux amendements adoptés, comme dans V2-gram+llm.
    llm = avec_llm and raw["sort"] == "Adopté"
    output, preceding, derniere = [], None, None
    for instruction in instructions:
        operations, source, confidence = analyze_with_grammar(instruction, scope, preceding, derniere), "grammaire", None
        if operations is None and llm:
            try:
                operations, confidence = analyze_with_llm(instruction, scope, contexte, preceding, LLM_CACHE_SEUL)
                source = "LLM"
            except PlafondDepasse:
                operations = None
        if not operations:
            operations, source = [], "non reconnu"
        else:
            preceding, derniere = operations[-1].target, operations[-1]
        output.append({"texte": "\n".join([instruction.heading, *instruction.quotes]), "source": source,
                       "confiance": confidence,
                       "operations": [_decrire_operation(op, scope, initial, bool(raw["parent_uid"])) for op in operations]})
    return output


def _decrire_operation(op, scope, initial, sous_amendement):
    target = op.target
    decrit = {"verbe": op.verb, "alinea": target.alinea, "jusqua": target.until, "phrase": target.sentence, "noeud": target.node,
              "position": target.position, "mots": target.words, "occurrence": target.occurrence,
              "anciens_mots": op.old_words, "texte": op.text, "trouve": None, "erreur": None, "note": None}
    if sous_amendement:
        decrit["note"] = "Vise le texte de l'amendement parent, pas celui de l'article."
    elif scope.node == "article_additionnel":
        decrit["note"] = f"Nouvel article, {'avant' if scope.before_after == 'avant' else 'après'} l'article {scope.article}."
    else:
        try:
            place = resolve(op, scope, initial, initial)
            decrit["trouve"] = _extrait(place, initial)
        except TargetError as error:
            decrit["erreur"] = str(error)
    return decrit


def _extrait(place, initial):
    """Où la cible a été trouvée dans le texte d'entrée : le rang de l'alinéa et les mots visés."""
    article = article_by_id(initial, place.article_id)
    if not place.paragraph_ids:
        return {"lieu": "tout l'article", "extrait": None}
    rangs = [i for i, a in enumerate(article.paragraphs, 1) if a.id in place.paragraph_ids]
    lieu = f"alinéa {rangs[0]}" if len(rangs) == 1 else f"alinéas {rangs[0]} à {rangs[-1]}"
    alinea = next(a for a in article.paragraphs if a.id == place.paragraph_ids[0])
    if place.beginning is None or len(rangs) > 1:
        return {"lieu": lieu, "extrait": None}
    if place.beginning == 0 and place.end == len(alinea.tokens):
        return {"lieu": lieu, "extrait": None}
    return {"lieu": lieu, "extrait": "".join(j.text for j in alinea.tokens[place.beginning:place.end]).strip()}


def _operations(step, raw, initial, par_uid, avec_llm):
    """Opérations à exécuter : celles de l'amendement, ou celles du parent modifiées par le sous-amendement choisi."""
    parent = par_uid.get(raw["parent_uid"]) if raw["parent_uid"] else None
    principal = parent or raw
    analysis = _analyze(principal, initial, avec_llm)
    if parent is not None:
        sub = [raw]
    else:
        sub = [x for x in step["amendements"] if x["parent_uid"] == raw["uid"] and x["sort"] == "Adopté"]
    if analysis.errors:
        raise ValueError("; ".join(analysis.errors))
    avec_sous = [(x["uid"], _analyze(x, initial, avec_llm).operations) for x in sub]
    return (integrate_subamendments(analysis.operations, analysis.scope, principal["uid"], avec_sous), analysis.scope,
            principal["uid"])


def _analyze(raw, initial, avec_llm):
    amendment = AmendementEval(**{k: v for k, v in raw.items() if k in CHAMPS})
    if not avec_llm or raw["sort"] != "Adopté":
        return analyze_amendment(amendment)
    scope = analyze_scope(raw["article_vise"])
    article = find_article(initial, scope.article)
    text = "\n".join(text_of(a) for a in article.paragraphs) if article else None
    return analyze_amendment(amendment, text, LLM_CACHE_SEUL)


def _amendements(article, par_uid, statuses):
    """Les amendements adoptés qui visent l'article, puis les autres amendements déposés sur le même article."""
    uids = list(article["uids"])
    targeted_one = number_key(article["numero"])
    for uid, x in par_uid.items():
        if uid not in uids and _meme_article(x, targeted_one):
            uids.append(uid)
    output = []
    for uid in uids:
        x, s = par_uid.get(uid), statuses.get(uid, {})
        if x is not None:
            output.append({"uid": uid, "numero": x["numero"], "sort": x["sort"], "etat": s.get("etat"),
                           "parent": bool(x["parent_uid"]), "tri": x["tri_amendement"] or "", "dispositif": x["dispositif"]})
    return sorted(output, key=lambda x: (x["sort"] != "Adopté", x["tri"]))


def _meme_article(amendment, targeted_one):
    try:
        scope = analyze_scope(amendment["article_vise"])
    except ValueError:
        return False
    return scope.node == "article" and number_key(scope.article) == targeted_one
