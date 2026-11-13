"""Les versions du moteur comparées par l'évaluation : un nom → une fonction (texte de départ, amendements) → résultat.

V0-noop      recopie le texte d'entrée : ce qu'on obtient sans rien faire, pour juger tous les autres scores.
V1-gram      grammaire seule, ordre réel des votes.
V1-gram-tri  ordre prévu de discussion (tri_amendement) au lieu de date_sort.
V1-gram-depot ordre de dépôt : la mesure de ce que l'ordre apporte.
V2-gram+llm  grammaire, puis LLM sur ce qu'elle rejette, pour les amendements adoptés seulement.
V3-gram-tolerant  grammaire seule, résolution tolérante (typographie, coquille, autre alinéa, « à la fin » approché).

Le moteur n'a qu'une résolution, la tolérante : les résultats V1 et V2 gardés sur le disque datent d'avant elle, et les
relancer donnerait ceux de V3 (plus le LLM pour V2). V3 a son propre nom pour ne pas les écraser.
V5-gram-etendue  V3 avec la grammaire étendue : renvois « même / ledit / audit », phrase sans alinéa, alinéas épars,
                 renvois multiples, sous-instructions numérotées, ponctuation variable autour des citations.
                 Même remarque : le moteur n'a qu'une grammaire, relancer V3 donnerait désormais V5.
V6-gram-etendue+llm  V5, puis le LLM sur ce que la grammaire étendue rejette (adoptés seulement), comme V2.
"""

import os
from concurrent.futures import ThreadPoolExecutor

from evals.moteur.llm_openai import AppelOpenAI
from moteur.analyse import analyser_amendement, analyser_portee
from moteur.consolidation import Consolidation, consolider
from moteur.texte import texte_de, trouver_article

MODELE_LLM = os.environ.get("MOTEUR_MODELE_LLM", "gpt-6-luna")
PLAFOND_LLM = float(os.environ.get("MOTEUR_PLAFOND_LLM", "3"))
FILS_LLM = 8
APPEL_LLM = AppelOpenAI(MODELE_LLM, plafond=PLAFOND_LLM)


def _par_tri(amendements):
    return sorted(amendements, key=lambda a: (a.tri_amendement is None, a.tri_amendement or "", a.uid))


def _par_depot(amendements):
    return sorted(amendements, key=lambda a: (a.numero_ordre is None, a.numero_ordre or 0, a.uid))


def _texte_article(depart, amendement):
    try:
        article = trouver_article(depart, analyser_portee(amendement.article_vise).article)
    except ValueError:
        return None
    return "\n".join(texte_de(a) for a in article.alineas) if article else None


def _gram_llm(depart, amendements):
    def analyseur(a):
        return analyser_amendement(a, _texte_article(depart, a), APPEL_LLM if a.sort == "Adopté" else None)
    # Les appels se font en parallèle et remplissent le cache ; la consolidation relit ensuite ce cache, dans l'ordre.
    with ThreadPoolExecutor(FILS_LLM) as fils:
        list(fils.map(analyseur, [a for a in amendements if a.sort == "Adopté"]))
    return consolider(depart, amendements, analyseur=analyseur)


def _rien(depart, amendements):
    return Consolidation(depart, {}, {}, {})


VERSIONS = {
    "V0-noop": _rien,
    "V1-gram": lambda depart, amendements: consolider(depart, amendements),
    "V1-gram-tri": lambda depart, amendements: consolider(depart, amendements, ordre=_par_tri),
    "V1-gram-depot": lambda depart, amendements: consolider(depart, amendements, ordre=_par_depot),
    "V2-gram+llm": _gram_llm,
    "V3-gram-tolerant": lambda depart, amendements: consolider(depart, amendements),
    "V5-gram-etendue": lambda depart, amendements: consolider(depart, amendements),
    "V6-gram-etendue+llm": _gram_llm,
}
