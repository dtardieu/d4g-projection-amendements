"""Appel à un modèle OpenAI pour le niveau LLM : sortie JSON imposée, cache disque, comptage des jetons.

Le cache rend les exécutions reproductibles et ne repaie jamais un appel déjà fait. Un plafond de dépense arrête tout.
"""

import hashlib
import json
from pathlib import Path

from openai import OpenAI

CACHE = Path(__file__).parent / "donnees" / "cache_llm"
# Tarifs par million de jetons (entrée, entrée en cache, sortie), lus sur developers.openai.com le 2026-11-02.
PRICES = {"gpt-5.4-mini": (0.75, 0.075, 4.50), "gpt-5.4-nano": (0.20, 0.02, 1.25), "gpt-6-luna": (0.10, 0.01, 0.50)}
MAX_OUTPUT = 8000


class BudgetExceeded(Exception):
    pass


class OpenAICall:
    def __init__(self, model="gpt-6-luna", effort="low", budget=2.0, client=None):
        self.model, self.effort, self.budget = model, effort, budget
        self.client = client
        self.calls = self.cached = self.invalid = 0
        self.input_ = self.cache_input = self.output = self.reasoning = 0

    @property
    def cost(self):
        input_price, cache_price, output_price = PRICES[self.model]
        return ((self.input_ - self.cache_input) * input_price + self.cache_input * cache_price
                + self.output * output_price) / 1e6

    def __call__(self, guidelines, message, schema):
        key = hashlib.sha256(json.dumps([self.model, self.effort, guidelines, message, schema],
                                        ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        path = CACHE / f"{key}.json"
        if path.exists():
            self.cached += 1
            return self._output(json.loads(path.read_text(encoding="utf-8")))
        if self.cost >= self.budget:
            raise BudgetExceeded(f"{self.cost:.2f} $ dépensés, plafond {self.budget} $")
        self.client = self.client or OpenAI()
        response = self.client.responses.create(
            model=self.model,
            instructions=guidelines,
            input=message,
            reasoning={"effort": self.effort},
            max_output_tokens=MAX_OUTPUT,
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
        self.input_ += trace["entree"]
        self.cache_input += trace["entree_cache"]
        self.output += trace["sortie"]
        self.reasoning += trace["reflexion"]
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")
        return self._output(trace)

    def _output(self, trace):
        try:
            return json.loads(trace["texte"]) if trace["statut"] == "completed" else None
        except (TypeError, ValueError):
            self.invalid += 1
            return None
