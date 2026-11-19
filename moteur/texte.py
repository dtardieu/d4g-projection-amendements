"""Le texte comme arbre à identifiants stables : article → alinéa → jeton.

Chaque nœud reçoit un identifiant à sa création et ne le perd plus ; la numérotation (« alinéa 4 ») est
calculée à la demande. Une Version est immuable : appliquer une opération en crée une autre, qui partage les
nœuds inchangés avec sa parente. Son empreinte ne dépend que du contenu, donc deux amendements de même effet
donnent la même empreinte.

La phrase n'est pas un nœud mais une vue sur les jetons : ajouter un point suffit à changer le découpage.
"""

import difflib
import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace

JETON = re.compile(r"\s+|[\w\u0300-\u036f]+|[^\w\s]")
EQUIVALENTS = str.maketrans({"’": "'", "‑": "-", "–": "-", "—": "-", "−": "-", " ": " ", " ": " "})

PONCTUATION_COLLEE_A_GAUCHE = {",", ".", ")", "]", "…"}
PONCTUATION_COLLEE_A_DROITE = {"(", "[", "'", "’", "-", "‑"}
TERMINAUX = {".", ";", ":", ",", "»", "!", "?", "…"}
ABREVIATIONS = {"art", "n", "no", "chap", "sect", "al", "cf", "m", "mme", "mm", "p", "pp", "vol", "c", "ex"}
CHIFFRE_ROMAIN = re.compile(r"^[IVXLCDM]+$")


@dataclass(frozen=True)
class Jeton:
    id: str
    texte: str
    origine: str          # amendement qui l'a écrit (ou le texte de départ) : la provenance vient de là


@dataclass(frozen=True)
class Alinea:
    id: str
    jetons: tuple
    origine: str


@dataclass(frozen=True)
class Article:
    id: str
    numero: str | None    # numéro dans le texte de départ ; None pour un article additionnel
    alineas: tuple
    origine: str
    supprime: bool = False
    ancre: str | None = None   # article après lequel un article additionnel a été inséré


@dataclass(frozen=True)
class Version:
    articles: tuple
    titre: Article | None = None


class Identifiants:
    """Fabrique d'identifiants d'un auteur : déterministe, donc les exécutions sont reproductibles."""

    def __init__(self, origine):
        self.origine, self.n = origine, 0

    def nouveau(self):
        self.n += 1
        return f"{self.origine}/{self.n}"


def normaliser(texte):
    """Efface les variantes typographiques (apostrophes, tirets, espaces) pour comparer deux textes."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", texte).translate(EQUIVALENTS)).strip()


def est_blanc(jeton):
    return jeton.texte.isspace()


def decouper(texte):
    """Texte → mots, ponctuation et espaces. Certaines sources de l'AN décomposent les accents (e + ◌́) : on les recompose,
    sinon « créée » serait coupé en morceaux et ne ressemblerait plus au même mot écrit ailleurs avec un accent composé.
    """
    return JETON.findall(unicodedata.normalize("NFC", texte))


def creer_jetons(texte, ids):
    return tuple(Jeton(ids.nouveau(), t, ids.origine) for t in decouper(texte))


def creer_alinea(texte, ids):
    return Alinea(ids.nouveau(), creer_jetons(texte, ids), ids.origine)


def creer_article(numero, lignes, ids, ancre=None):
    return Article(ids.nouveau(), numero, tuple(creer_alinea(l, ids) for l in lignes), ids.origine, ancre=ancre)


def creer_version(articles, titre=None, origine="T0"):
    """articles : liste de (numero, contenu), un alinéa par ligne de contenu, comme textes_articles."""
    ids = Identifiants(origine)
    return Version(
        tuple(creer_article(numero, [l for l in contenu.split("\n") if l.strip()], ids) for numero, contenu in articles),
        creer_article("TITRE", [titre], ids) if titre else None,
    )


def texte_de(alinea):
    return "".join(j.texte for j in alinea.jetons)


def lignes_de(article):
    return [texte_de(a) for a in article.alineas]


def cle_numero(numero):
    """« ART. PREMIER », « 1 », « 1ER » → même clé ; un numéro collé à son intitulé (« 34FORFAITISER… ») → « 34 »."""
    numero = re.sub(r"^(PREMIER|1 ?ER)\b", "1", (numero or "").strip().upper())
    collé = re.match(r"^(\d+)[A-ZÉÈ]{3,}", numero)
    if collé and not re.match(r"^\d+\s", numero):
        return collé.group(1)
    return re.sub(r"\s+", " ", numero)


def trouver_article(version, numero):
    cle = cle_numero(numero)
    return next((a for a in version.articles if a.numero is not None and cle_numero(a.numero) == cle), None)


def article_par_id(version, article_id):
    if version.titre is not None and version.titre.id == article_id:
        return version.titre
    return next((a for a in version.articles if a.id == article_id), None)


def remplacer_article(version, article):
    if version.titre is not None and version.titre.id == article.id:
        return replace(version, titre=article)
    return replace(version, articles=tuple(article if a.id == article.id else a for a in version.articles))


SUFFIXES = ("bis", "ter", "quater", "quinquies", "sexies", "septies", "octies", "nonies", "decies")


def numeros(version):
    """Vue de la numérotation : {id d'article → numéro affiché}, jamais stockée.

    Un article unique qui reçoit des articles additionnels devient l'article 1er, et les suivants 2, 3… ;
    ailleurs un additionnel prend le numéro de son voisin plus un suffixe (6 bis, 6 ter).
    """
    view = {}
    if len(version.articles) > 1 and version.articles[0].numero == "UNIQUE":
        return {a.id: ("PREMIER" if i == 0 else str(i + 1)) for i, a in enumerate(version.articles)}
    base, rang = None, 0
    for article in version.articles:
        if article.numero is not None:
            base, rang = article.numero, 0
            view[article.id] = article.numero
        else:
            suffixe = SUFFIXES[rang] if rang < len(SUFFIXES) else f"#{rang + 1}"
            view[article.id] = f"{base} {suffixe}".upper() if base else suffixe.upper()
            rang += 1
    return view


def rendre(version):
    """Le texte d'une version : [(numéro, [lignes])], un article supprimé n'ayant pas de ligne."""
    view = numeros(version)
    return [(view[a.id], [] if a.supprime else lignes_de(a)) for a in version.articles]


def empreinte(version):
    """Hachage du contenu seul : mêmes mots aux mêmes places donnent la même empreinte."""
    contenu = [("TITRE", _lignes_memorisees(version.titre)) if version.titre else None]
    contenu += [(a.numero or "", a.supprime, _lignes_memorisees(a)) for a in version.articles]
    return hashlib.sha256(repr(contenu).encode("utf-8")).hexdigest()


_LIGNES = {}


def _lignes_memorisees(article):
    """lignes_de, par identité d'objet : deux versions partagent leurs articles inchangés, seuls ceux-ci sont relus."""
    trouve = _LIGNES.get(id(article))
    if trouve is None or trouve[0] is not article:
        if len(_LIGNES) > 20000:
            _LIGNES.clear()
        trouve = _LIGNES[id(article)] = (article, lignes_de(article))
    return trouve[1]


def provenance(alinea):
    """Regroupe les jetons par auteur successif : [(origine, texte)], à la manière de git blame."""
    morceaux = []
    for jeton in alinea.jetons:
        if morceaux and (morceaux[-1][0] == jeton.origine or est_blanc(jeton)):
            morceaux[-1][1].append(jeton.texte)
        else:
            morceaux.append((jeton.origine, [jeton.texte]))
    return [(origine, "".join(t).strip()) for origine, t in morceaux]


def phrases(jetons):
    """Découpe des jetons en phrases : [(début, fin)], fin exclue.

    Coupe sur . ! ? hors citation quand la suite commence par une majuscule : « L. 251-10 » ne coupe donc pas,
    « au titre I. Le décret » si. Exceptions : une abréviation (« M. Dupont », « art. L. 5 ») ou un tiret
    (« I. – », « Art. L. 250-10. – »). Le guillemet qui ouvre l'alinéa ne compte pas comme citation : il entoure
    tout l'article cité.
    """
    visibles = [i for i, j in enumerate(jetons) if not est_blanc(j)]
    if not visibles:
        return []
    bornes, debut, profondeur = [], visibles[0], 0
    for rang, i in enumerate(visibles):
        texte = jetons[i].texte
        if texte == "«" and rang > 0:
            profondeur += 1
        elif texte == "»":
            profondeur = max(0, profondeur - 1)
        elif texte in (".", "!", "?") and profondeur == 0:
            avant = jetons[visibles[rang - 1]].texte if rang else ""
            apres = jetons[visibles[rang + 1]].texte if rang + 1 < len(visibles) else ""
            if not apres or apres in ("»", ";", "–", "-", ","):
                continue
            if texte == "." and avant.lower() in ABREVIATIONS:
                continue
            if not (apres[0].isupper() or apres == "«"):
                continue
            bornes.append((debut, i + 1))
            debut = visibles[rang + 1]
    bornes.append((debut, visibles[-1] + 1))
    return bornes


def debut_terminal(jetons, debut, fin):
    """Index où commence la ponctuation finale de [debut, fin) (« . » ; », « : ») ; fin s'il n'y en a pas."""
    i = fin
    while i > debut and (jetons[i - 1].texte in TERMINAUX or est_blanc(jetons[i - 1])):
        i -= 1
    while i < fin and est_blanc(jetons[i]):
        i += 1
    return i


def coudre(ids, *morceaux):
    """Assemble des suites de jetons en réglant les espaces aux seules jointures.

    Garde l'espace d'origine quand il en faut un (une espace insécable reste insécable).
    """
    resultat = []
    for morceau in morceaux:
        morceau = list(morceau)
        if not morceau:
            continue
        if not resultat:
            resultat = morceau
            continue
        blanc = None
        while resultat and est_blanc(resultat[-1]):
            blanc = resultat.pop()
        while morceau and est_blanc(morceau[0]):
            blanc = blanc or morceau[0]
            morceau.pop(0)
        if resultat and morceau and _espace_requis(resultat[-1].texte, morceau[0].texte):
            resultat.append(blanc or Jeton(ids.nouveau(), " ", ids.origine))
        resultat += morceau
    while resultat and est_blanc(resultat[-1]):
        resultat.pop()
    return tuple(resultat)


def _espace_requis(gauche, droite):
    if droite in PONCTUATION_COLLEE_A_GAUCHE or gauche in PONCTUATION_COLLEE_A_DROITE:
        return False
    return droite not in ("-", "‑", "'", "’")


def diff_mots(avant, apres):
    """Différence mot à mot entre deux textes (lignes jointes) : [(type, mots retirés, mots ajoutés)]."""
    a, b = normaliser(" ".join(avant)).split(), normaliser(" ".join(apres)).split()
    return [(tag, " ".join(a[i1:i2]), " ".join(b[j1:j2]))
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
            if tag != "equal"]


def diff_versions(avant, apres):
    """Différence entre deux versions, article par article : {numéro affiché dans apres : changements}."""
    view, changements = numeros(apres), {}
    anciens = {a.id: a for a in avant.articles}
    if avant.titre is not None and apres.titre is not None and lignes_de(avant.titre) != lignes_de(apres.titre):
        changements["TITRE"] = diff_mots(lignes_de(avant.titre), lignes_de(apres.titre))
    for article in apres.articles:
        ancien = anciens.get(article.id)
        lignes_avant = lignes_de(ancien) if ancien and not ancien.supprime else []
        lignes_apres = [] if article.supprime else lignes_de(article)
        if lignes_avant != lignes_apres:
            changements[view[article.id]] = diff_mots(lignes_avant, lignes_apres)
    return changements
