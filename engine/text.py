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

TOKEN = re.compile(r"\s+|[\w\u0300-\u036f]+|[^\w\s]")
EQUIVALENTS = str.maketrans({"’": "'", "‑": "-", "–": "-", "—": "-", "−": "-", " ": " ", " ": " "})

PUNCTUATION_ATTACHED_LEFT = {",", ".", ")", "]", "…"}
PUNCTUATION_ATTACHED_RIGHT = {"(", "[", "'", "’", "-", "‑"}
TERMINALS = {".", ";", ":", ",", "»", "!", "?", "…"}
ABBREVIATIONS = {"art", "n", "no", "chap", "sect", "al", "cf", "m", "mme", "mm", "p", "pp", "vol", "c", "ex"}
ROMAN_NUMERAL = re.compile(r"^[IVXLCDM]+$")


@dataclass(frozen=True)
class Token:
    id: str
    text: str
    origin: str          # amendement qui l'a écrit (ou le texte de départ) : la provenance vient de là


@dataclass(frozen=True)
class Paragraph:
    id: str
    tokens: tuple
    origin: str


@dataclass(frozen=True)
class Article:
    id: str
    numero: str | None    # numéro dans le texte de départ ; None pour un article additionnel
    paragraphs: tuple
    origin: str
    supprime: bool = False
    anchor: str | None = None   # article après lequel un article additionnel a été inséré


@dataclass(frozen=True)
class Version:
    articles: tuple
    titre: Article | None = None


class Identifiers:
    """Fabrique d'identifiants d'un auteur : déterministe, donc les exécutions sont reproductibles."""

    def __init__(self, origin):
        self.origin, self.n = origin, 0

    def new(self):
        self.n += 1
        return f"{self.origin}/{self.n}"


def normalize(text):
    """Efface les variantes typographiques (apostrophes, tirets, espaces) pour comparer deux textes."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text).translate(EQUIVALENTS)).strip()


def is_blank(token):
    return token.text.isspace()


def split_tokens(text):
    """Texte → mots, ponctuation et espaces. Certaines sources de l'AN décomposent les accents (e + ◌́) : on les recompose,
    sinon « créée » serait coupé en morceaux et ne ressemblerait plus au même mot écrit ailleurs avec un accent composé.
    """
    return TOKEN.findall(unicodedata.normalize("NFC", text))


def create_tokens(text, ids):
    return tuple(Token(ids.new(), t, ids.origin) for t in split_tokens(text))


def create_paragraph(text, ids):
    return Paragraph(ids.new(), create_tokens(text, ids), ids.origin)


def create_article(numero, lignes, ids, anchor=None):
    return Article(ids.new(), numero, tuple(create_paragraph(l, ids) for l in lignes), ids.origin, anchor=anchor)


def create_version(articles, titre=None, origin="T0"):
    """articles : liste de (numero, contenu), un alinéa par ligne de contenu, comme textes_articles."""
    ids = Identifiers(origin)
    return Version(
        tuple(create_article(numero, [l for l in content.split("\n") if l.strip()], ids) for numero, content in articles),
        create_article("TITRE", [titre], ids) if titre else None,
    )


def text_of(alinea):
    return "".join(j.text for j in alinea.tokens)


def lines_of(article):
    return [text_of(a) for a in article.paragraphs]


def number_key(numero):
    """« ART. PREMIER », « 1 », « 1ER » → même clé ; un numéro collé à son intitulé (« 34FORFAITISER… ») → « 34 »."""
    numero = re.sub(r"^(PREMIER|1 ?ER)\b", "1", (numero or "").strip().upper())
    collé = re.match(r"^(\d+)[A-ZÉÈ]{3,}", numero)
    if collé and not re.match(r"^\d+\s", numero):
        return collé.group(1)
    return re.sub(r"\s+", " ", numero)


def find_article(version, numero):
    key = number_key(numero)
    return next((a for a in version.articles if a.numero is not None and number_key(a.numero) == key), None)


def article_by_id(version, article_id):
    if version.titre is not None and version.titre.id == article_id:
        return version.titre
    return next((a for a in version.articles if a.id == article_id), None)


def replace_article(version, article):
    if version.titre is not None and version.titre.id == article.id:
        return replace(version, titre=article)
    return replace(version, articles=tuple(article if a.id == article.id else a for a in version.articles))


SUFFIXES = ("bis", "ter", "quater", "quinquies", "sexies", "septies", "octies", "nonies", "decies")


def numbers(version):
    """Vue de la numérotation : {id d'article → numéro affiché}, jamais stockée.

    Un article unique qui reçoit des articles additionnels devient l'article 1er, et les suivants 2, 3… ;
    ailleurs un additionnel prend le numéro de son voisin plus un suffixe (6 bis, 6 ter).
    """
    view = {}
    if len(version.articles) > 1 and version.articles[0].numero == "UNIQUE":
        return {a.id: ("PREMIER" if i == 0 else str(i + 1)) for i, a in enumerate(version.articles)}
    base, rank = None, 0
    for article in version.articles:
        if article.numero is not None:
            base, rank = article.numero, 0
            view[article.id] = article.numero
        else:
            suffix = SUFFIXES[rank] if rank < len(SUFFIXES) else f"#{rank + 1}"
            view[article.id] = f"{base} {suffix}".upper() if base else suffix.upper()
            rank += 1
    return view


def render(version):
    """Le texte d'une version : [(numéro, [lignes])], un article supprimé n'ayant pas de ligne."""
    view = numbers(version)
    return [(view[a.id], [] if a.supprime else lines_of(a)) for a in version.articles]


def fingerprint(version):
    """Hachage du contenu seul : mêmes mots aux mêmes places donnent la même empreinte."""
    content = [("TITRE", _memoized_lines(version.titre)) if version.titre else None]
    content += [(a.numero or "", a.supprime, _memoized_lines(a)) for a in version.articles]
    return hashlib.sha256(repr(content).encode("utf-8")).hexdigest()


_LINES = {}


def _memoized_lines(article):
    """lignes_de, par identité d'objet : deux versions partagent leurs articles inchangés, seuls ceux-ci sont relus."""
    found = _LINES.get(id(article))
    if found is None or found[0] is not article:
        if len(_LINES) > 20000:
            _LINES.clear()
        found = _LINES[id(article)] = (article, lines_of(article))
    return found[1]


def provenance(alinea):
    """Regroupe les jetons par auteur successif : [(origine, texte)], à la manière de git blame."""
    pieces = []
    for token in alinea.tokens:
        if pieces and (pieces[-1][0] == token.origin or is_blank(token)):
            pieces[-1][1].append(token.text)
        else:
            pieces.append((token.origin, [token.text]))
    return [(origin, "".join(t).strip()) for origin, t in pieces]


def sentences(tokens):
    """Découpe des jetons en phrases : [(début, fin)], fin exclue.

    Coupe sur . ! ? hors citation quand la suite commence par une majuscule : « L. 251-10 » ne coupe donc pas,
    « au titre I. Le décret » si. Exceptions : une abréviation (« M. Dupont », « art. L. 5 ») ou un tiret
    (« I. – », « Art. L. 250-10. – »). Le guillemet qui ouvre l'alinéa ne compte pas comme citation : il entoure
    tout l'article cité.
    """
    visible_items = [i for i, j in enumerate(tokens) if not is_blank(j)]
    if not visible_items:
        return []
    bounds, beginning, depth = [], visible_items[0], 0
    for rank, i in enumerate(visible_items):
        text = tokens[i].text
        if text == "«" and rank > 0:
            depth += 1
        elif text == "»":
            depth = max(0, depth - 1)
        elif text in (".", "!", "?") and depth == 0:
            before = tokens[visible_items[rank - 1]].text if rank else ""
            after = tokens[visible_items[rank + 1]].text if rank + 1 < len(visible_items) else ""
            if not after or after in ("»", ";", "–", "-", ","):
                continue
            if text == "." and before.lower() in ABBREVIATIONS:
                continue
            if not (after[0].isupper() or after == "«"):
                continue
            bounds.append((beginning, i + 1))
            beginning = visible_items[rank + 1]
    bounds.append((beginning, visible_items[-1] + 1))
    return bounds


def terminal_beginning(tokens, beginning, end):
    """Index où commence la ponctuation finale de [debut, fin) (« . » ; », « : ») ; fin s'il n'y en a pas."""
    i = end
    while i > beginning and (tokens[i - 1].text in TERMINALS or is_blank(tokens[i - 1])):
        i -= 1
    while i < end and is_blank(tokens[i]):
        i += 1
    return i


def stitch(ids, *pieces):
    """Assemble des suites de jetons en réglant les espaces aux seules jointures.

    Garde l'espace d'origine quand il en faut un (une espace insécable reste insécable).
    """
    result = []
    for piece in pieces:
        piece = list(piece)
        if not piece:
            continue
        if not result:
            result = piece
            continue
        blank = None
        while result and is_blank(result[-1]):
            blank = result.pop()
        while piece and is_blank(piece[0]):
            blank = blank or piece[0]
            piece.pop(0)
        if result and piece and _space_required(result[-1].text, piece[0].text):
            result.append(blank or Token(ids.new(), " ", ids.origin))
        result += piece
    while result and is_blank(result[-1]):
        result.pop()
    return tuple(result)


def _space_required(left, right):
    if right in PUNCTUATION_ATTACHED_LEFT or left in PUNCTUATION_ATTACHED_RIGHT:
        return False
    return right not in ("-", "‑", "'", "’")


def diff_words(before, after):
    """Différence mot à mot entre deux textes (lignes jointes) : [(type, mots retirés, mots ajoutés)]."""
    a, b = normalize(" ".join(before)).split(), normalize(" ".join(after)).split()
    return [(tag, " ".join(a[i1:i2]), " ".join(b[j1:j2]))
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
            if tag != "equal"]


def diff_versions(before, after):
    """Différence entre deux versions, article par article : {numéro affiché dans apres : changements}."""
    view, changes = numbers(after), {}
    old_items = {a.id: a for a in before.articles}
    if before.titre is not None and after.titre is not None and lines_of(before.titre) != lines_of(after.titre):
        changes["TITRE"] = diff_words(lines_of(before.titre), lines_of(after.titre))
    for article in after.articles:
        old = old_items.get(article.id)
        lines_before = lines_of(old) if old and not old.supprime else []
        lines_after = [] if article.supprime else lines_of(article)
        if lines_before != lines_after:
            changes[view[article.id]] = diff_words(lines_before, lines_after)
    return changes
