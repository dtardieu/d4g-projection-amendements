"""Visionneuse des résultats : ce que le moteur a changé, comparé au texte officiel. Jeu de développement seulement.

Usage : uv run uvicorn evals.moteur.visu.app:app --port 8766
"""

import difflib
import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from evals.moteur.corpus import MANIFESTE
from evals.moteur.llm_openai import AppelOpenAI, PlafondDepasse
from evals.moteur.run import RESULTATS, AmendementEval, CHAMPS, article_sortie, etapes_du_split, version_d_entree
from evals.moteur.score import (adoptes_par_article, apparier, classer_ecarts, diagnostiquer_article, echecs_etape,
                                lire_resultats, mots_officiels, sans_mentions)
from evals.moteur.versions import MODELE_LLM
from moteur.analyse import (Instruction, analyser_amendement, analyser_par_grammaire, analyser_par_llm, analyser_portee,
                            decouper_instructions, nettoyer_dispositif)
from moteur.application import appliquer, integrer_sous_amendements
from moteur.resolution import ErreurCible, resoudre
from moteur.texte import article_par_id, cle_numero, lignes_de, numeros, texte_de, trouver_article

SPLIT = "dev"  # le test ne sert qu'à la comparaison finale : on ne le regarde pas ici
SANS_ESPACE_AVANT = {",", ".", ")", "]", "…", "-", "‑", "'", "’", "°"}
SANS_ESPACE_APRES = {"(", "[", "-", "‑", "'", "’"}

ARTICLE_ABSENT = {"entree": [], "officiel": [], "uids": []}  # amendement rattaché à aucun article : article additionnel, titre

# Plafond nul : la visionneuse lit les réponses déjà payées, elle n'appelle jamais le modèle.
LLM_CACHE_SEUL = AppelOpenAI(MODELE_LLM, plafond=0)

app = FastAPI(title="Visionneuse du moteur")


@lru_cache(maxsize=1)
def etapes():
    return {e["id"]: e for e in etapes_du_split(SPLIT)}


@lru_cache(maxsize=8)
def resultats(version):
    if not (RESULTATS / version / f"{SPLIT}.jsonl").exists():
        raise HTTPException(404, f"version inconnue : {version}")
    return lire_resultats(version, SPLIT)[1]


def morceaux(mots):
    """Mots → texte avec ses espaces, un morceau par mot, en tenant compte du mot précédent. ¶ devient un saut de ligne."""
    sortie, precedent = [], None
    for mot in mots:
        if mot == "¶":
            sortie.append("\n")
        elif precedent in (None, "¶") or mot in SANS_ESPACE_AVANT or precedent in SANS_ESPACE_APRES:
            sortie.append(mot)
        else:
            sortie.append(" " + mot)
        precedent = mot
    return sortie


def segments_diff(avant, apres, drapeaux=None):
    """Diff mot à mot : [{t: "=" | "+" | "-", s: texte}]. drapeaux marque les mots d'apres écrits par un amendement."""
    suite = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, avant, apres, autojunk=False).get_opcodes():
        if tag == "equal":
            suite += [("=", m, bool(drapeaux and drapeaux[j1 + k])) for k, m in enumerate(apres[j1:j2])]
        else:
            suite += [("-", m, False) for m in avant[i1:i2]]
            suite += [("+", m, False) for m in apres[j1:j2]]
    return _regrouper(suite)


def _regrouper(suite):
    textes = morceaux([m for _, m, _ in suite])
    segments = []
    for (t, _, marque), texte in zip(suite, textes):
        if segments and segments[-1]["t"] == t and segments[-1].get("amdt") == marque:
            segments[-1]["s"] += texte
        else:
            segments.append({"t": t, "s": texte, "amdt": marque})
    return segments


def segments_ecarts(predit, officiel, drapeaux, ecarts):
    """Texte produit → texte officiel, chaque écart portant son étiquette."""
    suite, position = [], 0
    for etiquette, i1, i2, j1, j2 in ecarts:
        suite += [("=", m, None) for m in predit[position:i1]]
        suite += [("-", m, etiquette) for m in predit[i1:i2]]
        suite += [("+", m, etiquette) for m in officiel[j1:j2]]
        position = i2
    suite += [("=", m, None) for m in predit[position:]]
    textes = morceaux([m for _, m, _ in suite])
    segments = []
    for (t, _, etiquette), texte in zip(suite, textes):
        if segments and segments[-1]["t"] == t and segments[-1].get("cat") == etiquette:
            segments[-1]["s"] += texte
        else:
            segments.append({"t": t, "s": texte, "cat": etiquette})
    return segments


@lru_cache(maxsize=64)
def analyser_etape(version, etape_id):
    etape = etapes().get(etape_id)
    if etape is None:
        raise HTTPException(404, "étape inconnue (seul le développement est visible)")
    r = resultats(version)[etape_id]
    paires, manquants = apparier(r["articles"], etape["sortie"]["articles"])
    entrees = {cle_numero(a["numero"]): a for a in etape["entree"]["articles"]}
    adoptes = adoptes_par_article(etape)
    articles = {}
    for p in r["articles"]:
        o = paires.get(p["id"])
        predit, drapeaux = sans_mentions(p["mots"], p["amendement"])
        entree_art = entrees.get(cle_numero(p["numero_depart"] or "\0"))
        entree = sans_mentions(mots_officiels(entree_art["lignes"])) if entree_art else []
        officiel = sans_mentions(mots_officiels(o["lignes"])) if o is not None else []
        if entree_art is None and o is None and not predit:
            continue
        vises, ecarts, categorie = diagnostiquer_article(p, o, predit, drapeaux, officiel, r["statuts"], adoptes)
        articles[p["id"]] = {
            "numero": p["numero"], "numero_officiel": o["numero"] if o else None, "categorie": categorie,
            "exact": predit == officiel, "touche": entree != officiel or entree != predit, "supprime": p["supprime"],
            "officiel_supprime": bool(o and o["supprime"]), "uids": [a["uid"] for a in vises],
            "entree": entree, "predit": predit, "drapeaux": drapeaux, "officiel": officiel, "ecarts": ecarts,
        }
    return articles, [o["numero"] for o in manquants], r["statuts"]


@app.get("/")
def page():
    return FileResponse(Path(__file__).with_name("index.html"))


@app.get("/revue")
def page_revue():
    chemin = RESULTATS / "revue_amendements" / "revue.html"
    if not chemin.exists():
        raise HTTPException(404, "lancez d'abord : python -m evals.moteur.revue_amendements lancer")
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
        resultat, score = d / f"{SPLIT}.jsonl", d / f"score_{SPLIT}.json"
        if not resultat.exists():
            continue
        with resultat.open(encoding="utf-8") as f:
            entete = json.loads(f.readline())
        s = json.loads(score.read_text(encoding="utf-8")) if score.exists() else {}
        date_fichier = datetime.fromtimestamp(resultat.stat().st_mtime).isoformat(timespec="seconds")
        lignes.append({
            "version": d.name, "date": entete.get("date") or date_fichier, "date_fichier": "date" not in entete,
            "commit": entete.get("commit"), "modele": entete.get("modele"),
            # Un run fait sur un autre corpus (textes relus depuis) n'est pas comparable aux autres.
            "corpus_ancien": corpus_actuel is not None and entete.get("corpus_sha256") != corpus_actuel,
            # Le score d'un run plus ancien que le résultat ne décrit plus ce résultat.
            "score_perime": bool(s) and score.stat().st_mtime < resultat.stat().st_mtime,
            **s.get("consolidation", {}), "f1_tombes_%": s.get("tombes", {}).get("f1_%"),
            **{etat: s.get("etats", {}).get(etat) for etat in ETATS_COMPARES},
        })
    return lignes


@app.get("/api/comparaison")
def comparaison():
    return comparer_versions(RESULTATS, json.loads(MANIFESTE.read_text(encoding="utf-8"))["sha256"])


@app.get("/api/etapes")
def liste_etapes(version: str):
    chemin = RESULTATS / version / f"score_{SPLIT}.json"
    scores = {t["id"]: t for t in json.loads(chemin.read_text(encoding="utf-8"))["textes"]} if chemin.exists() else {}
    resultats(version)
    return [{"id": i, "titre": e["titre"], "etape": e["etape"], "finances": e["finances"],
             "adoptes": sum(a["sort"] == "Adopté" for a in e["amendements"]), **{
                 k: scores.get(i, {}).get(k) for k in ("touches", "exacts_%")}}
            for i, e in etapes().items()]


@app.get("/api/etape")
def detail_etape(version: str, etape: str):
    articles, manquants, _ = analyser_etape(version, etape)
    return {
        "titre": etapes()[etape]["titre"], "entree": etapes()[etape]["entree"]["ref"],
        "sortie": etapes()[etape]["sortie"]["ref"], "manquants": manquants,
        "articles": [{"id": i, **{k: a[k] for k in ("numero", "categorie", "exact", "touche", "supprime")},
                      "nb_amendements": len(a["uids"])} for i, a in articles.items()],
    }


@lru_cache(maxsize=8)
def echecs_corpus(version):
    """Tout le jeu de développement : long au premier appel, puis gardé en mémoire."""
    sortie = []
    for etape_id, etape in etapes().items():
        articles, _, statuts = analyser_etape(version, etape_id)
        for echec in echecs_etape(etape["amendements"], articles, statuts):
            article = articles.get(echec["article"])
            sortie.append({**echec, "etape": etape_id, "titre": etape["titre"], "stade": etape["etape"],
                           "numero_article": article["numero"] if article else None})
    return sortie


@app.get("/api/echecs")
def liste_echecs(version: str):
    resultats(version)
    return echecs_corpus(version)


@app.get("/api/article")
def detail_article(version: str, etape: str, article: str):
    articles, _, statuts = analyser_etape(version, etape)
    a = articles.get(article)
    if a is None:
        raise HTTPException(404, "article inconnu")
    par_uid = {x["uid"]: x for x in etapes()[etape]["amendements"]}
    return {
        **{k: a[k] for k in ("numero", "numero_officiel", "categorie", "exact", "supprime", "officiel_supprime")},
        "effet": segments_diff(a["entree"], a["predit"], a["drapeaux"]),
        "reel": segments_diff(a["entree"], a["officiel"]),
        "ecart": segments_ecarts(a["predit"], a["officiel"], a["drapeaux"], a["ecarts"]),
        "amendements": _amendements(a, par_uid, statuts),
        "autres_adoptes": len(a["uids"]),
    }


@app.get("/api/amendement")
def detail_amendement(version: str, etape: str, uid: str, article: str | None = None):
    articles, _, statuts = analyser_etape(version, etape)
    a = articles.get(article) if article else ARTICLE_ABSENT
    if a is None:
        raise HTTPException(404, "article inconnu")
    return effet_amendement(etapes()[etape], uid, a, statuts, "llm" in version)


def effet_amendement(etape, uid, article, statuts, avec_llm=False):
    """Ce que produit cet amendement seul sur le texte d'entrée, comparé au texte officiel de l'article.

    Avec ses sous-amendements adoptés ; un sous-amendement est montré à travers son parent. Marche aussi pour un
    amendement rejeté ou tombé : c'est le texte qu'on aurait obtenu.
    """
    par_uid = {x["uid"]: x for x in etape["amendements"]}
    brut = par_uid.get(uid)
    if brut is None:
        raise HTTPException(404, "amendement inconnu")
    statut = statuts.get(uid, {})
    reponse = {
        "uid": uid, "numero": brut["numero"], "sort": brut["sort"], "etat": statut.get("etat"),
        "detail": statut.get("detail"), "dispositif": brut["dispositif"], "article_vise": brut["article_vise"],
        "parent": par_uid[brut["parent_uid"]]["numero"] if brut["parent_uid"] in par_uid else None,
        "erreur": None, "article_produit": None, "effet": [], "ecart": [],
        "reel": segments_diff(article["entree"], article["officiel"]),
        "autres_adoptes": len([u for u in article["uids"] if u != uid]),
    }
    depart = version_d_entree(etape)
    reponse["analyse"] = analyse_detaillee(brut, depart, avec_llm)
    try:
        operations, portee, auteur = _operations(etape, brut, depart, par_uid, avec_llm)
        produit = appliquer(operations, portee, depart, depart, auteur)[0]
    except (ErreurCible, ValueError, PlafondDepasse) as erreur:
        reponse["erreur"] = str(erreur)
        return reponse
    if portee.noeud == "article_additionnel":
        nouvel = next(a for a in produit.articles if a.numero is None)
        entree, cible = [], nouvel
    else:
        visee = trouver_article(depart, portee.article)
        cible = next(a for a in produit.articles if a.id == visee.id)
        entree = sans_mentions(mots_officiels(lignes_de(visee))) if not visee.supprime else []
    sortie = article_sortie(cible, numeros(produit))
    apres, drapeaux = sans_mentions(sortie["mots"], sortie["amendement"])
    reponse["article_produit"] = sortie["numero"]
    reponse["effet"] = segments_diff(entree, apres, drapeaux)
    reponse["ecart"] = segments_ecarts(apres, article["officiel"], drapeaux, classer_ecarts(apres, drapeaux, article["officiel"]))
    return reponse


def analyse_detaillee(brut, depart, avec_llm=False):
    """Pour chaque instruction : qui l'a comprise (grammaire, LLM ou personne), ses opérations, ce qu'elles visent."""
    try:
        portee = analyser_portee(brut["article_vise"])
    except ValueError as erreur:
        return [{"texte": brut["article_vise"] or "", "source": "non reconnu", "operations": [], "erreur": str(erreur)}]
    alineas = nettoyer_dispositif(brut["dispositif"] or "")
    if portee.noeud == "article_additionnel":
        instructions = [Instruction(None, alineas[0], alineas[1:])] if alineas else []
    else:
        instructions = decouper_instructions(alineas)
    article = None if portee.noeud == "titre" else trouver_article(depart, portee.article)
    contexte = "\n".join(texte_de(a) for a in article.alineas) if article else None
    # Le LLM ne sert qu'aux amendements adoptés, comme dans V2-gram+llm.
    llm = avec_llm and brut["sort"] == "Adopté"
    sortie, precedente, derniere = [], None, None
    for instruction in instructions:
        operations, source, confiance = analyser_par_grammaire(instruction, portee, precedente, derniere), "grammaire", None
        if operations is None and llm:
            try:
                operations, confiance = analyser_par_llm(instruction, portee, contexte, precedente, LLM_CACHE_SEUL)
                source = "LLM"
            except PlafondDepasse:
                operations = None
        if not operations:
            operations, source = [], "non reconnu"
        else:
            precedente, derniere = operations[-1].cible, operations[-1]
        sortie.append({"texte": "\n".join([instruction.chapeau, *instruction.citations]), "source": source,
                       "confiance": confiance,
                       "operations": [_decrire_operation(op, portee, depart, bool(brut["parent_uid"])) for op in operations]})
    return sortie


def _decrire_operation(op, portee, depart, sous_amendement):
    cible = op.cible
    decrit = {"verbe": op.verbe, "alinea": cible.alinea, "jusqua": cible.jusqua, "phrase": cible.phrase, "noeud": cible.noeud,
              "position": cible.position, "mots": cible.mots, "occurrence": cible.occurrence,
              "anciens_mots": op.anciens_mots, "texte": op.texte, "trouve": None, "erreur": None, "note": None}
    if sous_amendement:
        decrit["note"] = "Vise le texte de l'amendement parent, pas celui de l'article."
    elif portee.noeud == "article_additionnel":
        decrit["note"] = f"Nouvel article, {'avant' if portee.avant_apres == 'avant' else 'après'} l'article {portee.article}."
    else:
        try:
            place = resoudre(op, portee, depart, depart)
            decrit["trouve"] = _extrait(place, depart)
        except ErreurCible as erreur:
            decrit["erreur"] = str(erreur)
    return decrit


def _extrait(place, depart):
    """Où la cible a été trouvée dans le texte d'entrée : le rang de l'alinéa et les mots visés."""
    article = article_par_id(depart, place.article_id)
    if not place.alinea_ids:
        return {"lieu": "tout l'article", "extrait": None}
    rangs = [i for i, a in enumerate(article.alineas, 1) if a.id in place.alinea_ids]
    lieu = f"alinéa {rangs[0]}" if len(rangs) == 1 else f"alinéas {rangs[0]} à {rangs[-1]}"
    alinea = next(a for a in article.alineas if a.id == place.alinea_ids[0])
    if place.debut is None or len(rangs) > 1:
        return {"lieu": lieu, "extrait": None}
    if place.debut == 0 and place.fin == len(alinea.jetons):
        return {"lieu": lieu, "extrait": None}
    return {"lieu": lieu, "extrait": "".join(j.texte for j in alinea.jetons[place.debut:place.fin]).strip()}


def _operations(etape, brut, depart, par_uid, avec_llm):
    """Opérations à exécuter : celles de l'amendement, ou celles du parent modifiées par le sous-amendement choisi."""
    parent = par_uid.get(brut["parent_uid"]) if brut["parent_uid"] else None
    principal = parent or brut
    analyse = _analyser(principal, depart, avec_llm)
    if parent is not None:
        sous = [brut]
    else:
        sous = [x for x in etape["amendements"] if x["parent_uid"] == brut["uid"] and x["sort"] == "Adopté"]
    if analyse.erreurs:
        raise ValueError("; ".join(analyse.erreurs))
    avec_sous = [(x["uid"], _analyser(x, depart, avec_llm).operations) for x in sous]
    return (integrer_sous_amendements(analyse.operations, analyse.portee, principal["uid"], avec_sous), analyse.portee,
            principal["uid"])


def _analyser(brut, depart, avec_llm):
    amendement = AmendementEval(**{k: v for k, v in brut.items() if k in CHAMPS})
    if not avec_llm or brut["sort"] != "Adopté":
        return analyser_amendement(amendement)
    portee = analyser_portee(brut["article_vise"])
    article = trouver_article(depart, portee.article)
    texte = "\n".join(texte_de(a) for a in article.alineas) if article else None
    return analyser_amendement(amendement, texte, LLM_CACHE_SEUL)


def _amendements(article, par_uid, statuts):
    """Les amendements adoptés qui visent l'article, puis les autres amendements déposés sur le même article."""
    uids = list(article["uids"])
    visee = cle_numero(article["numero"])
    for uid, x in par_uid.items():
        if uid not in uids and _meme_article(x, visee):
            uids.append(uid)
    sortie = []
    for uid in uids:
        x, s = par_uid.get(uid), statuts.get(uid, {})
        if x is not None:
            sortie.append({"uid": uid, "numero": x["numero"], "sort": x["sort"], "etat": s.get("etat"),
                           "parent": bool(x["parent_uid"]), "tri": x["tri_amendement"] or "", "dispositif": x["dispositif"]})
    return sorted(sortie, key=lambda x: (x["sort"] != "Adopté", x["tri"]))


def _meme_article(amendement, visee):
    try:
        portee = analyser_portee(amendement["article_vise"])
    except ValueError:
        return False
    return portee.noeud == "article" and cle_numero(portee.article) == visee
