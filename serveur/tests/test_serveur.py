"""Tests de l'adaptateur Tricoteuses et du serveur, sans réseau (get remplacé par monkeypatch)."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import requests

from serveur import app
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
AMENDEMENT = {
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
SUPPRESSION = dict(
    AMENDEMENT,
    uid="AM3",
    numeroLong="CE3",
    dispositifHtml="<p>Supprimer l’alinéa 1.</p>",
    dateSort="2025-02-19T15:00:00.000Z",
)


class Reponse:
    def __init__(self, donnees, pages=1):
        self.donnees, self.headers = donnees, {"total-page": str(pages)}

    def json(self):
        return {"data": self.donnees}


@pytest.fixture
def api(monkeypatch):
    """Remplace l'API : le document de test, et ses amendements sur deux pages."""
    appels = []

    def get(chemin, params=None):
        appels.append((chemin, params))
        if chemin == "/documents/PIONTEST":
            assert params == {"include": "subdivisions,alineas"}
            return Reponse(DOCUMENT)
        if chemin.startswith("/documents/"):
            raise tricoteuses.TexteIntrouvable(chemin)
        if chemin == "/amendements/json":
            retire = dict(AMENDEMENT, uid="AM2", sortAmendement="Retiré")
            page = [AMENDEMENT, SUPPRESSION] if params["page"] == 1 else [retire]
            return Reponse(page, pages=2)
        raise AssertionError(chemin)

    monkeypatch.setattr(tricoteuses, "get", get)
    app.projection.cache_clear()
    return appels


def test_numeros_du_titre():
    assert tricoteuses.numeros_du_titre("Article 1er") == ["PREMIER"]
    assert tricoteuses.numeros_du_titre("Article unique") == ["UNIQUE"]
    assert tricoteuses.numeros_du_titre("Article 5 bis (nouveau)") == ["5 BIS"]
    assert tricoteuses.numeros_du_titre("Articles 12") == ["12"]
    assert tricoteuses.numeros_du_titre("Articles 2 à 4 (Supprimés)") == ["2", "3", "4"]
    assert tricoteuses.numeros_du_titre("Articles 8 ter et 8 quater") == ["8 TER", "8 QUATER"]


def test_version_ecarte_les_mentions_et_suit_l_ordre(api):
    titre, version = tricoteuses.texte("PIONTEST")
    assert titre == "Proposition de loi de test"
    assert [a.numero for a in version.articles] == ["PREMIER", "2"]
    assert [a.supprime for a in version.articles] == [False, True]
    premier = version.articles[0]
    textes = ["".join(j.texte for j in alinea.jetons) for alinea in premier.alineas]
    assert textes == ["Le premier alinéa.", "Le second alinéa."]


def test_amendement_depuis():
    amendement = tricoteuses.amendement_depuis(dict(AMENDEMENT, idDiscussionIdentique=42))
    assert amendement.article_vise == "ART. PREMIER"
    assert amendement.dispositif.startswith("<p>")
    assert amendement.date_sort == "2025-02-19T14:21:18"
    assert amendement.id_identique == "42"
    assert amendement.auteur == "Mme Barthès"


def test_amendements_lit_toutes_les_pages(api):
    amendements = tricoteuses.amendements_du_texte("PIONTEST")
    assert [a.uid for a in amendements] == ["AM1", "AM3", "AM2"]
    assert [p["page"] for c, p in api if c == "/amendements/json"] == [1, 2]


def test_numero_ordre_du_depot():
    assert tricoteuses.amendement_depuis(dict(AMENDEMENT, numeroOrdreDepot="393")).numero_ordre == 393
    assert tricoteuses.amendement_depuis(dict(AMENDEMENT, numeroOrdreDepot=393)).numero_ordre == 393
    assert tricoteuses.amendement_depuis(AMENDEMENT).numero_ordre is None


def test_articles_depuis_document():
    articles = tricoteuses.articles_depuis_document(DOCUMENT)
    assert articles == (
        tricoteuses.Article("PREMIER", ("Le premier alinéa.", "Le second alinéa.")),
        tricoteuses.Article("2", (), supprime=True),
    )


def test_lire_texte(api):
    texte = tricoteuses.lire_texte("PIONTEST")
    assert (texte.uid, texte.titre) == ("PIONTEST", "Proposition de loi de test")
    assert [a.numero for a in texte.articles] == ["PREMIER", "2"]


def test_tableau_aplati_reste_un_alinea():
    tableau = {"articleRefHash": "h1", "numero": 3, "texte": "«\tPopulation\tTaux\t\t\n\tMoins de 500\t28,1\t\t\n"}
    articles = tricoteuses.articles_depuis_document(dict(DOCUMENT, alineas=DOCUMENT["alineas"] + [tableau]))
    assert articles[0].lignes[-1] == "« Population Taux Moins de 500 28,1"


def test_textes_de_premiere_lecture(monkeypatch):
    actes = {
        "AN1-DEPOT": [{"dossierRefUid": "DL1", "texteAssocieRefUid": "PIONB1"}],
        "AN1-COM-FOND-RAPPORT": [{"dossierRefUid": "DL1", "texteAdopteRefUid": "PIONBTC1"},
                                 {"dossierRefUid": "DL2", "texteAdopteRefUid": None}],
        # La décision en séance peut viser un TAP : seul le BTA est le texte adopté.
        "AN1-DEBATS-DEC": [{"dossierRefUid": "DL1", "texteAssocieRefUid": "PIONTAP1"},
                           {"dossierRefUid": "DL1", "texteAssocieRefUid": "PIONBTA1"}],
    }

    def get(chemin, params=None):
        assert chemin == "/actesLegislatifs/json" and params["legislature"] == 17
        return Reponse(actes[params["codeActe"]])

    monkeypatch.setattr(tricoteuses, "get", get)
    assert tricoteuses.textes_de_premiere_lecture(17) == [
        tricoteuses.PremiereLecture("DL1", depose="PIONB1", commission="PIONBTC1", adopte="PIONBTA1")]


def lisible(alinea):
    """Un alinéa en texte : [-supprimé-] et {+inséré+}, suivis de l'amendement."""
    marques = {"eq": "{}", "del": "[-{}-]", "ins": "{{+{}+}}"}
    return "".join(
        marques[s["type"]].format(s["texte"]) + (f"@{s['amendement']}" if s["amendement"] else "")
        for s in alinea["segments"]
    )


def test_projection_segments_attribues(api):
    resultat = app.projection("PIONTEST")
    assert resultat["titre"] == "Proposition de loi de test"
    premier, deuxieme = resultat["articles"]
    assert premier["etat"] == "modifie"
    assert premier["depart"] == ["Le premier alinéa.", "Le second alinéa."]
    assert [a["numero"] for a in premier["alineas"]] == [None, 1]
    assert [lisible(a) for a in premier["alineas"]] == [
        "[-Le premier alinéa.-]@AM3",
        "Le [-second-]@AM1{+dernier+}@AM1 alinéa.",
    ]
    assert set(premier["amendements"]) == {"AM1", "AM2", "AM3"}
    assert deuxieme == {
        "numero": "2",
        "numero_depart": "2",
        "etat": "inchange",
        "depart": [],
        "alineas": [],
        "amendements": [],
    }
    etats = {uid: s["etat"] for uid, s in resultat["amendements"].items()}
    assert etats == {"AM1": "applique", "AM3": "applique", "AM2": "ignore"}
    assert resultat["amendements"]["AM1"]["auteur"] == "Mme Barthès"
    assert resultat["amendements"]["AM3"]["dispositif"] == ["Supprimer l’alinéa 1."]


@pytest.fixture
def serveur(api):
    serveur = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=serveur.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{serveur.server_port}"
    serveur.shutdown()


def lire(url):
    try:
        with urllib.request.urlopen(url) as reponse:
            return reponse.status, json.load(reponse)
    except urllib.error.HTTPError as erreur:
        return erreur.code, json.load(erreur)


def test_route_projection(serveur):
    statut, corps = lire(f"{serveur}/textes/PIONTEST/projection")
    assert statut == 200
    assert corps["resume"] == {"applique": 2, "ignore": 1}


def test_interface_et_fichiers_statiques(serveur):
    with urllib.request.urlopen(f"{serveur}/") as reponse:
        assert reponse.headers["Content-Type"].startswith("text/html")
        assert b"Texte projet" in reponse.read()
    with urllib.request.urlopen(f"{serveur}/static/assets/chevron.svg") as reponse:
        assert reponse.headers["Content-Type"] == "image/svg+xml"
    assert lire(f"{serveur}/static/../app.py")[0] == 404
    assert lire(f"{serveur}/static/absent.css")[0] == 404


def test_texte_introuvable_et_route_inconnue(serveur):
    assert lire(f"{serveur}/textes/INCONNU/projection")[0] == 404
    assert lire(f"{serveur}/autre")[0] == 404


def test_tricoteuses_injoignable(serveur, monkeypatch):
    def panne(chemin, params=None):
        raise requests.ConnectionError("hors ligne")

    monkeypatch.setattr(tricoteuses, "get", panne)
    assert lire(f"{serveur}/textes/PIONTEST/projection")[0] == 502
