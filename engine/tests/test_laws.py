"""Grammaire des lois (grammaires/lois.lark) et consolidation d'un article par une section modificative (moteur/lois.py)."""

import pytest

from engine.laws import consolidate_article, neutralize_references, replace_subdivisions

ARTICLE = "Premier alinéa a, la dernière semaine d'août, à Mayotte, ici. Seconde phrase.\nDeuxième alinéa b.\nTroisième."
SUBDIVIDED = "I. - Premier.\n1° Un a ;\n2° Deux b ;\nSuite du 2°.\nII. - Second c.\n1° Autre d."


@pytest.mark.parametrize("section, expected", [
    ("A l'article L. 571-2 du même code, les mots : « à Mayotte, » sont supprimés.",
     "Premier alinéa a, la dernière semaine d'août, ici. Seconde phrase.\nDeuxième alinéa b.\nTroisième."),
    ("L'article 10 est ainsi modifié :\n"
     "1° Au premier alinéa, les mots : « la dernière semaine d'août » sont remplacés par les mots : « le mois de décembre » ;\n"
     "2° La seconde phrase du premier alinéa est supprimée.",
     "Premier alinéa a, le mois de décembre, à Mayotte, ici.\nDeuxième alinéa b.\nTroisième."),
    ("L'article 3 est remplacé par les dispositions suivantes :\n« Art. 3.- Nouveau texte :\n« a) Le président. »",
     "Nouveau texte :\na) Le président."),
    ("Le deuxième alinéa de l'article 5 du décret susvisé est ainsi modifié :\n"
     "a) Les mots : « b » sont remplacés par les mots : « B » ;\nb) Il est ajouté une phrase ainsi rédigée : « Fin. »",
     "Premier alinéa a, la dernière semaine d'août, à Mayotte, ici. Seconde phrase.\nDeuxième alinéa B. Fin.\nTroisième."),
    ("L'article 5 du décret susviséest complété par l'alinéa suivant :\nTexte libre.", ARTICLE + "\nTexte libre."),
    ("Les deuxième et troisième alinéas sont supprimés.",
     "Premier alinéa a, la dernière semaine d'août, à Mayotte, ici. Seconde phrase."),
])
def test_consolidate(section, expected):
    output, analysis, error = consolidate_article(ARTICLE, section)
    assert (error, analysis.unparsed) == (None, [])
    assert output == expected


@pytest.mark.parametrize("section, expected", [
    ("Le 2° du I est supprimé.", "I. - Premier.\n1° Un a ;\nII. - Second c.\n1° Autre d."),
    ("Le I est ainsi modifié :\n1° Au 1°, les mots : « Un » sont remplacés par les mots : « Uno » ;\n"
     "2° Il est ajouté un alinéa ainsi rédigé :\n« 3° Trois. »",
     "I. - Premier.\n1° Uno a ;\n2° Deux b ;\nSuite du 2°.\n3° Trois.\nII. - Second c.\n1° Autre d."),
    ("Il est inséré avant le dernier alinéa un alinéa ainsi rédigé :\n« Nouveau. »",
     "I. - Premier.\n1° Un a ;\n2° Deux b ;\nSuite du 2°.\nII. - Second c.\nNouveau.\n1° Autre d."),
])
def test_subdivisions(section, expected):
    output, analysis, error = consolidate_article(SUBDIVIDED, section)
    assert (error, analysis.unparsed) == (None, [])
    assert output == expected


def test_ambiguous_subdivision_left_as_is():
    # Le 1° existe sous le I et sous le II : sans parent, on ne choisit pas.
    lignes = SUBDIVIDED.split("\n")
    assert replace_subdivisions("Au 1°, les mots", lignes) == ("Au 1°, les mots", False)
    assert replace_subdivisions("Au 1° du II, les mots", lignes) == ("À l'alinéa 6, les mots", True)


def test_references_neutralized_outside_quotes():
    assert neutralize_references("L'article est complété par un alinéa ainsi rédigé :") == \
        "L'article est complété par un alinéa ainsi rédigé :"
    assert neutralize_references("Au premier alinéa de l'article L. 12 du code X, les mots : « l'article 3 » sont") == \
        "Au premier alinéa, les mots : « l'article 3 » sont"
