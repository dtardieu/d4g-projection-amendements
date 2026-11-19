"""Étape 1 du moteur : traduire le dispositif d'un amendement en opérations typées.

Voir « Tracking Amendment Effects on Law Text » §2-3 et docs/amendment_tracking_worked_examples_fr.md.
Deux niveaux : une grammaire couvre les formules canoniques du guide de légistique (« Rédiger ainsi
cet article », « substituer au mot… », « Compléter l'alinéa N par… ») ; ce qu'elle rejette part vers un
LLM contraint au schéma des opérations. Chaque analyse porte sa source (grammar | llm) et une confiance.

Cette étape ne lit que l'amendement et l'article visé : elle ne résout rien contre une version du texte
(c'est l'étape 2). Les cibles restent donc symboliques (« alinéa 10 », « le mot puni »).

Exemples : amendements de la PPL 822 (CE11, CE12, CE10) et de BTC1003 (8, 1, 6, 15-17).
Le niveau LLM (lot 5) et les opérations imbriquées texte → code (phase 2) ne sont pas encore écrits.
"""

import html
import re
import unicodedata
from dataclasses import dataclass, field, replace
from functools import cache, lru_cache
from pathlib import Path
from typing import Literal

from lark import Lark, Transformer, v_args
from lark.exceptions import LarkError, VisitError

from engine.text import number_key

Verb = Literal["substitute", "insert", "delete", "complete", "rewrite", "insert_node", "delete_node"]
Source = Literal["grammar", "llm"]


@dataclass
class RawAmendment:
    """Ce qu'on lit de la table amendements de CLAIR (source [DB-A])."""
    uid: str
    numero: str                       # "CE11", "3 (Rect)"
    texte_ref: str                    # texte sur lequel l'amendement est déposé
    article_vise: str | None          # "ART. UNIQUE", "APRÈS ART. UNIQUE", "TITRE"
    alinea: str | None                # indice seulement : peut contredire le dispositif (amendement 1 de BTC1003)
    dispositif: str                   # alinéas séparés par une ligne vide (ou HTML)
    parent_uid: str | None            # sous-amendement : amendement_parent_ref
    tri_amendement: str | None
    date_sort: str | None             # heure réelle de la décision (ISO)
    sort: str | None = None
    id_identique: str | None = None   # id_discussion_identique : une série votée ensemble


@dataclass
class Target:
    """Cible symbolique, résolue plus tard contre une version (étape 2).

    alinea : numéro dans le texte visé (ou dans l'amendement parent pour un sous-amendement) ;
    None = l'article entier. phrase : rang de la phrase dans l'alinéa. -1 = « dernier », -2 = « avant-dernier ».
    jusqua : dernier alinéa d'une plage (« les alinéas 4 à 6 »).
    mots : mots d'ancrage cités ; occurrence : rang si précisé ;
    position : où agir dans le nœud ou autour des mots.
    """
    alinea: int | None = None
    until: int | None = None
    sentence: int | None = None
    words: str | None = None
    occurrence: int | None = None
    position: Literal["dans", "debut", "fin", "avant", "apres"] = "dans"
    node: Literal["alinea", "phrase", "article", "titre"] = "alinea"


@dataclass
class Operation:
    verb: Verb
    target: Target
    old_words: str | None = None   # substitute, delete d'une portion
    text: str | None = None          # nouveaux mots, phrase, alinéa(s) ou article inséré
    nested: "Operation | None" = None   # opération sur un autre texte (le code) portée par le texte inséré
    # Alinéas déjà construits quand des sous-amendements ont modifié le texte : garde qui a écrit quels mots.
    provenance: tuple | None = field(default=None, compare=False, repr=False)


@dataclass
class Scope:
    """Où l'amendement agit, d'après article_vise."""
    node: Literal["article", "titre", "article_additionnel"]
    article: str | None               # "UNIQUE", "PREMIER", "2"
    before_after: Literal["avant", "apres"] | None = None


@dataclass
class AmendmentAnalysis:
    uid: str
    parent_uid: str | None
    scope: Scope
    operations: list[Operation]
    source: Source
    confidence: float
    errors: list[str] = field(default_factory=list)


@dataclass
class Instruction:
    """Une instruction du dispositif : le chapeau (« À l'alinéa 2, substituer au mot : ») et les alinéas qui suivent.

    Dans l'open data, chaque citation est souvent un alinéa à part (« puni », / le mot : / « passible ».) :
    citations garde ces alinéas tels quels, guillemets compris.
    """
    numero: str | None                # "I", "II" ; None si le dispositif n'en a qu'une
    heading: str
    quotes: list[str]
    consequently: bool = False


COLUMNS = ("uid, numero, texte_ref, article_vise, alinea, dispositif, amendement_parent_ref, tri_amendement, "
            "to_char(date_sort, 'YYYY-MM-DD\"T\"HH24:MI:SS'), sort, id_discussion_identique")


def load_amendment(conn, uid):
    """Lit un amendement dans CLAIR et renvoie un AmendementBrut. Lève KeyError si l'uid est absent.

    CLAIR ne garde qu'une ligne par amendement : celle de la dernière rectification.
    """
    line = conn.execute(f"SELECT {COLUMNS} FROM amendements WHERE uid = %s", (uid,)).fetchone()
    if line is None:
        raise KeyError(uid)
    return RawAmendment(*line)


ADDITIONAL_FORM = re.compile(r"^(APRÈS|AVANT)\s+(?:L['’]\s*)?ART(?:ICLE)?\.?\s*(.+)$", re.I)
ARTICLE_FORM = re.compile(r"^ART(?:ICLE)?\.?\s+(.+)$", re.I)


def analyze_scope(article_vise):
    """Traduit article_vise en Portee.

    « ART. UNIQUE » → article UNIQUE ; « APRÈS ART. UNIQUE » → article additionnel après UNIQUE ;
    « TITRE » → titre du texte. Lève ValueError sur une forme inconnue (à compter dans les stats).
    """
    targeted = (article_vise or "").strip()
    if targeted.upper().startswith("TITRE"):
        return Scope("titre", None)
    if additional := ADDITIONAL_FORM.match(targeted):
        direction = "apres" if additional.group(1).upper() == "APRÈS" else "avant"
        return Scope("article_additionnel", additional.group(2).strip().upper(), direction)
    if article := ARTICLE_FORM.match(targeted):
        return Scope("article", article.group(1).strip().upper())
    raise ValueError(f"article_vise inconnu : {article_vise!r}")


PARAGRAPH = re.compile(r"<p\b[^>]*>(.*?)</p>", re.I | re.S)
TAG = re.compile(r"<[^>]+>")


def clean_dispositif(dispositif):
    """Dispositif → liste d'alinéas en texte brut, dans l'ordre. Un <p> = un alinéa, sinon une ligne vide.

    Les accents décomposés sont recomposés, sinon la grammaire ne reconnaît pas « À l'alinéa ». Le reste n'est pas
    normalisé : la grammaire et la recherche d'ancres tolèrent les variantes typographiques, et le texte inséré doit
    rester celui de l'amendement.
    """
    dispositif = unicodedata.normalize("NFC", dispositif)
    pieces = PARAGRAPH.findall(dispositif) or dispositif.split("\n\n")
    paragraphs = (html.unescape(TAG.sub("", m)).strip() for m in pieces)
    return [a for a in paragraphs if a]


INSTRUCTION_NUMBER = re.compile(r"^([IVXLC]+)\.?\s*[–-]\s*")
INSTRUCTION_VERBS = r"(?:À|A|Au|Aux|Après|Avant|Compléter|Supprimer|Substituer|Remplacer|Rédiger|Insérer|Ajouter|Procéder)\b"
# « 1° Supprimer les mots : », « a) À la première phrase, … » : une sous-instruction, pas un alinéa cité.
SUB_INSTRUCTION = re.compile(rf"^(\d+°|[a-z]\))\s+(?={INSTRUCTION_VERBS})")
# « À l'alinéa 3 : », « Modifier ainsi l'alinéa 3 : » : donne sa cible aux sous-instructions qui suivent.
TARGET_HEADER = re.compile(r"^(?:modifier\s+ainsi|à|a)\s+(l['’]\s*alinéa\s+\d+)\s*:$", re.I)
CARRIES_ITS_TARGET = re.compile(r"^(?:à|a|au|aux|après\s+l['’]\s*alinéa|avant\s+l['’]\s*alinéa)\b", re.I)
CONSEQUENTLY = re.compile(r"^en\s+conséquence\s*,\s*", re.I)


def split_instructions(paragraphs):
    """Regroupe les alinéas du dispositif en Instructions.

    Coupe sur « I. – », « II. – »… ; rattache à chaque chapeau les alinéas cités qui le suivent
    (« Rédiger ainsi cet article : » suivi de 9 alinéas pour CE11). Marque en_consequence les
    instructions qui commencent par « En conséquence ».
    """
    instructions, header, items = [], None, False
    for alinea in (a.strip() for a in paragraphs):
        if not alinea:
            continue
        numero = INSTRUCTION_NUMBER.match(alinea)
        # « 1° Après… » est le plus souvent un alinéa cité (un article qui modifie un code). Ce n'est une
        # sous-instruction que si le dispositif s'ouvre ainsi ou si un en-tête de cible l'annonce.
        sub = SUB_INSTRUCTION.match(alinea) if not numero and (items or header or not instructions) else None
        if numero or sub or not (instructions or header):
            heading = alinea[(numero or sub).end():] if numero or sub else alinea
            consequence = CONSEQUENTLY.match(heading)
            if consequence:
                heading = heading[consequence.end():]
            target = TARGET_HEADER.match(heading)
            if not sub:
                header, items = None, False
            if target and not sub:
                header = f"À {target.group(1)}"   # pas une instruction : la cible des sous-instructions
                continue
            items = items or bool(sub)
            if sub and header and not CARRIES_ITS_TARGET.match(heading):
                heading = f"{header}, {heading[0].lower()}{heading[1:]}"
            wording = numero.group(1) if numero else sub.group(1) if sub else None
            instructions.append(Instruction(wording, heading, [], bool(consequence)))
        elif instructions:
            instructions[-1].quotes.append(alinea)
    return instructions


ORDINALS = {
    "premier": 1, "première": 1, "deuxième": 2, "second": 2, "seconde": 2, "troisième": 3, "quatrième": 4,
    "cinquième": 5, "sixième": 6, "septième": 7, "huitième": 8, "neuvième": 9, "dixième": 10, "onzième": 11,
    "douzième": 12, "treizième": 13, "quatorzième": 14, "quinzième": 15, "seizième": 16, "dix-septième": 17,
    "dix-huitième": 18, "dix-neuvième": 19, "vingtième": 20, "dernier": -1, "dernière": -1,
    "avant-dernier": -2, "avant-dernière": -2,
}
CARDINALS = {"deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
             "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16, "dix-sept": 17,
             "dix-huit": 18, "dix-neuf": 19, "vingt": 20}


def ordinal_as_int(word):
    """« premier » → 1, « dixième » → 10, « 10 » → 10 ; None si ce n'est pas un ordinal."""
    word = word.strip().lower()
    return int(word) if word.isdigit() else ORDINALS.get(word)


def analyze_with_grammar(instruction, scope, preceding=None, previous_operation=None):
    """Formules canoniques → liste d'Operation, ou None si aucune règle ne s'applique.

    Exemples :
      « Rédiger ainsi cet article : »                → rewrite(article)
      « À l'alinéa 2, substituer au mot « a » le mot « b » » → substitute(al.2, "a", "b")
      « Compléter l'alinéa 2 par les mots : »        → complete(al.2, texte)
      « Après l'alinéa 3, insérer les alinéas : »    → insert_node(après al.3)
      « Supprimer cet article. »                     → delete_node(article)
      « À la fin de l'alinéa 10, supprimer les mots » → delete(al.10, mots, position=fin)
    Ne devine jamais : une formule partiellement reconnue, ou qui admet deux analyses, renvoie None.

    Un article additionnel n'a pas de formule : tout le dispositif est le nouvel article (insert_node).
    precedente : cible reprise par « ce même alinéa » ; operation_precedente : celle que recopie
    « Procéder à la même substitution à l'alinéa N » (le texte est copié, jamais ré-analysé).
    """
    if scope.node == "article_additionnel":
        text = "\n".join([instruction.heading, *instruction.quotes])
        return [Operation("insert_node", Target(node="article", position=scope.before_after or "apres"), text=text)]
    try:
        tree = _tree(" ".join([instruction.heading, *instruction.quotes]), "en_ligne")
        if tree:
            # Une instruction qui vise plusieurs alinéas épars (« supprimer les alinéas 4 et 9 ») donne plusieurs opérations.
            output = _ToOperations(scope, preceding, previous_operation=previous_operation).transform(tree)
            return output if isinstance(output, list) else [output]
        tree = instruction.quotes and _tree(instruction.heading, "bloc")
        if tree:
            return [_ToOperations(scope, preceding, instruction.quotes).transform(tree)]
    except VisitError as error:
        if not isinstance(error.orig_exc, _Rejection):
            raise error.orig_exc
    return None


GRAMMARS = Path(__file__).with_name("grammaires")
QUOTE_ANNOUNCEMENT = re.compile(r"(rédigée?s?|suivante?s?)[\s\u00a0\u202f]*:$")
BLANKS = "   "


class _Rejection(Exception):
    """La formule est reconnue mais incohérente (renvoi sans antécédent, nombre d'alinéas faux)."""


def grammar_text(name):
    """Texte Lark de grammaires/<nom>.lark, précédé de grammaires/commun.lark (voir l'en-tête de commun.lark)."""
    return "\n".join((GRAMMARS / f"{n}.lark").read_text(encoding="utf-8") for n in ("commun", name))


@cache
def _parser():
    return Lark(grammar_text("amendements"), start=["en_ligne", "bloc"], parser="earley",
                ambiguity="explicit")


@lru_cache(maxsize=50000)
def _tree(text, start):
    """Arbre de texte selon la règle start, ou None s'il n'y en a aucun ou plusieurs.

    En cache : Earley coûte cher, et un même amendement s'analyse plusieurs fois (consolidation, mesures).
    Le Transformer ne modifie pas l'arbre qu'il reçoit.
    """
    try:
        tree = _parser().parse(text, start=start)
    except LarkError:
        return None
    return None if any(tree.find_data("_ambig")) else tree


@v_args(inline=True)
class _ToOperations(Transformer):
    """Arbre de grammaires/amendements.lark → Operation, une méthode par alias, des feuilles vers la racine.

    Les désignations deviennent des Cible ; une action en ligne devient une fonction qui attend la cible
    du préfixe (« À la fin de l'alinéa 3, … »), que seule la règle en_ligne connaît.
    """

    def __init__(self, scope, preceding=None, quotes=(), previous_operation=None):
        super().__init__()
        self.scope, self.preceding, self.quotes = scope, preceding, list(quotes)
        self.previous_operation = previous_operation

    # ---- terminaux

    def CITATION(self, token):
        """« puni » → puni ; la ponctuation intérieure est gardée."""
        return token[1:-1].strip(BLANKS)

    def APRES(self, token):
        return "apres" if token.lower() == "après" else "avant"

    def NOMBRE(self, token):
        return int(token)

    def ORDINAL(self, token):
        return ordinal_as_int(token)

    def CARDINAL(self, token):
        return int(token) if token.isdigit() else CARDINALS[token.lower()]

    # ---- désignations

    def alinea_numero(self, numero):
        return Target(alinea=numero)

    alinea_ordinal = alinea_numero

    def meme_alinea(self, numero=None):
        if numero is not None:
            return Target(alinea=numero)
        if self.preceding is None or self.preceding.alinea is None:
            raise _Rejection("« même alinéa » sans alinéa précédent")
        return Target(alinea=self.preceding.alinea)

    def alineas_de_a(self, beginning, end):
        return Target(alinea=beginning, until=end)

    def alineas_liste(self, *numbers):
        """« les alinéas 6 et 7 » : une plage ; « les alinéas 4, 9 et 12 » : une liste de cibles, pour les seules
        instructions qui savent se répéter (supprimer, « procéder à la même… »)."""
        if list(numbers) == list(range(numbers[0], numbers[-1] + 1)):
            return Target(alinea=numbers[0], until=numbers[-1])
        if sorted(set(numbers)) != list(numbers):
            raise _Rejection("alinéas en désordre")
        return [Target(alinea=n) for n in numbers]

    def cet_alinea(self):
        # Le préfixe de l'instruction dit lequel (« À l'alinéa 3, après le mot…, rédiger ainsi la fin de cet alinéa »).
        return Target(node="courant")

    def occurrence(self, rank):
        return rank

    def phrase_de(self, rank, alinea=None):
        return replace(_one(alinea) if alinea else self._alinea_implicite(), sentence=rank, node="phrase")

    def meme_phrase(self, alinea=None):
        if self.preceding is None or self.preceding.sentence is None:
            raise _Rejection("« même phrase » sans phrase précédente")
        return self.phrase_de(self.preceding.sentence, alinea)

    def _alinea_implicite(self):
        """« À la seconde phrase, … » : l'alinéa de l'instruction précédente ; sinon alinea reste None et l'étape 3
        ne l'accepte que si l'article n'a qu'un alinéa."""
        previous = self.preceding.alinea if self.preceding is not None else None
        return Target(alinea=previous)

    def cet_article(self):
        return Target(node="article")

    def article_numero(self, numero):
        # « Rédiger ainsi l'article 6 » : seulement si c'est l'article que vise l'amendement.
        if number_key(str(numero)) != number_key(self.scope.article or ""):
            raise _Rejection(f"article {numero} désigné, article {self.scope.article} visé")
        return Target(node="article")

    def titre(self):
        return Target(node="titre")

    # ---- en ligne

    def fin_de(self, target=None):
        return _everywhere(target or Target(node="article"), position="fin")

    def debut_de(self, target=None):
        return _everywhere(target or Target(node="article"), position="debut")

    def dans(self, target):
        return target

    def en_ligne(self, *children):
        *prefix, operation = children
        base = prefix[0] if prefix else Target(node="titre" if self.scope.node == "titre" else "article")
        return operation(_one(base))

    def substitute(self, *args):
        rank, (old_items, new_items) = _rank_and_quotes(args)
        return lambda base: Operation("substitute", replace(base, occurrence=rank), old_words=old_items, text=new_items)

    def delete_mots(self, *args):
        rank, (old_items,) = _rank_and_quotes(args)
        return lambda base: Operation("delete", replace(base, occurrence=rank), old_words=old_items)

    def insert_mots(self, direction, *args):
        rank, (anchor, words) = _rank_and_quotes(args)
        return lambda base: Operation("insert", replace(base, words=anchor, position=direction, occurrence=rank), text=words)

    def rewrite_fin(self, direction, *args):
        # « Après le mot : « a », rédiger ainsi la fin de l'alinéa 4 : « … » » : tout ce qui suit l'ancre est remplacé.
        target = next((a for a in args if isinstance(a, Target)), Target(node="courant"))
        rank, (anchor, text) = _rank_and_quotes([a for a in args if not isinstance(a, Target)])
        return lambda base: Operation(
            "rewrite", replace(_or_prefix(target, base, self.preceding), words=anchor, position=direction, occurrence=rank), text=text)

    def delete_fin(self, direction, *args):
        target = next((a for a in args if isinstance(a, Target)), Target(node="courant"))
        rank, (anchor,) = _rank_and_quotes([a for a in args if not isinstance(a, Target)])
        return lambda base: Operation("delete", replace(_or_prefix(target, base, self.preceding), words=anchor, position=direction, occurrence=rank))

    def renvoi(self, gender, target=None):
        # « Procéder à la même substitution à l'alinéa 5 » : mêmes mots, autre cible.
        model = self.previous_operation
        verbs = {"substitution": ("substitute",), "suppression": ("delete",), "insertion": ("insert", "complete")}
        if model is None or model.verb not in verbs[gender.lower()]:
            raise _Rejection("renvoi sans opération de ce genre")
        def operation(base):
            # « aux alinéas 7 et 8 », « à la fin des alinéas 41 et 71 » : une copie par alinéa visé.
            targeted_items = target if target is not None else base
            operations = []
            for targeted_one in targeted_items if isinstance(targeted_items, list) else [targeted_items]:
                for numero in range(targeted_one.alinea, targeted_one.until + 1) if targeted_one.until else [targeted_one.alinea]:
                    edge = targeted_one.position in ("fin", "debut") and model.target.position in ("dans", "fin", "debut")
                    operations.append(replace(model, provenance=None, target=replace(
                        model.target, alinea=numero, sentence=targeted_one.sentence, node=targeted_one.node, until=None,
                        position=targeted_one.position if edge else model.target.position)))
            return operations
        return operation

    def insert_bord(self, *count_and_text):
        # « Au début de l'alinéa 3, ajouter les mots : » : sans mot d'ancrage, seul un bord du nœud situe l'ajout.
        def operation(base):
            if base.position not in ("debut", "fin"):
                raise _Rejection("insertion sans mot d'ancrage ni début ou fin")
            return Operation("insert", base, text=count_and_text[-1])
        return operation

    def complete(self, target, *count_and_text):
        # « par les deux phrases suivantes » laisse passer le CARDINAL avant la citation.
        return lambda base: Operation("complete", _or_prefix(_one(target), base, self.preceding), text=count_and_text[-1])

    def delete_node(self, target):
        if isinstance(target, list):
            return lambda base: [Operation("delete_node", c) for c in target]
        return lambda base: Operation("delete_node", _or_prefix(target, base, self.preceding))

    def rewrite(self, target, text):
        return lambda base: Operation("rewrite", _or_prefix(_one(target), base, self.preceding), text=text)

    # ---- bloc : le texte inséré est fait des alinéas cités

    def un_alinea(self):
        return 1, "alinea"

    def n_alineas(self, count):
        return count, "alinea"

    def alineas_sans_nombre(self):
        return None, "alinea"

    def une_phrase(self):
        return 1, "phrase"

    def n_phrases(self, count):
        return count, "phrase"

    def _texte(self, announcement=None):
        """Texte des alinéas cités ; vérifie le nombre d'alinéas annoncés (pas celui des phrases)."""
        if announcement and announcement[1] == "alinea" and announcement[0] not in (None, len(self.quotes)):
            raise _Rejection(f"{announcement[0]} alinéas annoncés, {len(self.quotes)} cités")
        return _quoted_text(self.quotes)

    def b_rewrite(self, target):
        return Operation("rewrite", _or_prefix(_one(target), None, self.preceding), text=self._texte())

    def b_rewrite_compte(self, target, announcement):
        return Operation("rewrite", _or_prefix(_one(target), None, self.preceding), text=self._texte(announcement))

    def b_insert_node(self, direction, target, announcement):
        target = _or_prefix(_one(target), None, self.preceding)
        return Operation("insert_node", replace(target, position=direction), text=self._texte(announcement))

    def b_insert_debut(self, *target_and_announcement):
        # « Au début, ajouter l'alinéa suivant : » vise l'article ; « Au début de l'alinéa 3, … » cet alinéa.
        *target, announcement = target_and_announcement
        target = _one(target[0]) if target else Target(node="article")
        return Operation("insert_node", replace(target, position="debut"), text=self._texte(announcement))

    def b_complete(self, target, announcement=None):
        target = _or_prefix(_one(target), None, self.preceding)
        # Des mots ou des phrases au bout d'un alinéa : complete ; des alinéas, ou un article : nouveaux nœuds.
        if (announcement is None or announcement[1] == "phrase") and target.node != "article":
            return Operation("complete", target, text=self._texte())
        position = "fin" if target.alinea is None else "apres"
        return Operation("insert_node", replace(target, position=position), text=self._texte(announcement))


def _rank_and_quotes(args):
    """Sépare le rang d'occurrence éventuel (un entier, « la seconde occurrence du mot ») des citations."""
    return next((a for a in args if isinstance(a, int)), None), [a for a in args if isinstance(a, str)]


def _one(target):
    """Refuse une liste d'alinéas épars là où l'instruction ne peut viser qu'un nœud ou une plage."""
    if isinstance(target, list):
        raise _Rejection("plusieurs alinéas épars pour une instruction qui n'en vise qu'un")
    return target


def _everywhere(target, **fields):
    if isinstance(target, list):
        return [replace(c, **fields) for c in target]
    return replace(target, **fields)


def _or_prefix(target, base, preceding=None):
    """« cet alinéa », « l'alinéa » sans numéro : l'alinéa du préfixe, sinon celui de l'instruction précédente
    (« II. – En conséquence, compléter cet alinéa… »). Aucun des deux : on ne devine pas.
    La phrase visée est gardée (« supprimer la dernière phrase de l'alinéa »)."""
    known = base if base is not None and base.alinea is not None else preceding
    if target.node == "courant":
        if base is not None and (base.alinea is not None or base.position != "dans"):
            return base
        if known is None or known.alinea is None:
            raise _Rejection("« cet alinéa » sans alinéa connu")
        return Target(alinea=known.alinea)
    if target.node == "phrase" and target.alinea is None and known is not None and known.alinea is not None:
        return replace(target, alinea=known.alinea)
    return target


def _quoted_text(quotes):
    """Alinéas cités → texte inséré, un alinéa par ligne.

    Entre guillemets (convention : « ouvre chaque alinéa, » ferme le dernier), on retire ces guillemets ;
    sans guillemets (CE11 : « Rédiger ainsi cet article : » puis le nouvel article), on garde tout.
    Mais un texte qui insère à son tour un article cite ce qui suit « ainsi rédigé : ». L'amendement n'écrit souvent
    qu'un seul guillemet pour les deux niveaux : ces alinéas gardent le leur jusqu'à celui qui ferme la citation.
    Le » final n'est retiré que s'il reste sans « : quand le texte finit par une citation
    (« … les mots : « peut également intervenir ». »), l'AN n'écrit souvent qu'un seul », celui de la citation.
    """
    lignes = [c.strip(BLANKS) for c in quotes]
    if not lignes[0].startswith("«"):
        return "\n".join(lignes)
    output, quoted = [], False
    for line in lignes:
        body = line[1:].lstrip(BLANKS) if line.startswith("«") else line
        closes = body.count("»") > body.count("«")
        if quoted and not body.startswith("«"):
            body = f"« {body}"
        output.append(body)
        if quoted and closes:
            quoted = False
        elif QUOTE_ANNOUNCEMENT.search(body):
            quoted = True
    text = "\n".join(output)
    if text.count("»") > text.count("«"):
        text = re.sub(r"[\s\u00a0\u202f]*»[\s\u00a0\u202f]*[.;]?$", "", text)
    return text


# Clés de la cible dans le JSON du LLM (schéma et cache en français), dans l'ordre des champs de Target.
LLM_TARGET_KEYS = ("alinea", "jusqua", "phrase", "mots", "occurrence", "position", "noeud")
LLM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["operations", "confiance", "rejet"],
    "properties": {
        "operations": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["verbe", "cible", "anciens_mots", "texte"],
            "properties": {
                "verbe": {"enum": ["substitute", "insert", "delete", "complete", "rewrite", "insert_node", "delete_node"]},
                "cible": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["alinea", "jusqua", "phrase", "mots", "occurrence", "position", "noeud"],
                    "properties": {
                        "alinea": {"type": ["integer", "null"]},
                        "jusqua": {"type": ["integer", "null"]},
                        "phrase": {"type": ["integer", "null"]},
                        "mots": {"type": ["string", "null"]},
                        "occurrence": {"type": ["integer", "null"]},
                        "position": {"enum": ["dans", "debut", "fin", "avant", "apres"]},
                        "noeud": {"enum": ["alinea", "phrase", "article", "titre"]},
                    },
                },
                "anciens_mots": {"type": ["string", "null"]},
                "texte": {"type": ["string", "null"]},
            },
        }},
        "confiance": {"type": "number"},
        "rejet": {"type": ["string", "null"]},
    },
}

LLM_GUIDELINES = """Tu traduis une instruction d'amendement parlementaire français en opérations exécutées par un moteur \
déterministe. Tu ne réécris jamais le texte : tu décris le changement.

Verbes :
- substitute : remplace anciens_mots par texte.
- delete : retire anciens_mots ; ou, avec cible.mots et position "apres", retire la fin après ces mots.
- insert : ajoute texte après ou avant cible.mots (position "apres" ou "avant"), ou au début (position "debut").
- complete : ajoute texte à la fin de l'alinéa ou de la phrase visés.
- rewrite : remplace tout le nœud visé par texte ; avec cible.mots et position "apres", remplace la fin après ces mots.
- insert_node : insère de nouveaux alinéas (position "apres", "avant", "debut" ou "fin").
- delete_node : supprime le nœud visé. Ni mots ni texte.

Cible :
- alinea : numéro cité par l'amendement, tel quel ; -1 pour le dernier. jusqua : fin d'une plage d'alinéas contigus.
- phrase : rang de la phrase dans l'alinéa ; -1 pour la dernière. Alors noeud vaut "phrase", sinon "alinea".
- noeud "article" avec alinea null : tout l'article. noeud "titre" : le titre.
- occurrence : rang si l'amendement le précise (« la seconde occurrence »), sinon null. Ne le déduis jamais.
- position : "dans" par défaut ; "fin" pour « à la fin de », "debut" pour « au début de ».

Règles :
- Recopie les mots cités exactement, sans les guillemets qui les entourent, ponctuation intérieure comprise.
- Dans texte, un alinéa par ligne. Retire le guillemet qui ouvre chaque alinéa cité et celui qui ferme la citation, \
mais garde ceux d'un article de code cité à l'intérieur (après « ainsi rédigé : »).
- Plusieurs modifications dans l'instruction : une opération par modification, dans l'ordre.
- « Le même alinéa » reprend la cible précédente, donnée dans le message.
- Si l'instruction n'est pas une modification traduisible (tableau, phrase ambiguë, texte tronqué), renvoie operations \
vide et explique dans rejet. Ne devine pas.
- confiance : entre 0 et 1."""


def llm_message(instruction, scope, article_text, preceding=None):
    """Le message propre à une instruction : les alinéas sont numérotés comme le cite l'amendement."""
    lignes = [f"Portée : {scope.node} {scope.article or ''}".strip()]
    if preceding is not None:
        lignes.append(f"Cible précédente : alinéa {preceding.alinea}, phrase {preceding.sentence}")
    if article_text:
        lignes += ["", "Article visé :"] + [f"({i}) {l}" for i, l in enumerate(article_text.split("\n"), 1)]
    lignes += ["", "Instruction :", instruction.heading, *instruction.quotes]
    return "\n".join(lignes)


def analyze_with_llm(instruction, scope, article_text, preceding=None, call=None):
    """Repli pour ce que la grammaire rejette : LLM contraint au schéma JSON des Operation.

    Le texte de l'article visé est donné en contexte. appel(consignes, message, schema) renvoie le JSON du modèle ;
    le fournisseur reste hors du moteur. Renvoie (operations, confiance) ; une sortie invalide renvoie ([], 0.0).
    """
    output = call(LLM_GUIDELINES, llm_message(instruction, scope, article_text, preceding), LLM_SCHEMA)
    if not output or not output.get("operations"):
        return [], 0.0
    try:
        operations = [Operation(o["verbe"], Target(*(o["cible"][k] for k in LLM_TARGET_KEYS)), o["anciens_mots"], o["texte"])
                      for o in output["operations"]]
    except (KeyError, TypeError):
        return [], 0.0
    if validate_operations(operations, scope):
        return [], 0.0
    return operations, float(output.get("confiance") or 0.0)


def detect_nesting(operation):
    """Repère une opération sur le code portée par le texte inséré (cas « texte → code »).

    CE1 insère un article « Au premier alinéa de l'article L. 201-4 du code rural…, après le mot :
    « administrative » sont insérés les mots… » : l'opération sur le texte est insert_node, et son
    texte porte une opération imbriquée (insert dans L. 201-4) qui sera résolue en phase 2 contre le
    code consolidé ([LEGI]). Renvoie l'opération complétée de son champ imbriquee.
    """
    raise NotImplementedError


def validate_operations(operations, scope):
    """Contrôles de cohérence, sans le texte cible. Renvoie la liste des erreurs (vide = valide).

    Texte non vide pour substitute/complete/insert/rewrite/insert_node ; anciens_mots ou ancre pour
    substitute/delete ; une suppression de nœud ne porte pas de mots.
    """
    errors = []
    for op in operations:
        if op.verb in ("substitute", "complete", "insert", "rewrite", "insert_node") and not (op.text or "").strip():
            errors.append(f"{op.verb} sans texte")
        if op.verb == "substitute" and not op.old_words:
            errors.append("substitute sans anciens mots")
        if op.verb == "delete" and not (op.old_words or op.target.words):
            errors.append("delete sans mots")
        if op.verb == "delete_node" and (op.old_words or op.target.words):
            errors.append("delete_node avec des mots")
    return errors


def analyze_amendment(amendment, article_text=None, llm_call=None):
    """Point d'entrée de l'étape 1 : AmendementBrut → AnalyseAmendement.

    nettoyer_dispositif → decouper_instructions → grammaire pour chaque instruction, puis LLM si appel_llm est
    fourni → valider_operations. Un article additionnel est une seule opération, même si son texte contient
    « I. – ». Une instruction non reconnue est rapportée dans erreurs et laisse operations vide : pas d'amendement à
    moitié appliqué. source vaut "llm" dès qu'une instruction passe par le LLM ; confiance = minimum des instructions.
    """
    try:
        scope = analyze_scope(amendment.article_vise)
    except ValueError as error:
        return AmendmentAnalysis(amendment.uid, amendment.parent_uid, Scope("article", None), [], "grammar", 0.0,
                                 [str(error)])
    paragraphs = clean_dispositif(amendment.dispositif or "")
    if scope.node == "article_additionnel":
        instructions = [Instruction(None, paragraphs[0], paragraphs[1:])] if paragraphs else []
    else:
        instructions = split_instructions(paragraphs)
    operations, preceding, errors = [], None, []
    source, confidence = "grammar", 1.0
    for instruction in instructions:
        ops = analyze_with_grammar(instruction, scope, preceding, operations[-1] if operations else None)
        if ops is None and llm_call is not None:
            ops, score = analyze_with_llm(instruction, scope, article_text, preceding, llm_call)
            source, confidence = "llm", min(confidence, score)
        if not ops:
            errors.append(f"non reconnu : {instruction.heading[:80]}")
            continue
        operations += ops
        preceding = ops[-1].target
    errors += validate_operations(operations, scope)
    if not instructions:
        errors.append("dispositif vide")
    return AmendmentAnalysis(amendment.uid, amendment.parent_uid, scope, [] if errors else operations, source,
                             0.0 if errors else confidence, errors)
