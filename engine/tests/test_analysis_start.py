"""« Au début… » et le verbe « ajouter » (synonyme d'« insérer »).

Fixture : fixtures/debut_amendements.json, des amendements réels de la 17e législature. Le premier (n° 337 sur
PRJLANR5L17BTC2984) est celui qui a fait ajouter ces règles ; les autres couvrent les chapeaux voisins les plus
fréquents de CLAIR (« au début de l'alinéa N, substituer aux mots : » : 188).
"""

import json
from pathlib import Path

import pytest

from engine.analysis import Target, Instruction, Operation, Scope, analyze_with_grammar, split_instructions

AMENDEMENTS = {a["numero"]: a for a in json.loads((Path(__file__).parent / "fixtures/debut_amendements.json")
                                                     .read_text(encoding="utf-8"))}
SCOPE = Scope("article", "PREMIER")


def analyze(numero):
    (instruction,) = split_instructions(AMENDEMENTS[numero]["dispositif"].split("\n\n"))
    return analyze_with_grammar(instruction, SCOPE)


def test_at_start_add_following_alinea():
    # Le « I. – » du texte cité ne doit pas être pris pour une nouvelle instruction : il suit un guillemet.
    (op,) = analyze("337")
    assert (op.verb, op.target) == ("insert_node", Target(node="article", position="debut"))
    assert op.text.startswith("I. – Au deuxième alinéa du II de l’article 9 de la loi n° 2000‑614")
    # Un seul » à la fin dans l'open data : il ferme la citation intérieure, il faut le garder.
    assert op.text.endswith("les mots : « peut également intervenir ».")


@pytest.mark.parametrize("numero, verb, target", [
    ("AS83", "insert", Target(alinea=1, position="debut")),                          # ajouter les mots
    ("I-1137", "insert", Target(alinea=2, position="debut")),                        # insérer les mots
    ("CF14", "substitute", Target(alinea=20, position="debut")),
    ("619", "delete", Target(alinea=18, phrase=1, node="phrase", position="debut")),
    ("1356", "insert_node", Target(node="article", position="debut")),              # les deux alinéas suivants
    ("CS1192", "insert_node", Target(alinea=1, position="avant")),                   # Avant l'alinéa 1, ajouter
    ("II-889", "insert_node", Target(alinea=15, position="apres")),                  # un alinéa ainsi rédigé
])
def test_at_start_and_add_wordings(numero, verb, target):
    (op,) = analyze(numero)
    assert (op.verb, op.target) == (verb, target)


def test_insert_words_without_anchor_rejected():
    # Sans « après le mot », ni début ni fin, on ne sait pas où mettre les mots.
    instruction = Instruction(None, "À l’alinéa 3, ajouter les mots : « en tout état de cause ».", [])
    assert analyze_with_grammar(instruction, SCOPE) is None


def test_at_alinea_start_insert_words():
    instruction = Instruction(None, "Au début de l’alinéa 3, insérer les mots : « Sauf exception, ».", [])
    assert analyze_with_grammar(instruction, SCOPE) == \
        [Operation("insert", Target(alinea=3, position="debut"), text="Sauf exception,")]
