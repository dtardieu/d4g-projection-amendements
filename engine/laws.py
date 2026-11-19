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

from engine.analysis import Target, Operation, Scope, _Rejection, _ToOperations, grammar_text
from engine.application import apply
from engine.text import create_version, lines_of

SCOPE = Scope("article", "X")

# « I. – », « 1° », « 2° bis », « a) », « b bis) » : le niveau de l'énumération, du plus haut au plus bas.
MARKS = [
    (1, re.compile(r"^([IVXLC]+)\s*\.?\s*[-–―—]\s*")),
    (2, re.compile(r"^(\d+°(?:\s*(?:bis|ter|quater))?)\s*")),
    (3, re.compile(r"^([a-z]{1,2}(?:\s*(?:bis|ter|quater))?\))\s*")),
]
# Les mêmes marques au début d'un alinéa de l'article visé (« II. - », « II.- », « 3° », « b) »).
ARTICLE_MARKS = [
    (1, re.compile(r"^([IVXLC]+)\s*(?:\.\s*[-–―—]?|[-–―—])")),
    (2, re.compile(r"^(\d+°(?:\s*(?:bis|ter|quater))?)")),
    (3, re.compile(r"^([a-z](?:\s*(?:bis|ter|quater))?)\)")),
]
CITATION = re.compile(r"«[^«»]*(?:«[^«»]*»[^«»]*)*»")
REFERENCE_END = r"(?=\s*,|\s*:|\s+(?:est|sont|il\s+est)\b|\s*$)"
OF_THE_ARTICLE = re.compile(rf"\s+(?:de|du)\s+l['’]\s*article\s+.*?{REFERENCE_END}", re.I)
THE_ARTICLE = re.compile(rf"\b(l['’]\s*article)\s+(?!(?:est|sont|premier\s+alinéa)\b)\S.*?{REFERENCE_END}", re.I)
SAME_ARTICLE = re.compile(r"\s+(?:de\s+cet|du\s+même|dudit|de\s+ce\s+même)\s+article\b", re.I)
# « Le code de la route est ainsi modifié : » : un chapeau de plus haut niveau, qui vaut pour l'article visé.
TEXT_SUBJECT = re.compile(r"^(?:Le|La|L['’])\s*(?:code|chapitre|titre|livre|section|sous-section|paragraphe|décret|arrêté|"
                         r"loi|ordonnance)\b.*?(?=\s+(?:est|sont)\s+(?:ainsi\s+)?modifiée?s?)", re.I)
# Coquilles de LEGI : « susviséest », « dix- neuvième ».
IS_ATTACHED = re.compile(r"(?<=[^\s«])(?=(?:est|sont)\s+(?:remplac|modifi|complét|ainsi|abrog|supprim|rédig|insér|ajout))")
DASH_SPACE = re.compile(r"(\w)-\s+(\w)")
SUBDIVISION = re.compile(
    r"\b(?P<det>(?i:le|la|au|à\s+la|a\s+la|du|de\s+la|à|a))\s+"
    r"(?P<m>[IVX]+|\d+°(?:\s*(?:bis|ter|quater))?|[a-z](?:\s*(?:bis|ter))?)"
    r"(?P<parent>\s+(?:du|de\s+la|de\s+l['’])\s*(?:[IVX]+|\d+°(?:\s*(?:bis|ter))?))?"
    r"(?=\s*,|\s*:|\s+(?:est|sont|il|après|avant)\b|\s*$)")
# Le texte qui suit « ainsi rédigé : » sur la même ligne est cité : il devient la première citation du bloc.
INLINE_ANNOUNCEMENT = re.compile(r"^(.*?(?:rédigée?s?|suivante?s?|qu['’]il\s+suit)\s*:)\s*(«.*)$", re.I)
ARTICLE_HEADER = re.compile(r"^(«\s*)?Art(?:icle|\.)\s*[^\s«»]*(?:\s+(?:bis|ter|quater))?\s*[.\-–―—]+\s*", re.I)
INSTRUCTION_END = re.compile(r"[\s;.,]*(?:\bet\b)?[\s;.,]*$")
# « les mots : « a » sont remplacés par les mots : « b » et les mots : « c » sont supprimés » : deux instructions.
AND_BETWEEN_INSTRUCTIONS = re.compile(r"\s*[;,]?\s+et\s*,?\s+(?=(?:à|a|au|aux|après|avant|les?|la|l['’])\b)", re.I)
INSTRUCTION_VERB = re.compile(
    r"\b(?:est|sont)\s+(?:ainsi\s+|remplacée?s?|supprimée?s?|complétée?s?|insérée?s?|ajoutée?s?|abrogée?s?|rédigée?s?|"
    r"modifiée?s?)|\bil\s+est\s+(?:inséré|ajouté)", re.I)
CONTEXT = re.compile(r"(?:ainsi\s+modifiée?s?|modifiée?s?\s+(?:ainsi\s+qu['’]il|comme)\s+suit)\s*:$", re.I)


@dataclass
class LawInstruction:
    level: int
    mark: str | None
    heading: str
    quotes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Context:
    """Cible posée par « … est ainsi modifié : ». borne : alinéas de la subdivision visée (premier, dernier), où se
    cherchent les subdivisions et se comptent les alinéas des instructions qui suivent ; None hors subdivision."""
    target: Target
    bound: tuple | None = None


@dataclass
class SectionAnalysis:
    operations: list[Operation]
    instructions: list[LawInstruction]
    unparsed: list[str]


@cache
def _parser():
    return Lark(grammar_text("lois"), start=["en_ligne", "bloc"], parser="earley", ambiguity="explicit")


@lru_cache(maxsize=50000)
def _tree(text, start):
    try:
        tree = _parser().parse(text, start=start)
    except LarkError:
        return None
    return None if any(tree.find_data("_ambig")) else tree


class _ToLawOperations(_ToOperations):
    """Les alias de lois.lark sont ceux de amendements.lark, plus quelques-uns propres aux lois."""

    @v_args(inline=True)
    def premiers_alineas(self, count):
        # « les deux premiers alinéas »
        return Target(alinea=1, until=count)

    @v_args(inline=True)
    def contexte(self, target):
        # « Le deuxième alinéa est ainsi modifié : » : pas d'opération, la cible des 1°, a)… qui suivent.
        if isinstance(target, list):
            raise _Rejection("plusieurs nœuds épars")
        return target

    @v_args(inline=True)
    def b_ajout_fin(self, *args):
        """« Il est ajouté un alinéa ainsi rédigé : », « À la fin du I, sont ajoutés les mots : », « Il est complété par
        la phrase suivante : » : au bout du nœud nommé, sinon de celui du contexte, sinon de l'article."""
        target = next((a for a in args if isinstance(a, Target)), None)
        announcement = next((a for a in args if isinstance(a, tuple)), None)
        base = target if target is not None and target.alinea is not None else self.preceding or Target(node="article")
        base = replace(base, position="dans", words=None)
        if announcement is None or announcement[1] == "phrase":
            return Operation("complete", base, text=self._texte())
        position = "fin" if base.alinea is None else "apres"
        if base.until:
            base = replace(base, alinea=base.until, until=None)
        return Operation("insert_node", replace(base, position=position), text=self._texte(announcement))


# ---- préparation du chapeau

def _outside_quotes(text, function):
    pieces, beginning = [], 0
    for m in CITATION.finditer(text):
        pieces += [function(text[beginning:m.start()]), m.group(0)]
        beginning = m.end()
    return "".join(pieces + [function(text[beginning:])])


def neutralize_references(text):
    """« Au premier alinéa de l'article L. 12 du code X, » → « Au premier alinéa, » ; « L'article 3 du décret… est » →
    « L'article est ». Les citations ne sont pas touchées."""
    def neutralize(piece):
        piece = SAME_ARTICLE.sub("", piece)
        piece = OF_THE_ARTICLE.sub("", piece)
        return THE_ARTICLE.sub(lambda m: m.group(1), piece)
    return _outside_quotes(TEXT_SUBJECT.sub("L'article", text), neutralize)


def _paragraph_level(line):
    for level, pattern in ARTICLE_MARKS:
        m = pattern.match(line)
        if m:
            return level, re.sub(r"\s+", " ", m.group(1))
    return None, None


def _mark_level(mark):
    return 1 if re.fullmatch(r"[IVX]+", mark) else 2 if "°" in mark else 3


def locate_subdivision(lignes, mark, bound=None):
    """Alinéas (premier, dernier), comptés à partir de 1, de la subdivision « marque » dans borne ; None si elle
    est absente ou s'y trouve plusieurs fois (le 1° du I et le 1° du II : il faut le parent pour choisir)."""
    first, last = bound or (1, len(lignes))
    mark, level = re.sub(r"\s+", " ", mark), _mark_level(mark)
    found_items = [i for i in range(first, last + 1) if _paragraph_level(lignes[i - 1]) == (level, mark)]
    if len(found_items) != 1:
        return None
    beginning = end = found_items[0]
    while end < last:
        following, _ = _paragraph_level(lignes[end])
        if following is not None and following <= level:
            break
        end += 1
    return beginning, end


def replace_subdivisions(text, lignes, bound=None):
    """« Au 2° du I, les mots… » → « Aux alinéas 4 à 6, les mots… ». Renvoie (texte, remplacé ?)."""
    if not lignes:
        return text, False
    change = [False]

    def replace_with(m):
        zone = bound
        if m.group("parent"):
            parent = re.sub(r"^\s*(?:du|de\s+la|de\s+l['’])\s*", "", m.group("parent"))
            zone = locate_subdivision(lignes, parent, bound)
            if zone is None:
                return m.group(0)
        place = locate_subdivision(lignes, m.group("m"), zone)
        if place is None:
            return m.group(0)
        change[0] = True
        det = re.sub(r"\s+", " ", m.group("det").lower())
        un, several = {"le": ("l'alinéa", "les alinéas"), "la": ("l'alinéa", "les alinéas"),
                         "du": ("de l'alinéa", "des alinéas"), "de la": ("de l'alinéa", "des alinéas")}.get(
            det, ("à l'alinéa", "aux alinéas"))
        output = f"{un} {place[0]}" if place[0] == place[1] else f"{several} {place[0]} à {place[1]}"
        return output[0].upper() + output[1:] if m.group("det")[0].isupper() else output

    return _outside_quotes(text, lambda piece: SUBDIVISION.sub(replace_with, piece)), change[0]


def prepare(heading, lignes, bound=None):
    heading = unicodedata.normalize("NFC", heading)
    heading = _outside_quotes(heading, lambda m: DASH_SPACE.sub(r"\1-\2", IS_ATTACHED.sub(" ", m)))
    return replace_subdivisions(neutralize_references(heading), lignes, bound)


# ---- découpage

def split_section(section):
    """Section modificative → InstructionLoi. Une ligne qui s'ouvre sur « est citée et suit l'instruction en cours.

    Après un chapeau qui annonce un texte (« … ainsi rédigé : »), les lignes sans guillemets (lignes de tableau,
    nouvel article recopié tel quel) sont aussi citées, tant qu'elles ne portent pas de verbe d'instruction.
    """
    instructions = []
    for line in unicodedata.normalize("NFC", section).split("\n"):
        line = line.strip()
        if not line:
            continue
        if instructions and (line.startswith("«") or _expects_text(instructions[-1]) and not _is_instruction(line)):
            instructions[-1].quotes.append(line)
            continue
        level, mark = 0, None
        for n, pattern in MARKS:
            m = pattern.match(line)
            if m:
                level, mark, line = n, m.group(1), line[m.end():]
                break
        announcement = INLINE_ANNOUNCEMENT.match(line)
        quotes = [announcement.group(2)] if announcement else []
        heading = announcement.group(1) if announcement else line
        instructions.append(LawInstruction(level, mark, heading, quotes))
    return instructions


def _expects_text(instruction):
    return instruction.heading.rstrip().endswith(":") and not CONTEXT.search(instruction.heading)


def _is_instruction(line):
    return bool(INSTRUCTION_VERB.search(CITATION.sub("", line)))


def _without_article_header(quotes):
    """« Art. 3.- Le Conseil… » : le numéro de l'article réécrit n'est pas dans le texte consolidé."""
    if not quotes:
        return quotes
    return [ARTICLE_HEADER.sub(lambda m: m.group(1) or "", quotes[0]), *quotes[1:]]


# ---- analyse

def analyze_instruction(instruction, contexte=None, previous_operation=None, lignes=()):
    """→ (operations, nouveau Contexte) ; operations None si la grammaire ne reconnaît pas l'instruction."""
    bound = contexte.bound if contexte else None
    heading, subdivision = prepare(instruction.heading, lignes, bound)
    quotes = _without_article_header(instruction.quotes)
    preceding = contexte.target if contexte else None
    relative = bound if not subdivision else None
    try:
        if not heading.rstrip().endswith(":"):
            ops = _inline(INSTRUCTION_END.sub("", " ".join([heading, *quotes])), preceding, previous_operation)
            if ops is not None:
                return [_in_context(op, preceding, relative) for op in ops], None
        tree = _tree(heading.strip(), "bloc")
        if tree:
            output = _ToLawOperations(SCOPE, preceding, quotes).transform(tree)
            if isinstance(output, Target):
                target = _in_context(Operation("rewrite", output), preceding, relative).target
                new_bound = (target.alinea, target.until or target.alinea) if target.alinea and target.alinea > 0 else None
                return [], Context(target, new_bound if subdivision or bound else None)
            if quotes:
                return [_in_context(output, preceding, relative)], None
    except VisitError as error:
        if not isinstance(error.orig_exc, _Rejection):
            raise error.orig_exc
    return None, None


def _inline(text, preceding, previous_operation):
    """Une instruction en ligne ; à défaut, plusieurs reliées par « et » (« … sont remplacés … et les mots … sont
    supprimés »), la cible de la première valant pour les suivantes."""
    ops = _transform_inline(text, preceding, previous_operation)
    if ops is not None:
        return ops
    kept = CITATION.findall(text)
    mask = CITATION.sub(lambda m: "\x00", text)
    parts = AND_BETWEEN_INSTRUCTIONS.split(mask)
    if len(parts) < 2:
        return None
    output, rank = [], 0
    for part in parts:
        n = part.count("\x00")
        piece = re.sub("\x00", lambda _: kept.pop(0), part) if n else part
        base = preceding
        if output and output[0].target.alinea is not None:
            base = replace(output[0].target, words=None, position="dans", occurrence=None)
        ops = _transform_inline(piece, base, output[-1] if output else previous_operation)
        if ops is None:
            return None
        output += [_in_context(op, base, None) for op in ops]
        rank += n
    return output


def _transform_inline(text, preceding, previous_operation):
    tree = _tree(text, "en_ligne")
    if not tree:
        return None
    output = _ToLawOperations(SCOPE, preceding, previous_operation=previous_operation).transform(tree)
    return output if isinstance(output, list) else [output]


def _in_context(op, contexte, relative=None):
    """L'opération sans alinéa prend celui du contexte (« Le deuxième alinéa est ainsi modifié : a) les mots… ») ;
    sous une subdivision (relatif : ses bornes), « au premier alinéa » se compte depuis son début."""
    target = op.target
    if relative and target.alinea is not None:
        first, last = relative
        realign = lambda n: first + n - 1 if n > 0 else last + n + 1
        return replace(op, target=replace(target, alinea=realign(target.alinea),
                                         until=realign(target.until) if target.until else None))
    if contexte is None or contexte.alinea is None or target.alinea is not None:
        return op
    if target.node == "article" and op.verb in ("rewrite", "delete_node"):
        return op
    sentence = target.sentence if target.sentence is not None else contexte.sentence
    node = "phrase" if sentence is not None else "alinea"
    return replace(op, target=replace(target, alinea=contexte.alinea, until=contexte.until, sentence=sentence, node=node))


def analyze_section(section, article=""):
    """article : le texte de départ, où se cherchent les subdivisions (« le 4° ») ; un alinéa par ligne non vide."""
    lignes = [l.strip() for l in article.split("\n") if l.strip()]
    instructions = split_section(section)
    operations, unparsed, contexts, preceding = [], [], {}, None
    for instruction in instructions:
        # Le contexte vaut pour les niveaux plus bas que celui qui l'a posé.
        contexts = {n: c for n, c in contexts.items() if n < instruction.level}
        contexte = contexts[max(contexts)] if contexts else None
        ops, new = analyze_instruction(instruction, contexte, preceding, lignes)
        if ops is None:
            unparsed.append(instruction.heading)
            continue
        if new is not None:
            contexts[instruction.level] = new
        operations += ops
        preceding = ops[-1] if ops else preceding
    return SectionAnalysis(operations, instructions, unparsed)


def consolidate_article(article, section):
    """→ (texte consolidé ou None, AnalyseSection, erreur). None si une instruction n'est pas analysée ou ne s'applique pas."""
    analysis = analyze_section(section, article)
    if analysis.unparsed or not analysis.operations:
        return None, analysis, "non analysée"
    initial = create_version([("X", article)])
    try:
        version, _ = apply(analysis.operations, SCOPE, initial, initial, "LOI")
    except Exception as error:   # ErreurCible, CiblePendante… : l'opération ne trouve pas sa cible
        return None, analysis, f"{type(error).__name__}: {error}"
    article_final = version.articles[0]
    if article_final.supprime:
        return "", analysis, None
    return "\n".join(lines_of(article_final)), analysis, None
