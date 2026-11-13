"""« Au début… » et le verbe « ajouter » (synonyme d'« insérer »).

Fixture : fixtures/debut_amendements.json, des amendements réels de la 17e législature. Le premier (n° 337 sur
PRJLANR5L17BTC2984) est celui qui a fait ajouter ces règles ; les autres couvrent les chapeaux voisins les plus
fréquents de CLAIR (« au début de l'alinéa N, substituer aux mots : » : 188).
"""

import json
from pathlib import Path

import pytest

from moteur.analyse import Cible, Instruction, Operation, Portee, analyser_par_grammaire, decouper_instructions

AMENDEMENTS = {a["numero"]: a for a in json.loads((Path(__file__).parent / "fixtures/debut_amendements.json")
                                                     .read_text(encoding="utf-8"))}
PORTEE = Portee("article", "PREMIER")


def analyser(numero):
    (instruction,) = decouper_instructions(AMENDEMENTS[numero]["dispositif"].split("\n\n"))
    return analyser_par_grammaire(instruction, PORTEE)


def test_au_debut_ajouter_l_alinea_suivant():
    # Le « I. – » du texte cité ne doit pas être pris pour une nouvelle instruction : il suit un guillemet.
    (op,) = analyser("337")
    assert (op.verbe, op.cible) == ("insert_node", Cible(noeud="article", position="debut"))
    assert op.texte.startswith("I. – Au deuxième alinéa du II de l’article 9 de la loi n° 2000‑614")
    # Un seul » à la fin dans l'open data : il ferme la citation intérieure, il faut le garder.
    assert op.texte.endswith("les mots : « peut également intervenir ».")


@pytest.mark.parametrize("numero, verbe, cible", [
    ("AS83", "insert", Cible(alinea=1, position="debut")),                          # ajouter les mots
    ("I-1137", "insert", Cible(alinea=2, position="debut")),                        # insérer les mots
    ("CF14", "substitute", Cible(alinea=20, position="debut")),
    ("619", "delete", Cible(alinea=18, phrase=1, noeud="phrase", position="debut")),
    ("1356", "insert_node", Cible(noeud="article", position="debut")),              # les deux alinéas suivants
    ("CS1192", "insert_node", Cible(alinea=1, position="avant")),                   # Avant l'alinéa 1, ajouter
    ("II-889", "insert_node", Cible(alinea=15, position="apres")),                  # un alinéa ainsi rédigé
])
def test_formules_au_debut_et_ajouter(numero, verbe, cible):
    (op,) = analyser(numero)
    assert (op.verbe, op.cible) == (verbe, cible)


def test_inserer_des_mots_sans_ancrage_rejete():
    # Sans « après le mot », ni début ni fin, on ne sait pas où mettre les mots.
    instruction = Instruction(None, "À l’alinéa 3, ajouter les mots : « en tout état de cause ».", [])
    assert analyser_par_grammaire(instruction, PORTEE) is None


def test_au_debut_de_l_alinea_inserer_les_mots():
    instruction = Instruction(None, "Au début de l’alinéa 3, insérer les mots : « Sauf exception, ».", [])
    assert analyser_par_grammaire(instruction, PORTEE) == \
        [Operation("insert", Cible(alinea=3, position="debut"), texte="Sauf exception,")]
