"""Étape 3 : exécuter les opérations résolues et produire une nouvelle version et son empreinte d'accès.

L'Effet liste ce que l'opération lit et écrit, en identifiants (jetons, alinéas, articles), plus des positions
(« a.fin », « a.apres ») pour ce qui s'ajoute à un bord. L'étape 4 compare ces ensembles.
"""

import re
from dataclasses import dataclass, replace

from moteur.analyse import ANNONCE_CITATION, Portee
from moteur.resolution import CiblePendante, article_reference, bornes_portion, resoudre
from moteur.texte import (Alinea, Article, Identifiants, Version, article_par_id, coudre, creer_jetons, debut_terminal,
                          est_blanc, remplacer_article, texte_de, trouver_article)

ARTICLE_AMENDEMENT = "AMENDEMENT"
FERMETURE = re.compile(r"»[\s\u00a0\u202f]*[.;]?$")
FIN_DE_PHRASE = {".", ";", ":", "!", "?", "…"}


@dataclass(frozen=True)
class Effet:
    ecritures: frozenset = frozenset()
    lectures: frozenset = frozenset()
    tolerances: frozenset = frozenset()   # écarts acceptés à la résolution : à montrer, pas à comparer

    def __or__(self, autre):
        return Effet(self.ecritures | autre.ecritures, self.lectures | autre.lectures,
                     self.tolerances | autre.tolerances)


def appliquer(operations, portee, reference, courant, origine):
    """Toutes les opérations d'un amendement, ou aucune : lève ErreurCible et laisse courant intact."""
    ids, effet, version = Identifiants(origine), Effet(), courant
    for op in operations:
        version, effet_op = appliquer_operation(op, portee, reference, version, ids)
        effet |= effet_op
    return version, effet


def appliquer_operation(op, portee, reference, courant, ids):
    if portee.noeud == "article_additionnel":
        return _inserer_article(op, portee, reference, courant, ids)
    if op.cible.phrase is not None and op.cible.alinea is None:
        # « À la seconde phrase, … » sans alinéa : sans équivoque seulement si l'article n'en a qu'un.
        article_ref = article_reference(portee, op.cible, reference)
        if article_ref is None or len(article_ref.alineas) != 1:
            raise CiblePendante("phrase visée sans alinéa dans un article de plusieurs alinéas")
        op = replace(op, cible=replace(op.cible, alinea=1))
    place = resoudre(op, portee, reference, courant)
    article = article_par_id(courant, place.article_id)
    version, effet = _VERBES[op.verbe](op, place, article, portee, reference, courant, ids)
    return version, replace(effet, tolerances=place.tolerances)


def integrer_sous_amendements(operations, portee, parent_uid, sous_amendements):
    """Applique les sous-amendements au texte de leur parent, puis renvoie les opérations du parent modifiées.

    Le sous-amendement vise un alinéa de l'amendement, chapeau compris : « Rédiger ainsi cet article : » est
    l'alinéa 1, sauf pour un article additionnel, dont le dispositif n'a pas de chapeau. sous_amendements :
    [(uid, operations)] dans l'ordre d'appel. Lève ErreurCible si un sous-amendement ne s'applique pas.
    """
    if not sous_amendements:
        return operations
    index = [i for i, op in enumerate(operations) if op.texte]
    if len(index) != 1:
        raise CiblePendante("sous-amendement d'un amendement à plusieurs textes")
    op = operations[index[0]]
    ids = Identifiants(parent_uid)
    chapeau = [] if portee.noeud == "article_additionnel" else [Alinea(ids.nouveau(), (), parent_uid)]
    corps = [Alinea(ids.nouveau(), jetons, parent_uid) for jetons in _lignes_de_jetons(op, ids)]
    article = Article(ids.nouveau(), ARTICLE_AMENDEMENT, tuple(chapeau + corps), parent_uid)
    reference = version = Version((article,))
    interne = Portee("article", ARTICLE_AMENDEMENT)
    for uid, ops in sous_amendements:
        version, _ = appliquer(ops, interne, reference, version, uid)
    alineas = version.articles[0].alineas[len(chapeau):]
    nouvelle = replace(op, texte="\n".join(texte_de(a) for a in alineas), provenance=tuple(alineas))
    return [nouvelle if i == index[0] else o for i, o in enumerate(operations)]


# ---- texte inséré


def _lignes_de_jetons(op, ids):
    if op.provenance is not None:
        return [a.jetons for a in op.provenance]
    return [creer_jetons(ligne, ids) for ligne in op.texte.split("\n")]


def _jetons_en_ligne(op, ids):
    return coudre(ids, *_lignes_de_jetons(op, ids))


def _nouveaux_alineas(op, ids, voisin=None, remplace=False):
    """Alinéas à insérer. Le texte cité a perdu ses guillemets : on les rétablit dans un article déjà cité.

    On est dans une citation si le voisin en ouvre une sans la fermer, ou s'il l'annonce (« ainsi rédigé : »).
    Celui qui remplace le dernier alinéa d'une citation reprend sa fermeture (« » ; »).
    """
    lignes = _lignes_de_jetons(op, ids)
    if voisin is None:
        return [Alinea(ids.nouveau(), l, ids.origine) for l in lignes]
    texte = texte_de(voisin).strip()
    ouvre = texte.startswith("«")
    ferme = FERMETURE.search(texte)
    if (ouvre and (remplace or not ferme)) or (not remplace and ANNONCE_CITATION.search(texte)):
        lignes = [l if _ouvre_citation(l) else coudre(ids, creer_jetons("«", ids), l) for l in lignes]
        if remplace and ferme and "»" not in "".join(j.texte for j in lignes[-1]):
            lignes[-1] = coudre(ids, lignes[-1], creer_jetons(ferme.group(0).strip(), ids))
    return [Alinea(ids.nouveau(), l, ids.origine) for l in lignes]


def _ouvre_citation(jetons):
    return next((j.texte for j in jetons if not est_blanc(j)), "") == "«"


# ---- modifications de l'arbre


def _changer_alinea(courant, article, alinea_id, jetons):
    alineas = tuple(replace(a, jetons=jetons) if a.id == alinea_id else a for a in article.alineas)
    return remplacer_article(courant, replace(article, alineas=alineas))


def _alinea(article, alinea_id):
    return next(a for a in article.alineas if a.id == alinea_id)


def _ids_visibles(jetons):
    return {j.id for j in jetons if not est_blanc(j)}


def _tout_alinea(alinea):
    return {alinea.id, f"{alinea.id}.fin", f"{alinea.id}.debut", f"{alinea.id}.apres"} | _ids_visibles(alinea.jetons)


def _tout_article(article):
    ecritures = {article.id, f"{article.id}.fin", f"{article.id}.debut"}
    for alinea in article.alineas:
        ecritures |= _tout_alinea(alinea)
    return ecritures


# ---- verbes


def _substitute(op, place, article, portee, reference, courant, ids):
    alinea = _alinea(article, place.alinea_ids[0])
    jetons = coudre(ids, alinea.jetons[:place.debut], _jetons_en_ligne(op, ids), alinea.jetons[place.fin:])
    ecrits = _ids_visibles(alinea.jetons[place.debut:place.fin])
    return _changer_alinea(courant, article, alinea.id, jetons), Effet(frozenset(ecrits), place.lectures)


def _delete(op, place, article, portee, reference, courant, ids):
    alinea = _alinea(article, place.alinea_ids[0])
    if op.anciens_mots:
        debut, fin = place.debut, place.fin
    else:
        # « Après le mot : « a », supprimer la fin de l'alinéa » : de l'ancre à la ponctuation finale.
        debut, fin = _queue_apres_ancre(op, place, alinea, article, portee, reference)
    jetons = coudre(ids, alinea.jetons[:debut], alinea.jetons[fin:])
    ecrits = _ids_visibles(alinea.jetons[debut:fin])
    return _changer_alinea(courant, article, alinea.id, jetons), Effet(frozenset(ecrits), place.lectures)


def _queue_apres_ancre(op, place, alinea, article, portee, reference):
    ref = article_reference(portee, op.cible, reference)
    _, borne = bornes_portion(alinea, op.cible, ref)
    if op.cible.position == "avant":
        return 0, place.debut
    return place.fin, debut_terminal(alinea.jetons, place.fin, borne)


def _insert(op, place, article, portee, reference, courant, ids):
    alinea = _alinea(article, place.alinea_ids[0])
    position = op.cible.position
    if position in ("fin", "dans") and not op.cible.mots:
        return _complete(op, place, article, portee, reference, courant, ids)
    if position == "debut" and not op.cible.mots:
        i = place.debut
        while i < place.fin and (est_blanc(alinea.jetons[i]) or alinea.jetons[i].texte == "«"):
            i += 1
        ecrit = f"{alinea.id}.debut"
    else:
        i = place.fin if position == "apres" else place.debut
        repere = alinea.jetons[place.fin - 1 if position == "apres" else place.debut]
        ecrit = f"{repere.id}.{position}"
    jetons = coudre(ids, alinea.jetons[:i], _jetons_en_ligne(op, ids), alinea.jetons[i:])
    return _changer_alinea(courant, article, alinea.id, jetons), Effet(frozenset({ecrit}), place.lectures)


def _complete(op, place, article, portee, reference, courant, ids):
    """Ajoute à la fin d'un alinéa ou d'une phrase, avant sa ponctuation finale (« … L. 201-4, si… préalable. »).

    Des mots continuent la phrase et prennent la place du point ; une phrase (majuscule) vient après le point.
    """
    alinea = _alinea(article, place.alinea_ids[0])
    ajout = list(_jetons_en_ligne(op, ids))
    terminal = debut_terminal(alinea.jetons, place.debut, place.fin)
    queue = [j.texte for j in alinea.jetons[terminal:place.fin]]
    visible = next((j.texte for j in ajout if not est_blanc(j)), "")
    if visible[:1].isupper() and "." in queue:
        i = terminal + queue.index(".") + 1
    else:
        i = terminal
        while ajout and "." in queue and ajout[-1].texte in (".", " ", " "):
            ajout.pop()
    jetons = coudre(ids, alinea.jetons[:i], ajout, alinea.jetons[i:])
    effet = Effet(frozenset({f"{alinea.id}.fin"}), place.lectures)
    return _changer_alinea(courant, article, alinea.id, jetons), effet


def _rewrite(op, place, article, portee, reference, courant, ids):
    if not place.alinea_ids:
        lignes = _lignes_de_jetons(op, ids)
        alineas = tuple(Alinea(ids.nouveau(), l, ids.origine) for l in lignes)
        nouveau = replace(article, alineas=alineas, supprime=False)
        return remplacer_article(courant, nouveau), Effet(frozenset(_tout_article(article)), place.lectures)
    alinea = _alinea(article, place.alinea_ids[0])
    if op.cible.mots and op.cible.phrase is not None:
        return _rewrite_fin_de_phrase(op, place, alinea, article, portee, reference, courant, ids)
    if op.cible.mots:
        debut, fin = _queue_apres_ancre(op, place, alinea, article, portee, reference)
        fermeture = [j for j in alinea.jetons[fin:] if j.texte == "»"] if "»" not in op.texte else []
        jetons = coudre(ids, alinea.jetons[:debut], _jetons_en_ligne(op, ids), fermeture)
        ecrits = _ids_visibles(alinea.jetons[debut:]) | {f"{alinea.id}.fin"}
        return _changer_alinea(courant, article, alinea.id, jetons), Effet(frozenset(ecrits), place.lectures)
    if op.cible.phrase is not None:
        return _substitute(op, place, article, portee, reference, courant, ids)
    nouveaux = _nouveaux_alineas(op, ids, alinea, remplace=True)
    visees = set(place.alinea_ids)
    alineas, posees = [], False
    ecrits = set()
    for a in article.alineas:
        if a.id in visees:
            ecrits |= _tout_alinea(a)
            if not posees:
                alineas += nouveaux
                posees = True
        else:
            alineas.append(a)
    nouveau = replace(article, alineas=tuple(alineas))
    return remplacer_article(courant, nouveau), Effet(frozenset(ecrits), place.lectures)


def _rewrite_fin_de_phrase(op, place, alinea, article, portee, reference, courant, ids):
    """« Après le mot…, rédiger ainsi la fin de la première phrase » : seule la phrase change, les suivantes restent.

    La ponctuation qui termine la phrase est gardée, sauf si le texte nouveau en apporte une.
    """
    debut, fin = _queue_apres_ancre(op, place, alinea, article, portee, reference)
    _, borne = bornes_portion(alinea, op.cible, article_reference(portee, op.cible, reference))
    nouveau = _jetons_en_ligne(op, ids)
    dernier = next((j.texte for j in reversed(nouveau) if not est_blanc(j)), "")
    suite = alinea.jetons[borne:] if dernier in FIN_DE_PHRASE else alinea.jetons[fin:]
    jetons = coudre(ids, alinea.jetons[:debut], nouveau, suite)
    return _changer_alinea(courant, article, alinea.id, jetons), Effet(
        frozenset(_ids_visibles(alinea.jetons[debut:fin])), place.lectures)


def _insert_node(op, place, article, portee, reference, courant, ids):
    position = op.cible.position
    if not place.alinea_ids:
        # « Compléter cet article par l'alinéa suivant », « Au début, ajouter l'alinéa suivant »
        if position == "debut":
            nouveaux = _nouveaux_alineas(op, ids, article.alineas[0] if article.alineas else None)
            alineas, ecrit = tuple(nouveaux) + article.alineas, f"{article.id}.debut"
        else:
            nouveaux = _nouveaux_alineas(op, ids, article.alineas[-1] if article.alineas else None)
            alineas, ecrit = article.alineas + tuple(nouveaux), f"{article.id}.fin"
        nouveau = replace(article, alineas=alineas)
        return remplacer_article(courant, nouveau), Effet(frozenset({ecrit}), place.lectures)
    alinea = _alinea(article, place.alinea_ids[0])
    if op.cible.phrase is not None:
        i = place.fin if position == "apres" else place.debut
        jetons = coudre(ids, alinea.jetons[:i], _jetons_en_ligne(op, ids), alinea.jetons[i:])
        ecrit = f"{alinea.jetons[place.fin - 1].id}.{position}"
        return _changer_alinea(courant, article, alinea.id, jetons), Effet(frozenset({ecrit}), place.lectures)
    nouveaux = _nouveaux_alineas(op, ids, alinea)
    if position == "debut":
        # « Au début de l'alinéa 3, ajouter l'alinéa suivant » : avant cet alinéa.
        position = "avant"
    rang = next(i for i, a in enumerate(article.alineas) if a.id == alinea.id)
    rang += 1 if position == "apres" else 0
    nouveau = replace(article, alineas=article.alineas[:rang] + tuple(nouveaux) + article.alineas[rang:])
    ecrit = f"{alinea.id}.{'apres' if position == 'apres' else 'debut'}"
    return remplacer_article(courant, nouveau), Effet(frozenset({ecrit}), place.lectures)


def _delete_node(op, place, article, portee, reference, courant, ids):
    if not place.alinea_ids:
        ecrits = _tout_article(article)
        nouveau = replace(article, alineas=(), supprime=True)
        return remplacer_article(courant, nouveau), Effet(frozenset(ecrits), place.lectures)
    if op.cible.phrase is not None:
        alinea = _alinea(article, place.alinea_ids[0])
        # La fermeture de la citation et le point-virgule final survivent à la phrase supprimée.
        fin = place.fin
        while fin > place.debut and (est_blanc(alinea.jetons[fin - 1]) or alinea.jetons[fin - 1].texte in ("»", ";")):
            fin -= 1
        reste_ouvert = alinea.jetons[fin:place.fin]
        jetons = coudre(ids, alinea.jetons[:place.debut], reste_ouvert, alinea.jetons[place.fin:])
        ecrits = _ids_visibles(alinea.jetons[place.debut:fin])
        return _changer_alinea(courant, article, alinea.id, jetons), Effet(frozenset(ecrits), place.lectures)
    visees = set(place.alinea_ids)
    ecrits = set()
    for a in article.alineas:
        if a.id in visees:
            ecrits |= _tout_alinea(a)
    nouveau = replace(article, alineas=tuple(a for a in article.alineas if a.id not in visees))
    return remplacer_article(courant, nouveau), Effet(frozenset(ecrits), place.lectures)


def _inserer_article(op, portee, reference, courant, ids):
    """Article additionnel : placé après l'article visé et les additionnels déjà posés au même endroit.

    Il ne dépend pas du contenu de son voisin : le réécrire ou le supprimer (il reste en « (Supprimé) ») ne le fait
    pas tomber.
    """
    ancre_ref = trouver_article(reference, portee.article)
    if ancre_ref is None:
        raise CiblePendante(f"article {portee.article} absent du texte de référence")
    rang = next((i for i, a in enumerate(courant.articles) if a.id == ancre_ref.id), None)
    if rang is None:
        raise CiblePendante(f"article {portee.article} disparu")
    if portee.avant_apres != "avant":
        rang += 1
        while rang < len(courant.articles) and courant.articles[rang].ancre == ancre_ref.id:
            rang += 1
    alineas = tuple(Alinea(ids.nouveau(), l, ids.origine) for l in _lignes_de_jetons(op, ids))
    nouveau = Article(ids.nouveau(), None, alineas, ids.origine, ancre=ancre_ref.id)
    version = replace(courant, articles=courant.articles[:rang] + (nouveau,) + courant.articles[rang:])
    return version, Effet(frozenset({nouveau.id}))


_VERBES = {
    "substitute": _substitute,
    "delete": _delete,
    "insert": _insert,
    "complete": _complete,
    "rewrite": _rewrite,
    "insert_node": _insert_node,
    "delete_node": _delete_node,
}
