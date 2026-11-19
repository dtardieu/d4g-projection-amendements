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
TIMEOUT = 120  # secondes : un texte long pèse plusieurs Mo
PER_PAGE = 100
MENTION = re.compile(
    r"\((non modifié|supprimés?|suppression maintenue|suppression conforme|conforme)\)\.?", re.I
)
RANGE = re.compile(r"^(\d+) À (\d+)$")


@dataclasses.dataclass
class Amendment(AmendementBrut):
    """AmendementBrut et ce que l'interface affiche en plus."""

    auteur: str | None = None
    numero_ordre: int | None = None


@dataclasses.dataclass(frozen=True)
class Article:
    """Un article tel que le donne Tricoteuses, avant le moteur."""

    numero: str  # « PREMIER », « 5 BIS »
    lignes: tuple[str, ...]  # un alinéa par élément, mentions seules retirées
    supprime: bool = False


@dataclasses.dataclass(frozen=True)
class Text:
    uid: str
    titre: str
    articles: tuple[Article, ...]


@dataclasses.dataclass(frozen=True)
class FirstReading:
    """Textes de la première lecture à l'Assemblée d'un dossier ; None quand l'acte manque."""

    dossier: str
    depose: str | None = None
    commission: str | None = None
    adopte: str | None = None  # le BTA, jamais le TAP


class TextNotFound(Exception):
    """Le texte demandé n'existe pas chez Tricoteuses, ou n'a aucun article."""


def get(chemin, params=None):
    """GET sur l'API, renvoie la réponse (lève requests.HTTPError si le statut n'est pas 2xx)."""
    reponse = requests.get(f"{URL}{chemin}", params=params, timeout=TIMEOUT)
    reponse.raise_for_status()
    return reponse


def numbers_from_title(titre):
    """Numéros d'un titre d'article.

    « Article 1er » → [PREMIER] ; « Article 5 bis (nouveau) » → [5 BIS] ;
    « Articles 2 à 4 » → [2, 3, 4].
    """
    corps = re.sub(r"\(.*?\)", "", titre).replace("\xa0", " ")
    corps = re.sub(r"^\s*Articles?\s+", "", corps, flags=re.I)
    corps = re.sub(r"\s+", " ", corps).strip().upper()
    range_ = RANGE.match(corps)
    if range_:
        return [str(n) for n in range(int(range_[1]), int(range_[2]) + 1)]
    morceaux = [m.strip() for m in re.split(r",| ET ", corps) if m.strip()]
    return ["PREMIER" if re.fullmatch(r"1 ?ER|PREMIER", m) else m for m in morceaux]


def articles_from_document(document):
    """Réponse de /documents/{uid} (avec subdivisions et alinéas) → les Article, dans l'ordre du texte."""
    alineas = {}
    for alinea in document.get("alineas") or []:
        alineas.setdefault(alinea["articleRefHash"], []).append(alinea)
    articles = []
    subdivisions = sorted(document.get("subdivisions") or [], key=lambda s: s.get("sequence") or 0)
    for subdivision in subdivisions:
        if subdivision["type"] != "Article":
            continue
        lignes, supprime = [], "supprim" in subdivision["titre"].lower()
        for alinea in sorted(alineas.get(subdivision["hash"], []), key=lambda a: a["numero"]):
            # Un tableau arrive aplati en un seul alinéa, cellules séparées par des tabulations et lignes par des
            # retours : sans ce remplacement, chaque ligne du tableau deviendrait un faux alinéa.
            texte = re.sub(r"[ \t]*[\t\n][ \t\n]*", " ", alinea["texte"] or "").strip()
            if MENTION.fullmatch(texte):
                supprime = supprime or "suppr" in texte.lower()
            elif texte:
                lignes.append(texte)
        for numero in numbers_from_title(subdivision["titre"]):
            articles.append(Article(numero, tuple(lignes), supprime))
    if not articles:
        raise TextNotFound(f"{document.get('uid')} : aucun article")
    return tuple(articles)


def version_from_document(document):
    """Réponse de /documents/{uid} (avec subdivisions et alinéas) → Version du moteur (articles dans l'ordre du texte)."""
    articles = articles_from_document(document)
    deleted = {a.numero for a in articles if a.supprime}
    version = creer_version([(a.numero, "\n".join(a.lignes)) for a in articles])
    return dataclasses.replace(
        version,
        articles=tuple(
            dataclasses.replace(a, supprime=a.numero in deleted) for a in version.articles
        ),
    )


def amendment_from(donnees):
    """Un amendement de /amendements/json → Amendement."""
    identical = donnees.get("idDiscussionIdentique")
    date_sort = donnees.get("dateSort")
    return Amendment(
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
        id_identique=str(identical) if identical is not None else None,
        # Les noms arrivent parfois avec des entités HTML (« Barth&#232;s »).
        auteur=html.unescape(donnees.get("signatairesLibelle") or "") or None,
        # Arrive en texte ou en entier selon les amendements.
        numero_ordre=int(ordre) if (ordre := str(donnees.get("numeroOrdreDepot") or "")).isdigit() else None,
    )


def document(uid):
    """Réponse brute de /documents/{uid}, avec subdivisions et alinéas."""
    try:
        reponse = get(f"/documents/{uid}", {"include": "subdivisions,alineas"})
    except requests.HTTPError as erreur:
        if erreur.response is not None and erreur.response.status_code == 404:
            raise TextNotFound(uid) from erreur
        raise
    return reponse.json().get("data") or {}


def document_title(donnees):
    return donnees.get("titrePrincipal") or donnees.get("uid")


def lire_texte(uid):
    """Le Texte uid, découpé en articles (lève TexteIntrouvable s'il n'existe pas ou n'a aucun article)."""
    donnees = document(uid)
    return Text(uid, document_title(donnees), articles_from_document(donnees))


def texte(uid):
    """(titre, Version) du texte uid."""
    donnees = document(uid)
    return document_title(donnees), version_from_document(donnees)


def all_pages(chemin, params):
    """Tous les éléments d'une liste paginée de l'API."""
    page, pages = 1, 1
    while page <= pages:
        reponse = get(chemin, {**params, "page": page, "perPage": PER_PAGE})
        yield from reponse.json().get("data") or []
        pages = int(reponse.headers.get("total-page") or 1)
        page += 1


def text_amendments(uid):
    """Tous les amendements déposés sur le texte, toutes pages lues."""
    return [amendment_from(a) for a in all_pages("/amendements/json", {"documentRefUid": uid})]


def first_reading_texts(legislature):
    """Les PremiereLecture des dossiers de la législature, triées par dossier.

    Pour la décision en séance, `texteAssocieRefUid` donne le texte adopté (BTA) ; `documentRefUid` donnerait le TAP.
    """
    dossiers = {}
    acts = (("depose", "AN1-DEPOT", "texteAssocieRefUid"),
             ("commission", "AN1-COM-FOND-RAPPORT", "texteAdopteRefUid"),
             ("adopte", "AN1-DEBATS-DEC", "texteAssocieRefUid"))
    for cle, code, field in acts:
        for act in all_pages("/actesLegislatifs/json", {"codeActe": code, "legislature": legislature}):
            ref = act.get(field)
            if act.get("dossierRefUid") and ref and (cle != "adopte" or "BTA" in ref):
                dossiers.setdefault(act["dossierRefUid"], {})[cle] = ref
    return [FirstReading(dossier, **textes) for dossier, textes in sorted(dossiers.items())]
