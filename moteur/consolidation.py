"""Étape 5 : rejouer les décisions dans l'ordre réel et produire le texte consolidé.

L'ordre est celui des votes : date_sort d'abord, tri_amendement en cas d'égalité. Une série d'amendements
identiques est un seul vote et ne s'applique qu'une fois. Un sous-amendement adopté modifie le texte de son parent
avant que le parent ne s'applique. Pour les amendements non adoptés, on prédit s'ils sont tombés : soit leur cible a
disparu, soit ils entrent en conflit avec un amendement adopté avant eux, sur le texte de départ.
"""

from dataclasses import dataclass, field

from moteur.analyse import analyser_amendement
from moteur.application import appliquer, integrer_sous_amendements
from moteur.comparaison import conflit
from moteur.resolution import CibleAmbigue, CiblePendante, ErreurCible
from moteur.texte import diff_versions

SORTS_VOTES = {"Adopté", "Rejeté", "Tombé"}


@dataclass
class Statut:
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
    statuts: dict
    changements: dict
    effets: dict


def ordre_d_appel(amendements):
    """Date du vote, puis clé de discussion. Un amendement sans date passe après les autres."""
    return sorted(amendements, key=lambda a: (a.date_sort is None, a.date_sort or "", a.tri_amendement or "", a.uid))


def consolider(texte_depart, amendements, analyseur=analyser_amendement, ordre=ordre_d_appel):
    """texte_depart : Version sur laquelle les amendements ont été écrits ; amendements : AmendementBrut.

    ordre : fonction qui trie les amendements, remplaçable pour mesurer ce qu'apporte l'ordre réel des votes.
    """
    analyses = {a.uid: analyseur(a) for a in amendements}
    sous = _sous_amendements_adoptes(amendements, analyses, ordre)
    statuts = {}
    principaux = []
    for a in ordre(amendements):
        if a.sort not in SORTS_VOTES:
            statuts[a.uid] = Statut(a.uid, a.numero, a.sort, "ignore")
        elif a.parent_uid:
            adopte = a.sort == "Adopté"
            statuts[a.uid] = Statut(a.uid, a.numero, a.sort, "sous_amendement" if adopte else "indetermine")
        else:
            principaux.append(a)

    version, series, effets, adoptes = texte_depart, {}, {}, []
    for a in principaux:
        analyse = analyses[a.uid]
        adopte = a.sort == "Adopté"
        if adopte and a.id_identique and a.id_identique in series:
            statuts[a.uid] = Statut(a.uid, a.numero, a.sort, "identique", f"même vote que {series[a.id_identique]}")
            continue
        if analyse.erreurs:
            etat = "non_analyse" if adopte else "indetermine"
            statuts[a.uid] = Statut(a.uid, a.numero, a.sort, etat, "; ".join(analyse.erreurs))
            continue
        try:
            operations = integrer_sous_amendements(analyse.operations, analyse.portee, a.uid, sous.get(a.uid, []))
        except ErreurCible as erreur:
            etat = "pendant" if adopte else "indetermine"
            statuts[a.uid] = Statut(a.uid, a.numero, a.sort, etat, f"sous-amendement : {erreur}")
            continue
        if adopte:
            effet, detail = _effet_sur_depart(operations, analyse.portee, texte_depart, a.uid)
            if effet is not None:
                effets[a.uid] = effet
            try:
                version, applique = appliquer(operations, analyse.portee, texte_depart, version, a.uid)
                # Le détail dit ce que la résolution a toléré (« typographie », « autre alinéa »…) : à relire en priorité.
                statuts[a.uid] = Statut(a.uid, a.numero, a.sort, "applique", "; ".join(sorted(applique.tolerances)))
                adoptes.append(a.uid)
                if a.id_identique:
                    series[a.id_identique] = a.numero
            except CibleAmbigue as erreur:
                statuts[a.uid] = Statut(a.uid, a.numero, a.sort, "ambigu", str(erreur))
            except CiblePendante as erreur:
                statuts[a.uid] = Statut(a.uid, a.numero, a.sort, "pendant", str(erreur))
        else:
            statuts[a.uid] = _predire(a, operations, analyse.portee, texte_depart, version, effets, adoptes)
    return Consolidation(version, statuts, diff_versions(texte_depart, version), effets)


def _sous_amendements_adoptes(amendements, analyses, ordre):
    """{parent : [(uid, operations)]} dans l'ordre d'appel, une seule fois par série identique."""
    sous, vus = {}, set()
    for a in ordre(amendements):
        if not a.parent_uid or a.sort != "Adopté" or analyses[a.uid].erreurs:
            continue
        if a.id_identique and (a.parent_uid, a.id_identique) in vus:
            continue
        vus.add((a.parent_uid, a.id_identique))
        sous.setdefault(a.parent_uid, []).append((a.uid, analyses[a.uid].operations))
    return sous


def _effet_sur_depart(operations, portee, texte_depart, uid):
    try:
        return appliquer(operations, portee, texte_depart, texte_depart, uid)[1], ""
    except ErreurCible as erreur:
        return None, str(erreur)


def _predire(a, operations, portee, texte_depart, version, effets, adoptes):
    """Tombé si sa cible a disparu de la version courante ou s'il est en conflit avec un adopté antérieur."""
    effet, detail = _effet_sur_depart(operations, portee, texte_depart, a.uid)
    if effet is None:
        return Statut(a.uid, a.numero, a.sort, "predit_tombe", detail)
    conflits = [uid for uid in adoptes if uid in effets and conflit(effets[uid], effet)]
    if conflits:
        return Statut(a.uid, a.numero, a.sort, "predit_tombe", "conflit", conflits)
    try:
        appliquer(operations, portee, texte_depart, version, a.uid)
    except CiblePendante as erreur:
        return Statut(a.uid, a.numero, a.sort, "predit_tombe", str(erreur))
    except CibleAmbigue as erreur:
        return Statut(a.uid, a.numero, a.sort, "indetermine", str(erreur))
    return Statut(a.uid, a.numero, a.sort, "predit_vivant")
