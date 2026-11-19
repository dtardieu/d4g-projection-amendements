"""Tests de l'adaptateur Tricoteuses et du serveur, sans réseau (get remplacé par monkeypatch)."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import requests

from server import app
from datasources import tricoteuses

DOCUMENT = {
    "uid": "PIONTEST",
    "titrePrincipal": "Proposition de loi de test",
    "subdivisions": [
        {"type": "Article", "hash": "h2", "titre": "Article 2 (Supprimé)", "sequence": 2},
        {"type": "Exposé des motifs", "hash": "hx", "titre": "EXPOSÉ DES MOTIFS", "sequence": 0},
        {"type": "Article", "hash": "h1", "titre": "Article 1er (nouveau)", "sequence": 1},
    ],
    "alineas": [
        {"articleRefHash": "h1", "numero": 2, "texte": "Le second alinéa."},
        {"articleRefHash": "h1", "numero": 0, "texte": "(Non modifié)"},
        {"articleRefHash": "h1", "numero": 1, "texte": "Le premier alinéa."},
        {"articleRefHash": "hx", "numero": 0, "texte": "Mesdames, Messieurs,"},
    ],
}
AMENDMENT = {
    "uid": "AM1",
    "numeroLong": "CE1",
    "documentRefUid": "PIONTEST",
    "divisionArticleDesignationCourte": "ART. PREMIER",
    "alineaDesignation": "Alinéa 2",
    "dispositifHtml": "<p>À l’alinéa 2, substituer au mot :</p><p>« second »</p>"
    "<p>le mot :</p><p>« dernier ».</p>",
    "amendementParentRefUid": None,
    "triAmendement": "a",
    "dateSort": "2025-02-19T14:21:18.000Z",
    "sortAmendement": "Adopté",
    "idDiscussionIdentique": None,
    "signatairesLibelle": "Mme Barth&#232;s",
}
DELETION = dict(
    AMENDMENT,
    uid="AM3",
    numeroLong="CE3",
    dispositifHtml="<p>Supprimer l’alinéa 1.</p>",
    dateSort="2025-02-19T15:00:00.000Z",
)


class Response:
    def __init__(self, donnees, pages=1):
        self.donnees, self.headers = donnees, {"total-page": str(pages)}

    def json(self):
        return {"data": self.donnees}


@pytest.fixture
def api(monkeypatch):
    """Remplace l'API : le document de test, et ses amendements sur deux pages."""
    calls = []

    def get(chemin, params=None):
        calls.append((chemin, params))
        if chemin == "/documents/PIONTEST":
            assert params == {"include": "subdivisions,alineas"}
            return Response(DOCUMENT)
        if chemin.startswith("/documents/"):
            raise tricoteuses.TextNotFound(chemin)
        if chemin == "/amendements/json":
            removed = dict(AMENDMENT, uid="AM2", sortAmendement="Retiré")
            page = [AMENDMENT, DELETION] if params["page"] == 1 else [removed]
            return Response(page, pages=2)
        raise AssertionError(chemin)

    monkeypatch.setattr(tricoteuses, "get", get)
    app.projection.cache_clear()
    return calls


def test_numbers_from_title():
    assert tricoteuses.numbers_from_title("Article 1er") == ["PREMIER"]
    assert tricoteuses.numbers_from_title("Article unique") == ["UNIQUE"]
    assert tricoteuses.numbers_from_title("Article 5 bis (nouveau)") == ["5 BIS"]
    assert tricoteuses.numbers_from_title("Articles 12") == ["12"]
    assert tricoteuses.numbers_from_title("Articles 2 à 4 (Supprimés)") == ["2", "3", "4"]
    assert tricoteuses.numbers_from_title("Articles 8 ter et 8 quater") == ["8 TER", "8 QUATER"]


def test_version_drops_mentions_and_keeps_order(api):
    titre, version = tricoteuses.text("PIONTEST")
    assert titre == "Proposition de loi de test"
    assert [a.numero for a in version.articles] == ["PREMIER", "2"]
    assert [a.supprime for a in version.articles] == [False, True]
    first = version.articles[0]
    texts = ["".join(j.text for j in alinea.tokens) for alinea in first.paragraphs]
    assert texts == ["Le premier alinéa.", "Le second alinéa."]


def test_amendment_from():
    amendment = tricoteuses.amendment_from(dict(AMENDMENT, idDiscussionIdentique=42))
    assert amendment.article_vise == "ART. PREMIER"
    assert amendment.dispositif.startswith("<p>")
    assert amendment.date_sort == "2025-02-19T14:21:18"
    assert amendment.id_identique == "42"
    assert amendment.auteur == "Mme Barthès"


def test_amendments_reads_all_pages(api):
    amendments = tricoteuses.text_amendments("PIONTEST")
    assert [a.uid for a in amendments] == ["AM1", "AM3", "AM2"]
    assert [p["page"] for c, p in api if c == "/amendements/json"] == [1, 2]


def test_deposit_order_number():
    assert tricoteuses.amendment_from(dict(AMENDMENT, numeroOrdreDepot="393")).numero_ordre == 393
    assert tricoteuses.amendment_from(dict(AMENDMENT, numeroOrdreDepot=393)).numero_ordre == 393
    assert tricoteuses.amendment_from(AMENDMENT).numero_ordre is None


def test_articles_from_document():
    articles = tricoteuses.articles_from_document(DOCUMENT)
    assert articles == (
        tricoteuses.Article("PREMIER", ("Le premier alinéa.", "Le second alinéa.")),
        tricoteuses.Article("2", (), supprime=True),
    )


def test_read_text(api):
    text = tricoteuses.read_text("PIONTEST")
    assert (text.uid, text.titre) == ("PIONTEST", "Proposition de loi de test")
    assert [a.numero for a in text.articles] == ["PREMIER", "2"]


def test_flattened_table_stays_one_paragraph():
    tableau = {"articleRefHash": "h1", "numero": 3, "texte": "«\tPopulation\tTaux\t\t\n\tMoins de 500\t28,1\t\t\n"}
    articles = tricoteuses.articles_from_document(dict(DOCUMENT, alineas=DOCUMENT["alineas"] + [tableau]))
    assert articles[0].lignes[-1] == "« Population Taux Moins de 500 28,1"


def test_first_reading_texts(monkeypatch):
    acts = {
        "AN1-DEPOT": [{"dossierRefUid": "DL1", "texteAssocieRefUid": "PIONB1"}],
        "AN1-COM-FOND-RAPPORT": [{"dossierRefUid": "DL1", "texteAdopteRefUid": "PIONBTC1"},
                                 {"dossierRefUid": "DL2", "texteAdopteRefUid": None}],
        # La décision en séance peut viser un TAP : seul le BTA est le texte adopté.
        "AN1-DEBATS-DEC": [{"dossierRefUid": "DL1", "texteAssocieRefUid": "PIONTAP1"},
                           {"dossierRefUid": "DL1", "texteAssocieRefUid": "PIONBTA1"}],
    }

    def get(chemin, params=None):
        assert chemin == "/actesLegislatifs/json" and params["legislature"] == 17
        return Response(acts[params["codeActe"]])

    monkeypatch.setattr(tricoteuses, "get", get)
    assert tricoteuses.first_reading_texts(17) == [
        tricoteuses.FirstReading("DL1", depose="PIONB1", commission="PIONBTC1", adopte="PIONBTA1")]


def readable(alinea):
    """Un alinéa en texte : [-supprimé-] et {+inséré+}, suivis de l'amendement."""
    marks = {"eq": "{}", "del": "[-{}-]", "ins": "{{+{}+}}"}
    return "".join(
        marks[s["type"]].format(s["texte"]) + (f"@{s['amendement']}" if s["amendement"] else "")
        for s in alinea["segments"]
    )


def test_projection_attributed_segments(api):
    result = app.projection("PIONTEST")
    assert result["titre"] == "Proposition de loi de test"
    first, second = result["articles"]
    assert first["etat"] == "modifie"
    assert first["depart"] == ["Le premier alinéa.", "Le second alinéa."]
    assert [a["numero"] for a in first["alineas"]] == [None, 1]
    assert [readable(a) for a in first["alineas"]] == [
        "[-Le premier alinéa.-]@AM3",
        "Le [-second-]@AM1{+dernier+}@AM1 alinéa.",
    ]
    assert set(first["amendements"]) == {"AM1", "AM2", "AM3"}
    assert second == {
        "numero": "2",
        "numero_depart": "2",
        "etat": "inchange",
        "depart": [],
        "alineas": [],
        "amendements": [],
    }
    etats = {uid: s["etat"] for uid, s in result["amendements"].items()}
    assert etats == {"AM1": "applique", "AM3": "applique", "AM2": "ignore"}
    assert result["amendements"]["AM1"]["auteur"] == "Mme Barthès"
    assert result["amendements"]["AM3"]["dispositif"] == ["Supprimer l’alinéa 1."]


@pytest.fixture
def server(api):
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def lire(url):
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_route_projection(server):
    status, body = lire(f"{server}/textes/PIONTEST/projection")
    assert status == 200
    assert body["resume"] == {"applique": 2, "ignore": 1}


def test_interface_and_static_files(server):
    with urllib.request.urlopen(f"{server}/") as response:
        assert response.headers["Content-Type"].startswith("text/html")
        assert b"Texte projet" in response.read()
    with urllib.request.urlopen(f"{server}/static/assets/chevron.svg") as response:
        assert response.headers["Content-Type"] == "image/svg+xml"
    assert lire(f"{server}/static/../app.py")[0] == 404
    assert lire(f"{server}/static/absent.css")[0] == 404


def test_text_not_found_and_unknown_route(server):
    assert lire(f"{server}/textes/INCONNU/projection")[0] == 404
    assert lire(f"{server}/autre")[0] == 404


def test_tricoteuses_unreachable(server, monkeypatch):
    def outage(chemin, params=None):
        raise requests.ConnectionError("hors ligne")

    monkeypatch.setattr(tricoteuses, "get", outage)
    assert lire(f"{server}/textes/PIONTEST/projection")[0] == 502
