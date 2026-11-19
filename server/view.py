"""Mise en forme d'une projection pour l'interface : article par article, le texte de départ et le
texte projeté découpé en segments (inchangé, inséré, supprimé), chacun rattaché à son amendement.

Le diff ne compare pas des mots : un jeton du moteur garde son identifiant d'un bout à l'autre.
Un jeton du départ absent de la version projetée a été supprimé ; un jeton d'une autre origine que
« T0 » a été inséré par l'amendement dont il porte l'uid. L'auteur d'une suppression se lit dans les
effets des amendements adoptés (ce qu'ils écrivent : jeton, alinéa ou article).
"""

from engine.analysis import analyze_scope
from engine.text import number_key, lines_of, numbers

START_ORIGIN = "T0"


def articles(initial, projected, effects, amendments, statuses):
    """[{numero, numero_depart, etat, depart, alineas, amendements}], dans l'ordre projeté."""
    old_items = {a.id: a for a in initial.articles}
    authors = _writing_authors(effects)
    targeted_amendments = _amendments_by_article(amendments)
    view = numbers(projected)
    result = []
    for article in projected.articles:
        old = old_items.get(article.id)
        paragraphs = _paragraphs(old, article, authors)
        touched = {s["amendement"] for a in paragraphs for s in a["segments"] if s["amendement"]}
        key = number_key(old.numero) if old and old.numero else None
        linked = touched | set(targeted_amendments.get(key, []))
        result.append(
            {
                "numero": view[article.id],
                "numero_depart": old.numero if old else None,
                "etat": _state(old, article, paragraphs),
                "depart": [] if old is None or old.supprime else lines_of(old),
                "alineas": paragraphs,
                "amendements": [a.uid for a in amendments if a.uid in linked and a.uid in statuses],
            }
        )
    return result


def _state(old, article, paragraphs):
    if old is None:
        return "nouveau"
    if article.supprime and not old.supprime:
        return "supprime"
    if any(s["type"] != "eq" for a in paragraphs for s in a["segments"]):
        return "modifie"
    return "inchange"


def _writing_authors(effects):
    """{identifiant écrit (jeton, alinéa, article) : uid de l'amendement adopté qui l'écrit}."""
    authors = {}
    for uid, effect in effects.items():
        for identifier in effect.writings:
            authors.setdefault(identifier, uid)
    return authors


def _amendments_by_article(amendments):
    """{clé du numéro d'article visé : [uid]}, « APRÈS ART. 3 » compris dans l'article 3."""
    targeted_amendments = {}
    for amendment in amendments:
        try:
            article = analyze_scope(amendment.article_vise).article
        except ValueError:
            continue
        if article:
            targeted_amendments.setdefault(number_key(article), []).append(amendment.uid)
    return targeted_amendments


def _paragraphs(old, article, authors):
    """Alinéas affichés : [{numero, segments}] ; un alinéa supprimé en entier n'a pas de numéro."""
    if article.supprime:
        if old is None or old.supprime:
            return []
        return [_deleted_paragraph(a, old, authors) for a in old.paragraphs]
    alive = {j.id for a in article.paragraphs for j in a.tokens}
    before, after, end = _deletions(old, alive, authors) if old else ({}, {}, [])
    result = []
    for numero, alinea in enumerate(article.paragraphs, 1):
        segments = []
        for token in alinea.tokens:
            removed_paragraphs, removed_words = before.pop(token.id, ([], []))
            result += removed_paragraphs
            # Le texte retiré se lit avant ce qui le remplace : on le place devant les insertions
            # qui précèdent immédiatement ce jeton.
            beginning = len(segments)
            while beginning and segments[beginning - 1]["type"] == "ins":
                beginning -= 1
            segments[beginning:beginning] = removed_words
            if token.origin == START_ORIGIN:
                segments.append(_segment("eq", token.text, None))
            else:
                segments.append(_segment("ins", token.text, token.origin))
            segments += after.pop(token.id, [])
        result.append({"numero": numero, "segments": _merge(segments)})
    if old and len(end) == len(old.paragraphs):
        return end + result  # article réécrit en entier : l'ancien texte d'abord, puis le nouveau
    return result + end


def _deletions(old, alive, authors):
    """Place les jetons supprimés du départ dans le texte projeté.

    avant[id] : (alinéas supprimés en entier, mots supprimés) à montrer avant le jeton vivant id ;
    apres[id] : mots supprimés en fin d'alinéa, après le dernier jeton vivant ;
    fin : alinéas supprimés après le dernier jeton vivant de l'article.
    """
    before, after, removed_paragraphs = {}, {}, []
    for alinea in old.paragraphs:
        if not any(j.id in alive for j in alinea.tokens):
            removed_paragraphs.append(_deleted_paragraph(alinea, old, authors))
            continue
        words, last = [], None
        for token in alinea.tokens:
            if token.id not in alive:
                words.append(_deleted(token, alinea, old, authors))
            else:
                if words or removed_paragraphs:
                    before[token.id] = (removed_paragraphs, words)
                    words, removed_paragraphs = [], []
                last = token.id
        if words:
            after[last] = words
    return before, after, removed_paragraphs


def _deleted_paragraph(alinea, article, authors):
    segments = [_deleted(j, alinea, article, authors) for j in alinea.tokens]
    return {"numero": None, "segments": _merge(segments)}


def _deleted(token, alinea, article, authors):
    auteur = authors.get(token.id) or authors.get(alinea.id) or authors.get(article.id)
    return _segment("del", token.text, auteur)


def _segment(type_, text, amendment):
    return {"type": type_, "texte": text, "amendement": amendment}


def _merge(segments):
    """Fusionne les segments voisins de même type et de même amendement.

    Une espace supprimée n'est écrite par aucun amendement : elle rejoint le mot supprimé qu'elle
    suit.
    """
    fusion = []
    for s in segments:
        same_type = fusion and fusion[-1]["type"] == s["type"]
        if same_type and (fusion[-1]["amendement"] == s["amendement"] or not s["texte"].strip()):
            fusion[-1]["texte"] += s["texte"]
        else:
            fusion.append(dict(s))
    return fusion
