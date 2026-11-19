"""Construction du corpus depuis Tricoteuses, sans réseau : l'adaptateur est remplacé par monkeypatch."""

import dataclasses
from collections import Counter

import pytest

from evals.engine import corpus
from evals.engine.run import amendements_of, input_version
from datasources import tricoteuses

ARTICLES = (tricoteuses.Article("PREMIER", ("Le premier alinéa.",)),)


def amendement(uid, sort):
    return tricoteuses.Amendement(
        uid=uid, numero=uid, texte_ref="B1", article_vise="ART. PREMIER", alinea=None, dispositif="<p>Rédiger.</p>",
        parent_uid=None, tri_amendement="a", date_sort=None, sort=sort, auteur="M. Untel", numero_ordre=1)


@pytest.fixture
def api(monkeypatch, tmp_path):
    """Trois textes : B1 (déposé), BTC1 (commission), BTA1 absent de Tricoteuses. Cache dans un dossier temporaire."""
    monkeypatch.setattr(corpus, "CACHE", tmp_path)
    calls = Counter()

    def read_text(ref):
        calls[ref] += 1
        if ref == "BTA1":
            raise tricoteuses.TextNotFound(ref)
        return tricoteuses.Text(ref, f"Texte {ref}", ARTICLES)

    monkeypatch.setattr(tricoteuses, "read_text", read_text)
    monkeypatch.setattr(tricoteuses, "text_amendements", lambda ref: {
        "B1": [amendement("AM1", "Adopté")], "BTC1": [amendement("AM2", "Adopté")]}.get(ref, []))
    return calls


def test_dossier_steps(api):
    excluded = Counter()
    lecture = tricoteuses.PremiereLecture("DL1", depose="B1", commission="BTC1", adopte="BTA1")
    steps = corpus.dossier_steps(lecture, excluded)
    assert [e["id"] for e in steps] == ["DL1:commission"]
    assert excluded == {"texte de sortie introuvable": 1}
    step = steps[0]
    assert step["titre"] == "Texte B1" and step["sortie"]["ref"] == "BTC1"
    assert "auteur" not in step["amendements"][0]
    # Le format est celui que run.py lit.
    assert [a.uid for a in amendements_of(step)] == ["AM1"]
    assert [a.numero for a in input_version(step).articles] == ["PREMIER"]


def test_missing_act_and_cache(api):
    excluded = Counter()
    assert corpus.dossier_steps(tricoteuses.PremiereLecture("DL2", depose="B1"), excluded) == []
    assert excluded == {"acte manquant": 2}
    corpus.text("B1")
    corpus.text("B1")
    assert api["B1"] == 1


def test_corpus_fields():
    expected_items = {f.name for f in dataclasses.fields(tricoteuses.Amendement)} - {"auteur"}
    assert set(corpus.FIELDS) == expected_items

