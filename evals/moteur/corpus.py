"""Lot 3 : construit le corpus d'évaluation du moteur et le fige par un manifeste.

Une étape = (texte d'entrée, amendements déposés dessus, texte de sortie officiel), pour la première lecture à
l'Assemblée : commission (texte déposé → texte de la commission) et séance (texte de la commission → texte adopté).
Sources : CLAIR (amendements, actes du dossier) et, pour les textes, l'open data de l'AN (téléchargé une fois, gardé
dans donnees/textes/). textes_articles n'est pas utilisé : sur 627 articles de commission comparés, il compte des
intitulés (« TITRE III ») et des lignes de points comme alinéas, ce qui décale les numéros, ou tronque des articles.

Le découpage développement/test se fait par dossier, par hachage : stable, et les deux étapes d'un dossier restent
ensemble. Le test ne sert qu'à la comparaison finale.

Usage : uv run python -m evals.moteur.corpus
        uv run python -m evals.moteur.corpus --textes   (relit les textes déjà téléchargés, sans toucher à la base)
"""

import argparse
import hashlib
import html
import json
import os
import re
import time
from collections import Counter
from datetime import date
from pathlib import Path

import psycopg
import requests

ICI = Path(__file__).parent
DONNEES = ICI / "donnees"
CORPUS = DONNEES / "corpus.jsonl"
MANIFESTE = ICI / "corpus_manifeste.json"
URL_TEXTE = "https://www.assemblee-nationale.fr/dyn/opendata/{ref}.html"
PART_DEV = 70
SEL = "moteur-v1"
# Dossiers vus pendant la construction du moteur (exemples, fixtures, tests) : ni développement ni test.
DOSSIERS_EXCLUS = {"DLR5L17N51328": "PPL n° 822, tombée en test alors qu'elle a servi à construire le moteur"}

SQL_AMENDEMENTS = """
    SELECT uid, numero, texte_ref, article_vise, alinea, dispositif, amendement_parent_ref AS parent_uid, tri_amendement,
           to_char(date_sort, 'YYYY-MM-DD"T"HH24:MI:SS') AS date_sort, sort,
           id_discussion_identique AS id_identique, numero_ordre
    FROM amendements
    WHERE chambre = 'assemblee' AND legislature = 17 AND texte_ref = ANY(%s)
"""
PARAGRAPHE = re.compile(r'<p\b[^>]*class="?assnat(\w+)[^>]*>(.*?)</p>', re.S)
MENTION = re.compile(r"\((non modifié|supprimés?|suppression maintenue|suppression conforme|conforme)\)\.?", re.I)
STRUCTURE = re.compile(r"^\d(Partie|Livre|Titre|Chapitre|Section|Sous|Paragraphe)")
# Le rapport annexé d'une loi de programmation : un titre seul (« RAPPORT ANNEXÉ »), puis tout ce qui suit.
TITRE_ANNEXE = re.compile(r"<p\b[^>]*>\s*(?:<[^>]+>\s*)*rapports?\s+annex[ée]s?\s*(?:<[^>]+>\s*)*</p>", re.I)
BLOC_ANNEXE = re.compile(r"<table\b.*?</table>|<p\b[^>]*>.*?</p>", re.S | re.I)
CELLULE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
APPROUVE = re.compile(r"rapport annexé.*approuvé|approuvé.*rapport annexé", re.I)   # « Est approuvé le rapport annexé »
SUFFIXES = ["", "bis", "ter", "quater", "quinquies", "sexies", "septies", "octies", "nonies", "decies"]


def split_de(dossier):
    if dossier in DOSSIERS_EXCLUS:
        return "exclu"
    return "dev" if int(hashlib.sha256(f"{SEL}:{dossier}".encode()).hexdigest(), 16) % 100 < PART_DEV else "test"


def texte_paragraphe(fragment):
    texte = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    return re.sub(r"[ \t\r\n]+", " ", texte.replace("\xa0", "\xa0")).strip()


def numeros_d_en_tete(en_tete):
    """« Article 1er » → [PREMIER] ; « Articles 2 à 6 (nouveaux) (Supprimés) » → [2…6] ; « Articles 8 ter et 8 quater »."""
    corps = re.sub(r"\(.*?\)", "", en_tete)
    corps = re.sub(r"^Articles?\s+", "", corps.strip(), flags=re.I).strip().upper().replace("\xa0", " ")
    corps = re.sub(r"\s+", " ", corps)
    plage = re.match(r"^(\d+)(?: ([A-Z]+))? À (\d+)(?: ([A-Z]+))?$", corps)
    if plage:
        d, sd, f, sf = plage.groups()
        if d == f and sd and sf and sd.lower() in SUFFIXES and sf.lower() in SUFFIXES:
            rangs = range(SUFFIXES.index(sd.lower()), SUFFIXES.index(sf.lower()) + 1)
            return [f"{d} {SUFFIXES[r].upper()}" for r in rangs]
        if not sd and not sf:
            return [str(n) for n in range(int(d), int(f) + 1)]
    morceaux = [m.strip() for m in re.split(r",| ET ", corps) if m.strip()]
    return ["PREMIER" if re.fullmatch(r"1 ?ER|PREMIER", m) else m for m in morceaux]


def lire_texte_html(contenu):
    """HTML d'un texte de l'AN → [{numero, lignes, supprime}], dans l'ordre. Un assnatLoiTexte = un alinéa.

    Le rapport annexé n'est pas un article, mais les amendements le visent comme les alinéas de l'article qui
    l'approuve (« ART. PREMIER, alinéa 179 ») : cet article reçoit donc les alinéas du rapport à la place des siens.
    """
    annexe = list(TITRE_ANNEXE.finditer(contenu))
    corps = contenu[:annexe[-1].start()] if annexe else contenu
    articles = _lire_articles(corps)
    if annexe:
        porteur = next((a for a in articles if any(APPROUVE.search(ligne) for ligne in a["lignes"])), None)
        if porteur is not None:
            porteur["lignes"] = lire_rapport_annexe(contenu[annexe[-1].end():])
    return articles


def lire_rapport_annexe(contenu):
    """Alinéas du rapport annexé, numérotés comme les cite l'AN : un paragraphe par alinéa, intertitres compris, et un
    tableau entier pour un seul alinéa (ses cellules jointes par « | »). Vérifié sur les amendements des lois de
    programmation Mayotte et militaire : quand les mots cités se trouvent une seule fois, c'est dans l'alinéa cité."""
    alineas = []
    for bloc in BLOC_ANNEXE.findall(contenu):
        if bloc[:6].lower() == "<table":
            texte = " | ".join(c for c in map(texte_paragraphe, CELLULE.findall(bloc)) if c)
        else:
            texte = texte_paragraphe(bloc)
        if texte:
            alineas.append(texte)
    return alineas


def _lire_articles(contenu):
    articles = []
    courants = []
    for classe, fragment in PARAGRAPHE.findall(contenu):
        texte = texte_paragraphe(fragment)
        if not texte:
            continue
        if classe == "9ArticleNum":
            courants = [{"numero": n, "lignes": [], "supprime": "supprim" in texte.lower()}
                        for n in numeros_d_en_tete(texte)]
            articles += courants
        elif classe == "Mentionsousarticle" and "supprim" in texte.lower():
            for article in courants:
                article["supprime"] = True
        elif classe in ("LoiTexte", "Texte") and courants:
            if MENTION.fullmatch(texte):
                if "suppr" in texte.lower():
                    courants[-1]["supprime"] = True
            else:
                courants[-1]["lignes"].append(texte)
        elif STRUCTURE.match(classe):
            # Un intitulé cité (« « Chapitre VIII ») appartient à l'article ; sinon il sépare deux articles du texte.
            if texte.startswith("«") and courants:
                courants[-1]["lignes"].append(texte)
            else:
                courants = []
    return articles


def telecharger(ref):
    chemin = DONNEES / "textes" / f"{ref}.html"
    if not chemin.exists():
        chemin.parent.mkdir(parents=True, exist_ok=True)
        reponse = requests.get(URL_TEXTE.format(ref=ref), timeout=60)
        time.sleep(0.3)
        if reponse.status_code != 200:
            return None
        chemin.write_text(reponse.text, encoding="utf-8")
    return chemin.read_text(encoding="utf-8")


def actes(noeud):
    """Parcourt tous les actes d'un dossier, sous-actes compris."""
    if isinstance(noeud, list):
        for x in noeud:
            yield from actes(x)
    elif isinstance(noeud, dict):
        if "codeActe" in noeud:
            yield noeud
        yield from actes((noeud.get("actesLegislatifs") or {}).get("acteLegislatif") or [])


def etapes_du_dossier(source):
    """Textes de la première lecture AN : (déposé, commission, adopté), None quand l'acte manque."""
    depose = commission = adopte = None
    for acte in actes((source.get("actesLegislatifs") or {}).get("acteLegislatif") or []):
        code = acte["codeActe"]
        if code == "AN1-DEPOT":
            depose = acte.get("texteAssocie")
        elif code == "AN1-COM-FOND-RAPPORT" and acte.get("texteAdopte"):
            commission = acte["texteAdopte"]
        elif code == "AN1-DEBATS-DEC":
            associes = (acte.get("textesAssocies") or {}).get("texteAssocie") or []
            associes = associes if isinstance(associes, list) else [associes]
            adopte = next((t["refTexteAssocie"] for t in associes if t.get("typeTexte") == "BTA"), adopte)
    return depose, commission, adopte


def est_finances(titre):
    return bool(re.search(r"finances|financement de la sécurité sociale", titre or "", re.I))


def articles_du_texte(ref):
    contenu = telecharger(ref)
    return lire_texte_html(contenu) if contenu else []


def construire(conn):
    exclus, etapes = Counter(), []
    dossiers = conn.execute(
        "SELECT uid, titre, source_data FROM dossiers_legislatifs WHERE legislature = 17 AND source_data IS NOT NULL"
    ).fetchall()
    for uid, titre, source in dossiers:
        depose, commission, adopte = etapes_du_dossier(source)
        for nom, entree, sortie in (("commission", depose, commission), ("seance", commission, adopte)):
            if not entree or not sortie:
                exclus["acte manquant"] += 1
                continue
            lignes = conn.execute(SQL_AMENDEMENTS, ([entree],))
            noms = [c.name for c in lignes.description]
            amendements = [dict(zip(noms, r)) for r in lignes.fetchall()]
            if not any(a["sort"] == "Adopté" for a in amendements):
                exclus["aucun amendement adopté"] += 1
                continue
            articles_entree, articles_sortie = articles_du_texte(entree), articles_du_texte(sortie)
            if not articles_entree:
                exclus["texte d'entrée introuvable"] += 1
                continue
            if not articles_sortie:
                exclus["texte de sortie introuvable"] += 1
                continue
            etapes.append({
                "id": f"{uid}:{nom}", "dossier": uid, "titre": titre, "etape": nom, "split": split_de(uid),
                "finances": est_finances(titre), "entree": {"ref": entree, "articles": articles_entree},
                "sortie": {"ref": sortie, "articles": articles_sortie}, "amendements": amendements,
            })
    return etapes, exclus


def relire_textes():
    """Le corpus figé, aux textes relus depuis le HTML déjà téléchargé : mêmes étapes, mêmes amendements, sans la base."""
    etapes, exclus = [], json.loads(MANIFESTE.read_text(encoding="utf-8"))["exclus"]
    with CORPUS.open(encoding="utf-8") as f:
        for ligne in f:
            etape = json.loads(ligne)
            for cote in ("entree", "sortie"):
                etape[cote]["articles"] = articles_du_texte(etape[cote]["ref"])
            etapes.append(etape)
    return etapes, exclus


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--textes", action="store_true", help="relire les textes téléchargés, sans la base")
    args = parser.parse_args()
    DONNEES.mkdir(exist_ok=True)
    if args.textes:
        etapes, exclus = relire_textes()
    else:
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
            etapes, exclus = construire(conn)
    etapes.sort(key=lambda e: e["id"])
    corps = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in etapes)
    CORPUS.write_text(corps, encoding="utf-8")
    compte = Counter((e["etape"], e["split"]) for e in etapes)
    MANIFESTE.write_text(json.dumps({
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
    print(MANIFESTE.read_text(encoding="utf-8")[:400])


if __name__ == "__main__":
    main()
