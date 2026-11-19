"""Le moteur de bout en bout sur la PPL 822 : commission (B0822 → BTC1003) et séance (BTC1003 → T.A. 62).

Reprend les exemples 1 à 6 de docs/amendment_tracking_worked_examples_fr.md. Fixtures : exports de CLAIR, et
pour le T.A. n° 62 l'open data de l'AN (PIONANR5L17BTA0062.html), absent de textes_articles.
"""

import json
from pathlib import Path

import pytest

from engine.analysis import RawAmendement, analyze_amendement
from engine.application import apply, integrate_sous_amendements
from engine.comparison import commute, conflict, identical_effect, near_duplicates
from engine.consolidation import consolidate, call_order
from engine.text import create_version, diff_words, lines_of, normalize, provenance, render

FIXTURES = Path(__file__).parent / "fixtures"
AMENDEMENTS = [RawAmendement(**{k: a[k] for k in RawAmendement.__dataclass_fields__})
               for a in json.loads((FIXTURES / "ppl822_amendements.json").read_text(encoding="utf-8"))]
TEXTS = json.loads((FIXTURES / "ppl822_textes.json").read_text(encoding="utf-8"))
DEPOSIT, COMMISSION, ADOPTED = "PIONANR5L17B0822", "PIONANR5L17BTC1003", "PIONANR5L17BTA0062"


def version(ref):
    return create_version(TEXTS[ref]["articles"], TEXTS[ref]["titre"])


def amendements(ref):
    return [a for a in AMENDEMENTS if a.texte_ref == ref]


def amendement(ref, numero):
    return next(a for a in amendements(ref) if a.numero == numero)


def official(ref):
    return {numero: content.split("\n") for numero, content in TEXTS[ref]["articles"]}


def prepare(ref, numero):
    a = amendement(ref, numero)
    analysis = analyze_amendement(a)
    assert not analysis.errors
    return analysis.operations, analysis.scope, a.uid


@pytest.fixture(scope="module")
def commission():
    return consolidate(version(DEPOSIT), amendements(DEPOSIT))


@pytest.fixture(scope="module")
def seance():
    return consolidate(version(COMMISSION), amendements(COMMISSION))


def etat(consolidation, ref, numero):
    return consolidation.statuses[amendement(ref, numero).uid].etat


def test_all_amendements_analyzed():
    assert [a.numero for a in AMENDEMENTS if analyze_amendement(a).errors] == []


# ---- exemple 1 : CE11 fait tomber CE10, CE5, CE4


@pytest.mark.parametrize("numero", ["CE10", "CE5", "CE4"])
def test_rewrite_makes_fall(commission, numero):
    status = commission.statuses[amendement(DEPOSIT, numero).uid]
    assert status.etat == "predit_tombe" and status.sort == "Tombé"
    assert status.conflits == [amendement(DEPOSIT, "CE11").uid]


def test_predicted_fallen_equal_recorded_fallen(commission, seance):
    for consolidation in (commission, seance):
        for status in consolidation.statuses.values():
            if status.etat in ("predit_tombe", "predit_vivant"):
                assert (status.etat == "predit_tombe") == (status.sort == "Tombé"), status.numero


def test_ce10_from_original_text_applies():
    # Sur T₀, CE10 est valide : c'est l'adoption de CE11 qui le fait tomber, pas son texte.
    operations, scope, uid = prepare(DEPOSIT, "CE10")
    new_value, _ = apply(operations, scope, version(DEPOSIT), version(DEPOSIT), uid)
    line = lines_of(new_value.articles[0])[1]
    assert "Est passible d’une contravention" in line
    assert line.endswith("vignes. L’autorité administrative compétente étudie avec le propriétaire des parcelles "
                          "précitées les modalités de mise en œuvre des mesures mentionnées à l’article L251‑10. »")


# ---- exemple 2 : sous-amendement CE12, alinéa compté chapeau compris


def test_sous_amendement_targets_parent_heading_included(commission):
    lignes = dict(render(commission.version))["PREMIER"]
    assert lignes[8].endswith("du II de l’article L. 201‑4, si une possibilité de financement de l’arrachage a été "
                              "portée à la connaissance du détenteur de la parcelle au préalable. »")
    assert etat(commission, DEPOSIT, "CE12") == "sous_amendement"


def test_sous_amendement_voted_before_parent():
    numbers = [a.numero for a in call_order(amendements(DEPOSIT))]
    assert numbers.index("CE12") < numbers.index("CE11")


def test_near_duplicate_across_levels():
    # CE5 (tombé) et CE12 (adopté) : mêmes mots, cibles différentes, effets différents.
    ce5, ce12 = amendement(DEPOSIT, "CE5"), amendement(DEPOSIT, "CE12")
    pairs = near_duplicates({a.uid: analyze_amendement(a).operations for a in (ce5, ce12)})
    assert pairs == [tuple(sorted((ce5.uid, ce12.uid)))]


# ---- exemple 3 : articles additionnels et renumérotation dérivée


def test_articles_additionnels_in_vote_order(commission):
    assert [numero for numero, _ in render(commission.version)] == ["PREMIER", "2", "3", "4", "5", "6"]
    assert dict(render(commission.version))["3"][0].startswith("Le Gouvernement remet chaque année")
    assert "identifiant la typologie" in dict(render(commission.version))["6"][0]


def test_rejected_change_nothing(commission):
    assert etat(commission, DEPOSIT, "CE8") == etat(commission, DEPOSIT, "CE9") == "predit_vivant"


def test_renumbering_without_changing_identifiers(commission):
    identifiers = {a.id for a in version(DEPOSIT).articles}
    assert commission.version.articles[0].id in identifiers


# ---- exemple 4 : consolidation de la commission contre BTC1003


def test_commission_against_official_text(commission):
    expected, obtained = official(COMMISSION), dict(render(commission.version))
    assert diff_words(obtained["3"], expected["3"]) == []
    # Écarts qu'aucun amendement n'explique : tous viennent de la réécriture par les services.
    assert diff_words(obtained["2"], expected["2"]) == [("insert", "", "du I"), ("replace", "administrative »", "prend »,")]
    assert diff_words(obtained["5"], expected["5"]) == [("replace", "incluant", "qui inclut")]
    assert diff_words(obtained["6"], expected["6"]) == [("replace", "envisager", "d'envisager")]
    assert diff_words(obtained["4"], expected["4"]) == [("replace", "les", "un délai de"),
                                                     ("replace", "non respect", "non-respect")]


# ---- exemple 5 : séance


def test_substitution_at_end(seance):
    line = dict(render(seance.version))["PREMIER"][2]
    assert line == ("« Art. L. 250‑10. – Lorsque les agents habilités constatent un manquement ou une infraction au "
                     "présent titre, ils peuvent enjoindre à l’intéressé de se conformer à ses obligations dans un "
                     "délai qu’ils fixent. » ;")


def test_identicals_applied_once(seance):
    assert etat(seance, COMMISSION, "6") == "applique"
    assert etat(seance, COMMISSION, "9") == "identique"
    assert dict(render(seance.version))["PREMIER"][9].endswith("en application du II de l’article L. 201‑4. »")


def test_identicals_same_fingerprint():
    assert identical_effect(prepare(COMMISSION, "6"), prepare(COMMISSION, "9"), version(COMMISSION))
    assert not identical_effect(prepare(COMMISSION, "6"), prepare(COMMISSION, "8"), version(COMMISSION))


def test_sous_amendements_on_title(seance):
    assert lines_of(seance.version.titre) == [TEXTS[ADOPTED]["titre"]]


def test_sous_amendements_commute():
    operations, scope, uid = prepare(COMMISSION, "15")
    a, b = prepare(COMMISSION, "16"), prepare(COMMISSION, "17")
    un = integrate_sous_amendements(operations, scope, uid, [(a[2], a[0]), (b[2], b[0])])
    other = integrate_sous_amendements(operations, scope, uid, [(b[2], b[0]), (a[2], a[0])])
    assert un[0].text == other[0].text


def effect(numero):
    operations, scope, uid = prepare(COMMISSION, numero)
    return apply(operations, scope, version(COMMISSION), version(COMMISSION), uid)[1]


def test_conflict_on_position_anchor():
    # L'amendement 1 écrit à la fin de l'alinéa 10 ; le 6 supprime les mots « à la fin » de ce même alinéa.
    assert conflict(effect("1"), effect("6"))
    assert not commute(prepare(COMMISSION, "1"), prepare(COMMISSION, "6"), version(COMMISSION))


def test_without_conflict_orders_give_same_fingerprint():
    assert not conflict(effect("8"), effect("6"))
    assert commute(prepare(COMMISSION, "8"), prepare(COMMISSION, "6"), version(COMMISSION))
    assert not conflict(effect("8"), effect("5"))
    assert commute(prepare(COMMISSION, "8"), prepare(COMMISSION, "5"), version(COMMISSION))


def test_title_provenance(seance):
    pieces = provenance(seance.version.titre.alineas[0])
    authors = {text: origin for origin, text in pieces}
    assert authors["instaurant des réponses"] == amendement(COMMISSION, "16").uid
    assert authors["notamment"] == amendement(COMMISSION, "17").uid
    assert pieces[1][0] == amendement(COMMISSION, "15").uid


def test_provenance_of_substitution(seance):
    article = seance.version.articles[0]
    authors = provenance(article.alineas[2])
    assert (amendement(COMMISSION, "8").uid, "de se conformer à ses obligations dans un délai qu’ils fixent") in authors


# ---- exemple 6 : consolidation de la séance contre le T.A. n° 62


def test_seance_against_adopted_text(seance):
    obtained = dict(render(seance.version))
    assert diff_words(obtained["PREMIER"], official(ADOPTED)["PREMIER"]) == [("insert", "", "un"),
                                                                         ("replace", "prise", "adressée")]
    for numero in TEXTS[ADOPTED]["supprimes"]:
        assert obtained[numero] == []


def test_deleted_articles_keep_their_identifier(seance):
    assert [a.id for a in seance.version.articles] == [a.id for a in version(COMMISSION).articles]


def test_rejected_after_deleted_article_not_fallen(seance):
    # 3 (Rect) ajoutait un article après l'article 6, supprimé juste avant : rejeté, pas tombé.
    assert etat(seance, COMMISSION, "3 (Rect)") == "predit_vivant"
    assert normalize("a  b") == "a b"
