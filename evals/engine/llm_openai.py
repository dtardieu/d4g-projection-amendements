"""Appel à un modèle OpenAI pour le niveau LLM : sortie JSON imposée, cache disque, comptage des jetons.

Le cache rend les exécutions reproductibles et ne repaie jamais un appel déjà fait. Un plafond de dépense arrête tout.
"""

import hashlib
import json
from pathlib import Path

from openai import OpenAI

CACHE = Path(__file__).parent / "donnees" / "cache_llm"
# Tarifs par million de jetons (entrée, entrée en cache, sortie), lus sur developers.openai.com le 2026-11-02.
TARIFS = {"gpt-5.4-mini": (0.75, 0.075, 4.50), "gpt-5.4-nano": (0.20, 0.02, 1.25), "gpt-6-luna": (0.10, 0.01, 0.50)}
MAX_SORTIE = 8000


class PlafondDepasse(Exception):
    pass


class AppelOpenAI:
    def __init__(self, model="gpt-6-luna", effort="low", plafond=2.0, client=None):
        self.model, self.effort, self.plafond = model, effort, plafond
        self.client = client
        self.calls = self.en_cache = self.invalides = 0
        self.entree = self.entree_cache = self.output = self.reflexion = 0

    @property
    def cout(self):
        prix_entree, prix_cache, prix_sortie = TARIFS[self.model]
        return ((self.entree - self.entree_cache) * prix_entree + self.entree_cache * prix_cache
                + self.output * prix_sortie) / 1e6

    def __call__(self, guidelines, message, schema):
        key = hashlib.sha256(json.dumps([self.model, self.effort, guidelines, message, schema],
                                        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        chemin = CACHE / f"{key}.json"
        if chemin.exists():
            self.en_cache += 1
            return self._sortie(json.loads(chemin.read_text(encoding="utf-8")))
        if self.cout >= self.plafond:
            raise PlafondDepasse(f"{self.cout:.2f} $ dépensés, plafond {self.plafond} $")
        self.client = self.client or OpenAI()
        response = self.client.responses.create(
            model=self.model,
            instructions=guidelines,
            input=message,
            reasoning={"effort": self.effort},
            max_output_tokens=MAX_SORTIE,
            text={"format": {"type": "json_schema", "name": "operations", "schema": schema, "strict": True}},
        )
        usage = response.usage
        trace = {
            "statut": response.status,
            "texte": response.output_text,
            "entree": usage.input_tokens,
            "entree_cache": usage.input_tokens_details.cached_tokens,
            "sortie": usage.output_tokens,
            "reflexion": usage.output_tokens_details.reasoning_tokens,
        }
        self.calls += 1
        self.entree += trace["entree"]
        self.entree_cache += trace["entree_cache"]
        self.output += trace["sortie"]
        self.reflexion += trace["reflexion"]
        CACHE.mkdir(parents=True, exist_ok=True)
        chemin.write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")
        return self._sortie(trace)

    def _sortie(self, trace):
        try:
            return json.loads(trace["texte"]) if trace["statut"] == "completed" else None
        except (TypeError, ValueError):
            self.invalides += 1
            return None
