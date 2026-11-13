"""Métrique produit : quand on montre l'effet d'un amendement, est-il juste ? Développement seulement.

L'effet d'un amendement seul n'a pas de texte officiel : pour un amendement rejeté, ce texte n'existe pas, et pour un
adopté il mêle d'autres votes et des retouches. On le fait donc juger par quelqu'un, à partir de l'amendement.

lancer : mesure la couverture sur tous les amendements qui ont un dispositif, tire l'échantillon, écrit la page de revue.
bilan : lit le CSV exporté par la page et donne la métrique, avec sa marge d'erreur.

Une série d'amendements identiques compte pour un : c'est un seul texte à montrer.

Usage : uv run python -m evals.moteur.revue_amendements lancer
        uv run python -m evals.moteur.revue_amendements bilan chemin/vers/revue_verdicts.csv
"""

import argparse
import csv
import json
import math
import random
from collections import Counter
from pathlib import Path

from evals.moteur.llm_openai import PlafondDepasse
from evals.moteur.run import RESULTATS, etapes_du_split, version_d_entree
from evals.moteur.score import lire_resultats
from evals.moteur.visu.app import ARTICLE_ABSENT, _operations, effet_amendement
from moteur.application import appliquer
from moteur.resolution import ErreurCible

VERSION = "V2-gram+llm"
DOSSIER = RESULTATS / "revue_amendements"
PAGE = DOSSIER / "revue.html"
CLE = DOSSIER / "cle.jsonl"
COUVERTURE = DOSSIER / "couverture.json"
PAR_STRATE = 75
VERDICTS = {"juste", "mineur", "faux", "indetermine"}
UTILISABLES = {"juste", "mineur"}  # « mineur » : le lecteur comprend le bon effet, malgré un défaut de forme


def strate_de(amendement):
    return "adopté" if amendement["sort"] == "Adopté" else "non adopté"


def issue(etape, amendement, depart, par_uid):
    """Ce que le produit afficherait : « affiché » ou la raison de l'absence. Le LLM ne sert qu'aux réponses déjà payées."""
    try:
        operations, portee, auteur = _operations(etape, amendement, depart, par_uid, True)
        appliquer(operations, portee, depart, depart, auteur)
    except ValueError:
        return "non analysé"
    except ErreurCible:
        return "cible introuvable"
    except PlafondDepasse:
        return "non analysé"
    return "affiché"


def parcourir():
    """Tous les amendements à montrer : hors sous-amendements (vus à travers leur parent), un seul par série identique."""
    for etape in etapes_du_split("dev"):
        depart = version_d_entree(etape)
        par_uid = {a["uid"]: a for a in etape["amendements"]}
        series = set()
        for a in etape["amendements"]:
            if a["parent_uid"] or not (a["dispositif"] or "").strip():
                continue
            if a["id_identique"]:
                if a["id_identique"] in series:
                    continue
                series.add(a["id_identique"])
            yield etape, a, issue(etape, a, depart, par_uid)


def wilson(succes, total, z=1.96):
    if not total:
        return None
    p = succes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    marge = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return round(100 * (centre - marge), 1), round(100 * (centre + marge), 1)


def lancer(_args):
    DOSSIER.mkdir(parents=True, exist_ok=True)
    comptes = {"adopté": Counter(), "non adopté": Counter()}
    affiches = {"adopté": [], "non adopté": []}
    for etape, a, resultat in parcourir():
        strate = strate_de(a)
        comptes[strate][resultat] += 1
        if resultat == "affiché":
            affiches[strate].append((etape["id"], a["uid"]))
    COUVERTURE.write_text(json.dumps(comptes, ensure_ascii=False, indent=1), encoding="utf-8")
    for strate, compte in comptes.items():
        total = sum(compte.values())
        print(f"{strate} : {total} amendements, affichés {100 * compte['affiché'] / total:.1f} %, {dict(compte)}")

    alea = random.Random(822)
    tirage = []
    for strate, liste in affiches.items():
        tirage += [(strate, *x) for x in alea.sample(sorted(liste), PAR_STRATE)]
    alea.shuffle(tirage)
    etapes = {e["id"]: e for e in etapes_du_split("dev")}
    _, resultats = lire_resultats(VERSION, "dev")
    fiches = []
    with CLE.open("w", encoding="utf-8") as f:
        for numero, (strate, etape_id, uid) in enumerate(tirage, 1):
            effet = effet_amendement(etapes[etape_id], uid, ARTICLE_ABSENT, resultats[etape_id]["statuts"], True)
            sources = sorted({i["source"] for i in effet["analyse"]})
            f.write(json.dumps({"numero": numero, "strate": strate, "etape": etape_id, "uid": uid, "amendement": effet["numero"],
                                "sort": effet["sort"], "sources": sources}, ensure_ascii=False) + "\n")
            fiches.append({"numero": numero, "vise": effet["article_vise"] or "", "dispositif": effet["dispositif"] or "",
                           "article": effet["article_produit"] or "", "effet": effet["effet"]})
    donnees = json.dumps(fiches, ensure_ascii=False).replace("</", "<\\/")
    PAGE.write_text(MODELE_PAGE.replace("__FICHES__", donnees), encoding="utf-8")
    print(f"page : {PAGE}")


def calculer(lignes, comptes):
    """lignes : [(strate, verdict)]. La couverture vient de tous les amendements, la justesse de l'échantillon affiché."""
    total = sum(sum(c.values()) for c in comptes.values())
    bilan = {"strates": {}}
    utilisable = serieux = 0.0
    for strate, compte in comptes.items():
        n_strate = sum(compte.values())
        verdicts = [v for s, v in lignes if s == strate and v != "indetermine"]
        bons, faux = sum(v in UTILISABLES for v in verdicts), sum(v == "faux" for v in verdicts)
        couverture = compte["affiché"] / n_strate
        justesse = bons / len(verdicts) if verdicts else 0.0
        bilan["strates"][strate] = {
            "amendements": n_strate, "couverture %": round(100 * couverture, 1), "revus": len(verdicts),
            "justes parmi les affichés %": round(100 * justesse, 1), "marge à 95 %": wilson(bons, len(verdicts)),
            "faux": faux, "effet juste au total %": round(100 * couverture * justesse, 1)}
        utilisable += n_strate / total * couverture * justesse
        serieux += n_strate / total * couverture * (faux / len(verdicts) if verdicts else 0.0)
    bilan["effet juste, tous amendements %"] = round(100 * utilisable, 1)
    bilan["effet affiché mais faux, tous amendements %"] = round(100 * serieux, 1)
    return bilan


def bilan(args):
    cles = {d["numero"]: d for d in map(json.loads, CLE.open(encoding="utf-8"))}
    with Path(args.verdicts).open(encoding="utf-8-sig", newline="") as f:
        verdicts = [(int(l["numero"]), l["verdict"].strip()) for l in csv.DictReader(f) if l["verdict"].strip()]
    inconnus = {v for _, v in verdicts} - VERDICTS
    if inconnus:
        raise SystemExit(f"verdicts inconnus : {inconnus}. Attendus : {sorted(VERDICTS)}")
    print(f"{len(verdicts)} verdicts sur {len(cles)}")
    comptes = {s: Counter(c) for s, c in json.loads(COUVERTURE.read_text(encoding="utf-8")).items()}
    print(json.dumps(calculer([(cles[n]["strate"], v) for n, v in verdicts], comptes), ensure_ascii=False, indent=1))
    print("par sort :", dict(Counter((cles[n]["sort"], v) for n, v in verdicts)))
    print("par source :", dict(Counter(("+".join(cles[n]["sources"]), v) for n, v in verdicts)))


MODELE_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Revue des amendements</title>
<style>
:root { color-scheme: light dark; --fond:#fff; --fond2:#f6f6f6; --trait:#ddd; --texte:#161616; --discret:#666; --action:#000091; --choisi:#e3e3fd; --plus:#b8f0c8; --moins:#fbc9c9; }
@media (prefers-color-scheme: dark) { :root { --fond:#1c1c1e; --fond2:#27272a; --trait:#3f3f46; --texte:#eee; --discret:#aaa; --action:#a9a9ff; --choisi:#33335e; --plus:#1d5a33; --moins:#6b2424; } }
* { box-sizing: border-box; }
body { margin: 0 auto; max-width: 860px; padding: 16px; background: var(--fond); color: var(--texte); font: 15px/1.5 system-ui, sans-serif; }
h1 { font-size: 17px; margin: 0; } h2 { font-size: 14px; margin: 18px 0 6px; }
.note { color: var(--discret); font-size: 13px; }
.barre { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 12px 0; }
button { font: inherit; color: inherit; background: var(--fond); border: 1px solid var(--trait); border-radius: 4px; padding: 6px 10px; cursor: pointer; }
button[aria-pressed=true] { background: var(--choisi); border-color: var(--action); }
.texte { white-space: pre-wrap; overflow-wrap: anywhere; border: 1px solid var(--trait); border-radius: 4px; padding: 10px; font-family: Georgia, serif; line-height: 1.6; }
.dispositif { background: var(--fond2); border-left: 3px solid var(--action); }
ins { background: var(--plus); text-decoration: none; } del { background: var(--moins); }
.replie { border: 0; background: none; padding: 0; color: var(--action); font: 12px system-ui, sans-serif; }
.verdicts button { text-align: left; flex: 1 1 180px; } .verdicts b { display: block; }
</style></head><body>
<h1>Revue des amendements</h1>
<p class="note">Lisez l'amendement, puis l'effet affiché. Jugez si l'effet affiché correspond à ce que dit l'amendement, sans chercher s'il a été adopté. Vos réponses restent dans ce navigateur : exportez le CSV à la fin.</p>
<div class="barre"><button id="prec">← Précédent</button><button id="suiv">Suivant →</button><button id="manque">Prochain sans verdict</button><span id="etat" class="note"></span><button id="export">Exporter le CSV</button></div>
<div id="fiche"></div>
<script type="application/json" id="fiches">__FICHES__</script>
<script>
const FICHES = JSON.parse(document.getElementById("fiches").textContent);
const CLE = "revue_amendements_v1", CONTEXTE = 30;
const CHOIX = [["juste", "Juste", "L'effet affiché est celui de l'amendement."], ["mineur", "Défaut mineur", "Le bon effet se comprend : typographie, guillemets, saut d'alinéa."], ["faux", "Faux", "Mauvais endroit, effet manquant ou en trop : le lecteur serait trompé."], ["indetermine", "Indéterminé", "Impossible de juger avec ce qui est montré."]];
let verdicts = {}, rang = 0;
try { verdicts = JSON.parse(localStorage.getItem(CLE) || "{}"); } catch (e) {}
const garder = () => { try { localStorage.setItem(CLE, JSON.stringify(verdicts)); } catch (e) {} };
const el = (tag, attr = {}, ...enfants) => { const n = document.createElement(tag); for (const [k, v] of Object.entries(attr)) { if (k === "text") n.textContent = v; else if (k === "onclick") n.onclick = v; else n.setAttribute(k, v); } n.append(...enfants); return n; };
function repliable(texte, debut, fin) {
  const mots = texte.split(/(?<=\\s)/), limite = (debut ? 0 : CONTEXTE) + (fin ? 0 : CONTEXTE);
  if (mots.length <= limite + 20) return [document.createTextNode(texte)];
  const bouton = el("button", {class: "replie", text: `[… ${mots.length - limite} mots inchangés …]`});
  const milieu = mots.slice(debut ? 0 : CONTEXTE, fin ? mots.length : -CONTEXTE).join("");
  bouton.onclick = () => bouton.replaceWith(document.createTextNode(milieu));
  return [document.createTextNode(debut ? "" : mots.slice(0, CONTEXTE).join("")), bouton, document.createTextNode(fin ? "" : mots.slice(-CONTEXTE).join(""))];
}
function rendreEffet(segments) {
  const boite = el("div", {class: "texte"});
  if (!segments.some(s => s.t !== "=")) boite.append(el("p", {class: "note", text: "Aucun changement affiché."}));
  segments.forEach((s, i) => { if (s.t === "=") boite.append(...repliable(s.s, i === 0, i === segments.length - 1)); else boite.append(el(s.t === "+" ? "ins" : "del", {text: s.s})); });
  return boite;
}
function afficher() {
  const f = FICHES[rang], faits = FICHES.filter(x => verdicts[x.numero]).length;
  document.getElementById("etat").textContent = `Fiche ${rang + 1} sur ${FICHES.length} · ${faits} verdicts`;
  document.getElementById("fiche").replaceChildren(
    el("h2", {text: `Amendement visant : ${f.vise || "non précisé"}`}),
    el("div", {class: "texte dispositif", text: f.dispositif}),
    el("h2", {text: `Effet affiché${f.article ? ` sur l'article ${f.article}` : ""}`}),
    rendreEffet(f.effet),
    el("h2", {text: "Verdict"}),
    el("div", {class: "barre verdicts"}, ...CHOIX.map(([v, titre, aide]) => el("button", {"aria-pressed": verdicts[f.numero] === v, onclick: () => { verdicts[f.numero] = v; garder(); if (rang < FICHES.length - 1) rang++; afficher(); window.scrollTo(0, 0); }}, el("b", {text: titre}), el("span", {class: "note", text: aide})))));
}
document.getElementById("prec").onclick = () => { rang = Math.max(0, rang - 1); afficher(); };
document.getElementById("suiv").onclick = () => { rang = Math.min(FICHES.length - 1, rang + 1); afficher(); };
document.getElementById("manque").onclick = () => { const i = FICHES.findIndex(x => !verdicts[x.numero]); if (i >= 0) { rang = i; afficher(); } };
document.getElementById("export").onclick = () => {
  const csv = "numero,verdict\\n" + FICHES.map(f => `${f.numero},${verdicts[f.numero] || ""}`).join("\\n") + "\\n";
  const lien = el("a", {href: URL.createObjectURL(new Blob([csv], {type: "text/csv"})), download: "revue_verdicts.csv"});
  document.body.append(lien); lien.click(); lien.remove();
};
afficher();
</script></body></html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sous = parser.add_subparsers(dest="commande", required=True)
    sous.add_parser("lancer").set_defaults(fonction=lancer)
    sortie = sous.add_parser("bilan")
    sortie.add_argument("verdicts")
    sortie.set_defaults(fonction=bilan)
    args = parser.parse_args()
    args.fonction(args)


if __name__ == "__main__":
    main()
