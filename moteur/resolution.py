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

from moteur.texte import (article_par_id, decouper, debut_terminal, est_blanc, normaliser, phrases, trouver_article)


class ErreurCible(Exception):
    """L'opération ne peut pas s'appliquer à cette version."""


class CiblePendante(ErreurCible):
    """Ce que vise l'opération n'existe plus : l'amendement est tombé."""


class CibleAmbigue(ErreurCible):
    """Les mots apparaissent plusieurs fois et aucun rang n'est donné : on signale, on ne devine pas."""


# Ce qui peut précéder le vrai début d'un alinéa : guillemet, « Art. L. 250-10. – », « I. – », « 1° », « a) ».
ETIQUETTE = re.compile(r"^[«\s]*(Art\.[^–-]*[–-]\s*)?([IVXLCDM]+\.\s*[–-]\s*)?(\d+°(\s*bis)?\s*)?([a-z]\)\s*)?$")


@dataclass(frozen=True)
class Emplacement:
    article_id: str
    alinea_ids: tuple = ()          # vide : l'article entier
    debut: int | None = None        # portion de jetons dans alinea_ids[0], fin exclue
    fin: int | None = None
    lectures: frozenset = frozenset()
    tolerances: frozenset = frozenset()   # écarts acceptés entre la citation et le texte (TYPOGRAPHIE…)


TYPOGRAPHIE = "typographie"          # mêmes lettres et chiffres, à la ponctuation, aux tirets et aux accents près
APPROCHE = "mots approchés"          # une coquille dans la citation
AUTRE_ALINEA = "autre alinéa"        # mots absents de l'alinéa cité, uniques ailleurs dans l'article
PAS_AU_BORD = "pas au bord"          # « à la fin », « au début » : les mots n'y sont pas exactement
RATIO_APPROCHE = 0.9
LONGUEUR_APPROCHE = 12               # lettres : en dessous, une coquille change trop facilement de mot


def resoudre(op, portee, reference, courant):
    """Opération → Emplacement dans courant. Lève CiblePendante ou CibleAmbigue."""
    cible = op.cible
    mots = op.anciens_mots if op.anciens_mots else cible.mots
    article_ref, article = _article(op, portee, reference, courant)
    lectures, tolerances = {article.id}, set()
    if cible.alinea is None and not mots:
        if op.verbe in ("complete", "insert"):
            # « Compléter le titre », « à la fin, ajouter » : le bord de l'article est celui de son dernier alinéa.
            if not article.alineas:
                raise CiblePendante("article sans alinéa")
            alinea = article.alineas[0 if cible.position == "debut" else -1]
            return Emplacement(article.id, (alinea.id,), 0, len(alinea.jetons), frozenset(lectures | {alinea.id}))
        return Emplacement(article.id, lectures=frozenset(lectures))
    if cible.alinea is None:
        alineas = _bord_article(article, cible.position)
        portions = [(a, 0, len(a.jetons)) for a in alineas]
        lectures |= {a.id for a in alineas}
    else:
        try:
            visees = _alineas_reference(article_ref, cible)
        except CiblePendante:
            ailleurs = _alinea_par_contenu(article_ref, cible, mots)
            if ailleurs is None:
                raise
            visees = [ailleurs]
            tolerances.add(AUTRE_ALINEA)
        alineas = [_alinea_courant(article, a) for a in visees]
        lectures |= {a.id for a in alineas}
        if cible.phrase is not None:
            debut, fin = _portion_phrase(visees[0], alineas[0], cible.phrase)
            portions = [(alineas[0], debut, fin)]
        else:
            portions = [(a, 0, len(a.jetons)) for a in alineas]
    if not mots:
        alinea, debut, fin = portions[0]
        return Emplacement(article.id, tuple(a.id for a in alineas), debut, fin, frozenset(lectures))
    trouve, ecart = _occurrences(portions, mots)
    if not trouve and cible.alinea is not None and not tolerances:
        ailleurs = _alinea_par_contenu(article_ref, cible, mots, sauf=visees)
        if ailleurs is not None:
            alinea = _alinea_courant(article, ailleurs)
            portions = [(alinea, 0, len(alinea.jetons))]
            lectures.add(alinea.id)
            tolerances.add(AUTRE_ALINEA)
            trouve, ecart = _occurrences(portions, mots)
    (alinea, debut, fin), au_bord = _choisir(trouve, portions, mots, cible)
    if not au_bord and _au_bord_dans_la_reference(article_ref, alinea.id, cible, mots):
        # L'amendement disait vrai sur le texte de départ : un amendement adopté a écrit au bord depuis.
        bord = "à la fin" if cible.position == "fin" else "au début"
        raise CiblePendante(f"mots qui ne sont plus {bord} : « {mots[:60]} »")
    tolerances |= {t for t in (ecart, None if au_bord else PAS_AU_BORD) if t}
    lectures |= {j.id for j in alinea.jetons[debut:fin] if not est_blanc(j)}
    if cible.position == "fin":
        lectures.add(f"{alinea.id}.fin")
    if cible.position == "debut":
        lectures.add(f"{alinea.id}.debut")
    return Emplacement(article.id, (alinea.id,), debut, fin, frozenset(lectures), frozenset(tolerances))


def _au_bord_dans_la_reference(article_ref, alinea_id, cible, mots):
    """Les mots étaient-ils au bord annoncé dans le texte sur lequel l'amendement a été écrit ?

    Oui : « à la fin » est une condition, et la perdre fait tomber l'amendement. Non : c'était une approximation
    du rédacteur, qu'on tolère. Dans le doute (alinéa ou mots absents de la référence), on reste strict.
    """
    visee = next((a for a in article_ref.alineas if a.id == alinea_id), None)
    if visee is None:
        return True
    debut, fin = (0, len(visee.jetons)) if cible.phrase is None else _portion_phrase(visee, visee, cible.phrase)
    trouve, _ = _occurrences([(visee, debut, fin)], mots)
    return not trouve or _choisir(trouve, [(visee, debut, fin)], mots, cible)[1]


def _alinea_par_contenu(article_ref, cible, mots, sauf=()):
    """L'alinéa de la référence qui porte les mots, quand le numéro cité est faux. None s'il n'y en a pas un seul.

    On cherche dans la référence, le texte sur lequel l'amendement a été écrit : si les mots étaient bien dans
    l'alinéa cité (sauf) et n'y sont plus, un amendement adopté les a retirés et l'amendement est tombé.
    Une coquille et un mauvais numéro à la fois, c'est trop d'écarts pour conclure.
    """
    if not mots or cible.phrase is not None or cible.jusqua is not None:
        return None
    if sauf and _occurrences([(a, 0, len(a.jetons)) for a in sauf], mots)[0]:
        return None
    trouve, ecart = _occurrences([(a, 0, len(a.jetons)) for a in article_ref.alineas], mots)
    return trouve[0][0] if len(trouve) == 1 and ecart != APPROCHE else None


def bornes_portion(alinea, cible, reference_article, courant_alinea=None):
    """Portion de la phrase visée, ou tout l'alinéa, pour une opération qui écrit après une ancre."""
    if cible.phrase is None:
        return 0, len(alinea.jetons)
    visee = _alineas_reference(reference_article, cible)[0]
    return _portion_phrase(visee, alinea, cible.phrase)


def article_reference(portee, cible, reference):
    if portee.noeud == "titre" or cible.noeud == "titre":
        return reference.titre
    return trouver_article(reference, portee.article)


def _article(op, portee, reference, courant):
    ref = article_reference(portee, op.cible, reference)
    if ref is None:
        raise CiblePendante(f"article {portee.article or 'titre'} absent du texte de référence")
    article = article_par_id(courant, ref.id)
    if article is None:
        raise CiblePendante(f"article {ref.numero} disparu")
    if article.supprime and op.verbe != "rewrite":
        raise CiblePendante(f"article {ref.numero} supprimé")
    return ref, article


def _bord_article(article, position):
    if not article.alineas:
        raise CiblePendante("article sans alinéa")
    if position == "fin":
        return [article.alineas[-1]]
    if position == "debut":
        return [article.alineas[0]]
    return list(article.alineas)


def _alineas_reference(article, cible):
    """Les alinéas visés, lus dans la version de référence (numéro → identifiant). -1 compte depuis la fin."""
    total = len(article.alineas)
    premier = cible.alinea if cible.alinea > 0 else total + cible.alinea + 1
    dernier = cible.jusqua or premier
    if not 1 <= premier <= dernier <= total:
        raise CiblePendante(f"alinéa {cible.alinea} absent de la référence ({total} alinéas)")
    return list(article.alineas[premier - 1:dernier])


def _alinea_courant(article, visee):
    alinea = next((a for a in article.alineas if a.id == visee.id), None)
    if alinea is None:
        raise CiblePendante("alinéa supprimé par un amendement adopté")
    return alinea


def _portion_phrase(visee, alinea, rang):
    """La phrase se compte dans la référence ; on la retrouve dans l'alinéa courant par les identifiants."""
    bornes = phrases(visee.jetons)
    index = rang - 1 if rang > 0 else len(bornes) + rang
    if not 0 <= index < len(bornes):
        raise CiblePendante(f"phrase {rang} absente de la référence ({len(bornes)} phrases)")
    debut, fin = bornes[index]
    ids = {j.id for j in visee.jetons[debut:fin]}
    positions = [i for i, j in enumerate(alinea.jetons) if j.id in ids]
    if not positions:
        raise CiblePendante("phrase supprimée par un amendement adopté")
    return positions[0], positions[-1] + 1


def _visibles(texte):
    return [normaliser(t) for t in decouper(texte) if not t.isspace()]


def _cle(texte):
    """Lettres et chiffres seuls, sans accents ni majuscules : « Au‑delà » et « au dela » ont la même clé."""
    decompose = unicodedata.normalize("NFD", texte.casefold().replace("œ", "oe").replace("æ", "ae"))
    return "".join(c for c in decompose if c.isalnum())


def _occurrences(portions, mots):
    """(places de mots dans les portions, écart toléré). Le premier cran qui trouve l'emporte :
    exact, à la casse près, à la typographie près, puis à une coquille près si la place est unique."""
    cherche = _visibles(mots)
    if not cherche:
        return [], None
    for pli in (str, str.casefold):
        attendu = [pli(t) for t in cherche]
        trouve = []
        for alinea, debut, fin in portions:
            places = [i for i in range(debut, fin) if not est_blanc(alinea.jetons[i])]
            textes = [pli(normaliser(alinea.jetons[i].texte)) for i in places]
            for k in range(len(textes) - len(attendu) + 1):
                if textes[k:k + len(attendu)] == attendu:
                    trouve.append((alinea, places[k], places[k + len(attendu) - 1] + 1))
        if trouve:
            return trouve, None
    cles = [c for c in map(_cle, cherche) if c]
    if not cles:
        return [], None
    lettrees = [_lettres_portion(p) for p in portions]
    trouve = [place for portion in lettrees for place in _a_la_typographie_pres(portion, "".join(cles))]
    if trouve:
        return trouve, TYPOGRAPHIE
    trouve = _a_une_coquille_pres(lettrees, cles)
    return trouve, APPROCHE if trouve else None


def _lettres_portion(portion):
    """(alinéa, index des jetons qui portent des lettres ou des chiffres, leurs clés)."""
    alinea, debut, fin = portion
    paires = [(i, _cle(alinea.jetons[i].texte)) for i in range(debut, fin)]
    return alinea, [i for i, c in paires if c], [c for _, c in paires if c]


def _a_la_typographie_pres(portion, cherche):
    """Les clés mises bout à bout : « 4000 » se retrouve dans « 4 000 », « au delà » dans « au‑delà ».
    Une place doit commencer et finir sur un bord de mot."""
    alinea, places, cles = portion
    debuts, fins, n = {}, {}, 0
    for rang, cle in enumerate(cles):
        debuts[n] = rang
        n += len(cle)
        fins[n] = rang
    chaine, trouve, depart = "".join(cles), [], 0
    while (depart := chaine.find(cherche, depart)) >= 0:
        if depart in debuts and depart + len(cherche) in fins:
            trouve.append((alinea, places[debuts[depart]], places[fins[depart + len(cherche)]] + 1))
        depart += 1
    return trouve


def _a_une_coquille_pres(lettrees, cles):
    """Au plus une place : la suite d'autant de mots qui ressemble le plus à la citation (« substante létale »).

    Les nombres doivent être les mêmes : « article 12 » n'est pas une coquille d'« article 13 ». Deux places
    distinctes qui ressemblent assez : on ne choisit pas.
    """
    cherche, k = "".join(cles), len(cles)
    if len(cherche) < LONGUEUR_APPROCHE:
        return []
    nombres = sorted(c for c in cles if any(x.isdigit() for x in c))
    candidats = []
    for alinea, places, textes in lettrees:
        for rang in range(len(textes) - k + 1):
            fenetre = textes[rang:rang + k]
            if sorted(c for c in fenetre if any(x.isdigit() for x in c)) != nombres:
                continue
            ratio = difflib.SequenceMatcher(None, cherche, "".join(fenetre), autojunk=False).ratio()
            if ratio >= RATIO_APPROCHE:
                candidats.append((ratio, alinea, places[rang], places[rang + k - 1] + 1))
    if not candidats:
        return []
    _, alinea, debut, fin = max(candidats, key=lambda c: c[0])
    if any(a.id != alinea.id or f <= debut or d >= fin for _, a, d, f in candidats):
        return []
    return [(alinea, debut, fin)]


def _choisir(trouve, portions, mots, cible):
    """(place retenue, vrai si elle est bien au bord annoncé).

    « À la fin de l'alinéa » et « au début » désignent la dernière et la première occurrence : les amendements le
    disent aussi de mots proches du bord, donc on ne l'exige pas, on le signale.
    """
    resume = mots[:60]
    if not trouve:
        raise CiblePendante(f"mots absents : « {resume} »")
    if cible.occurrence is not None:
        index = cible.occurrence - 1 if cible.occurrence > 0 else len(trouve) + cible.occurrence
        if not 0 <= index < len(trouve):
            raise CiblePendante(f"occurrence {cible.occurrence} absente ({len(trouve)}) : « {resume} »")
        return trouve[index], True
    if cible.position == "fin":
        alinea, debut, fin = trouve[-1]
        borne = next(p for p in portions if p[0].id == alinea.id)
        return trouve[-1], fin >= debut_terminal(alinea.jetons, borne[1], borne[2])
    if cible.position == "debut":
        alinea, debut, fin = trouve[0]
        borne = next(p for p in portions if p[0].id == alinea.id)
        avant = "".join(j.texte for j in alinea.jetons[borne[1]:debut])
        return trouve[0], bool(ETIQUETTE.match(avant))
    if len(trouve) > 1:
        raise CibleAmbigue(f"{len(trouve)} occurrences sans rang : « {resume} »")
    return trouve[0], True
