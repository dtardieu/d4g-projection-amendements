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

from moteur.texte import cle_numero

Verbe = Literal["substitute", "insert", "delete", "complete", "rewrite", "insert_node", "delete_node"]
Source = Literal["grammar", "llm"]


@dataclass
class AmendementBrut:
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
class Cible:
    """Cible symbolique, résolue plus tard contre une version (étape 2).

    alinea : numéro dans le texte visé (ou dans l'amendement parent pour un sous-amendement) ;
    None = l'article entier. phrase : rang de la phrase dans l'alinéa. -1 = « dernier », -2 = « avant-dernier ».
    jusqua : dernier alinéa d'une plage (« les alinéas 4 à 6 »).
    mots : mots d'ancrage cités ; occurrence : rang si précisé ;
    position : où agir dans le nœud ou autour des mots.
    """
    alinea: int | None = None
    jusqua: int | None = None
    phrase: int | None = None
    mots: str | None = None
    occurrence: int | None = None
    position: Literal["dans", "debut", "fin", "avant", "apres"] = "dans"
    noeud: Literal["alinea", "phrase", "article", "titre"] = "alinea"


@dataclass
class Operation:
    verbe: Verbe
    cible: Cible
    anciens_mots: str | None = None   # substitute, delete d'une portion
    texte: str | None = None          # nouveaux mots, phrase, alinéa(s) ou article inséré
    imbriquee: "Operation | None" = None   # opération sur un autre texte (le code) portée par le texte inséré
    # Alinéas déjà construits quand des sous-amendements ont modifié le texte : garde qui a écrit quels mots.
    provenance: tuple | None = field(default=None, compare=False, repr=False)


@dataclass
class Portee:
    """Où l'amendement agit, d'après article_vise."""
    noeud: Literal["article", "titre", "article_additionnel"]
    article: str | None               # "UNIQUE", "PREMIER", "2"
    avant_apres: Literal["avant", "apres"] | None = None


@dataclass
class AnalyseAmendement:
    uid: str
    parent_uid: str | None
    portee: Portee
    operations: list[Operation]
    source: Source
    confiance: float
    erreurs: list[str] = field(default_factory=list)


@dataclass
class Instruction:
    """Une instruction du dispositif : le chapeau (« À l'alinéa 2, substituer au mot : ») et les alinéas qui suivent.

    Dans l'open data, chaque citation est souvent un alinéa à part (« puni », / le mot : / « passible ».) :
    citations garde ces alinéas tels quels, guillemets compris.
    """
    numero: str | None                # "I", "II" ; None si le dispositif n'en a qu'une
    chapeau: str
    citations: list[str]
    en_consequence: bool = False


COLONNES = ("uid, numero, texte_ref, article_vise, alinea, dispositif, amendement_parent_ref, tri_amendement, "
            "to_char(date_sort, 'YYYY-MM-DD\"T\"HH24:MI:SS'), sort, id_discussion_identique")


def charger_amendement(conn, uid):
    """Lit un amendement dans CLAIR et renvoie un AmendementBrut. Lève KeyError si l'uid est absent.

    CLAIR ne garde qu'une ligne par amendement : celle de la dernière rectification.
    """
    ligne = conn.execute(f"SELECT {COLONNES} FROM amendements WHERE uid = %s", (uid,)).fetchone()
    if ligne is None:
        raise KeyError(uid)
    return AmendementBrut(*ligne)


FORME_ADDITIONNEL = re.compile(r"^(APRÈS|AVANT)\s+(?:L['’]\s*)?ART(?:ICLE)?\.?\s*(.+)$", re.I)
FORME_ARTICLE = re.compile(r"^ART(?:ICLE)?\.?\s+(.+)$", re.I)


def analyser_portee(article_vise):
    """Traduit article_vise en Portee.

    « ART. UNIQUE » → article UNIQUE ; « APRÈS ART. UNIQUE » → article additionnel après UNIQUE ;
    « TITRE » → titre du texte. Lève ValueError sur une forme inconnue (à compter dans les stats).
    """
    vise = (article_vise or "").strip()
    if vise.upper().startswith("TITRE"):
        return Portee("titre", None)
    if additionnel := FORME_ADDITIONNEL.match(vise):
        sens = "apres" if additionnel.group(1).upper() == "APRÈS" else "avant"
        return Portee("article_additionnel", additionnel.group(2).strip().upper(), sens)
    if article := FORME_ARTICLE.match(vise):
        return Portee("article", article.group(1).strip().upper())
    raise ValueError(f"article_vise inconnu : {article_vise!r}")


PARAGRAPHE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.I | re.S)
BALISE = re.compile(r"<[^>]+>")


def nettoyer_dispositif(dispositif):
    """Dispositif → liste d'alinéas en texte brut, dans l'ordre. Un <p> = un alinéa, sinon une ligne vide.

    Les accents décomposés sont recomposés, sinon la grammaire ne reconnaît pas « À l'alinéa ». Le reste n'est pas
    normalisé : la grammaire et la recherche d'ancres tolèrent les variantes typographiques, et le texte inséré doit
    rester celui de l'amendement.
    """
    dispositif = unicodedata.normalize("NFC", dispositif)
    morceaux = PARAGRAPHE.findall(dispositif) or dispositif.split("\n\n")
    alineas = (html.unescape(BALISE.sub("", m)).strip() for m in morceaux)
    return [a for a in alineas if a]


NUMERO_INSTRUCTION = re.compile(r"^([IVXLC]+)\.?\s*[–-]\s*")
VERBES_D_INSTRUCTION = r"(?:À|A|Au|Aux|Après|Avant|Compléter|Supprimer|Substituer|Remplacer|Rédiger|Insérer|Ajouter|Procéder)\b"
# « 1° Supprimer les mots : », « a) À la première phrase, … » : une sous-instruction, pas un alinéa cité.
SOUS_INSTRUCTION = re.compile(rf"^(\d+°|[a-z]\))\s+(?={VERBES_D_INSTRUCTION})")
# « À l'alinéa 3 : », « Modifier ainsi l'alinéa 3 : » : donne sa cible aux sous-instructions qui suivent.
EN_TETE_CIBLE = re.compile(r"^(?:modifier\s+ainsi|à|a)\s+(l['’]\s*alinéa\s+\d+)\s*:$", re.I)
PORTE_SA_CIBLE = re.compile(r"^(?:à|a|au|aux|après\s+l['’]\s*alinéa|avant\s+l['’]\s*alinéa)\b", re.I)
EN_CONSEQUENCE = re.compile(r"^en\s+conséquence\s*,\s*", re.I)


def decouper_instructions(alineas):
    """Regroupe les alinéas du dispositif en Instructions.

    Coupe sur « I. – », « II. – »… ; rattache à chaque chapeau les alinéas cités qui le suivent
    (« Rédiger ainsi cet article : » suivi de 9 alinéas pour CE11). Marque en_consequence les
    instructions qui commencent par « En conséquence ».
    """
    instructions, en_tete, liste = [], None, False
    for alinea in (a.strip() for a in alineas):
        if not alinea:
            continue
        numero = NUMERO_INSTRUCTION.match(alinea)
        # « 1° Après… » est le plus souvent un alinéa cité (un article qui modifie un code). Ce n'est une
        # sous-instruction que si le dispositif s'ouvre ainsi ou si un en-tête de cible l'annonce.
        sous = SOUS_INSTRUCTION.match(alinea) if not numero and (liste or en_tete or not instructions) else None
        if numero or sous or not (instructions or en_tete):
            chapeau = alinea[(numero or sous).end():] if numero or sous else alinea
            consequence = EN_CONSEQUENCE.match(chapeau)
            if consequence:
                chapeau = chapeau[consequence.end():]
            cible = EN_TETE_CIBLE.match(chapeau)
            if not sous:
                en_tete, liste = None, False
            if cible and not sous:
                en_tete = f"À {cible.group(1)}"   # pas une instruction : la cible des sous-instructions
                continue
            liste = liste or bool(sous)
            if sous and en_tete and not PORTE_SA_CIBLE.match(chapeau):
                chapeau = f"{en_tete}, {chapeau[0].lower()}{chapeau[1:]}"
            libelle = numero.group(1) if numero else sous.group(1) if sous else None
            instructions.append(Instruction(libelle, chapeau, [], bool(consequence)))
        elif instructions:
            instructions[-1].citations.append(alinea)
    return instructions


ORDINAUX = {
    "premier": 1, "première": 1, "deuxième": 2, "second": 2, "seconde": 2, "troisième": 3, "quatrième": 4,
    "cinquième": 5, "sixième": 6, "septième": 7, "huitième": 8, "neuvième": 9, "dixième": 10, "onzième": 11,
    "douzième": 12, "treizième": 13, "quatorzième": 14, "quinzième": 15, "seizième": 16, "dix-septième": 17,
    "dix-huitième": 18, "dix-neuvième": 19, "vingtième": 20, "dernier": -1, "dernière": -1,
    "avant-dernier": -2, "avant-dernière": -2,
}
CARDINAUX = {"deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
             "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16, "dix-sept": 17,
             "dix-huit": 18, "dix-neuf": 19, "vingt": 20}


def ordinal_en_entier(mot):
    """« premier » → 1, « dixième » → 10, « 10 » → 10 ; None si ce n'est pas un ordinal."""
    mot = mot.strip().lower()
    return int(mot) if mot.isdigit() else ORDINAUX.get(mot)


def analyser_par_grammaire(instruction, portee, precedente=None, operation_precedente=None):
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
    if portee.noeud == "article_additionnel":
        texte = "\n".join([instruction.chapeau, *instruction.citations])
        return [Operation("insert_node", Cible(noeud="article", position=portee.avant_apres or "apres"), texte=texte)]
    try:
        arbre = _arbre(" ".join([instruction.chapeau, *instruction.citations]), "en_ligne")
        if arbre:
            # Une instruction qui vise plusieurs alinéas épars (« supprimer les alinéas 4 et 9 ») donne plusieurs opérations.
            sortie = _VersOperations(portee, precedente, operation_precedente=operation_precedente).transform(arbre)
            return sortie if isinstance(sortie, list) else [sortie]
        arbre = instruction.citations and _arbre(instruction.chapeau, "bloc")
        if arbre:
            return [_VersOperations(portee, precedente, instruction.citations).transform(arbre)]
    except VisitError as erreur:
        if not isinstance(erreur.orig_exc, _Rejet):
            raise erreur.orig_exc
    return None


GRAMMAIRES = Path(__file__).with_name("grammaires")
ANNONCE_CITATION = re.compile(r"(rédigée?s?|suivante?s?)[\s\u00a0\u202f]*:$")
BLANCS = "   "


class _Rejet(Exception):
    """La formule est reconnue mais incohérente (renvoi sans antécédent, nombre d'alinéas faux)."""


def texte_grammaire(nom):
    """Texte Lark de grammaires/<nom>.lark, précédé de grammaires/commun.lark (voir l'en-tête de commun.lark)."""
    return "\n".join((GRAMMAIRES / f"{n}.lark").read_text(encoding="utf-8") for n in ("commun", nom))


@cache
def _parser():
    return Lark(texte_grammaire("amendements"), start=["en_ligne", "bloc"], parser="earley",
                ambiguity="explicit")


@lru_cache(maxsize=50000)
def _arbre(texte, start):
    """Arbre de texte selon la règle start, ou None s'il n'y en a aucun ou plusieurs.

    En cache : Earley coûte cher, et un même amendement s'analyse plusieurs fois (consolidation, mesures).
    Le Transformer ne modifie pas l'arbre qu'il reçoit.
    """
    try:
        arbre = _parser().parse(texte, start=start)
    except LarkError:
        return None
    return None if any(arbre.find_data("_ambig")) else arbre


@v_args(inline=True)
class _VersOperations(Transformer):
    """Arbre de grammaires/amendements.lark → Operation, une méthode par alias, des feuilles vers la racine.

    Les désignations deviennent des Cible ; une action en ligne devient une fonction qui attend la cible
    du préfixe (« À la fin de l'alinéa 3, … »), que seule la règle en_ligne connaît.
    """

    def __init__(self, portee, precedente=None, citations=(), operation_precedente=None):
        super().__init__()
        self.portee, self.precedente, self.citations = portee, precedente, list(citations)
        self.operation_precedente = operation_precedente

    # ---- terminaux

    def CITATION(self, jeton):
        """« puni » → puni ; la ponctuation intérieure est gardée."""
        return jeton[1:-1].strip(BLANCS)

    def APRES(self, jeton):
        return "apres" if jeton.lower() == "après" else "avant"

    def NOMBRE(self, jeton):
        return int(jeton)

    def ORDINAL(self, jeton):
        return ordinal_en_entier(jeton)

    def CARDINAL(self, jeton):
        return int(jeton) if jeton.isdigit() else CARDINAUX[jeton.lower()]

    # ---- désignations

    def alinea_numero(self, numero):
        return Cible(alinea=numero)

    alinea_ordinal = alinea_numero

    def meme_alinea(self, numero=None):
        if numero is not None:
            return Cible(alinea=numero)
        if self.precedente is None or self.precedente.alinea is None:
            raise _Rejet("« même alinéa » sans alinéa précédent")
        return Cible(alinea=self.precedente.alinea)

    def alineas_de_a(self, debut, fin):
        return Cible(alinea=debut, jusqua=fin)

    def alineas_liste(self, *numeros):
        """« les alinéas 6 et 7 » : une plage ; « les alinéas 4, 9 et 12 » : une liste de cibles, pour les seules
        instructions qui savent se répéter (supprimer, « procéder à la même… »)."""
        if list(numeros) == list(range(numeros[0], numeros[-1] + 1)):
            return Cible(alinea=numeros[0], jusqua=numeros[-1])
        if sorted(set(numeros)) != list(numeros):
            raise _Rejet("alinéas en désordre")
        return [Cible(alinea=n) for n in numeros]

    def cet_alinea(self):
        # Le préfixe de l'instruction dit lequel (« À l'alinéa 3, après le mot…, rédiger ainsi la fin de cet alinéa »).
        return Cible(noeud="courant")

    def occurrence(self, rang):
        return rang

    def phrase_de(self, rang, alinea=None):
        return replace(_une(alinea) if alinea else self._alinea_implicite(), phrase=rang, noeud="phrase")

    def meme_phrase(self, alinea=None):
        if self.precedente is None or self.precedente.phrase is None:
            raise _Rejet("« même phrase » sans phrase précédente")
        return self.phrase_de(self.precedente.phrase, alinea)

    def _alinea_implicite(self):
        """« À la seconde phrase, … » : l'alinéa de l'instruction précédente ; sinon alinea reste None et l'étape 3
        ne l'accepte que si l'article n'a qu'un alinéa."""
        precedent = self.precedente.alinea if self.precedente is not None else None
        return Cible(alinea=precedent)

    def cet_article(self):
        return Cible(noeud="article")

    def article_numero(self, numero):
        # « Rédiger ainsi l'article 6 » : seulement si c'est l'article que vise l'amendement.
        if cle_numero(str(numero)) != cle_numero(self.portee.article or ""):
            raise _Rejet(f"article {numero} désigné, article {self.portee.article} visé")
        return Cible(noeud="article")

    def titre(self):
        return Cible(noeud="titre")

    # ---- en ligne

    def fin_de(self, cible=None):
        return _partout(cible or Cible(noeud="article"), position="fin")

    def debut_de(self, cible=None):
        return _partout(cible or Cible(noeud="article"), position="debut")

    def dans(self, cible):
        return cible

    def en_ligne(self, *enfants):
        *prefixe, operation = enfants
        base = prefixe[0] if prefixe else Cible(noeud="titre" if self.portee.noeud == "titre" else "article")
        return operation(_une(base))

    def substitute(self, *args):
        rang, (anciens, nouveaux) = _rang_et_citations(args)
        return lambda base: Operation("substitute", replace(base, occurrence=rang), anciens_mots=anciens, texte=nouveaux)

    def delete_mots(self, *args):
        rang, (anciens,) = _rang_et_citations(args)
        return lambda base: Operation("delete", replace(base, occurrence=rang), anciens_mots=anciens)

    def insert_mots(self, sens, *args):
        rang, (ancre, mots) = _rang_et_citations(args)
        return lambda base: Operation("insert", replace(base, mots=ancre, position=sens, occurrence=rang), texte=mots)

    def rewrite_fin(self, sens, *args):
        # « Après le mot : « a », rédiger ainsi la fin de l'alinéa 4 : « … » » : tout ce qui suit l'ancre est remplacé.
        cible = next((a for a in args if isinstance(a, Cible)), Cible(noeud="courant"))
        rang, (ancre, texte) = _rang_et_citations([a for a in args if not isinstance(a, Cible)])
        return lambda base: Operation(
            "rewrite", replace(_ou_prefixe(cible, base, self.precedente), mots=ancre, position=sens, occurrence=rang), texte=texte)

    def delete_fin(self, sens, *args):
        cible = next((a for a in args if isinstance(a, Cible)), Cible(noeud="courant"))
        rang, (ancre,) = _rang_et_citations([a for a in args if not isinstance(a, Cible)])
        return lambda base: Operation("delete", replace(_ou_prefixe(cible, base, self.precedente), mots=ancre, position=sens, occurrence=rang))

    def renvoi(self, genre, cible=None):
        # « Procéder à la même substitution à l'alinéa 5 » : mêmes mots, autre cible.
        modele = self.operation_precedente
        verbes = {"substitution": ("substitute",), "suppression": ("delete",), "insertion": ("insert", "complete")}
        if modele is None or modele.verbe not in verbes[genre.lower()]:
            raise _Rejet("renvoi sans opération de ce genre")
        def operation(base):
            # « aux alinéas 7 et 8 », « à la fin des alinéas 41 et 71 » : une copie par alinéa visé.
            visees = cible if cible is not None else base
            operations = []
            for visee in visees if isinstance(visees, list) else [visees]:
                for numero in range(visee.alinea, visee.jusqua + 1) if visee.jusqua else [visee.alinea]:
                    bord = visee.position in ("fin", "debut") and modele.cible.position in ("dans", "fin", "debut")
                    operations.append(replace(modele, provenance=None, cible=replace(
                        modele.cible, alinea=numero, phrase=visee.phrase, noeud=visee.noeud, jusqua=None,
                        position=visee.position if bord else modele.cible.position)))
            return operations
        return operation

    def insert_bord(self, *nombre_et_texte):
        # « Au début de l'alinéa 3, ajouter les mots : » : sans mot d'ancrage, seul un bord du nœud situe l'ajout.
        def operation(base):
            if base.position not in ("debut", "fin"):
                raise _Rejet("insertion sans mot d'ancrage ni début ou fin")
            return Operation("insert", base, texte=nombre_et_texte[-1])
        return operation

    def complete(self, cible, *nombre_et_texte):
        # « par les deux phrases suivantes » laisse passer le CARDINAL avant la citation.
        return lambda base: Operation("complete", _ou_prefixe(_une(cible), base, self.precedente), texte=nombre_et_texte[-1])

    def delete_node(self, cible):
        if isinstance(cible, list):
            return lambda base: [Operation("delete_node", c) for c in cible]
        return lambda base: Operation("delete_node", _ou_prefixe(cible, base, self.precedente))

    def rewrite(self, cible, texte):
        return lambda base: Operation("rewrite", _ou_prefixe(_une(cible), base, self.precedente), texte=texte)

    # ---- bloc : le texte inséré est fait des alinéas cités

    def un_alinea(self):
        return 1, "alinea"

    def n_alineas(self, nombre):
        return nombre, "alinea"

    def alineas_sans_nombre(self):
        return None, "alinea"

    def une_phrase(self):
        return 1, "phrase"

    def n_phrases(self, nombre):
        return nombre, "phrase"

    def _texte(self, annonce=None):
        """Texte des alinéas cités ; vérifie le nombre d'alinéas annoncés (pas celui des phrases)."""
        if annonce and annonce[1] == "alinea" and annonce[0] not in (None, len(self.citations)):
            raise _Rejet(f"{annonce[0]} alinéas annoncés, {len(self.citations)} cités")
        return _texte_cite(self.citations)

    def b_rewrite(self, cible):
        return Operation("rewrite", _ou_prefixe(_une(cible), None, self.precedente), texte=self._texte())

    def b_rewrite_compte(self, cible, annonce):
        return Operation("rewrite", _ou_prefixe(_une(cible), None, self.precedente), texte=self._texte(annonce))

    def b_insert_node(self, sens, cible, annonce):
        cible = _ou_prefixe(_une(cible), None, self.precedente)
        return Operation("insert_node", replace(cible, position=sens), texte=self._texte(annonce))

    def b_insert_debut(self, *cible_et_annonce):
        # « Au début, ajouter l'alinéa suivant : » vise l'article ; « Au début de l'alinéa 3, … » cet alinéa.
        *cible, annonce = cible_et_annonce
        cible = _une(cible[0]) if cible else Cible(noeud="article")
        return Operation("insert_node", replace(cible, position="debut"), texte=self._texte(annonce))

    def b_complete(self, cible, annonce=None):
        cible = _ou_prefixe(_une(cible), None, self.precedente)
        # Des mots ou des phrases au bout d'un alinéa : complete ; des alinéas, ou un article : nouveaux nœuds.
        if (annonce is None or annonce[1] == "phrase") and cible.noeud != "article":
            return Operation("complete", cible, texte=self._texte())
        position = "fin" if cible.alinea is None else "apres"
        return Operation("insert_node", replace(cible, position=position), texte=self._texte(annonce))


def _rang_et_citations(args):
    """Sépare le rang d'occurrence éventuel (un entier, « la seconde occurrence du mot ») des citations."""
    return next((a for a in args if isinstance(a, int)), None), [a for a in args if isinstance(a, str)]


def _une(cible):
    """Refuse une liste d'alinéas épars là où l'instruction ne peut viser qu'un nœud ou une plage."""
    if isinstance(cible, list):
        raise _Rejet("plusieurs alinéas épars pour une instruction qui n'en vise qu'un")
    return cible


def _partout(cible, **champs):
    if isinstance(cible, list):
        return [replace(c, **champs) for c in cible]
    return replace(cible, **champs)


def _ou_prefixe(cible, base, precedente=None):
    """« cet alinéa », « l'alinéa » sans numéro : l'alinéa du préfixe, sinon celui de l'instruction précédente
    (« II. – En conséquence, compléter cet alinéa… »). Aucun des deux : on ne devine pas.
    La phrase visée est gardée (« supprimer la dernière phrase de l'alinéa »)."""
    connu = base if base is not None and base.alinea is not None else precedente
    if cible.noeud == "courant":
        if base is not None and (base.alinea is not None or base.position != "dans"):
            return base
        if connu is None or connu.alinea is None:
            raise _Rejet("« cet alinéa » sans alinéa connu")
        return Cible(alinea=connu.alinea)
    if cible.noeud == "phrase" and cible.alinea is None and connu is not None and connu.alinea is not None:
        return replace(cible, alinea=connu.alinea)
    return cible


def _texte_cite(citations):
    """Alinéas cités → texte inséré, un alinéa par ligne.

    Entre guillemets (convention : « ouvre chaque alinéa, » ferme le dernier), on retire ces guillemets ;
    sans guillemets (CE11 : « Rédiger ainsi cet article : » puis le nouvel article), on garde tout.
    Mais un texte qui insère à son tour un article cite ce qui suit « ainsi rédigé : ». L'amendement n'écrit souvent
    qu'un seul guillemet pour les deux niveaux : ces alinéas gardent le leur jusqu'à celui qui ferme la citation.
    Le » final n'est retiré que s'il reste sans « : quand le texte finit par une citation
    (« … les mots : « peut également intervenir ». »), l'AN n'écrit souvent qu'un seul », celui de la citation.
    """
    lignes = [c.strip(BLANCS) for c in citations]
    if not lignes[0].startswith("«"):
        return "\n".join(lignes)
    sortie, cite = [], False
    for ligne in lignes:
        corps = ligne[1:].lstrip(BLANCS) if ligne.startswith("«") else ligne
        ferme = corps.count("»") > corps.count("«")
        if cite and not corps.startswith("«"):
            corps = f"« {corps}"
        sortie.append(corps)
        if cite and ferme:
            cite = False
        elif ANNONCE_CITATION.search(corps):
            cite = True
    texte = "\n".join(sortie)
    if texte.count("»") > texte.count("«"):
        texte = re.sub(r"[\s\u00a0\u202f]*»[\s\u00a0\u202f]*[.;]?$", "", texte)
    return texte


SCHEMA_LLM = {
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

CONSIGNES_LLM = """Tu traduis une instruction d'amendement parlementaire français en opérations exécutées par un moteur \
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


def message_llm(instruction, portee, texte_article, precedente=None):
    """Le message propre à une instruction : les alinéas sont numérotés comme le cite l'amendement."""
    lignes = [f"Portée : {portee.noeud} {portee.article or ''}".strip()]
    if precedente is not None:
        lignes.append(f"Cible précédente : alinéa {precedente.alinea}, phrase {precedente.phrase}")
    if texte_article:
        lignes += ["", "Article visé :"] + [f"({i}) {l}" for i, l in enumerate(texte_article.split("\n"), 1)]
    lignes += ["", "Instruction :", instruction.chapeau, *instruction.citations]
    return "\n".join(lignes)


def analyser_par_llm(instruction, portee, texte_article, precedente=None, appel=None):
    """Repli pour ce que la grammaire rejette : LLM contraint au schéma JSON des Operation.

    Le texte de l'article visé est donné en contexte. appel(consignes, message, schema) renvoie le JSON du modèle ;
    le fournisseur reste hors du moteur. Renvoie (operations, confiance) ; une sortie invalide renvoie ([], 0.0).
    """
    sortie = appel(CONSIGNES_LLM, message_llm(instruction, portee, texte_article, precedente), SCHEMA_LLM)
    if not sortie or not sortie.get("operations"):
        return [], 0.0
    try:
        operations = [Operation(o["verbe"], Cible(**o["cible"]), o["anciens_mots"], o["texte"])
                      for o in sortie["operations"]]
    except (KeyError, TypeError):
        return [], 0.0
    if valider_operations(operations, portee):
        return [], 0.0
    return operations, float(sortie.get("confiance") or 0.0)


def detecter_imbrication(operation):
    """Repère une opération sur le code portée par le texte inséré (cas « texte → code »).

    CE1 insère un article « Au premier alinéa de l'article L. 201-4 du code rural…, après le mot :
    « administrative » sont insérés les mots… » : l'opération sur le texte est insert_node, et son
    texte porte une opération imbriquée (insert dans L. 201-4) qui sera résolue en phase 2 contre le
    code consolidé ([LEGI]). Renvoie l'opération complétée de son champ imbriquee.
    """
    raise NotImplementedError


def valider_operations(operations, portee):
    """Contrôles de cohérence, sans le texte cible. Renvoie la liste des erreurs (vide = valide).

    Texte non vide pour substitute/complete/insert/rewrite/insert_node ; anciens_mots ou ancre pour
    substitute/delete ; une suppression de nœud ne porte pas de mots.
    """
    erreurs = []
    for op in operations:
        if op.verbe in ("substitute", "complete", "insert", "rewrite", "insert_node") and not (op.texte or "").strip():
            erreurs.append(f"{op.verbe} sans texte")
        if op.verbe == "substitute" and not op.anciens_mots:
            erreurs.append("substitute sans anciens mots")
        if op.verbe == "delete" and not (op.anciens_mots or op.cible.mots):
            erreurs.append("delete sans mots")
        if op.verbe == "delete_node" and (op.anciens_mots or op.cible.mots):
            erreurs.append("delete_node avec des mots")
    return erreurs


def analyser_amendement(amendement, texte_article=None, appel_llm=None):
    """Point d'entrée de l'étape 1 : AmendementBrut → AnalyseAmendement.

    nettoyer_dispositif → decouper_instructions → grammaire pour chaque instruction, puis LLM si appel_llm est
    fourni → valider_operations. Un article additionnel est une seule opération, même si son texte contient
    « I. – ». Une instruction non reconnue est rapportée dans erreurs et laisse operations vide : pas d'amendement à
    moitié appliqué. source vaut "llm" dès qu'une instruction passe par le LLM ; confiance = minimum des instructions.
    """
    try:
        portee = analyser_portee(amendement.article_vise)
    except ValueError as erreur:
        return AnalyseAmendement(amendement.uid, amendement.parent_uid, Portee("article", None), [], "grammar", 0.0,
                                 [str(erreur)])
    alineas = nettoyer_dispositif(amendement.dispositif or "")
    if portee.noeud == "article_additionnel":
        instructions = [Instruction(None, alineas[0], alineas[1:])] if alineas else []
    else:
        instructions = decouper_instructions(alineas)
    operations, precedente, erreurs = [], None, []
    source, confiance = "grammar", 1.0
    for instruction in instructions:
        ops = analyser_par_grammaire(instruction, portee, precedente, operations[-1] if operations else None)
        if ops is None and appel_llm is not None:
            ops, score = analyser_par_llm(instruction, portee, texte_article, precedente, appel_llm)
            source, confiance = "llm", min(confiance, score)
        if not ops:
            erreurs.append(f"non reconnu : {instruction.chapeau[:80]}")
            continue
        operations += ops
        precedente = ops[-1].cible
    erreurs += valider_operations(operations, portee)
    if not instructions:
        erreurs.append("dispositif vide")
    return AnalyseAmendement(amendement.uid, amendement.parent_uid, portee, [] if erreurs else operations, source,
                             0.0 if erreurs else confiance, erreurs)
