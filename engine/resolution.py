"""Étape 2 : trouver dans la version courante ce que vise une opération.

Le contenu d'abord : le numéro d'alinéa se lit dans la version sur laquelle l'amendement a été écrit (reference),
ce qui donne un identifiant stable, puis les mots se cherchent dans la version courante. Ainsi une insertion
adoptée plus tôt ne décale rien. Jamais de repli sur une position : ce qui ne se résout pas lève une erreur.

Les amendements citent le texte avec des écarts (« au delà » pour « au‑delà », « 4000 » pour « 4 000 », une coquille,
un numéro d'alinéa faux, « à la fin » pour des mots proches de la fin). La recherche les tolère par crans, du plus sûr
au moins sûr, et l'Emplacement dit quels crans ont servi : rien n'est toléré en silence.
"""

import difflib
import re
import unicodedata
from dataclasses import dataclass

from engine.text import (article_by_id, split_tokens, terminal_beginning, is_blank, normalize, phrases, find_article)


class TargetError(Exception):
    """L'opération ne peut pas s'appliquer à cette version."""


class DanglingTarget(TargetError):
    """Ce que vise l'opération n'existe plus : l'amendement est tombé."""


class AmbiguousTarget(TargetError):
    """Les mots apparaissent plusieurs fois et aucun rang n'est donné : on signale, on ne devine pas."""


# Ce qui peut précéder le vrai début d'un alinéa : guillemet, « Art. L. 250-10. – », « I. – », « 1° », « a) ».
LABEL = re.compile(r"^[«\s]*(Art\.[^–-]*[–-]\s*)?([IVXLCDM]+\.\s*[–-]\s*)?(\d+°(\s*bis)?\s*)?([a-z]\)\s*)?$")


@dataclass(frozen=True)
class Location:
    article_id: str
    alinea_ids: tuple = ()          # vide : l'article entier
    beginning: int | None = None        # portion de jetons dans alinea_ids[0], fin exclue
    end: int | None = None
    lectures: frozenset = frozenset()
    tolerances: frozenset = frozenset()   # écarts acceptés entre la citation et le texte (TYPOGRAPHIE…)


TYPOGRAPHY = "typographie"          # mêmes lettres et chiffres, à la ponctuation, aux tirets et aux accents près
APPROXIMATE = "mots approchés"          # une coquille dans la citation
OTHER_ALINEA = "autre alinéa"        # mots absents de l'alinéa cité, uniques ailleurs dans l'article
NOT_AT_EDGE = "pas au bord"          # « à la fin », « au début » : les mots n'y sont pas exactement
APPROXIMATE_RATIO = 0.9
APPROXIMATE_LENGTH = 12               # lettres : en dessous, une coquille change trop facilement de mot


def resolve(op, scope, reference, current):
    """Opération → Emplacement dans courant. Lève CiblePendante ou CibleAmbigue."""
    target = op.target
    words = op.old_words if op.old_words else target.words
    article_ref, article = _article(op, scope, reference, current)
    lectures, tolerances = {article.id}, set()
    if target.alinea is None and not words:
        if op.verb in ("complete", "insert"):
            # « Compléter le titre », « à la fin, ajouter » : le bord de l'article est celui de son dernier alinéa.
            if not article.alineas:
                raise DanglingTarget("article sans alinéa")
            alinea = article.alineas[0 if target.position == "debut" else -1]
            return Location(article.id, (alinea.id,), 0, len(alinea.tokens), frozenset(lectures | {alinea.id}))
        return Location(article.id, lectures=frozenset(lectures))
    if target.alinea is None:
        alineas = _article_edge(article, target.position)
        portions = [(a, 0, len(a.tokens)) for a in alineas]
        lectures |= {a.id for a in alineas}
    else:
        try:
            targeted_items = _reference_alineas(article_ref, target)
        except DanglingTarget:
            elsewhere = _alinea_by_content(article_ref, target, words)
            if elsewhere is None:
                raise
            targeted_items = [elsewhere]
            tolerances.add(OTHER_ALINEA)
        alineas = [_current_alinea(article, a) for a in targeted_items]
        lectures |= {a.id for a in alineas}
        if target.phrase is not None:
            beginning, end = _phrase_portion(targeted_items[0], alineas[0], target.phrase)
            portions = [(alineas[0], beginning, end)]
        else:
            portions = [(a, 0, len(a.tokens)) for a in alineas]
    if not words:
        alinea, beginning, end = portions[0]
        return Location(article.id, tuple(a.id for a in alineas), beginning, end, frozenset(lectures))
    found, gap = _occurrences(portions, words)
    if not found and target.alinea is not None and not tolerances:
        elsewhere = _alinea_by_content(article_ref, target, words, unless=targeted_items)
        if elsewhere is not None:
            alinea = _current_alinea(article, elsewhere)
            portions = [(alinea, 0, len(alinea.tokens))]
            lectures.add(alinea.id)
            tolerances.add(OTHER_ALINEA)
            found, gap = _occurrences(portions, words)
    (alinea, beginning, end), at_edge = _choose(found, portions, words, target)
    if not at_edge and _at_edge_in_reference(article_ref, alinea.id, target, words):
        # L'amendement disait vrai sur le texte de départ : un amendement adopté a écrit au bord depuis.
        edge = "à la fin" if target.position == "fin" else "au début"
        raise DanglingTarget(f"mots qui ne sont plus {edge} : « {words[:60]} »")
    tolerances |= {t for t in (gap, None if at_edge else NOT_AT_EDGE) if t}
    lectures |= {j.id for j in alinea.tokens[beginning:end] if not is_blank(j)}
    if target.position == "fin":
        lectures.add(f"{alinea.id}.fin")
    if target.position == "debut":
        lectures.add(f"{alinea.id}.debut")
    return Location(article.id, (alinea.id,), beginning, end, frozenset(lectures), frozenset(tolerances))


def _at_edge_in_reference(article_ref, alinea_id, target, words):
    """Les mots étaient-ils au bord annoncé dans le texte sur lequel l'amendement a été écrit ?

    Oui : « à la fin » est une condition, et la perdre fait tomber l'amendement. Non : c'était une approximation
    du rédacteur, qu'on tolère. Dans le doute (alinéa ou mots absents de la référence), on reste strict.
    """
    targeted_one = next((a for a in article_ref.alineas if a.id == alinea_id), None)
    if targeted_one is None:
        return True
    beginning, end = (0, len(targeted_one.tokens)) if target.phrase is None else _phrase_portion(targeted_one, targeted_one, target.phrase)
    found, _ = _occurrences([(targeted_one, beginning, end)], words)
    return not found or _choose(found, [(targeted_one, beginning, end)], words, target)[1]


def _alinea_by_content(article_ref, target, words, unless=()):
    """L'alinéa de la référence qui porte les mots, quand le numéro cité est faux. None s'il n'y en a pas un seul.

    On cherche dans la référence, le texte sur lequel l'amendement a été écrit : si les mots étaient bien dans
    l'alinéa cité (sauf) et n'y sont plus, un amendement adopté les a retirés et l'amendement est tombé.
    Une coquille et un mauvais numéro à la fois, c'est trop d'écarts pour conclure.
    """
    if not words or target.phrase is not None or target.until is not None:
        return None
    if unless and _occurrences([(a, 0, len(a.tokens)) for a in unless], words)[0]:
        return None
    found, gap = _occurrences([(a, 0, len(a.tokens)) for a in article_ref.alineas], words)
    return found[0][0] if len(found) == 1 and gap != APPROXIMATE else None


def portion_bounds(alinea, target, reference_article, current_alinea_=None):
    """Portion de la phrase visée, ou tout l'alinéa, pour une opération qui écrit après une ancre."""
    if target.phrase is None:
        return 0, len(alinea.tokens)
    targeted_one = _reference_alineas(reference_article, target)[0]
    return _phrase_portion(targeted_one, alinea, target.phrase)


def article_reference(scope, target, reference):
    if scope.node == "titre" or target.node == "titre":
        return reference.titre
    return find_article(reference, scope.article)


def _article(op, scope, reference, current):
    ref = article_reference(scope, op.target, reference)
    if ref is None:
        raise DanglingTarget(f"article {scope.article or 'titre'} absent du texte de référence")
    article = article_by_id(current, ref.id)
    if article is None:
        raise DanglingTarget(f"article {ref.numero} disparu")
    if article.supprime and op.verb != "rewrite":
        raise DanglingTarget(f"article {ref.numero} supprimé")
    return ref, article


def _article_edge(article, position):
    if not article.alineas:
        raise DanglingTarget("article sans alinéa")
    if position == "fin":
        return [article.alineas[-1]]
    if position == "debut":
        return [article.alineas[0]]
    return list(article.alineas)


def _reference_alineas(article, target):
    """Les alinéas visés, lus dans la version de référence (numéro → identifiant). -1 compte depuis la fin."""
    total = len(article.alineas)
    first = target.alinea if target.alinea > 0 else total + target.alinea + 1
    last = target.until or first
    if not 1 <= first <= last <= total:
        raise DanglingTarget(f"alinéa {target.alinea} absent de la référence ({total} alinéas)")
    return list(article.alineas[first - 1:last])


def _current_alinea(article, targeted_one):
    alinea = next((a for a in article.alineas if a.id == targeted_one.id), None)
    if alinea is None:
        raise DanglingTarget("alinéa supprimé par un amendement adopté")
    return alinea


def _phrase_portion(targeted_one, alinea, rank):
    """La phrase se compte dans la référence ; on la retrouve dans l'alinéa courant par les identifiants."""
    bounds = phrases(targeted_one.tokens)
    index = rank - 1 if rank > 0 else len(bounds) + rank
    if not 0 <= index < len(bounds):
        raise DanglingTarget(f"phrase {rank} absente de la référence ({len(bounds)} phrases)")
    beginning, end = bounds[index]
    ids = {j.id for j in targeted_one.tokens[beginning:end]}
    positions = [i for i, j in enumerate(alinea.tokens) if j.id in ids]
    if not positions:
        raise DanglingTarget("phrase supprimée par un amendement adopté")
    return positions[0], positions[-1] + 1


def _visible(text):
    return [normalize(t) for t in split_tokens(text) if not t.isspace()]


def _key(text):
    """Lettres et chiffres seuls, sans accents ni majuscules : « Au‑delà » et « au dela » ont la même clé."""
    decomposed = unicodedata.normalize("NFD", text.casefold().replace("œ", "oe").replace("æ", "ae"))
    return "".join(c for c in decomposed if c.isalnum())


def _occurrences(portions, words):
    """(places de mots dans les portions, écart toléré). Le premier cran qui trouve l'emporte :
    exact, à la casse près, à la typographie près, puis à une coquille près si la place est unique."""
    searched = _visible(words)
    if not searched:
        return [], None
    for fold in (str, str.casefold):
        expected = [fold(t) for t in searched]
        found = []
        for alinea, beginning, end in portions:
            places = [i for i in range(beginning, end) if not is_blank(alinea.tokens[i])]
            texts = [fold(normalize(alinea.tokens[i].text)) for i in places]
            for k in range(len(texts) - len(expected) + 1):
                if texts[k:k + len(expected)] == expected:
                    found.append((alinea, places[k], places[k + len(expected) - 1] + 1))
        if found:
            return found, None
    keys = [c for c in map(_key, searched) if c]
    if not keys:
        return [], None
    lettered = [_portion_letters(p) for p in portions]
    found = [place for portion in lettered for place in _up_to_typography(portion, "".join(keys))]
    if found:
        return found, TYPOGRAPHY
    found = _up_to_a_typo(lettered, keys)
    return found, APPROXIMATE if found else None


def _portion_letters(portion):
    """(alinéa, index des jetons qui portent des lettres ou des chiffres, leurs clés)."""
    alinea, beginning, end = portion
    pairs = [(i, _key(alinea.tokens[i].text)) for i in range(beginning, end)]
    return alinea, [i for i, c in pairs if c], [c for _, c in pairs if c]


def _up_to_typography(portion, searched):
    """Les clés mises bout à bout : « 4000 » se retrouve dans « 4 000 », « au delà » dans « au‑delà ».
    Une place doit commencer et finir sur un bord de mot."""
    alinea, places, keys = portion
    beginnings, ends, n = {}, {}, 0
    for rank, key in enumerate(keys):
        beginnings[n] = rank
        n += len(key)
        ends[n] = rank
    string, found, initial = "".join(keys), [], 0
    while (initial := string.find(searched, initial)) >= 0:
        if initial in beginnings and initial + len(searched) in ends:
            found.append((alinea, places[beginnings[initial]], places[ends[initial + len(searched)]] + 1))
        initial += 1
    return found


def _up_to_a_typo(lettered, keys):
    """Au plus une place : la suite d'autant de mots qui ressemble le plus à la citation (« substante létale »).

    Les nombres doivent être les mêmes : « article 12 » n'est pas une coquille d'« article 13 ». Deux places
    distinctes qui ressemblent assez : on ne choisit pas.
    """
    searched, k = "".join(keys), len(keys)
    if len(searched) < APPROXIMATE_LENGTH:
        return []
    count_values = sorted(c for c in keys if any(x.isdigit() for x in c))
    candidates = []
    for alinea, places, texts in lettered:
        for rank in range(len(texts) - k + 1):
            window = texts[rank:rank + k]
            if sorted(c for c in window if any(x.isdigit() for x in c)) != count_values:
                continue
            ratio = difflib.SequenceMatcher(None, searched, "".join(window), autojunk=False).ratio()
            if ratio >= APPROXIMATE_RATIO:
                candidates.append((ratio, alinea, places[rank], places[rank + k - 1] + 1))
    if not candidates:
        return []
    _, alinea, beginning, end = max(candidates, key=lambda c: c[0])
    if any(a.id != alinea.id or f <= beginning or d >= end for _, a, d, f in candidates):
        return []
    return [(alinea, beginning, end)]


def _choose(found, portions, words, target):
    """(place retenue, vrai si elle est bien au bord annoncé).

    « À la fin de l'alinéa » et « au début » désignent la dernière et la première occurrence : les amendements le
    disent aussi de mots proches du bord, donc on ne l'exige pas, on le signale.
    """
    summarize = words[:60]
    if not found:
        raise DanglingTarget(f"mots absents : « {summarize} »")
    if target.occurrence is not None:
        index = target.occurrence - 1 if target.occurrence > 0 else len(found) + target.occurrence
        if not 0 <= index < len(found):
            raise DanglingTarget(f"occurrence {target.occurrence} absente ({len(found)}) : « {summarize} »")
        return found[index], True
    if target.position == "fin":
        alinea, beginning, end = found[-1]
        bound = next(p for p in portions if p[0].id == alinea.id)
        return found[-1], end >= terminal_beginning(alinea.tokens, bound[1], bound[2])
    if target.position == "debut":
        alinea, beginning, end = found[0]
        bound = next(p for p in portions if p[0].id == alinea.id)
        before = "".join(j.text for j in alinea.tokens[bound[1]:beginning])
        return found[0], bool(LABEL.match(before))
    if len(found) > 1:
        raise AmbiguousTarget(f"{len(found)} occurrences sans rang : « {summarize} »")
    return found[0], True
