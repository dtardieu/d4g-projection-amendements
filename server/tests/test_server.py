"""Tests du serveur, sans réseau (get de l'adaptateur Tricoteuses remplacé par monkeypatch)."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import requests

from datasources import tricoteuses
from datasources.tests.test_tricoteuses import AMENDEMENT, DELETION, DOCUMENT, Response
from server import app


@pytest.fixture
def api(monkeypatch):
    """Remplace l'API : le document de test, et ses amendements sur deux pages."""

    def get(path, params=None):
        if path == "/documents/PIONTEST":
            return Response(DOCUMENT)
        if path.startswith("/documents/"):
            raise tricoteuses.TextNotFound(path)
        if path == "/amendements/json":
            removed = dict(AMENDEMENT, uid="AM2", sortAmendement="Retiré")
            return Response([AMENDEMENT, DELETION] if params["page"] == 1 else [removed], pages=2)
        raise AssertionError(path)

    monkeypatch.setattr(tricoteuses, "get", get)
    app.projection.cache_clear()


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
    states = {uid: s["etat"] for uid, s in result["amendements"].items()}
    assert states == {"AM1": "applique", "AM3": "applique", "AM2": "ignore"}
    assert result["amendements"]["AM1"]["auteur"] == "Mme Barthès"
    assert result["amendements"]["AM3"]["dispositif"] == ["Supprimer l’alinéa 1."]


@pytest.fixture
def server(api):
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def read(url):
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_route_projection(server):
    status, body = read(f"{server}/textes/PIONTEST/projection")
    assert status == 200
    assert body["resume"] == {"applique": 2, "ignore": 1}


def test_interface_and_static_files(server):
    with urllib.request.urlopen(f"{server}/") as response:
        assert response.headers["Content-Type"].startswith("text/html")
        assert b"Texte projet" in response.read()
    with urllib.request.urlopen(f"{server}/static/assets/chevron.svg") as response:
        assert response.headers["Content-Type"] == "image/svg+xml"
    assert read(f"{server}/static/../app.py")[0] == 404
    assert read(f"{server}/static/absent.css")[0] == 404


def test_text_not_found_and_unknown_route(server):
    assert read(f"{server}/textes/INCONNU/projection")[0] == 404
    assert read(f"{server}/autre")[0] == 404


def test_tricoteuses_unreachable(server, monkeypatch):
    def outage(path, params=None):
        raise requests.ConnectionError("hors ligne")

    monkeypatch.setattr(tricoteuses, "get", outage)
    assert read(f"{server}/textes/PIONTEST/projection")[0] == 502
