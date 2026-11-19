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

from evals.engine.llm_openai import OpenAICall
from engine.analysis import analyze_amendement, analyze_scope
from engine.consolidation import Consolidation, consolidate
from engine.text import text_of, find_article

LLM_MODEL = os.environ.get("MOTEUR_MODELE_LLM", "gpt-6-luna")
LLM_BUDGET = float(os.environ.get("MOTEUR_PLAFOND_LLM", "3"))
LLM_THREADS = 8
LLM_CALL = OpenAICall(LLM_MODEL, budget=LLM_BUDGET)


def _by_sort_key(amendements):
    return sorted(amendements, key=lambda a: (a.tri_amendement is None, a.tri_amendement or "", a.uid))


def _by_deposit(amendements):
    return sorted(amendements, key=lambda a: (a.numero_ordre is None, a.numero_ordre or 0, a.uid))


def _article_text(initial, amendement):
    try:
        article = find_article(initial, analyze_scope(amendement.article_vise).article)
    except ValueError:
        return None
    return "\n".join(text_of(a) for a in article.alineas) if article else None


def _grammar_llm(initial, amendements):
    def analyzer(a):
        return analyze_amendement(a, _article_text(initial, a), LLM_CALL if a.sort == "Adopté" else None)
    # Les appels se font en parallèle et remplissent le cache ; la consolidation relit ensuite ce cache, dans l'ordre.
    with ThreadPoolExecutor(LLM_THREADS) as threads:
        list(threads.map(analyzer, [a for a in amendements if a.sort == "Adopté"]))
    return consolidate(initial, amendements, analyzer=analyzer)


def _nothing(initial, amendements):
    return Consolidation(initial, {}, {}, {})


VERSIONS = {
    "V0-noop": _nothing,
    "V1-gram": lambda initial, amendements: consolidate(initial, amendements),
    "V1-gram-tri": lambda initial, amendements: consolidate(initial, amendements, order=_by_sort_key),
    "V1-gram-depot": lambda initial, amendements: consolidate(initial, amendements, order=_by_deposit),
    "V2-gram+llm": _grammar_llm,
    "V3-gram-tolerant": lambda initial, amendements: consolidate(initial, amendements),
    "V5-gram-etendue": lambda initial, amendements: consolidate(initial, amendements),
    "V6-gram-etendue+llm": _grammar_llm,
}
