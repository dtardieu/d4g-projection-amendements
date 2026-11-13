"""Cibles au niveau de la phrase (« À la première phrase de l'alinéa 3, … »).

Fixture : fixtures/phrases_amendements.json, un amendement réel de la 17e législature par formule, choisi
parmi les chapeaux les plus fréquents de CLAIR (« à la première phrase de l'alinéa N, … » : ~1 900).
"""

import json
from pathlib import Path

import pytest

from moteur.analyse import Cible, Instruction, Operation, Portee, analyser_par_grammaire, decouper_instructions

AMENDEMENTS = {a["numero"]: a for a in json.loads((Path(__file__).parent / "fixtures/phrases_amendements.json")
                                                     .read_text(encoding="utf-8"))}
PORTEE = Portee("article", "PREMIER")


def analyser(numero):
    (instruction,) = decouper_instructions(AMENDEMENTS[numero]["dispositif"].split("\n\n"))
    return analyser_par_grammaire(instruction, PORTEE)


@pytest.mark.parametrize("numero, attendu", [
    # « À la première phrase de l'alinéa 3, supprimer les mots : »
    ("930", [Operation("delete", Cible(alinea=3, phrase=1, noeud="phrase"),
                       anciens_mots=", pendant une durée maximale de douze heures précédant et suivant lesdites "
                                    "manifestations")]),
    # « Supprimer la seconde phrase de l'alinéa 6. »
    ("474", [Operation("delete_node", Cible(alinea=6, phrase=2, noeud="phrase"))]),
    # « À la fin de la première phrase de l'alinéa 2, substituer aux mots : »
    ("I-3980", [Operation("substitute", Cible(alinea=2, phrase=1, noeud="phrase", position="fin"),
                          anciens_mots="inférieure de plus de 40 % à celle de la France en matière d’imposition sur "
                                       "les revenus du travail, du capital ou du patrimoine",
                          texte="sur les revenus du travail inférieure de plus de 40 % à celle de la France")]),
    # « À la première phrase du troisième alinéa, substituer au mot : »
    ("AC1891", [Operation("substitute", Cible(alinea=3, phrase=1, noeud="phrase"), anciens_mots="quatorze",
                          texte="cinquante-sept")]),
    # « Compléter la dernière phrase de l'alinéa 5 par les mots : »
    ("1213", [Operation("complete", Cible(alinea=5, phrase=-1, noeud="phrase"),
                        texte="et affichés de manière visible dans les écoles et établissements")]),
    # « À la deuxième phrase de l'alinéa 4, après le mot : « enfants », insérer les mots : »
    ("AS15", [Operation("insert", Cible(alinea=4, phrase=2, noeud="phrase", mots="enfants", position="apres"),
                        texte="et ,le cas échéant, à leur situation de handicap")]),
])
def test_cibles_phrase(numero, attendu):
    assert analyser(numero) == attendu


def test_inserer_une_phrase_apres_une_phrase():
    # « Après la première phrase de l'alinéa 11, insérer la phrase suivante : »
    (op,) = analyser("1181")
    assert (op.verbe, op.cible) == ("insert_node", Cible(alinea=11, phrase=1, noeud="phrase", position="apres"))
    assert op.texte.startswith("La répartition entre les régions") and op.texte.endswith("à l’insularité.")


def test_completer_un_alinea_par_des_phrases():
    # « par les deux phrases suivantes » : complete, et le nombre de phrases n'est pas compté sur les alinéas cités.
    (deux,) = analyser("AS14")
    assert (deux.verbe, deux.cible) == ("complete", Cible(alinea=4))
    assert deux.texte == "L’entretien est assuré hors la présence des parents de l’enfant. Il est conduit dans " \
                         "une langue comprise par l’enfant."
    (une,) = analyser("2965")
    assert (une.verbe, une.cible) == ("complete", Cible(alinea=3))


@pytest.mark.parametrize("ordinal, rang", [("seconde", 2), ("second", 2), ("dernière", -1), ("dix-septième", 17)])
def test_ordinaux_a_prefixe_commun(ordinal, rang):
    # L'expression régulière des ordinaux doit aller jusqu'au bout du mot (« second » est un préfixe de « seconde »).
    instruction = Instruction(None, f"Supprimer la {ordinal} phrase de l’alinéa 6.", [])
    assert analyser_par_grammaire(instruction, PORTEE) == \
        [Operation("delete_node", Cible(alinea=6, phrase=rang, noeud="phrase"))]
