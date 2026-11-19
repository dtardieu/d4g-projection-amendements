"""Étape 5 : rejouer les décisions dans l'ordre réel et produire le texte consolidé.

L'ordre est celui des votes : date_sort d'abord, tri_amendement en cas d'égalité. Une série d'amendements
identiques est un seul vote et ne s'applique qu'une fois. Un sous-amendement adopté modifie le texte de son parent
avant que le parent ne s'applique. Pour les amendements non adoptés, on prédit s'ils sont tombés : soit leur cible a
disparu, soit ils entrent en conflit avec un amendement adopté avant eux, sur le texte de départ.
"""

from dataclasses import dataclass, field

from engine.analysis import analyze_amendement
from engine.application import apply, integrate_sous_amendements
from engine.comparison import conflict
from engine.resolution import AmbiguousTarget, DanglingTarget, TargetError
from engine.text import diff_versions

VOTED_OUTCOMES = {"Adopté", "Rejeté", "Tombé"}


@dataclass
class Status:
    """Ce qui est arrivé à un amendement. etat :
    applique, identique, sous_amendement, non_analyse, pendant, ambigu (adoptés) ;
    predit_tombe, predit_vivant, indetermine (non adoptés) ; ignore (non voté).
    """
    uid: str
    numero: str
    sort: str | None
    etat: str
    detail: str = ""
    conflits: list = field(default_factory=list)


@dataclass
class Consolidation:
    version: object
    statuses: dict
    changes: dict
    effects: dict


def call_order(amendements):
    """Date du vote, puis clé de discussion. Un amendement sans date passe après les autres."""
    return sorted(amendements, key=lambda a: (a.date_sort is None, a.date_sort or "", a.tri_amendement or "", a.uid))


def consolidate(initial_text, amendements, analyzer=analyze_amendement, order=call_order):
    """texte_depart : Version sur laquelle les amendements ont été écrits ; amendements : AmendementBrut.

    ordre : fonction qui trie les amendements, remplaçable pour mesurer ce qu'apporte l'ordre réel des votes.
    """
    analyses = {a.uid: analyzer(a) for a in amendements}
    sub = _adopted_sous_amendements(amendements, analyses, order)
    statuses = {}
    main_ones = []
    for a in order(amendements):
        if a.sort not in VOTED_OUTCOMES:
            statuses[a.uid] = Status(a.uid, a.numero, a.sort, "ignore")
        elif a.parent_uid:
            adopte = a.sort == "Adopté"
            statuses[a.uid] = Status(a.uid, a.numero, a.sort, "sous_amendement" if adopte else "indetermine")
        else:
            main_ones.append(a)

    version, series, effects, adopted = initial_text, {}, {}, []
    for a in main_ones:
        analysis = analyses[a.uid]
        adopte = a.sort == "Adopté"
        if adopte and a.id_identique and a.id_identique in series:
            statuses[a.uid] = Status(a.uid, a.numero, a.sort, "identique", f"même vote que {series[a.id_identique]}")
            continue
        if analysis.errors:
            etat = "non_analyse" if adopte else "indetermine"
            statuses[a.uid] = Status(a.uid, a.numero, a.sort, etat, "; ".join(analysis.errors))
            continue
        try:
            operations = integrate_sous_amendements(analysis.operations, analysis.scope, a.uid, sub.get(a.uid, []))
        except TargetError as error:
            etat = "pendant" if adopte else "indetermine"
            statuses[a.uid] = Status(a.uid, a.numero, a.sort, etat, f"sous-amendement : {error}")
            continue
        if adopte:
            effect, detail = _effect_on_initial(operations, analysis.scope, initial_text, a.uid)
            if effect is not None:
                effects[a.uid] = effect
            try:
                version, applied = apply(operations, analysis.scope, initial_text, version, a.uid)
                # Le détail dit ce que la résolution a toléré (« typographie », « autre alinéa »…) : à relire en priorité.
                statuses[a.uid] = Status(a.uid, a.numero, a.sort, "applique", "; ".join(sorted(applied.tolerances)))
                adopted.append(a.uid)
                if a.id_identique:
                    series[a.id_identique] = a.numero
            except AmbiguousTarget as error:
                statuses[a.uid] = Status(a.uid, a.numero, a.sort, "ambigu", str(error))
            except DanglingTarget as error:
                statuses[a.uid] = Status(a.uid, a.numero, a.sort, "pendant", str(error))
        else:
            statuses[a.uid] = _predict(a, operations, analysis.scope, initial_text, version, effects, adopted)
    return Consolidation(version, statuses, diff_versions(initial_text, version), effects)


def _adopted_sous_amendements(amendements, analyses, order):
    """{parent : [(uid, operations)]} dans l'ordre d'appel, une seule fois par série identique."""
    sub, seen = {}, set()
    for a in order(amendements):
        if not a.parent_uid or a.sort != "Adopté" or analyses[a.uid].errors:
            continue
        if a.id_identique and (a.parent_uid, a.id_identique) in seen:
            continue
        seen.add((a.parent_uid, a.id_identique))
        sub.setdefault(a.parent_uid, []).append((a.uid, analyses[a.uid].operations))
    return sub


def _effect_on_initial(operations, scope, initial_text, uid):
    try:
        return apply(operations, scope, initial_text, initial_text, uid)[1], ""
    except TargetError as error:
        return None, str(error)


def _predict(a, operations, scope, initial_text, version, effects, adopted):
    """Tombé si sa cible a disparu de la version courante ou s'il est en conflit avec un adopté antérieur."""
    effect, detail = _effect_on_initial(operations, scope, initial_text, a.uid)
    if effect is None:
        return Status(a.uid, a.numero, a.sort, "predit_tombe", detail)
    conflits = [uid for uid in adopted if uid in effects and conflict(effects[uid], effect)]
    if conflits:
        return Status(a.uid, a.numero, a.sort, "predit_tombe", "conflit", conflits)
    try:
        apply(operations, scope, initial_text, version, a.uid)
    except DanglingTarget as error:
        return Status(a.uid, a.numero, a.sort, "predit_tombe", str(error))
    except AmbiguousTarget as error:
        return Status(a.uid, a.numero, a.sort, "indetermine", str(error))
    return Status(a.uid, a.numero, a.sort, "predit_vivant")
