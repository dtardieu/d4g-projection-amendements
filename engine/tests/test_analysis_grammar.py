"""analyser_par_grammaire sur les 29 amendements de la PPL 822 (commission sur B0822, séance sur BTC1003).

Fixture : fixtures/ppl822_amendements.json, export de la table amendements de CLAIR (sort Charge exclu).
Les attendus suivent docs/amendment_tracking_worked_examples_fr.md.
"""

import json
from pathlib import Path

import pytest

from engine.analysis import (RawAmendement, Target, Instruction, Operation, Scope, analyze_amendement,
                            analyze_with_grammar, split_instructions, llm_message)

AMENDEMENTS = {a["numero"]: a for a in json.loads((Path(__file__).parent / "fixtures/ppl822_amendements.json")
                                                     .read_text(encoding="utf-8"))}


def scope(article_vise):
    # analyser_portee n'est pas encore écrit : les trois formes présentes dans la PPL 822 suffisent ici.
    if article_vise == "TITRE":
        return Scope("titre", None)
    if article_vise.startswith("APRÈS ART."):
        return Scope("article_additionnel", article_vise.removeprefix("APRÈS ART.").strip(), "apres")
    return Scope("article", article_vise.removeprefix("ART.").strip())


def analyze(numero):
    """Opérations de toutes les instructions, chaque instruction recevant la cible de la précédente."""
    amendement, operations, preceding = AMENDEMENTS[numero], [], None
    for instruction in split_instructions(amendement["dispositif"].split("\n\n")):
        ops = analyze_with_grammar(instruction, scope(amendement["article_vise"]), preceding)
        assert ops is not None, f"{numero} : instruction {instruction.numero} non reconnue"
        operations += ops
        preceding = ops[-1].target
    return operations


def test_all_amendements_recognized():
    for numero in AMENDEMENTS:
        assert analyze(numero)


@pytest.mark.parametrize("numero, expected", [
    ("7", [Operation("substitute", Target(alinea=3), old_words="habilités",
                     text="mentionnés au premier alinéa de l’article L. 250‑3")]),
    ("8", [Operation("substitute", Target(alinea=3, position="fin"),
                     old_words=", en lui impartissant un délai raisonnable qu’ils fixent, de se conformer à ses "
                                  "obligations",
                     text="de se conformer à ses obligations dans un délai qu’ils fixent")]),
    ("6", [Operation("delete", Target(alinea=10, position="fin"),
                     old_words="si une possibilité de financement de l’arrachage a été préalablement portée à la "
                                  "connaissance du détenteur de la parcelle")]),
    ("10", [Operation("delete_node", Target(node="article"))]),
    ("CE5", [Operation("complete", Target(alinea=2),
                       text=", si une possibilité de financement de l’arrachage a été portée à la connaissance du "
                             "détenteur de la parcelle au préalable.")]),
    ("15", [Operation("rewrite", Target(node="titre"),
                      text="visant à instaurer des sanctions adaptées et proportionnées pour prévenir le "
                            "développement des vignes non cultivées")]),
    ("16", [Operation("substitute", Target(alinea=2), old_words="visant à instaurer des sanctions",
                      text="instaurant des réponses")]),
    ("17", [Operation("insert", Target(alinea=2, words="prévenir", position="apres"), text="notamment")]),
])
def test_inline_wordings(numero, expected):
    assert analyze(numero) == expected


def test_identicals_same_analysis():
    # Séries identiques 75790 et 75886 : même analyse, donc même effet.
    assert analyze("6") == analyze("9")
    assert analyze("17") == analyze("18")


def test_sous_amendement_keeps_parent_number():
    # « le dixième alinéa » de CE11 : le numéro reste tel quel, le décompte chapeau compris se fait à l'étape 2.
    assert analyze("CE12")[0].target == Target(alinea=10)


def test_same_alinea_reuses_previous_target():
    substitute, complete = analyze("CE10")
    assert (substitute.verb, substitute.target, substitute.old_words, substitute.text) == \
        ("substitute", Target(alinea=2), "puni", "passible")
    assert complete.verb == "complete" and complete.target == Target(alinea=2)
    assert complete.text.startswith("L’autorité administrative compétente")


def test_consequently_same_numbered_alinea():
    instructions = split_instructions(AMENDEMENTS["1"]["dispositif"].split("\n\n"))
    assert [i.consequently for i in instructions] == [False, True]
    assert [op.target for op in analyze("1")] == [Target(alinea=10), Target(alinea=10)]


def test_rewrite_article_keeps_alineas_without_quotes():
    (op,) = analyze("CE11")
    lignes = op.text.split("\n")
    assert (op.verb, op.target, len(lignes)) == ("rewrite", Target(node="article"), 9)
    assert lignes[0] == "Le code rural et de la pêche maritime est ainsi modifié :"
    assert lignes[-1].startswith("« Art. L. 251‑22.")


def test_quoted_alinea_blocks():
    (rewrite,) = analyze("4")
    assert (rewrite.verb, rewrite.target, rewrite.text.count("\n") + 1) == ("rewrite", Target(alinea=3), 3)
    assert rewrite.text.startswith("Art. L. 250‑10. –") and not rewrite.text.endswith("»")
    (insert,) = analyze("5")
    assert (insert.verb, insert.target, insert.text.count("\n") + 1) == \
        ("insert_node", Target(alinea=3, position="apres"), 6)
    (complete_article,) = analyze("CE4")
    assert (complete_article.verb, complete_article.target) == \
        ("insert_node", Target(node="article", position="fin"))


def test_article_additionnel():
    (op,) = analyze("CE2")
    assert (op.verb, op.target) == ("insert_node", Target(node="article", position="apres"))
    assert op.text.startswith("Le Gouvernement remet chaque année au Parlement")


@pytest.mark.parametrize("heading, quotes", [
    ("Substituer aux alinéas 4 et 7 l'alinéa suivant :", ["« Un. »"]),         # alinéas épars : pas une plage
    ("Rédiger ainsi cet alinéa :", ["« Un. »"]),                               # aucun alinéa connu
    ("Rédiger ainsi l'article 6 :", ["« Un. »"]),                              # l'amendement vise l'article premier
    ("Compléter la même phrase par les mots : « , etc. »", []),                # renvoi sans antécédent
    ("Après l’alinéa 3, insérer les deux alinéas suivants :", ["« Un. »"]),    # 2 annoncés, 1 cité
    ("Compléter ce même alinéa par les mots : « , etc. »", []),                # renvoi sans antécédent
])
def test_does_not_guess(heading, quotes):
    assert analyze_with_grammar(Instruction(None, heading, quotes), Scope("article", "PREMIER")) is None


# AC116 sur PIONANR5L17B0118 (ART. 13 BIS), tel que stocké dans CLAIR : le dernier alinéa cité n'a pas de « » »
# fermant. Le texte inséré modifie lui-même la loi n° 86-1067 : c'est l'opération imbriquée de la phase 2.
AC116 = ("Rédiger ainsi cet article :\n\n"
         "« Le premier alinéa de l’article 73 de la loi n° 86‑1067 du 30 septembre 1986 précitée est ainsi modifié :\n\n"
         "« 1° À la fin de la première phrase, les mots : « de deux interruptions publicitaires » sont remplacés par "
         "les mots : « d’une interruption publicitaire » ;\n\n"
         "« 2° La seconde phrase est supprimée.\n")


def test_rewrite_quoted_block_without_closing_quote():
    (instruction,) = split_instructions(AC116.split("\n\n"))
    (op,) = analyze_with_grammar(instruction, Scope("article", "13 BIS"))
    assert (op.verb, op.target) == ("rewrite", Target(node="article"))
    assert op.text.split("\n") == [
        "Le premier alinéa de l’article 73 de la loi n° 86‑1067 du 30 septembre 1986 précitée est ainsi modifié :",
        "1° À la fin de la première phrase, les mots : « de deux interruptions publicitaires » sont remplacés par "
        "les mots : « d’une interruption publicitaire » ;",
        "2° La seconde phrase est supprimée.",
    ]


def test_flattened_dispositif_rejected():
    # Copie de evals/amendements à la racine du dépôt (espaces écrasés) : sans sauts d'alinéa, les guillemets ouvrants ne sont plus
    # appariés et on ne sait plus où finit le chapeau. La grammaire refuse plutôt que de deviner.
    flattened = " ".join(AC116.split())
    (instruction,) = split_instructions([flattened])
    assert analyze_with_grammar(instruction, Scope("article", "13 BIS")) is None


def test_straight_or_typographic_apostrophe():
    scope_ = Scope("article", "PREMIER")
    for heading in ("À l'alinéa 2, supprimer le mot : « puni ».", "À l’alinéa 2, supprimer le mot : « puni »."):
        assert analyze_with_grammar(Instruction(None, heading, []), scope_) == \
            [Operation("delete", Target(alinea=2), old_words="puni")]


# ---- niveau LLM, avec un faux modèle : aucun appel réseau


def fake_model(output):
    return lambda guidelines, message, schema: output


LLM_OPERATION = {"verbe": "substitute", "cible": {"alinea": 2, "jusqua": None, "phrase": None, "mots": None,
                                                  "occurrence": None, "position": "dans", "noeud": "alinea"},
                 "anciens_mots": "a", "texte": "b"}


def raw_amendement(dispositif, targeted="ART. PREMIER"):
    return RawAmendement("u", "1", "T", targeted, None, dispositif, None, None, None)


def test_llm_only_if_grammar_rejects():
    def forbidden(*args):
        raise AssertionError("le LLM ne doit pas être appelé")
    analysis = analyze_amendement(raw_amendement("Supprimer cet article."), llm_call=forbidden)
    assert analysis.source == "grammar" and analysis.confidence == 1.0


def test_llm_takes_over():
    output = {"operations": [LLM_OPERATION], "confiance": 0.8, "rejet": None}
    analysis = analyze_amendement(raw_amendement("Formule inconnue du légiste."), "Un.", fake_model(output))
    assert analysis.source == "llm" and analysis.confidence == 0.8 and not analysis.errors
    assert analysis.operations == [Operation("substitute", Target(alinea=2), old_words="a", text="b")]


@pytest.mark.parametrize("output", [
    None,
    {"operations": [], "confiance": 0.9, "rejet": "tableau"},
    {"operations": [{**LLM_OPERATION, "texte": None}], "confiance": 0.9, "rejet": None},
    {"operations": [{"verbe": "substitute"}], "confiance": 0.9, "rejet": None},
])
def test_invalid_llm_output_reported(output):
    analysis = analyze_amendement(raw_amendement("Formule inconnue du légiste."), "Un.", fake_model(output))
    assert analysis.errors and analysis.operations == []


def test_llm_message_numbers_alineas():
    message = llm_message(Instruction(None, "Formule", ["« x »"]), Scope("article", "2"), "Un.\nDeux.", Target(alinea=3))
    assert "(2) Deux." in message and "Cible précédente : alinéa 3" in message and message.endswith("« x »")


def _analyze(dispositif):
    analysis = analyze_amendement(RawAmendement("a", "1", "T", "ART. PREMIER", None, dispositif, None, None, None))
    assert not analysis.errors, analysis.errors
    return analysis.operations


def _targets(dispositif):
    return [(op.verb, op.target.alinea, op.target.phrase, op.target.position) for op in _analyze(dispositif)]


@pytest.mark.parametrize("continuation, expected", [
    # Renvois à la phrase ou à l'alinéa de l'instruction précédente.
    ("à la même première phrase du même alinéa 4, supprimer le mot : « b ».", ("delete", 4, 1, "dans")),
    ("à la même phrase du même alinéa, supprimer le mot : « b ».", ("delete", 4, 1, "dans")),
    ("compléter ladite phrase dudit alinéa par les mots : « b ».", ("complete", 4, 1, "dans")),
    ("audit alinéa 4, supprimer le mot : « b ».", ("delete", 4, None, "dans")),
    ("à la fin dudit alinéa 4, supprimer le mot : « b ».", ("delete", 4, None, "fin")),
    ("supprimer la seconde phrase dudit alinéa.", ("delete_node", 4, 2, "dans")),
    ("à la seconde phrase, supprimer le mot : « b ».", ("delete", 4, 2, "dans")),
    ("compléter cet alinéa par les mots : « b ».", ("complete", 4, None, "dans")),
    ("après le mot : « b », rédiger ainsi la fin de l'alinéa : « c ».", ("rewrite", 4, None, "apres")),
])
def test_references_to_previous_instruction(continuation, expected):
    first_item = "I. – À la première phrase de l'alinéa 4, supprimer le mot : « a »."
    assert _targets(f"{first_item}\n\nII. – En conséquence, {continuation}")[1] == expected


def test_phrase_without_alinea_or_antecedent():
    assert _targets("À la seconde phrase, supprimer le mot : « b ».") == [("delete", None, 2, "dans")]


@pytest.mark.parametrize("dispositif, expected", [
    ("Supprimer les alinéas 4, 9 et 12.", [4, 9, 12]),          # épars : une suppression par alinéa
    ("Supprimer les alinéas 4 et 7.", [4, 7]),
])
def test_deleting_scattered_alineas(dispositif, expected):
    assert [(op.verb, op.target.alinea, op.target.until) for op in _analyze(dispositif)] == \
        [("delete_node", n, None) for n in expected]


def test_contiguous_alineas_stay_a_range():
    (op,) = _analyze("Supprimer les alinéas 4, 5 et 6.")
    assert (op.target.alinea, op.target.until) == (4, 6)


@pytest.mark.parametrize("renvoi, expected", [
    ("aux alinéas 7 et 8", [(7, "dans"), (8, "dans")]),
    ("aux alinéas 7, 9 et 12", [(7, "dans"), (9, "dans"), (12, "dans")]),
    ("à la fin des alinéas 41 et 71", [(41, "fin"), (71, "fin")]),
    ("au début de l'alinéa 6", [(6, "debut")]),
])
def test_reference_to_several_alineas(renvoi, expected):
    operations = _analyze("I. – À l'alinéa 2, substituer au mot : « a » le mot : « b ».\n\n"
                           f"II. – En conséquence, procéder à la même substitution {renvoi}.")
    assert [(op.verb, op.old_words, op.text) for op in operations[1:]] == [("substitute", "a", "b")] * len(expected)
    assert [(op.target.alinea, op.target.position) for op in operations[1:]] == expected


@pytest.mark.parametrize("dispositif", [
    "À l'alinéa 2, substituer au mot : « a » ; le mot : « b ».",
    "À l'alinéa 2, substituer au mot : « a ». le mot : « b ».",
    "À l'alinéa 2, substituer au mot ; « a », le mot : « b ».",
    "À l'alinéa 2, remplacer le mot : « a » par le mot : « b ».",
    "À l'alinéa 2, substituer au délai : « a » le délai : « b ».",
])
def test_punctuation_and_object_variants(dispositif):
    (op,) = _analyze(dispositif)
    assert (op.verb, op.target.alinea, op.old_words, op.text) == ("substitute", 2, "a", "b")


def test_mention_at_alinea_start():
    (op,) = _analyze("Au début de l'alinéa 1, insérer la mention : « I. – »")
    assert (op.verb, op.target.alinea, op.target.position, op.text) == ("insert", 1, "debut", "I. –")


def test_sub_instructions_under_header():
    operations = _analyze("À l'alinéa 3 :\n\n1° Supprimer les mots :\n\n« a » ;\n\n"
                           "2° Compléter l'alinéa par la phrase suivante :\n\n« B. »")
    assert [(op.verb, op.target.alinea) for op in operations] == [("delete", 3), ("complete", 3)]


def test_numbered_quoted_alinea_is_not_a_sub_instruction():
    # « 1° Après… » dans le texte cité d'un article qui modifie un code : un alinéa inséré, pas une instruction.
    (op,) = _analyze("Après l'alinéa 1, insérer les deux alinéas suivants :\n\n« I. – Le code est ainsi modifié :\n\n"
                      "1° Après le mot : « a », sont insérés les mots : « b ». »")
    assert (op.verb, len(op.text.split("\n"))) == ("insert_node", 2)


def test_restore_subdivision_and_substitute_with_comma():
    (restored,) = _analyze("Rétablir le I de l'alinéa 3 dans la rédaction suivante :\n\n« I. – Un. »")
    assert (restored.verb, restored.target.alinea) == ("rewrite", 3)
    (substituted,) = _analyze("Substituer à l'alinéa 18, les douze alinéas suivants :\n\n" + "\n\n".join(["« Un. »"] * 12))
    assert (substituted.verb, substituted.target.alinea, len(substituted.text.split("\n"))) == ("rewrite", 18, 12)


def test_article_designated_by_its_number():
    (op,) = _analyze("Rédiger ainsi l'article 1er :\n\n« Un. »")
    assert (op.verb, op.target.node) == ("rewrite", "article")
