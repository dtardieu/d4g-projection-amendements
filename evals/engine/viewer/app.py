"""Visionneuse des résultats : ce que le moteur a changé, comparé au texte officiel. Jeu de développement seulement.

Usage : uv run uvicorn evals.engine.viewer.app:app --port 8766
"""

import difflib
import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from evals.engine.corpus import MANIFEST
from evals.engine.llm_openai import OpenAICall, BudgetExceeded
from evals.engine.run import RESULTS, EvalAmendment, FIELDS, output_article, split_steps, input_version
from evals.engine.score import (adopted_by_article, match_articles, classify_gaps, diagnose_article, step_failures,
                                read_results, official_words, without_mentions)
from evals.engine.versions import LLM_MODEL
from engine.analysis import (Instruction, analyze_amendment, analyze_with_grammar, analyze_with_llm, analyze_scope,
                            split_instructions, clean_dispositif)
from engine.application import apply, integrate_subamendments
from engine.resolution import TargetError, resolve
from engine.text import article_by_id, number_key, lines_of, numbers, text_of, find_article

SPLIT = "dev"  # le test ne sert qu'à la comparaison finale : on ne le regarde pas ici
NO_SPACE_BEFORE = {",", ".", ")", "]", "…", "-", "‑", "'", "’", "°"}
NO_SPACE_AFTER = {"(", "[", "-", "‑", "'", "’"}

MISSING_ARTICLE = {"entree": [], "officiel": [], "uids": []}  # amendement rattaché à aucun article : article additionnel, titre

# Plafond nul : la visionneuse lit les réponses déjà payées, elle n'appelle jamais le modèle.
LLM_CACHE_ONLY = OpenAICall(LLM_MODEL, budget=0)

app = FastAPI(title="Visionneuse du moteur")


@lru_cache(maxsize=1)
def steps():
    return {e["id"]: e for e in split_steps(SPLIT)}


@lru_cache(maxsize=8)
def results(version):
    if not (RESULTS / version / f"{SPLIT}.jsonl").exists():
        raise HTTPException(404, f"version inconnue : {version}")
    return read_results(version, SPLIT)[1]


def pieces(words):
    """Mots → texte avec ses espaces, un morceau par mot, en tenant compte du mot précédent. ¶ devient un saut de ligne."""
    output, previous = [], None
    for word in words:
        if word == "¶":
            output.append("\n")
        elif previous in (None, "¶") or word in NO_SPACE_BEFORE or previous in NO_SPACE_AFTER:
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
    return _group(continuation)


def _group(continuation):
    texts = pieces([m for _, m, _ in continuation])
    segments = []
    for (t, _, mark), text in zip(continuation, texts):
        if segments and segments[-1]["t"] == t and segments[-1].get("amdt") == mark:
            segments[-1]["s"] += text
        else:
            segments.append({"t": t, "s": text, "amdt": mark})
    return segments


def segments_ecarts(predicted, official, flags, gaps):
    """Texte produit → texte officiel, chaque écart portant son étiquette."""
    continuation, position = [], 0
    for label, i1, i2, j1, j2 in gaps:
        continuation += [("=", m, None) for m in predicted[position:i1]]
        continuation += [("-", m, label) for m in predicted[i1:i2]]
        continuation += [("+", m, label) for m in official[j1:j2]]
        position = i2
    continuation += [("=", m, None) for m in predicted[position:]]
    texts = pieces([m for _, m, _ in continuation])
    segments = []
    for (t, _, label), text in zip(continuation, texts):
        if segments and segments[-1]["t"] == t and segments[-1].get("cat") == label:
            segments[-1]["s"] += text
        else:
            segments.append({"t": t, "s": text, "cat": label})
    return segments


@lru_cache(maxsize=64)
def analyze_step(version, step_id):
    step = steps().get(step_id)
    if step is None:
        raise HTTPException(404, "étape inconnue (seul le développement est visible)")
    r = results(version)[step_id]
    pairs, missing = match_articles(r["articles"], step["sortie"]["articles"])
    inputs = {number_key(a["numero"]): a for a in step["entree"]["articles"]}
    adopted = adopted_by_article(step)
    articles = {}
    for p in r["articles"]:
        o = pairs.get(p["id"])
        predicted, flags = without_mentions(p["mots"], p["amendement"])
        input_article = inputs.get(number_key(p["numero_depart"] or "\0"))
        input_ = without_mentions(official_words(input_article["lignes"])) if input_article else []
        official = without_mentions(official_words(o["lignes"])) if o is not None else []
        if input_article is None and o is None and not predicted:
            continue
        targeted_amendments, gaps, category = diagnose_article(p, o, predicted, flags, official, r["statuts"], adopted)
        articles[p["id"]] = {
            "numero": p["numero"], "numero_officiel": o["numero"] if o else None, "categorie": category,
            "exact": predicted == official, "touche": input_ != official or input_ != predicted, "supprime": p["supprime"],
            "officiel_supprime": bool(o and o["supprime"]), "uids": [a["uid"] for a in targeted_amendments],
            "entree": input_, "predit": predicted, "drapeaux": flags, "officiel": official, "ecarts": gaps,
        }
    return articles, [o["numero"] for o in missing], r["statuts"]


@app.get("/")
def page():
    return FileResponse(Path(__file__).with_name("index.html"))


@app.get("/revue")
def review_page():
    path = RESULTS / "revue_amendements" / "revue.html"
    if not path.exists():
        raise HTTPException(404, "lancez d'abord : python -m evals.engine.amendment_review lancer")
    return FileResponse(path)


@app.get("/api/versions")
def versions():
    return sorted(d.name for d in RESULTS.iterdir() if (d / f"{SPLIT}.jsonl").exists())


COMPARED_STATES = ("applique", "pendant", "non_analyse", "ambigu")


def compare_versions(dossier, current_corpus=None):
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
        file_date = datetime.fromtimestamp(result.stat().st_mtime).isoformat(timespec="seconds")
        lignes.append({
            "version": d.name, "date": header_line.get("date") or file_date, "date_fichier": "date" not in header_line,
            "commit": header_line.get("commit"), "modele": header_line.get("modele"),
            # Un run fait sur un autre corpus (textes relus depuis) n'est pas comparable aux autres.
            "corpus_ancien": current_corpus is not None and header_line.get("corpus_sha256") != current_corpus,
            # Le score d'un run plus ancien que le résultat ne décrit plus ce résultat.
            "score_perime": bool(s) and score.stat().st_mtime < result.stat().st_mtime,
            **s.get("consolidation", {}), "f1_tombes_%": s.get("tombes", {}).get("f1_%"),
            **{etat: s.get("etats", {}).get(etat) for etat in COMPARED_STATES},
        })
    return lignes


@app.get("/api/comparaison")
def comparison():
    return compare_versions(RESULTS, json.loads(MANIFEST.read_text(encoding="utf-8"))["sha256"])


@app.get("/api/etapes")
def list_steps(version: str):
    path = RESULTS / version / f"score_{SPLIT}.json"
    scores = {t["id"]: t for t in json.loads(path.read_text(encoding="utf-8"))["textes"]} if path.exists() else {}
    results(version)
    return [{"id": i, "titre": e["titre"], "etape": e["etape"], "finances": e["finances"],
             "adoptes": sum(a["sort"] == "Adopté" for a in e["amendements"]), **{
                 k: scores.get(i, {}).get(k) for k in ("touches", "exacts_%")}}
            for i, e in steps().items()]


@app.get("/api/etape")
def step_detail(version: str, step: str):
    articles, missing, _ = analyze_step(version, step)
    return {
        "titre": steps()[step]["titre"], "entree": steps()[step]["entree"]["ref"],
        "sortie": steps()[step]["sortie"]["ref"], "manquants": missing,
        "articles": [{"id": i, **{k: a[k] for k in ("numero", "categorie", "exact", "touche", "supprime")},
                      "nb_amendements": len(a["uids"])} for i, a in articles.items()],
    }


@lru_cache(maxsize=8)
def corpus_failures(version):
    """Tout le jeu de développement : long au premier appel, puis gardé en mémoire."""
    output = []
    for step_id, step in steps().items():
        articles, _, statuses = analyze_step(version, step_id)
        for failure in step_failures(step["amendements"], articles, statuses):
            article = articles.get(failure["article"])
            output.append({**failure, "etape": step_id, "titre": step["titre"], "stade": step["etape"],
                           "numero_article": article["numero"] if article else None})
    return output


@app.get("/api/echecs")
def list_failures(version: str):
    results(version)
    return corpus_failures(version)


@app.get("/api/article")
def article_detail(version: str, step: str, article: str):
    articles, _, statuses = analyze_step(version, step)
    a = articles.get(article)
    if a is None:
        raise HTTPException(404, "article inconnu")
    by_uid = {x["uid"]: x for x in steps()[step]["amendements"]}
    return {
        **{k: a[k] for k in ("numero", "numero_officiel", "categorie", "exact", "supprime", "officiel_supprime")},
        "effet": segments_diff(a["entree"], a["predit"], a["drapeaux"]),
        "reel": segments_diff(a["entree"], a["officiel"]),
        "ecart": segments_ecarts(a["predit"], a["officiel"], a["drapeaux"], a["ecarts"]),
        "amendements": _amendments(a, by_uid, statuses),
        "autres_adoptes": len(a["uids"]),
    }


@app.get("/api/amendement")
def amendment_detail(version: str, step: str, uid: str, article: str | None = None):
    articles, _, statuses = analyze_step(version, step)
    a = articles.get(article) if article else MISSING_ARTICLE
    if a is None:
        raise HTTPException(404, "article inconnu")
    return amendment_effect(steps()[step], uid, a, statuses, "llm" in version)


def amendment_effect(step, uid, article, statuses, with_llm=False):
    """Ce que produit cet amendement seul sur le texte d'entrée, comparé au texte officiel de l'article.

    Avec ses sous-amendements adoptés ; un sous-amendement est montré à travers son parent. Marche aussi pour un
    amendement rejeté ou tombé : c'est le texte qu'on aurait obtenu.
    """
    by_uid = {x["uid"]: x for x in step["amendements"]}
    raw = by_uid.get(uid)
    if raw is None:
        raise HTTPException(404, "amendement inconnu")
    status = statuses.get(uid, {})
    response = {
        "uid": uid, "numero": raw["numero"], "sort": raw["sort"], "etat": status.get("etat"),
        "detail": status.get("detail"), "dispositif": raw["dispositif"], "article_vise": raw["article_vise"],
        "parent": by_uid[raw["parent_uid"]]["numero"] if raw["parent_uid"] in by_uid else None,
        "erreur": None, "article_produit": None, "effet": [], "ecart": [],
        "reel": segments_diff(article["entree"], article["officiel"]),
        "autres_adoptes": len([u for u in article["uids"] if u != uid]),
    }
    initial = input_version(step)
    response["analyse"] = detailed_analysis(raw, initial, with_llm)
    try:
        operations, scope, auteur = _operations(step, raw, initial, by_uid, with_llm)
        product = apply(operations, scope, initial, initial, auteur)[0]
    except (TargetError, ValueError, BudgetExceeded) as error:
        response["erreur"] = str(error)
        return response
    if scope.node == "article_additionnel":
        new_entry = next(a for a in product.articles if a.numero is None)
        input_, target = [], new_entry
    else:
        targeted_one = find_article(initial, scope.article)
        target = next(a for a in product.articles if a.id == targeted_one.id)
        input_ = without_mentions(official_words(lines_of(targeted_one))) if not targeted_one.supprime else []
    output = output_article(target, numbers(product))
    after, flags = without_mentions(output["mots"], output["amendement"])
    response["article_produit"] = output["numero"]
    response["effet"] = segments_diff(input_, after, flags)
    response["ecart"] = segments_ecarts(after, article["officiel"], flags, classify_gaps(after, flags, article["officiel"]))
    return response


def detailed_analysis(raw, initial, with_llm=False):
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
    llm = with_llm and raw["sort"] == "Adopté"
    output, preceding, last_one = [], None, None
    for instruction in instructions:
        operations, source, confidence = analyze_with_grammar(instruction, scope, preceding, last_one), "grammaire", None
        if operations is None and llm:
            try:
                operations, confidence = analyze_with_llm(instruction, scope, contexte, preceding, LLM_CACHE_ONLY)
                source = "LLM"
            except BudgetExceeded:
                operations = None
        if not operations:
            operations, source = [], "non reconnu"
        else:
            preceding, last_one = operations[-1].target, operations[-1]
        output.append({"texte": "\n".join([instruction.heading, *instruction.quotes]), "source": source,
                       "confiance": confidence,
                       "operations": [_describe_operation(op, scope, initial, bool(raw["parent_uid"])) for op in operations]})
    return output


def _describe_operation(op, scope, initial, sous_amendement):
    target = op.target
    described = {"verbe": op.verb, "alinea": target.alinea, "jusqua": target.until, "phrase": target.sentence, "noeud": target.node,
              "position": target.position, "mots": target.words, "occurrence": target.occurrence,
              "anciens_mots": op.old_words, "texte": op.text, "trouve": None, "erreur": None, "note": None}
    if sous_amendement:
        described["note"] = "Vise le texte de l'amendement parent, pas celui de l'article."
    elif scope.node == "article_additionnel":
        described["note"] = f"Nouvel article, {'avant' if scope.before_after == 'avant' else 'après'} l'article {scope.article}."
    else:
        try:
            place = resolve(op, scope, initial, initial)
            described["trouve"] = _excerpt(place, initial)
        except TargetError as error:
            described["erreur"] = str(error)
    return described


def _excerpt(place, initial):
    """Où la cible a été trouvée dans le texte d'entrée : le rang de l'alinéa et les mots visés."""
    article = article_by_id(initial, place.article_id)
    if not place.paragraph_ids:
        return {"lieu": "tout l'article", "extrait": None}
    ranks = [i for i, a in enumerate(article.paragraphs, 1) if a.id in place.paragraph_ids]
    location = f"alinéa {ranks[0]}" if len(ranks) == 1 else f"alinéas {ranks[0]} à {ranks[-1]}"
    alinea = next(a for a in article.paragraphs if a.id == place.paragraph_ids[0])
    if place.beginning is None or len(ranks) > 1:
        return {"lieu": location, "extrait": None}
    if place.beginning == 0 and place.end == len(alinea.tokens):
        return {"lieu": location, "extrait": None}
    return {"lieu": location, "extrait": "".join(j.text for j in alinea.tokens[place.beginning:place.end]).strip()}


def _operations(step, raw, initial, by_uid, with_llm):
    """Opérations à exécuter : celles de l'amendement, ou celles du parent modifiées par le sous-amendement choisi."""
    parent = by_uid.get(raw["parent_uid"]) if raw["parent_uid"] else None
    principal = parent or raw
    analysis = _analyze(principal, initial, with_llm)
    if parent is not None:
        sub = [raw]
    else:
        sub = [x for x in step["amendements"] if x["parent_uid"] == raw["uid"] and x["sort"] == "Adopté"]
    if analysis.errors:
        raise ValueError("; ".join(analysis.errors))
    with_sub = [(x["uid"], _analyze(x, initial, with_llm).operations) for x in sub]
    return (integrate_subamendments(analysis.operations, analysis.scope, principal["uid"], with_sub), analysis.scope,
            principal["uid"])


def _analyze(raw, initial, with_llm):
    amendment = EvalAmendment(**{k: v for k, v in raw.items() if k in FIELDS})
    if not with_llm or raw["sort"] != "Adopté":
        return analyze_amendment(amendment)
    scope = analyze_scope(raw["article_vise"])
    article = find_article(initial, scope.article)
    text = "\n".join(text_of(a) for a in article.paragraphs) if article else None
    return analyze_amendment(amendment, text, LLM_CACHE_ONLY)


def _amendments(article, by_uid, statuses):
    """Les amendements adoptés qui visent l'article, puis les autres amendements déposés sur le même article."""
    uids = list(article["uids"])
    targeted_one = number_key(article["numero"])
    for uid, x in by_uid.items():
        if uid not in uids and _same_article(x, targeted_one):
            uids.append(uid)
    output = []
    for uid in uids:
        x, s = by_uid.get(uid), statuses.get(uid, {})
        if x is not None:
            output.append({"uid": uid, "numero": x["numero"], "sort": x["sort"], "etat": s.get("etat"),
                           "parent": bool(x["parent_uid"]), "tri": x["tri_amendement"] or "", "dispositif": x["dispositif"]})
    return sorted(output, key=lambda x: (x["sort"] != "Adopté", x["tri"]))


def _same_article(amendment, targeted_one):
    try:
        scope = analyze_scope(amendment["article_vise"])
    except ValueError:
        return False
    return scope.node == "article" and number_key(scope.article) == targeted_one
