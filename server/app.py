"""Serveur HTTP de projection : rejoue les amendements d'un texte, lus chez Tricoteuses, et
renvoie le texte consolidé.

  GET /textes/{uid}/projection
    → JSON : par article, texte de départ et texte projeté en segments ; statut des amendements
  GET /  → interface (static/index.html)

Lancement (depuis data-science/projection-amendements) :
  uv run python -m server.app [--port 8000]   puis ouvrir http://127.0.0.1:8000/
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
from engine.analysis import clean_dispositif
from engine.consolidation import consolidate

from server import view
from datasources import tricoteuses

ROUTE = re.compile(r"^/textes/([A-Za-z0-9_-]+)/projection/?$")
STATIC = Path(__file__).parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".svg": "image/svg+xml"}


@lru_cache(maxsize=32)
def projection(uid):
    """Projection d'un texte, gardée en mémoire : un texte long demande plusieurs secondes."""
    titre, initial = tricoteuses.text(uid)
    amendements = tricoteuses.text_amendements(uid)
    result = consolidate(initial, amendements)
    statuses = result.statuses
    return {
        "texte": uid,
        "titre": titre,
        "source": tricoteuses.URL,
        "resume": dict(Counter(s.etat for s in statuses.values())),
        "articles": view.articles(initial, result.version, result.effects, amendements, statuses),
        "amendements": {
            a.uid: asdict(statuses[a.uid])
            | {
                "article_vise": a.article_vise,
                "auteur": a.auteur,
                "dispositif": clean_dispositif(a.dispositif or ""),
            }
            for a in amendements
            if a.uid in statuses
        },
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self.file(STATIC / "index.html")
        if path.startswith("/static/"):
            return self.file(STATIC / path.removeprefix("/static/"))
        route = ROUTE.match(path)
        if not route:
            return self.respond(404, {"erreur": "route inconnue : GET /textes/{uid}/projection"})
        try:
            return self.respond(200, projection(route[1]))
        except tricoteuses.TextNotFound as error:
            return self.respond(404, {"erreur": f"texte introuvable : {error}"})
        except requests.RequestException as error:
            return self.respond(502, {"erreur": f"Tricoteuses injoignable : {error}"})

    def file(self, path):
        """Un fichier de static/, et rien en dehors (pas de « ../ »)."""
        path = path.resolve()
        if not path.is_relative_to(STATIC.resolve()) or not path.is_file():
            return self.respond(404, {"erreur": "fichier introuvable"})
        type_ = TYPES.get(path.suffix, "application/octet-stream")
        return self.send(200, path.read_bytes(), type_)

    def respond(self, status, body):
        content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return self.send(status, content, "application/json; charset=utf-8")

    def send(self, status, content, type_):
        self.send_response(status)
        self.send_header("Content-Type", type_)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Interface : http://127.0.0.1:{args.port}/  API : /textes/{{uid}}/projection")
    server.serve_forever()


if __name__ == "__main__":
    main()
