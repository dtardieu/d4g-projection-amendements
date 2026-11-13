"""Adaptateur de l'API Tricoteuses (https://parlement.tricoteuses.fr) pour le moteur de projection.

Seul ce module connaît les noms de champs de Tricoteuses. Il produit ce que le moteur attend :
une Version (texte découpé en articles et alinéas) et des AmendementBrut (plus leur auteur).

Pièges traités (voir learnings/categories/tricoteuses.md) :
- pas de champ numéro d'article : on le lit dans le titre
  (« Article 5 bis (nouveau) » → « 5 BIS ») ;
  l'identifiant (`article-1`) ne suit pas toujours le numéro officiel (« Article unique ») ;
- une mention seule (« (Non modifié) », « (Supprimé) ») est rangée comme un alinéa : on l'écarte,
  sinon tous les numéros d'alinéas de l'article sont décalés d'un cran.
"""

import dataclasses
import html
import re

import requests
from moteur.analyse import AmendementBrut
from moteur.texte import creer_version

URL = "https://parlement.tricoteuses.fr"
DELAI = 120  # secondes : un texte long pèse plusieurs Mo
PAR_PAGE = 100
MENTION = re.compile(
    r"\((non modifié|supprimés?|suppression maintenue|suppression conforme|conforme)\)\.?", re.I
)
PLAGE = re.compile(r"^(\d+) À (\d+)$")


@dataclasses.dataclass
class Amendement(AmendementBrut):
    """AmendementBrut et ce que l'interface affiche en plus."""

    auteur: str | None = None


class TexteIntrouvable(Exception):
    """Le texte demandé n'existe pas chez Tricoteuses, ou n'a aucun article."""


def get(chemin, params=None):
    """GET sur l'API, renvoie la réponse (lève requests.HTTPError si le statut n'est pas 2xx)."""
    reponse = requests.get(f"{URL}{chemin}", params=params, timeout=DELAI)
    reponse.raise_for_status()
    return reponse


def numeros_du_titre(titre):
    """Numéros d'un titre d'article.

    « Article 1er » → [PREMIER] ; « Article 5 bis (nouveau) » → [5 BIS] ;
    « Articles 2 à 4 » → [2, 3, 4].
    """
    corps = re.sub(r"\(.*?\)", "", titre).replace("\xa0", " ")
    corps = re.sub(r"^\s*Articles?\s+", "", corps, flags=re.I)
    corps = re.sub(r"\s+", " ", corps).strip().upper()
    plage = PLAGE.match(corps)
    if plage:
        return [str(n) for n in range(int(plage[1]), int(plage[2]) + 1)]
    morceaux = [m.strip() for m in re.split(r",| ET ", corps) if m.strip()]
    return ["PREMIER" if re.fullmatch(r"1 ?ER|PREMIER", m) else m for m in morceaux]


def version_depuis_document(document):
    """Réponse de /v2/documents/{uid} → Version du moteur (articles dans l'ordre du texte)."""
    alineas = {}
    for alinea in document.get("alineas") or []:
        alineas.setdefault(alinea["articleRefHash"], []).append(alinea)
    articles, supprimes = [], set()
    subdivisions = sorted(document.get("subdivisions") or [], key=lambda s: s.get("sequence") or 0)
    for subdivision in subdivisions:
        if subdivision["type"] != "Article":
            continue
        lignes, supprime = [], "supprim" in subdivision["titre"].lower()
        for alinea in sorted(alineas.get(subdivision["hash"], []), key=lambda a: a["numero"]):
            texte = (alinea["texte"] or "").strip()
            if MENTION.fullmatch(texte):
                supprime = supprime or "suppr" in texte.lower()
            elif texte:
                lignes.append(texte)
        for numero in numeros_du_titre(subdivision["titre"]):
            articles.append((numero, "\n".join(lignes)))
            if supprime:
                supprimes.add(numero)
    if not articles:
        raise TexteIntrouvable(f"{document.get('uid')} : aucun article")
    version = creer_version(articles)
    return dataclasses.replace(
        version,
        articles=tuple(
            dataclasses.replace(a, supprime=a.numero in supprimes) for a in version.articles
        ),
    )


def amendement_depuis(donnees):
    """Un amendement de /amendements/json → Amendement."""
    identique = donnees.get("idDiscussionIdentique")
    date_sort = donnees.get("dateSort")
    return Amendement(
        uid=donnees["uid"],
        numero=donnees.get("numeroLong") or "",
        texte_ref=donnees.get("documentRefUid") or "",
        article_vise=donnees.get("divisionArticleDesignationCourte"),
        alinea=donnees.get("alineaDesignation"),
        # Le HTML garde un <p> par alinéa ; la version texte coupe les lignes à 80 caractères.
        dispositif=donnees.get("dispositifHtml") or donnees.get("dispositif") or "",
        parent_uid=donnees.get("amendementParentRefUid"),
        tri_amendement=donnees.get("triAmendement"),
        # Seul l'ordre compte : on garde l'heure UTC, sans millisecondes ni fuseau.
        date_sort=date_sort[:19] if date_sort else None,
        sort=donnees.get("sortAmendement"),
        id_identique=str(identique) if identique is not None else None,
        # Les noms arrivent parfois avec des entités HTML (« Barth&#232;s »).
        auteur=html.unescape(donnees.get("signatairesLibelle") or "") or None,
    )


def texte(uid):
    """(titre, Version) du texte uid."""
    try:
        reponse = get(f"/v2/documents/{uid}")
    except requests.HTTPError as erreur:
        if erreur.response is not None and erreur.response.status_code == 404:
            raise TexteIntrouvable(uid) from erreur
        raise
    document = reponse.json().get("data") or {}
    return document.get("titrePrincipal") or uid, version_depuis_document(document)


def amendements_du_texte(uid):
    """Tous les amendements déposés sur le texte, toutes pages lues."""
    amendements, page, pages = [], 1, 1
    while page <= pages:
        params = {"documentRefUid": uid, "page": page, "perPage": PAR_PAGE}
        reponse = get("/amendements/json", params)
        amendements += [amendement_depuis(a) for a in reponse.json().get("data") or []]
        pages = int(reponse.headers.get("total-page") or 1)
        page += 1
    return amendements
