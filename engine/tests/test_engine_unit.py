"""Ce que la PPL 822 n'exerce pas : ancres stables, ambiguïtés, phrases, plages d'alinéas, renvois."""

import unicodedata

import pytest

from engine.analysis import (RawAmendment, Target, Operation, Scope, _quoted_text, analyze_amendment, analyze_scope,
                            clean_dispositif)
from engine.application import apply
from engine.consolidation import consolidate
from engine.resolution import APPROXIMATE, OTHER_PARAGRAPH, NOT_AT_EDGE, TYPOGRAPHY, AmbiguousTarget, DanglingTarget
from engine.text import number_key, create_version, fingerprint, lines_of, sentences, create_tokens, Identifiers

SCOPE = Scope("article", "PREMIER")
TEXT = create_version([("PREMIER", "Un alinéa un.\nLe mot cible et le mot cible.\nDernier alinéa. Seconde phrase.")])


def operations(dispositif):
    analysis = analyze_amendment(RawAmendment("a", "1", "T", "ART. PREMIER", None, dispositif, None, None, None))
    assert not analysis.errors, analysis.errors
    return analysis.operations


def apply_to(dispositif, current=TEXT, reference=TEXT):
    return lines_of(apply(operations(dispositif), SCOPE, reference, current, "a")[0].articles[0])


def test_number_read_in_reference_not_current_version():
    # Une insertion adoptée avant décale la numérotation courante, pas l'identité de l'alinéa visé.
    inserted, _ = apply(operations("Au début, ajouter l'alinéa suivant :\n\n« Nouveau. »"), SCOPE, TEXT, TEXT, "x")
    assert lines_of(inserted.articles[0])[0] == "Nouveau."
    lignes = apply_to("À l'alinéa 3, supprimer le mot : « Seconde ».", inserted)
    assert lignes[3] == "Dernier alinéa. phrase."


def test_decomposed_accents_recomposed():
    decomposed = unicodedata.normalize("NFD", "Est créée une commission.")
    assert [j.text for j in create_tokens(decomposed, Identifiers("t")) if not j.text.isspace()] == \
        ["Est", "créée", "une", "commission", "."]


def test_anchor_found_despite_decomposed_accents():
    text = create_version([("PREMIER", unicodedata.normalize("NFD", "Est créée une commission."))])
    ops = operations("À l'alinéa 1, substituer au mot : « créée », le mot : « instituée ».")
    assert lines_of(apply(ops, SCOPE, text, text, "a")[0].articles[0]) == ["Est instituée une commission."]


def test_missing_words_dangling_target():
    with pytest.raises(DanglingTarget):
        apply_to("À l'alinéa 1, supprimer le mot : « absent ».")


def test_complete_empty_article():
    empty = create_version([("PREMIER", "")])
    ops = [Operation("complete", Target(node="article"), text="mots")]
    with pytest.raises(DanglingTarget):
        apply(ops, SCOPE, empty, empty, "a")


def test_quoted_block_keeps_inserted_article_quotes():
    # Un seul guillemet par alinéa dans l'amendement, pour deux niveaux de citation.
    assert _quoted_text(["« I. – Le code est complété par un article L. 1 ainsi rédigé :", "« Art. L. 1. – Premier.",
                        "« Second. » ;", "« II. – Autre point."]).split("\n") == [
        "I. – Le code est complété par un article L. 1 ainsi rédigé :", "« Art. L. 1. – Premier.", "« Second. » ;",
        "II. – Autre point."]


def test_block_quoted_entirely_keeps_its_closing():
    assert _quoted_text(["« Le code est complété par un article L. 1 ainsi rédigé :", "« Art. L. 1. – Texte. »"]).split("\n") == [
        "Le code est complété par un article L. 1 ainsi rédigé :", "« Art. L. 1. – Texte. »"]


def test_amendment_double_quote():
    assert _quoted_text(["« Un article ainsi rédigé :", "« « Art. L. 1. – Texte. » »"]).split("\n") == [
        "Un article ainsi rédigé :", "« Art. L. 1. – Texte. »"]


def test_single_quoted_paragraph_loses_quotes():
    assert _quoted_text(["« Texte simple. »"]) == "Texte simple."
    assert _quoted_text(["« Un.", "« Deux. »"]) == "Un.\nDeux."


def test_insertion_after_quote_announcement():
    text = create_version([("PREMIER", "Il est inséré un article ainsi rédigé :\n« Art. L. 1. – Texte. »\nFin.")])
    ops = operations("Après l'alinéa 1, insérer l'alinéa suivant :\n\n« Art. L. 0. – Avant. »")
    assert lines_of(apply(ops, SCOPE, text, text, "a")[0].articles[0])[1] == "« Art. L. 0. – Avant."


def test_replacing_last_quoted_paragraph_restores_closing():
    text = create_version([("PREMIER", "Article ainsi rédigé :\n« Art. L. 1. – Texte. » ;\nFin.")])
    ops = operations("Rédiger ainsi l'alinéa 2 :\n\n« Art. L. 1. – Nouveau. »")
    assert lines_of(apply(ops, SCOPE, text, text, "a")[0].articles[0])[1] == "« Art. L. 1. – Nouveau. » ;"


def test_paragraph_missing_from_reference():
    with pytest.raises(DanglingTarget):
        apply_to("Supprimer l'alinéa 9.")


def test_paragraph_deleted_in_between():
    without, _ = apply(operations("Supprimer l'alinéa 1."), SCOPE, TEXT, TEXT, "x")
    with pytest.raises(DanglingTarget):
        apply_to("À l'alinéa 1, supprimer le mot : « alinéa ».", without)


def test_words_present_twice_without_rank_ambiguous():
    with pytest.raises(AmbiguousTarget):
        apply_to("À l'alinéa 2, supprimer le mot : « cible ».")


def test_occurrence_rank_resolves_ambiguity():
    lignes = apply_to("À l'alinéa 2, substituer à la seconde occurrence du mot : « cible », le mot : « but ».")
    assert lignes[1] == "Le mot cible et le mot but."


def test_at_end_targets_last_occurrence_even_far_from_edge():
    # Les amendements disent « à la fin » de mots proches de la fin : on applique, et on le signale.
    version, effect = apply(operations("À la fin de l'alinéa 2, supprimer les mots : « et le mot »."),
                               SCOPE, TEXT, TEXT, "a")
    assert lines_of(version.articles[0])[1] == "Le mot cible cible."
    assert effect.tolerances == {NOT_AT_EDGE}


def test_exact_at_end_flags_nothing():
    _, effect = apply(operations("À la fin de l'alinéa 2, supprimer le mot : « cible »."), SCOPE, TEXT, TEXT, "a")
    assert not effect.tolerances


GAPS_TEXT = create_version([("PREMIER", "Le délai va au‑delà de 4 000 euros.\n"
                                          "La substance létale est administrée par le médecin.\nRien ici.")])


def apply_with_gap(dispositif, current=GAPS_TEXT):
    version, effect = apply(operations(dispositif), SCOPE, GAPS_TEXT, current, "a")
    return lines_of(version.articles[0]), effect.tolerances


@pytest.mark.parametrize("dispositif, rank, expected, tolerance", [
    ("À l'alinéa 1, substituer au montant : « 4000 euros », le montant : « 8500 euros ».", 0,
     "Le délai va au‑delà de 8500 euros.", TYPOGRAPHY),
    ("À l'alinéa 1, supprimer les mots : « au delà ».", 0, "Le délai va de 4 000 euros.", TYPOGRAPHY),
    ("À l'alinéa 2, substituer aux mots : « substante létale », les mots : « dose ».", 1,
     "La dose est administrée par le médecin.", APPROXIMATE),
    ("À l'alinéa 3, supprimer le mot : « médecin ».", 1, "La substance létale est administrée par le.", OTHER_PARAGRAPH),
    ("À l'alinéa 9, supprimer le mot : « médecin ».", 1, "La substance létale est administrée par le.", OTHER_PARAGRAPH),
])
def test_quote_found_despite_a_gap(dispositif, rank, expected, tolerance):
    lignes, tolerances = apply_with_gap(dispositif)
    assert lignes[rank] == expected
    assert tolerances == {tolerance}


def test_exact_quote_without_tolerance():
    assert apply_with_gap("À l'alinéa 2, supprimer le mot : « létale ».")[1] == frozenset()


@pytest.mark.parametrize("dispositif", [
    # Un autre nombre n'est pas une coquille.
    "À l'alinéa 1, supprimer les mots : « au‑delà de 5 000 euros ».",
    # Trop court pour qu'une lettre de différence soit une coquille.
    "À l'alinéa 2, supprimer le mot : « médecine ».",
    # Une coquille et un mauvais numéro à la fois.
    "À l'alinéa 3, supprimer les mots : « substante létale ».",
])
def test_gap_too_large_dangling_target(dispositif):
    with pytest.raises(DanglingTarget):
        apply_with_gap(dispositif)


def test_words_removed_by_adopted_not_searched_elsewhere():
    without, _ = apply(operations("À l'alinéa 2, supprimer le mot : « médecin »."), SCOPE, GAPS_TEXT,
                        GAPS_TEXT, "x")
    with pytest.raises(DanglingTarget):
        apply_with_gap("À l'alinéa 2, substituer au mot : « médecin », le mot : « praticien ».", without)


def test_status_says_what_was_tolerated():
    amendment = RawAmendment("a", "1", "T", "ART. PREMIER", None,
                                "À l'alinéa 1, supprimer les mots : « au delà ».", None, None, None, "Adopté")
    status = consolidate(GAPS_TEXT, [amendment]).statuses["a"]
    assert (status.etat, status.detail) == ("applique", TYPOGRAPHY)


def test_paragraph_range():
    assert apply_to("Supprimer les alinéas 1 à 2.") == ["Dernier alinéa. Seconde phrase."]
    assert apply_to("Supprimer les alinéas 2 et 3.") == ["Un alinéa un."]
    assert apply_to("Substituer aux alinéas 1 à 2 l'alinéa suivant :\n\n« Fusion. »")[0] == "Fusion."


def test_targeted_sentence():
    assert apply_to("Supprimer la seconde phrase de l'alinéa 3.")[2] == "Dernier alinéa."
    assert apply_to("Compléter la première phrase de l'alinéa 3 par les mots : « bis ».")[2] == \
        "Dernier alinéa bis. Seconde phrase."


def test_word_searched_in_sentence_only():
    text = create_version([("PREMIER", "Le mot ici. Encore le mot là.")])
    ops = operations("À la seconde phrase de l'alinéa 1, substituer au mot : « mot », le mot : « terme ».")
    new_value, _ = apply(ops, SCOPE, text, text, "a")
    assert lines_of(new_value.articles[0]) == ["Le mot ici. Encore le terme là."]


def test_reference_copies_previous_operation():
    text = create_version([("PREMIER", "Un mot.\nDeux mots.\nTrois mot.")])
    ops = operations("I. – À l'alinéa 1, substituer au mot : « mot », le mot : « terme ».\n\n"
                     "II. – En conséquence, procéder à la même substitution à l'alinéa 3.")
    new_value, _ = apply(ops, SCOPE, text, text, "a")
    assert lines_of(new_value.articles[0]) == ["Un terme.", "Deux mots.", "Trois terme."]


def test_after_the_word_rewrite_the_end():
    assert apply_to("À l'alinéa 1, après le mot : « alinéa », rédiger ainsi la fin de cet alinéa : « deux. »")[0] == \
        "Un alinéa deux."
    assert apply_to("Après le mot : « alinéa », supprimer la fin de l'alinéa 1.")[0] == "Un alinéa."


def test_restore_deleted_article():
    without, _ = apply(operations("Supprimer cet article."), SCOPE, TEXT, TEXT, "x")
    assert without.articles[0].supprime
    assert apply_to("Rétablir cet article dans la rédaction suivante :\n\nNouveau texte.", without) == ["Nouveau texte."]


def test_no_operation_applied_if_one_fails():
    ops = [Operation("delete_node", Target(alinea=1)), Operation("delete_node", Target(alinea=9))]
    with pytest.raises(DanglingTarget):
        apply(ops, SCOPE, TEXT, TEXT, "a")


def test_same_content_same_fingerprint():
    un, _ = apply(operations("À l'alinéa 1, supprimer le mot : « alinéa »."), SCOPE, TEXT, TEXT, "a")
    other, _ = apply(operations("À l'alinéa 1, supprimer le mot : « alinéa »."), SCOPE, TEXT, TEXT, "b")
    assert fingerprint(un) == fingerprint(other) != fingerprint(TEXT)


@pytest.mark.parametrize("text, expected", [
    ("Une phrase. Une autre.", ["Une phrase.", "Une autre."]),
    ("Voir l’article L. 251‑10 du code. Fin.", ["Voir l’article L. 251‑10 du code.", "Fin."]),
    ("« Art. L. 250‑10. – Texte du I. Suite. »", ["« Art. L. 250‑10. – Texte du I.", "Suite. »"]),
    ("I. – Premier point. Second point.", ["I. – Premier point.", "Second point."]),
    ("Selon l’art. 5 du texte. Fin.", ["Selon l’art. 5 du texte.", "Fin."]),
    ("Les mots : « a. B » sont retirés. Fin.", ["Les mots : « a. B » sont retirés.", "Fin."]),
    ("Le règlement n° 1408/2013 s’applique.", ["Le règlement n° 1408/2013 s’applique."]),
])
def test_sentences(text, expected):
    tokens = create_tokens(text, Identifiers("t"))
    assert ["".join(j.text for j in tokens[d:f]) for d, f in sentences(tokens)] == expected


@pytest.mark.parametrize("targeted, expected", [
    ("ART. UNIQUE", Scope("article", "UNIQUE")),
    ("ART. 6 BIS", Scope("article", "6 BIS")),
    ("APRÈS ART. UNIQUE", Scope("article_additionnel", "UNIQUE", "apres")),
    ("AVANT L'ART. 3", Scope("article_additionnel", "3", "avant")),
    ("TITRE", Scope("titre", None)),
])
def test_scope(targeted, expected):
    assert analyze_scope(targeted) == expected


def test_unknown_scope():
    with pytest.raises(ValueError):
        analyze_scope("ANNEXE B")


def test_number_key():
    assert number_key("PREMIER") == number_key("1") == "1"
    assert number_key("34FORFAITISER ET HARMONISER") == "34"
    assert number_key("6 bis") == "6 BIS"


def test_wording_with_decomposed_accents_recognized():
    decomposed = unicodedata.normalize("NFD", "À l'alinéa 1, supprimer le mot : « alinéa ».")
    assert apply_to(decomposed)[0] == "Un un."


def test_clean_dispositif():
    assert clean_dispositif("<p>Un &amp; deux.</p><p><span>Trois</span></p>") == ["Un & deux.", "Trois"]
    assert clean_dispositif("Un.\n\nDeux.\n") == ["Un.", "Deux."]


def test_sentence_without_paragraph_in_single_paragraph_article():
    unique = create_version([("PREMIER", "Première phrase. Seconde phrase.")])
    version, _ = apply(operations("À la seconde phrase, supprimer le mot : « Seconde »."), SCOPE, unique, unique, "a")
    assert lines_of(version.articles[0]) == ["Première phrase. phrase."]


def test_sentence_without_paragraph_in_multi_paragraph_article():
    with pytest.raises(DanglingTarget):
        apply_to("Supprimer la seconde phrase.")


@pytest.mark.parametrize("new, expected", [
    ("« mot »", "Dernier mot. Seconde phrase."),               # la ponctuation de la phrase reste
    ("« mot final. »", "Dernier mot final. Seconde phrase."),  # le texte nouveau apporte la sienne
])
def test_rewrite_sentence_end_keeps_following(new, expected):
    lignes = apply_to(f"À la première phrase de l'alinéa 3, après le mot : « Dernier », "
                           f"rédiger ainsi la fin de la première phrase : {new}.")
    assert lignes[2] == expected
