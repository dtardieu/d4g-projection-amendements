"""Textes modificatifs (lois, décrets…) qui modifient un article de code ou de loi : analyse et application.

Même chaîne que pour les amendements, avec la grammaire grammaires/lois.lark :
  section modificative → instructions (découpage 1°, a), I. –) → Operation → appliquer() sur l'article visé.

Avant la grammaire, chaque chapeau est préparé (hors citations) :
  - la référence d'article (« l'article L. 571-2 du même code ») devient « l'article » : le triplet dit déjà lequel ;
  - une subdivision de l'article (« le 4° », « au II », « du b du 2° ») devient les alinéas qu'elle couvre dans
    l'article de départ (« l'alinéa 7 », « aux alinéas 3 à 6 ») : la grammaire n'a pas à connaître les subdivisions.
Un chapeau « … est ainsi modifié : » ne produit pas d'opération : il donne sa cible (un Contexte) aux 1°, a)… qui suivent.
Sous une subdivision, « au premier alinéa » se compte depuis le début de la subdivision.
"""

import re
import unicodedata
from dataclasses import dataclass, field, replace
from functools import cache, lru_cache

from lark import Lark, LarkError, v_args
from lark.exceptions import VisitError

from moteur.analyse import Cible, Operation, Portee, _Rejet, _VersOperations, texte_grammaire
from moteur.application import appliquer
from moteur.texte import creer_version, lignes_de

PORTEE = Portee("article", "X")

# « I. – », « 1° », « 2° bis », « a) », « b bis) » : le niveau de l'énumération, du plus haut au plus bas.
MARQUES = [
    (1, re.compile(r"^([IVXLC]+)\s*\.?\s*[-–―—]\s*")),
    (2, re.compile(r"^(\d+°(?:\s*(?:bis|ter|quater))?)\s*")),
    (3, re.compile(r"^([a-z]{1,2}(?:\s*(?:bis|ter|quater))?\))\s*")),
]
# Les mêmes marques au début d'un alinéa de l'article visé (« II. - », « II.- », « 3° », « b) »).
MARQUES_ARTICLE = [
    (1, re.compile(r"^([IVXLC]+)\s*(?:\.\s*[-–―—]?|[-–―—])")),
    (2, re.compile(r"^(\d+°(?:\s*(?:bis|ter|quater))?)")),
    (3, re.compile(r"^([a-z](?:\s*(?:bis|ter|quater))?)\)")),
]
CITATION = re.compile(r"«[^«»]*(?:«[^«»]*»[^«»]*)*»")
FIN_REFERENCE = r"(?=\s*,|\s*:|\s+(?:est|sont|il\s+est)\b|\s*$)"
DE_L_ARTICLE = re.compile(rf"\s+(?:de|du)\s+l['’]\s*article\s+.*?{FIN_REFERENCE}", re.I)
L_ARTICLE = re.compile(rf"\b(l['’]\s*article)\s+(?!(?:est|sont|premier\s+alinéa)\b)\S.*?{FIN_REFERENCE}", re.I)
MEME_ARTICLE = re.compile(r"\s+(?:de\s+cet|du\s+même|dudit|de\s+ce\s+même)\s+article\b", re.I)
# « Le code de la route est ainsi modifié : » : un chapeau de plus haut niveau, qui vaut pour l'article visé.
SUJET_TEXTE = re.compile(r"^(?:Le|La|L['’])\s*(?:code|chapitre|titre|livre|section|sous-section|paragraphe|décret|arrêté|"
                         r"loi|ordonnance)\b.*?(?=\s+(?:est|sont)\s+(?:ainsi\s+)?modifiée?s?)", re.I)
# Coquilles de LEGI : « susviséest », « dix- neuvième ».
EST_COLLE = re.compile(r"(?<=[^\s«])(?=(?:est|sont)\s+(?:remplac|modifi|complét|ainsi|abrog|supprim|rédig|insér|ajout))")
TIRET_ESPACE = re.compile(r"(\w)-\s+(\w)")
SUBDIVISION = re.compile(
    r"\b(?P<det>(?i:le|la|au|à\s+la|a\s+la|du|de\s+la|à|a))\s+"
    r"(?P<m>[IVX]+|\d+°(?:\s*(?:bis|ter|quater))?|[a-z](?:\s*(?:bis|ter))?)"
    r"(?P<parent>\s+(?:du|de\s+la|de\s+l['’])\s*(?:[IVX]+|\d+°(?:\s*(?:bis|ter))?))?"
    r"(?=\s*,|\s*:|\s+(?:est|sont|il|après|avant)\b|\s*$)")
# Le texte qui suit « ainsi rédigé : » sur la même ligne est cité : il devient la première citation du bloc.
ANNONCE_EN_LIGNE = re.compile(r"^(.*?(?:rédigée?s?|suivante?s?|qu['’]il\s+suit)\s*:)\s*(«.*)$", re.I)
EN_TETE_ARTICLE = re.compile(r"^(«\s*)?Art(?:icle|\.)\s*[^\s«»]*(?:\s+(?:bis|ter|quater))?\s*[.\-–―—]+\s*", re.I)
FIN_D_INSTRUCTION = re.compile(r"[\s;.,]*(?:\bet\b)?[\s;.,]*$")
# « les mots : « a » sont remplacés par les mots : « b » et les mots : « c » sont supprimés » : deux instructions.
ET_ENTRE_INSTRUCTIONS = re.compile(r"\s*[;,]?\s+et\s*,?\s+(?=(?:à|a|au|aux|après|avant|les?|la|l['’])\b)", re.I)
VERBE_D_INSTRUCTION = re.compile(
    r"\b(?:est|sont)\s+(?:ainsi\s+|remplacée?s?|supprimée?s?|complétée?s?|insérée?s?|ajoutée?s?|abrogée?s?|rédigée?s?|"
    r"modifiée?s?)|\bil\s+est\s+(?:inséré|ajouté)", re.I)
CONTEXTE = re.compile(r"(?:ainsi\s+modifiée?s?|modifiée?s?\s+(?:ainsi\s+qu['’]il|comme)\s+suit)\s*:$", re.I)


@dataclass
class InstructionLoi:
    niveau: int
    marque: str | None
    chapeau: str
    citations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Contexte:
    """Cible posée par « … est ainsi modifié : ». borne : alinéas de la subdivision visée (premier, dernier), où se
    cherchent les subdivisions et se comptent les alinéas des instructions qui suivent ; None hors subdivision."""
    cible: Cible
    borne: tuple | None = None


@dataclass
class AnalyseSection:
    operations: list[Operation]
    instructions: list[InstructionLoi]
    non_analysees: list[str]


@cache
def _parser():
    return Lark(texte_grammaire("lois"), start=["en_ligne", "bloc"], parser="earley", ambiguity="explicit")


@lru_cache(maxsize=50000)
def _arbre(texte, start):
    try:
        arbre = _parser().parse(texte, start=start)
    except LarkError:
        return None
    return None if any(arbre.find_data("_ambig")) else arbre


class _VersOperationsLois(_VersOperations):
    """Les alias de lois.lark sont ceux de amendements.lark, plus quelques-uns propres aux lois."""

    @v_args(inline=True)
    def premiers_alineas(self, nombre):
        # « les deux premiers alinéas »
        return Cible(alinea=1, jusqua=nombre)

    @v_args(inline=True)
    def contexte(self, cible):
        # « Le deuxième alinéa est ainsi modifié : » : pas d'opération, la cible des 1°, a)… qui suivent.
        if isinstance(cible, list):
            raise _Rejet("plusieurs nœuds épars")
        return cible

    @v_args(inline=True)
    def b_ajout_fin(self, *args):
        """« Il est ajouté un alinéa ainsi rédigé : », « À la fin du I, sont ajoutés les mots : », « Il est complété par
        la phrase suivante : » : au bout du nœud nommé, sinon de celui du contexte, sinon de l'article."""
        cible = next((a for a in args if isinstance(a, Cible)), None)
        annonce = next((a for a in args if isinstance(a, tuple)), None)
        base = cible if cible is not None and cible.alinea is not None else self.precedente or Cible(noeud="article")
        base = replace(base, position="dans", mots=None)
        if annonce is None or annonce[1] == "phrase":
            return Operation("complete", base, texte=self._texte())
        position = "fin" if base.alinea is None else "apres"
        if base.jusqua:
            base = replace(base, alinea=base.jusqua, jusqua=None)
        return Operation("insert_node", replace(base, position=position), texte=self._texte(annonce))


# ---- préparation du chapeau

def _hors_citations(texte, fonction):
    morceaux, debut = [], 0
    for m in CITATION.finditer(texte):
        morceaux += [fonction(texte[debut:m.start()]), m.group(0)]
        debut = m.end()
    return "".join(morceaux + [fonction(texte[debut:])])


def neutraliser_references(texte):
    """« Au premier alinéa de l'article L. 12 du code X, » → « Au premier alinéa, » ; « L'article 3 du décret… est » →
    « L'article est ». Les citations ne sont pas touchées."""
    def neutraliser(morceau):
        morceau = MEME_ARTICLE.sub("", morceau)
        morceau = DE_L_ARTICLE.sub("", morceau)
        return L_ARTICLE.sub(lambda m: m.group(1), morceau)
    return _hors_citations(SUJET_TEXTE.sub("L'article", texte), neutraliser)


def _niveau_alinea(ligne):
    for niveau, motif in MARQUES_ARTICLE:
        m = motif.match(ligne)
        if m:
            return niveau, re.sub(r"\s+", " ", m.group(1))
    return None, None


def _niveau_marque(marque):
    return 1 if re.fullmatch(r"[IVX]+", marque) else 2 if "°" in marque else 3


def localiser_subdivision(lignes, marque, borne=None):
    """Alinéas (premier, dernier), comptés à partir de 1, de la subdivision « marque » dans borne ; None si elle
    est absente ou s'y trouve plusieurs fois (le 1° du I et le 1° du II : il faut le parent pour choisir)."""
    premier, dernier = borne or (1, len(lignes))
    marque, niveau = re.sub(r"\s+", " ", marque), _niveau_marque(marque)
    trouves = [i for i in range(premier, dernier + 1) if _niveau_alinea(lignes[i - 1]) == (niveau, marque)]
    if len(trouves) != 1:
        return None
    debut = fin = trouves[0]
    while fin < dernier:
        suivant, _ = _niveau_alinea(lignes[fin])
        if suivant is not None and suivant <= niveau:
            break
        fin += 1
    return debut, fin


def remplacer_subdivisions(texte, lignes, borne=None):
    """« Au 2° du I, les mots… » → « Aux alinéas 4 à 6, les mots… ». Renvoie (texte, remplacé ?)."""
    if not lignes:
        return texte, False
    change = [False]

    def remplacer(m):
        zone = borne
        if m.group("parent"):
            parent = re.sub(r"^\s*(?:du|de\s+la|de\s+l['’])\s*", "", m.group("parent"))
            zone = localiser_subdivision(lignes, parent, borne)
            if zone is None:
                return m.group(0)
        place = localiser_subdivision(lignes, m.group("m"), zone)
        if place is None:
            return m.group(0)
        change[0] = True
        det = re.sub(r"\s+", " ", m.group("det").lower())
        un, plusieurs = {"le": ("l'alinéa", "les alinéas"), "la": ("l'alinéa", "les alinéas"),
                         "du": ("de l'alinéa", "des alinéas"), "de la": ("de l'alinéa", "des alinéas")}.get(
            det, ("à l'alinéa", "aux alinéas"))
        sortie = f"{un} {place[0]}" if place[0] == place[1] else f"{plusieurs} {place[0]} à {place[1]}"
        return sortie[0].upper() + sortie[1:] if m.group("det")[0].isupper() else sortie

    return _hors_citations(texte, lambda morceau: SUBDIVISION.sub(remplacer, morceau)), change[0]


def preparer(chapeau, lignes, borne=None):
    chapeau = unicodedata.normalize("NFC", chapeau)
    chapeau = _hors_citations(chapeau, lambda m: TIRET_ESPACE.sub(r"\1-\2", EST_COLLE.sub(" ", m)))
    return remplacer_subdivisions(neutraliser_references(chapeau), lignes, borne)


# ---- découpage

def decouper_section(section):
    """Section modificative → InstructionLoi. Une ligne qui s'ouvre sur « est citée et suit l'instruction en cours.

    Après un chapeau qui annonce un texte (« … ainsi rédigé : »), les lignes sans guillemets (lignes de tableau,
    nouvel article recopié tel quel) sont aussi citées, tant qu'elles ne portent pas de verbe d'instruction.
    """
    instructions = []
    for ligne in unicodedata.normalize("NFC", section).split("\n"):
        ligne = ligne.strip()
        if not ligne:
            continue
        if instructions and (ligne.startswith("«") or _attend_un_texte(instructions[-1]) and not _est_instruction(ligne)):
            instructions[-1].citations.append(ligne)
            continue
        niveau, marque = 0, None
        for n, motif in MARQUES:
            m = motif.match(ligne)
            if m:
                niveau, marque, ligne = n, m.group(1), ligne[m.end():]
                break
        annonce = ANNONCE_EN_LIGNE.match(ligne)
        citations = [annonce.group(2)] if annonce else []
        chapeau = annonce.group(1) if annonce else ligne
        instructions.append(InstructionLoi(niveau, marque, chapeau, citations))
    return instructions


def _attend_un_texte(instruction):
    return instruction.chapeau.rstrip().endswith(":") and not CONTEXTE.search(instruction.chapeau)


def _est_instruction(ligne):
    return bool(VERBE_D_INSTRUCTION.search(CITATION.sub("", ligne)))


def _sans_en_tete_article(citations):
    """« Art. 3.- Le Conseil… » : le numéro de l'article réécrit n'est pas dans le texte consolidé."""
    if not citations:
        return citations
    return [EN_TETE_ARTICLE.sub(lambda m: m.group(1) or "", citations[0]), *citations[1:]]


# ---- analyse

def analyser_instruction(instruction, contexte=None, operation_precedente=None, lignes=()):
    """→ (operations, nouveau Contexte) ; operations None si la grammaire ne reconnaît pas l'instruction."""
    borne = contexte.borne if contexte else None
    chapeau, subdivision = preparer(instruction.chapeau, lignes, borne)
    citations = _sans_en_tete_article(instruction.citations)
    precedente = contexte.cible if contexte else None
    relatif = borne if not subdivision else None
    try:
        if not chapeau.rstrip().endswith(":"):
            ops = _en_ligne(FIN_D_INSTRUCTION.sub("", " ".join([chapeau, *citations])), precedente, operation_precedente)
            if ops is not None:
                return [_dans_contexte(op, precedente, relatif) for op in ops], None
        arbre = _arbre(chapeau.strip(), "bloc")
        if arbre:
            sortie = _VersOperationsLois(PORTEE, precedente, citations).transform(arbre)
            if isinstance(sortie, Cible):
                cible = _dans_contexte(Operation("rewrite", sortie), precedente, relatif).cible
                nouvelle_borne = (cible.alinea, cible.jusqua or cible.alinea) if cible.alinea and cible.alinea > 0 else None
                return [], Contexte(cible, nouvelle_borne if subdivision or borne else None)
            if citations:
                return [_dans_contexte(sortie, precedente, relatif)], None
    except VisitError as erreur:
        if not isinstance(erreur.orig_exc, _Rejet):
            raise erreur.orig_exc
    return None, None


def _en_ligne(texte, precedente, operation_precedente):
    """Une instruction en ligne ; à défaut, plusieurs reliées par « et » (« … sont remplacés … et les mots … sont
    supprimés »), la cible de la première valant pour les suivantes."""
    ops = _transformer_en_ligne(texte, precedente, operation_precedente)
    if ops is not None:
        return ops
    gardees = CITATION.findall(texte)
    masque = CITATION.sub(lambda m: "\x00", texte)
    parties = ET_ENTRE_INSTRUCTIONS.split(masque)
    if len(parties) < 2:
        return None
    sortie, rang = [], 0
    for partie in parties:
        n = partie.count("\x00")
        morceau = re.sub("\x00", lambda _: gardees.pop(0), partie) if n else partie
        base = precedente
        if sortie and sortie[0].cible.alinea is not None:
            base = replace(sortie[0].cible, mots=None, position="dans", occurrence=None)
        ops = _transformer_en_ligne(morceau, base, sortie[-1] if sortie else operation_precedente)
        if ops is None:
            return None
        sortie += [_dans_contexte(op, base, None) for op in ops]
        rang += n
    return sortie


def _transformer_en_ligne(texte, precedente, operation_precedente):
    arbre = _arbre(texte, "en_ligne")
    if not arbre:
        return None
    sortie = _VersOperationsLois(PORTEE, precedente, operation_precedente=operation_precedente).transform(arbre)
    return sortie if isinstance(sortie, list) else [sortie]


def _dans_contexte(op, contexte, relatif=None):
    """L'opération sans alinéa prend celui du contexte (« Le deuxième alinéa est ainsi modifié : a) les mots… ») ;
    sous une subdivision (relatif : ses bornes), « au premier alinéa » se compte depuis son début."""
    cible = op.cible
    if relatif and cible.alinea is not None:
        premier, dernier = relatif
        recaler = lambda n: premier + n - 1 if n > 0 else dernier + n + 1
        return replace(op, cible=replace(cible, alinea=recaler(cible.alinea),
                                         jusqua=recaler(cible.jusqua) if cible.jusqua else None))
    if contexte is None or contexte.alinea is None or cible.alinea is not None:
        return op
    if cible.noeud == "article" and op.verbe in ("rewrite", "delete_node"):
        return op
    phrase = cible.phrase if cible.phrase is not None else contexte.phrase
    noeud = "phrase" if phrase is not None else "alinea"
    return replace(op, cible=replace(cible, alinea=contexte.alinea, jusqua=contexte.jusqua, phrase=phrase, noeud=noeud))


def analyser_section(section, article=""):
    """article : le texte de départ, où se cherchent les subdivisions (« le 4° ») ; un alinéa par ligne non vide."""
    lignes = [l.strip() for l in article.split("\n") if l.strip()]
    instructions = decouper_section(section)
    operations, non_analysees, contextes, precedente = [], [], {}, None
    for instruction in instructions:
        # Le contexte vaut pour les niveaux plus bas que celui qui l'a posé.
        contextes = {n: c for n, c in contextes.items() if n < instruction.niveau}
        contexte = contextes[max(contextes)] if contextes else None
        ops, nouveau = analyser_instruction(instruction, contexte, precedente, lignes)
        if ops is None:
            non_analysees.append(instruction.chapeau)
            continue
        if nouveau is not None:
            contextes[instruction.niveau] = nouveau
        operations += ops
        precedente = ops[-1] if ops else precedente
    return AnalyseSection(operations, instructions, non_analysees)


def consolider_article(article, section):
    """→ (texte consolidé ou None, AnalyseSection, erreur). None si une instruction n'est pas analysée ou ne s'applique pas."""
    analyse = analyser_section(section, article)
    if analyse.non_analysees or not analyse.operations:
        return None, analyse, "non analysée"
    depart = creer_version([("X", article)])
    try:
        version, _ = appliquer(analyse.operations, PORTEE, depart, depart, "LOI")
    except Exception as erreur:   # ErreurCible, CiblePendante… : l'opération ne trouve pas sa cible
        return None, analyse, f"{type(erreur).__name__}: {erreur}"
    article_final = version.articles[0]
    if article_final.supprime:
        return "", analyse, None
    return "\n".join(lignes_de(article_final)), analyse, None
