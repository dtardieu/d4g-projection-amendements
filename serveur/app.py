"""Serveur HTTP de projection : rejoue les amendements d'un texte, lus chez Tricoteuses, et
renvoie le texte consolidé.

  GET /textes/{uid}/projection
    → JSON : par article, texte de départ et texte projeté en segments ; statut des amendements
  GET /  → interface (static/index.html)

Lancement (depuis data-science/projection-amendements) :
  uv run python -m serveur.app [--port 8000]   puis ouvrir http://127.0.0.1:8000/
Exemple : curl localhost:8000/textes/PIONANR5L17B0822/projection
"""

import argparse
import json
import re
from collections import Counter
from dataclasses import asdict
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
from moteur.analyse import nettoyer_dispositif
from moteur.consolidation import consolider

from serveur import tricoteuses, vue

ROUTE = re.compile(r"^/textes/([A-Za-z0-9_-]+)/projection/?$")
STATIQUE = Path(__file__).parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".svg": "image/svg+xml"}


@lru_cache(maxsize=32)
def projection(uid):
    """Projection d'un texte, gardée en mémoire : un texte long demande plusieurs secondes."""
    titre, depart = tricoteuses.texte(uid)
    amendements = tricoteuses.amendements_du_texte(uid)
    resultat = consolider(depart, amendements)
    statuts = resultat.statuts
    return {
        "texte": uid,
        "titre": titre,
        "source": tricoteuses.URL,
        "resume": dict(Counter(s.etat for s in statuts.values())),
        "articles": vue.articles(depart, resultat.version, resultat.effets, amendements, statuts),
        "amendements": {
            a.uid: asdict(statuts[a.uid])
            | {
                "article_vise": a.article_vise,
                "auteur": a.auteur,
                "dispositif": nettoyer_dispositif(a.dispositif or ""),
            }
            for a in amendements
            if a.uid in statuts
        },
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        chemin = self.path.split("?")[0]
        if chemin == "/":
            return self.fichier(STATIQUE / "index.html")
        if chemin.startswith("/static/"):
            return self.fichier(STATIQUE / chemin.removeprefix("/static/"))
        route = ROUTE.match(chemin)
        if not route:
            return self.repondre(404, {"erreur": "route inconnue : GET /textes/{uid}/projection"})
        try:
            return self.repondre(200, projection(route[1]))
        except tricoteuses.TexteIntrouvable as erreur:
            return self.repondre(404, {"erreur": f"texte introuvable : {erreur}"})
        except requests.RequestException as erreur:
            return self.repondre(502, {"erreur": f"Tricoteuses injoignable : {erreur}"})

    def fichier(self, chemin):
        """Un fichier de static/, et rien en dehors (pas de « ../ »)."""
        chemin = chemin.resolve()
        if not chemin.is_relative_to(STATIQUE.resolve()) or not chemin.is_file():
            return self.repondre(404, {"erreur": "fichier introuvable"})
        type_ = TYPES.get(chemin.suffix, "application/octet-stream")
        return self.envoyer(200, chemin.read_bytes(), type_)

    def repondre(self, statut, corps):
        contenu = json.dumps(corps, ensure_ascii=False).encode("utf-8")
        return self.envoyer(statut, contenu, "application/json; charset=utf-8")

    def envoyer(self, statut, contenu, type_):
        self.send_response(statut)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(contenu)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(contenu)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    serveur = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Interface : http://127.0.0.1:{args.port}/  API : /textes/{{uid}}/projection")
    serveur.serve_forever()


if __name__ == "__main__":
    main()
