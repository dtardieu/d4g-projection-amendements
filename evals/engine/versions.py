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

from evals.engine.llm_openai import AppelOpenAI
from engine.analysis import analyze_amendment, analyze_scope
from engine.consolidation import Consolidation, consolidate
from engine.text import text_of, find_article

MODELE_LLM = os.environ.get("MOTEUR_MODELE_LLM", "gpt-6-luna")
PLAFOND_LLM = float(os.environ.get("MOTEUR_PLAFOND_LLM", "3"))
FILS_LLM = 8
APPEL_LLM = AppelOpenAI(MODELE_LLM, plafond=PLAFOND_LLM)


def _par_tri(amendments):
    return sorted(amendments, key=lambda a: (a.tri_amendement is None, a.tri_amendement or "", a.uid))


def _par_depot(amendments):
    return sorted(amendments, key=lambda a: (a.numero_ordre is None, a.numero_ordre or 0, a.uid))


def _texte_article(initial, amendment):
    try:
        article = find_article(initial, analyze_scope(amendment.article_vise).article)
    except ValueError:
        return None
    return "\n".join(text_of(a) for a in article.paragraphs) if article else None


def _gram_llm(initial, amendments):
    def analyzer(a):
        return analyze_amendment(a, _texte_article(initial, a), APPEL_LLM if a.sort == "Adopté" else None)
    # Les appels se font en parallèle et remplissent le cache ; la consolidation relit ensuite ce cache, dans l'ordre.
    with ThreadPoolExecutor(FILS_LLM) as fils:
        list(fils.map(analyzer, [a for a in amendments if a.sort == "Adopté"]))
    return consolidate(initial, amendments, analyzer=analyzer)


def _rien(initial, amendments):
    return Consolidation(initial, {}, {}, {})


VERSIONS = {
    "V0-noop": _rien,
    "V1-gram": lambda initial, amendments: consolidate(initial, amendments),
    "V1-gram-tri": lambda initial, amendments: consolidate(initial, amendments, order=_par_tri),
    "V1-gram-depot": lambda initial, amendments: consolidate(initial, amendments, order=_par_depot),
    "V2-gram+llm": _gram_llm,
    "V3-gram-tolerant": lambda initial, amendments: consolidate(initial, amendments),
    "V5-gram-etendue": lambda initial, amendments: consolidate(initial, amendments),
    "V6-gram-etendue+llm": _gram_llm,
}
