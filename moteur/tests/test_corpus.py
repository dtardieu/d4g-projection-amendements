"""Construction du corpus depuis Tricoteuses, sans réseau : l'adaptateur est remplacé par monkeypatch."""

import dataclasses
from collections import Counter

import pytest

from evals.moteur import corpus
from evals.moteur.run import amendements_de, version_d_entree
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
    appels = Counter()

    def lire_texte(ref):
        appels[ref] += 1
        if ref == "BTA1":
            raise tricoteuses.TexteIntrouvable(ref)
        return tricoteuses.Texte(ref, f"Texte {ref}", ARTICLES)

    monkeypatch.setattr(tricoteuses, "lire_texte", lire_texte)
    monkeypatch.setattr(tricoteuses, "amendements_du_texte", lambda ref: {
        "B1": [amendement("AM1", "Adopté")], "BTC1": [amendement("AM2", "Adopté")]}.get(ref, []))
    return appels


def test_etapes_du_dossier(api):
    exclus = Counter()
    lecture = tricoteuses.PremiereLecture("DL1", depose="B1", commission="BTC1", adopte="BTA1")
    etapes = corpus.etapes_du_dossier(lecture, exclus)
    assert [e["id"] for e in etapes] == ["DL1:commission"]
    assert exclus == {"texte de sortie introuvable": 1}
    etape = etapes[0]
    assert etape["titre"] == "Texte B1" and etape["sortie"]["ref"] == "BTC1"
    assert "auteur" not in etape["amendements"][0]
    # Le format est celui que run.py lit.
    assert [a.uid for a in amendements_de(etape)] == ["AM1"]
    assert [a.numero for a in version_d_entree(etape).articles] == ["PREMIER"]


def test_acte_manquant_et_cache(api):
    exclus = Counter()
    assert corpus.etapes_du_dossier(tricoteuses.PremiereLecture("DL2", depose="B1"), exclus) == []
    assert exclus == {"acte manquant": 2}
    corpus.texte("B1")
    corpus.texte("B1")
    assert api["B1"] == 1


def test_champs_du_corpus():
    attendus = {f.name for f in dataclasses.fields(tricoteuses.Amendement)} - {"auteur"}
    assert set(corpus.CHAMPS) == attendus

