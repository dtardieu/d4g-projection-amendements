"""Étape 3 : exécuter les opérations résolues et produire une nouvelle version et son empreinte d'accès.

L'Effet liste ce que l'opération lit et écrit, en identifiants (jetons, alinéas, articles), plus des positions
(« a.fin », « a.apres ») pour ce qui s'ajoute à un bord. L'étape 4 compare ces ensembles.
"""

import re
from dataclasses import dataclass, replace

from engine.analysis import QUOTE_ANNOUNCEMENT, Scope
from engine.resolution import DanglingTarget, article_reference, portion_bounds, resolve
from engine.text import (Alinea, Article, Identifiers, Version, article_by_id, stitch, create_tokens, terminal_beginning,
                          is_blank, replace_article, text_of, find_article)

ARTICLE_AMENDEMENT = "AMENDEMENT"
CLOSING = re.compile(r"»[\s\u00a0\u202f]*[.;]?$")
PHRASE_END = {".", ";", ":", "!", "?", "…"}


@dataclass(frozen=True)
class Effect:
    writings: frozenset = frozenset()
    lectures: frozenset = frozenset()
    tolerances: frozenset = frozenset()   # écarts acceptés à la résolution : à montrer, pas à comparer

    def __or__(self, other):
        return Effect(self.writings | other.writings, self.lectures | other.lectures,
                     self.tolerances | other.tolerances)


def apply(operations, scope, reference, current, origin):
    """Toutes les opérations d'un amendement, ou aucune : lève ErreurCible et laisse courant intact."""
    ids, effect, version = Identifiers(origin), Effect(), current
    for op in operations:
        version, op_effect = apply_operation(op, scope, reference, version, ids)
        effect |= op_effect
    return version, effect


def apply_operation(op, scope, reference, current, ids):
    if scope.node == "article_additionnel":
        return _insert_article(op, scope, reference, current, ids)
    if op.target.phrase is not None and op.target.alinea is None:
        # « À la seconde phrase, … » sans alinéa : sans équivoque seulement si l'article n'en a qu'un.
        article_ref = article_reference(scope, op.target, reference)
        if article_ref is None or len(article_ref.alineas) != 1:
            raise DanglingTarget("phrase visée sans alinéa dans un article de plusieurs alinéas")
        op = replace(op, target=replace(op.target, alinea=1))
    place = resolve(op, scope, reference, current)
    article = article_by_id(current, place.article_id)
    version, effect = _VERBS[op.verb](op, place, article, scope, reference, current, ids)
    return version, replace(effect, tolerances=place.tolerances)


def integrate_sous_amendements(operations, scope, parent_uid, sous_amendements):
    """Applique les sous-amendements au texte de leur parent, puis renvoie les opérations du parent modifiées.

    Le sous-amendement vise un alinéa de l'amendement, chapeau compris : « Rédiger ainsi cet article : » est
    l'alinéa 1, sauf pour un article additionnel, dont le dispositif n'a pas de chapeau. sous_amendements :
    [(uid, operations)] dans l'ordre d'appel. Lève ErreurCible si un sous-amendement ne s'applique pas.
    """
    if not sous_amendements:
        return operations
    index = [i for i, op in enumerate(operations) if op.text]
    if len(index) != 1:
        raise DanglingTarget("sous-amendement d'un amendement à plusieurs textes")
    op = operations[index[0]]
    ids = Identifiers(parent_uid)
    heading = [] if scope.node == "article_additionnel" else [Alinea(ids.new(), (), parent_uid)]
    body = [Alinea(ids.new(), tokens, parent_uid) for tokens in _token_lines(op, ids)]
    article = Article(ids.new(), ARTICLE_AMENDEMENT, tuple(heading + body), parent_uid)
    reference = version = Version((article,))
    internal = Scope("article", ARTICLE_AMENDEMENT)
    for uid, ops in sous_amendements:
        version, _ = apply(ops, internal, reference, version, uid)
    alineas = version.articles[0].alineas[len(heading):]
    new_value = replace(op, text="\n".join(text_of(a) for a in alineas), provenance=tuple(alineas))
    return [new_value if i == index[0] else o for i, o in enumerate(operations)]


# ---- texte inséré


def _token_lines(op, ids):
    if op.provenance is not None:
        return [a.tokens for a in op.provenance]
    return [create_tokens(line, ids) for line in op.text.split("\n")]


def _inline_tokens(op, ids):
    return stitch(ids, *_token_lines(op, ids))


def _new_alineas(op, ids, neighbor=None, replaced=False):
    """Alinéas à insérer. Le texte cité a perdu ses guillemets : on les rétablit dans un article déjà cité.

    On est dans une citation si le voisin en ouvre une sans la fermer, ou s'il l'annonce (« ainsi rédigé : »).
    Celui qui remplace le dernier alinéa d'une citation reprend sa fermeture (« » ; »).
    """
    lignes = _token_lines(op, ids)
    if neighbor is None:
        return [Alinea(ids.new(), l, ids.origin) for l in lignes]
    text = text_of(neighbor).strip()
    opens = text.startswith("«")
    closes = CLOSING.search(text)
    if (opens and (replaced or not closes)) or (not replaced and QUOTE_ANNOUNCEMENT.search(text)):
        lignes = [l if _opens_quote(l) else stitch(ids, create_tokens("«", ids), l) for l in lignes]
        if replaced and closes and "»" not in "".join(j.text for j in lignes[-1]):
            lignes[-1] = stitch(ids, lignes[-1], create_tokens(closes.group(0).strip(), ids))
    return [Alinea(ids.new(), l, ids.origin) for l in lignes]


def _opens_quote(tokens):
    return next((j.text for j in tokens if not is_blank(j)), "") == "«"


# ---- modifications de l'arbre


def _change_alinea(current, article, alinea_id, tokens):
    alineas = tuple(replace(a, tokens=tokens) if a.id == alinea_id else a for a in article.alineas)
    return replace_article(current, replace(article, alineas=alineas))


def _alinea(article, alinea_id):
    return next(a for a in article.alineas if a.id == alinea_id)


def _visible_ids(tokens):
    return {j.id for j in tokens if not is_blank(j)}


def _whole_alinea(alinea):
    return {alinea.id, f"{alinea.id}.fin", f"{alinea.id}.debut", f"{alinea.id}.apres"} | _visible_ids(alinea.tokens)


def _whole_article(article):
    writings = {article.id, f"{article.id}.fin", f"{article.id}.debut"}
    for alinea in article.alineas:
        writings |= _whole_alinea(alinea)
    return writings


# ---- verbes


def _substitute(op, place, article, scope, reference, current, ids):
    alinea = _alinea(article, place.alinea_ids[0])
    tokens = stitch(ids, alinea.tokens[:place.beginning], _inline_tokens(op, ids), alinea.tokens[place.end:])
    written_items = _visible_ids(alinea.tokens[place.beginning:place.end])
    return _change_alinea(current, article, alinea.id, tokens), Effect(frozenset(written_items), place.lectures)


def _delete(op, place, article, scope, reference, current, ids):
    alinea = _alinea(article, place.alinea_ids[0])
    if op.old_words:
        beginning, end = place.beginning, place.end
    else:
        # « Après le mot : « a », supprimer la fin de l'alinéa » : de l'ancre à la ponctuation finale.
        beginning, end = _tail_after_anchor(op, place, alinea, article, scope, reference)
    tokens = stitch(ids, alinea.tokens[:beginning], alinea.tokens[end:])
    written_items = _visible_ids(alinea.tokens[beginning:end])
    return _change_alinea(current, article, alinea.id, tokens), Effect(frozenset(written_items), place.lectures)


def _tail_after_anchor(op, place, alinea, article, scope, reference):
    ref = article_reference(scope, op.target, reference)
    _, bound = portion_bounds(alinea, op.target, ref)
    if op.target.position == "avant":
        return 0, place.beginning
    return place.end, terminal_beginning(alinea.tokens, place.end, bound)


def _insert(op, place, article, scope, reference, current, ids):
    alinea = _alinea(article, place.alinea_ids[0])
    position = op.target.position
    if position in ("fin", "dans") and not op.target.words:
        return _complete(op, place, article, scope, reference, current, ids)
    if position == "debut" and not op.target.words:
        i = place.beginning
        while i < place.end and (is_blank(alinea.tokens[i]) or alinea.tokens[i].text == "«"):
            i += 1
        written = f"{alinea.id}.debut"
    else:
        i = place.end if position == "apres" else place.beginning
        marker = alinea.tokens[place.end - 1 if position == "apres" else place.beginning]
        written = f"{marker.id}.{position}"
    tokens = stitch(ids, alinea.tokens[:i], _inline_tokens(op, ids), alinea.tokens[i:])
    return _change_alinea(current, article, alinea.id, tokens), Effect(frozenset({written}), place.lectures)


def _complete(op, place, article, scope, reference, current, ids):
    """Ajoute à la fin d'un alinéa ou d'une phrase, avant sa ponctuation finale (« … L. 201-4, si… préalable. »).

    Des mots continuent la phrase et prennent la place du point ; une phrase (majuscule) vient après le point.
    """
    alinea = _alinea(article, place.alinea_ids[0])
    addition = list(_inline_tokens(op, ids))
    terminal = terminal_beginning(alinea.tokens, place.beginning, place.end)
    tail = [j.text for j in alinea.tokens[terminal:place.end]]
    visible = next((j.text for j in addition if not is_blank(j)), "")
    if visible[:1].isupper() and "." in tail:
        i = terminal + tail.index(".") + 1
    else:
        i = terminal
        while addition and "." in tail and addition[-1].text in (".", " ", " "):
            addition.pop()
    tokens = stitch(ids, alinea.tokens[:i], addition, alinea.tokens[i:])
    effect = Effect(frozenset({f"{alinea.id}.fin"}), place.lectures)
    return _change_alinea(current, article, alinea.id, tokens), effect


def _rewrite(op, place, article, scope, reference, current, ids):
    if not place.alinea_ids:
        lignes = _token_lines(op, ids)
        alineas = tuple(Alinea(ids.new(), l, ids.origin) for l in lignes)
        new = replace(article, alineas=alineas, supprime=False)
        return replace_article(current, new), Effect(frozenset(_whole_article(article)), place.lectures)
    alinea = _alinea(article, place.alinea_ids[0])
    if op.target.words and op.target.phrase is not None:
        return _rewrite_phrase_end(op, place, alinea, article, scope, reference, current, ids)
    if op.target.words:
        beginning, end = _tail_after_anchor(op, place, alinea, article, scope, reference)
        closing = [j for j in alinea.tokens[end:] if j.text == "»"] if "»" not in op.text else []
        tokens = stitch(ids, alinea.tokens[:beginning], _inline_tokens(op, ids), closing)
        written_items = _visible_ids(alinea.tokens[beginning:]) | {f"{alinea.id}.fin"}
        return _change_alinea(current, article, alinea.id, tokens), Effect(frozenset(written_items), place.lectures)
    if op.target.phrase is not None:
        return _substitute(op, place, article, scope, reference, current, ids)
    new_items = _new_alineas(op, ids, alinea, replaced=True)
    targeted_items = set(place.alinea_ids)
    alineas, placed = [], False
    written_items = set()
    for a in article.alineas:
        if a.id in targeted_items:
            written_items |= _whole_alinea(a)
            if not placed:
                alineas += new_items
                placed = True
        else:
            alineas.append(a)
    new = replace(article, alineas=tuple(alineas))
    return replace_article(current, new), Effect(frozenset(written_items), place.lectures)


def _rewrite_phrase_end(op, place, alinea, article, scope, reference, current, ids):
    """« Après le mot…, rédiger ainsi la fin de la première phrase » : seule la phrase change, les suivantes restent.

    La ponctuation qui termine la phrase est gardée, sauf si le texte nouveau en apporte une.
    """
    beginning, end = _tail_after_anchor(op, place, alinea, article, scope, reference)
    _, bound = portion_bounds(alinea, op.target, article_reference(scope, op.target, reference))
    new = _inline_tokens(op, ids)
    last = next((j.text for j in reversed(new) if not is_blank(j)), "")
    continuation = alinea.tokens[bound:] if last in PHRASE_END else alinea.tokens[end:]
    tokens = stitch(ids, alinea.tokens[:beginning], new, continuation)
    return _change_alinea(current, article, alinea.id, tokens), Effect(
        frozenset(_visible_ids(alinea.tokens[beginning:end])), place.lectures)


def _insert_node(op, place, article, scope, reference, current, ids):
    position = op.target.position
    if not place.alinea_ids:
        # « Compléter cet article par l'alinéa suivant », « Au début, ajouter l'alinéa suivant »
        if position == "debut":
            new_items = _new_alineas(op, ids, article.alineas[0] if article.alineas else None)
            alineas, written = tuple(new_items) + article.alineas, f"{article.id}.debut"
        else:
            new_items = _new_alineas(op, ids, article.alineas[-1] if article.alineas else None)
            alineas, written = article.alineas + tuple(new_items), f"{article.id}.fin"
        new = replace(article, alineas=alineas)
        return replace_article(current, new), Effect(frozenset({written}), place.lectures)
    alinea = _alinea(article, place.alinea_ids[0])
    if op.target.phrase is not None:
        i = place.end if position == "apres" else place.beginning
        tokens = stitch(ids, alinea.tokens[:i], _inline_tokens(op, ids), alinea.tokens[i:])
        written = f"{alinea.tokens[place.end - 1].id}.{position}"
        return _change_alinea(current, article, alinea.id, tokens), Effect(frozenset({written}), place.lectures)
    new_items = _new_alineas(op, ids, alinea)
    if position == "debut":
        # « Au début de l'alinéa 3, ajouter l'alinéa suivant » : avant cet alinéa.
        position = "avant"
    rank = next(i for i, a in enumerate(article.alineas) if a.id == alinea.id)
    rank += 1 if position == "apres" else 0
    new = replace(article, alineas=article.alineas[:rank] + tuple(new_items) + article.alineas[rank:])
    written = f"{alinea.id}.{'apres' if position == 'apres' else 'debut'}"
    return replace_article(current, new), Effect(frozenset({written}), place.lectures)


def _delete_node(op, place, article, scope, reference, current, ids):
    if not place.alinea_ids:
        written_items = _whole_article(article)
        new = replace(article, alineas=(), supprime=True)
        return replace_article(current, new), Effect(frozenset(written_items), place.lectures)
    if op.target.phrase is not None:
        alinea = _alinea(article, place.alinea_ids[0])
        # La fermeture de la citation et le point-virgule final survivent à la phrase supprimée.
        end = place.end
        while end > place.beginning and (is_blank(alinea.tokens[end - 1]) or alinea.tokens[end - 1].text in ("»", ";")):
            end -= 1
        stays_open = alinea.tokens[end:place.end]
        tokens = stitch(ids, alinea.tokens[:place.beginning], stays_open, alinea.tokens[place.end:])
        written_items = _visible_ids(alinea.tokens[place.beginning:end])
        return _change_alinea(current, article, alinea.id, tokens), Effect(frozenset(written_items), place.lectures)
    targeted_items = set(place.alinea_ids)
    written_items = set()
    for a in article.alineas:
        if a.id in targeted_items:
            written_items |= _whole_alinea(a)
    new = replace(article, alineas=tuple(a for a in article.alineas if a.id not in targeted_items))
    return replace_article(current, new), Effect(frozenset(written_items), place.lectures)


def _insert_article(op, scope, reference, current, ids):
    """Article additionnel : placé après l'article visé et les additionnels déjà posés au même endroit.

    Il ne dépend pas du contenu de son voisin : le réécrire ou le supprimer (il reste en « (Supprimé) ») ne le fait
    pas tomber.
    """
    anchor_ref = find_article(reference, scope.article)
    if anchor_ref is None:
        raise DanglingTarget(f"article {scope.article} absent du texte de référence")
    rank = next((i for i, a in enumerate(current.articles) if a.id == anchor_ref.id), None)
    if rank is None:
        raise DanglingTarget(f"article {scope.article} disparu")
    if scope.before_after != "avant":
        rank += 1
        while rank < len(current.articles) and current.articles[rank].anchor == anchor_ref.id:
            rank += 1
    alineas = tuple(Alinea(ids.new(), l, ids.origin) for l in _token_lines(op, ids))
    new = Article(ids.new(), None, alineas, ids.origin, anchor=anchor_ref.id)
    version = replace(current, articles=current.articles[:rank] + (new,) + current.articles[rank:])
    return version, Effect(frozenset({new.id}))


_VERBS = {
    "substitute": _substitute,
    "delete": _delete,
    "insert": _insert,
    "complete": _complete,
    "rewrite": _rewrite,
    "insert_node": _insert_node,
    "delete_node": _delete_node,
}
