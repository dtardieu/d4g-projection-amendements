"""Cibles au niveau de la phrase (« À la première phrase de l'alinéa 3, … »).

Fixture : fixtures/phrases_amendements.json, un amendement réel de la 17e législature par formule, choisi
parmi les chapeaux les plus fréquents de CLAIR (« à la première phrase de l'alinéa N, … » : ~1 900).
"""

import json
from pathlib import Path

import pytest

from engine.analysis import Target, Instruction, Operation, Scope, analyze_with_grammar, split_instructions

AMENDMENTS = {a["numero"]: a for a in json.loads((Path(__file__).parent / "fixtures/phrases_amendements.json")
                                                     .read_text(encoding="utf-8"))}
SCOPE = Scope("article", "PREMIER")


def analyze(numero):
    (instruction,) = split_instructions(AMENDMENTS[numero]["dispositif"].split("\n\n"))
    return analyze_with_grammar(instruction, SCOPE)


@pytest.mark.parametrize("numero, expected", [
    # « À la première phrase de l'alinéa 3, supprimer les mots : »
    ("930", [Operation("delete", Target(alinea=3, sentence=1, node="phrase"),
                       old_words=", pendant une durée maximale de douze heures précédant et suivant lesdites "
                                    "manifestations")]),
    # « Supprimer la seconde phrase de l'alinéa 6. »
    ("474", [Operation("delete_node", Target(alinea=6, sentence=2, node="phrase"))]),
    # « À la fin de la première phrase de l'alinéa 2, substituer aux mots : »
    ("I-3980", [Operation("substitute", Target(alinea=2, sentence=1, node="phrase", position="fin"),
                          old_words="inférieure de plus de 40 % à celle de la France en matière d’imposition sur "
                                       "les revenus du travail, du capital ou du patrimoine",
                          text="sur les revenus du travail inférieure de plus de 40 % à celle de la France")]),
    # « À la première phrase du troisième alinéa, substituer au mot : »
    ("AC1891", [Operation("substitute", Target(alinea=3, sentence=1, node="phrase"), old_words="quatorze",
                          text="cinquante-sept")]),
    # « Compléter la dernière phrase de l'alinéa 5 par les mots : »
    ("1213", [Operation("complete", Target(alinea=5, sentence=-1, node="phrase"),
                        text="et affichés de manière visible dans les écoles et établissements")]),
    # « À la deuxième phrase de l'alinéa 4, après le mot : « enfants », insérer les mots : »
    ("AS15", [Operation("insert", Target(alinea=4, sentence=2, node="phrase", words="enfants", position="apres"),
                        text="et ,le cas échéant, à leur situation de handicap")]),
])
def test_sentence_targets(numero, expected):
    assert analyze(numero) == expected


def test_insert_sentence_after_sentence():
    # « Après la première phrase de l'alinéa 11, insérer la phrase suivante : »
    (op,) = analyze("1181")
    assert (op.verb, op.target) == ("insert_node", Target(alinea=11, sentence=1, node="phrase", position="apres"))
    assert op.text.startswith("La répartition entre les régions") and op.text.endswith("à l’insularité.")


def test_complete_paragraph_with_sentences():
    # « par les deux phrases suivantes » : complete, et le nombre de phrases n'est pas compté sur les alinéas cités.
    (two,) = analyze("AS14")
    assert (two.verb, two.target) == ("complete", Target(alinea=4))
    assert two.text == "L’entretien est assuré hors la présence des parents de l’enfant. Il est conduit dans " \
                         "une langue comprise par l’enfant."
    (une,) = analyze("2965")
    assert (une.verb, une.target) == ("complete", Target(alinea=3))


@pytest.mark.parametrize("ordinal, rank", [("seconde", 2), ("second", 2), ("dernière", -1), ("dix-septième", 17)])
def test_ordinals_with_common_prefix(ordinal, rank):
    # L'expression régulière des ordinaux doit aller jusqu'au bout du mot (« second » est un préfixe de « seconde »).
    instruction = Instruction(None, f"Supprimer la {ordinal} phrase de l’alinéa 6.", [])
    assert analyze_with_grammar(instruction, SCOPE) == \
        [Operation("delete_node", Target(alinea=6, sentence=rank, node="phrase"))]
