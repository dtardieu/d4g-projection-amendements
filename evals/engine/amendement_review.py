"""Métrique produit : quand on montre l'effet d'un amendement, est-il juste ? Développement seulement.

L'effet d'un amendement seul n'a pas de texte officiel : pour un amendement rejeté, ce texte n'existe pas, et pour un
adopté il mêle d'autres votes et des retouches. On le fait donc juger par quelqu'un, à partir de l'amendement.

lancer : mesure la couverture sur tous les amendements qui ont un dispositif, tire l'échantillon, écrit la page de revue.
bilan : lit le CSV exporté par la page et donne la métrique, avec sa marge d'erreur.

Une série d'amendements identiques compte pour un : c'est un seul texte à montrer.

Usage : uv run python -m evals.engine.amendement_review lancer
        uv run python -m evals.engine.amendement_review bilan chemin/vers/revue_verdicts.csv
"""

import argparse
import csv
import json
import math
import random
from collections import Counter
from pathlib import Path

from evals.engine.llm_openai import BudgetExceeded
from evals.engine.run import RESULTS, split_steps, input_version
from evals.engine.score import read_results
from evals.engine.viewer.app import MISSING_ARTICLE, _operations, amendement_effect
from engine.application import apply
from engine.resolution import TargetError

VERSION = "V2-gram+llm"
DIRECTORY = RESULTS / "revue_amendements"
PAGE = DIRECTORY / "revue.html"
KEY_FILE = DIRECTORY / "cle.jsonl"
COVERAGE = DIRECTORY / "couverture.json"
PER_STRATUM = 75
VERDICTS = {"juste", "mineur", "faux", "indetermine"}
USABLE = {"juste", "mineur"}  # « mineur » : le lecteur comprend le bon effet, malgré un défaut de forme


def stratum_of(amendement):
    return "adopté" if amendement["sort"] == "Adopté" else "non adopté"


def issue(step, amendement, initial, by_uid):
    """Ce que le produit afficherait : « affiché » ou la raison de l'absence. Le LLM ne sert qu'aux réponses déjà payées."""
    try:
        operations, scope, auteur = _operations(step, amendement, initial, by_uid, True)
        apply(operations, scope, initial, initial, auteur)
    except ValueError:
        return "non analysé"
    except TargetError:
        return "cible introuvable"
    except BudgetExceeded:
        return "non analysé"
    return "affiché"


def walk():
    """Tous les amendements à montrer : hors sous-amendements (vus à travers leur parent), un seul par série identique."""
    for step in split_steps("dev"):
        initial = input_version(step)
        by_uid = {a["uid"]: a for a in step["amendements"]}
        series = set()
        for a in step["amendements"]:
            if a["parent_uid"] or not (a["dispositif"] or "").strip():
                continue
            if a["id_identique"]:
                if a["id_identique"] in series:
                    continue
                series.add(a["id_identique"])
            yield step, a, issue(step, a, initial, by_uid)


def wilson(successes, total, z=1.96):
    if not total:
        return None
    p = successes / total
    center = (p + z * z / (2 * total)) / (1 + z * z / total)
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return round(100 * (center - margin), 1), round(100 * (center + margin), 1)


def launch(_args):
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    counts = {"adopté": Counter(), "non adopté": Counter()}
    displayed = {"adopté": [], "non adopté": []}
    for step, a, result in walk():
        stratum = stratum_of(a)
        counts[stratum][result] += 1
        if result == "affiché":
            displayed[stratum].append((step["id"], a["uid"]))
    COVERAGE.write_text(json.dumps(counts, ensure_ascii=False, indent=1), encoding="utf-8")
    for stratum, counted in counts.items():
        total = sum(counted.values())
        print(f"{stratum} : {total} amendements, affichés {100 * counted['affiché'] / total:.1f} %, {dict(counted)}")

    rng = random.Random(822)
    draw = []
    for stratum, items in displayed.items():
        draw += [(stratum, *x) for x in rng.sample(sorted(items), PER_STRATUM)]
    rng.shuffle(draw)
    steps = {e["id"]: e for e in split_steps("dev")}
    _, results = read_results(VERSION, "dev")
    records = []
    with KEY_FILE.open("w", encoding="utf-8") as f:
        for numero, (stratum, step_id, uid) in enumerate(draw, 1):
            effect = amendement_effect(steps[step_id], uid, MISSING_ARTICLE, results[step_id]["statuts"], True)
            sources = sorted({i["source"] for i in effect["analyse"]})
            f.write(json.dumps({"numero": numero, "strate": stratum, "etape": step_id, "uid": uid, "amendement": effect["numero"],
                                "sort": effect["sort"], "sources": sources}, ensure_ascii=False) + "\n")
            records.append({"numero": numero, "vise": effect["article_vise"] or "", "dispositif": effect["dispositif"] or "",
                           "article": effect["article_produit"] or "", "effet": effect["effet"]})
    data = json.dumps(records, ensure_ascii=False).replace("</", "<\\/")
    PAGE.write_text(PAGE_TEMPLATE.replace("__FICHES__", data), encoding="utf-8")
    print(f"page : {PAGE}")


def compute(lignes, counts):
    """lignes : [(strate, verdict)]. La couverture vient de tous les amendements, la justesse de l'échantillon affiché."""
    total = sum(sum(c.values()) for c in counts.values())
    report = {"strates": {}}
    usable = serieux = 0.0
    for stratum, counted in counts.items():
        stratum_size = sum(counted.values())
        verdicts = [v for s, v in lignes if s == stratum and v != "indetermine"]
        correct, wrong = sum(v in USABLE for v in verdicts), sum(v == "faux" for v in verdicts)
        coverage = counted["affiché"] / stratum_size
        accuracy = correct / len(verdicts) if verdicts else 0.0
        report["strates"][stratum] = {
            "amendements": stratum_size, "couverture %": round(100 * coverage, 1), "revus": len(verdicts),
            "justes parmi les affichés %": round(100 * accuracy, 1), "marge à 95 %": wilson(correct, len(verdicts)),
            "faux": wrong, "effet juste au total %": round(100 * coverage * accuracy, 1)}
        usable += stratum_size / total * coverage * accuracy
        serieux += stratum_size / total * coverage * (wrong / len(verdicts) if verdicts else 0.0)
    report["effet juste, tous amendements %"] = round(100 * usable, 1)
    report["effet affiché mais faux, tous amendements %"] = round(100 * serieux, 1)
    return report


def report(args):
    keys = {d["numero"]: d for d in map(json.loads, KEY_FILE.open(encoding="utf-8"))}
    with Path(args.verdicts).open(encoding="utf-8-sig", newline="") as f:
        verdicts = [(int(l["numero"]), l["verdict"].strip()) for l in csv.DictReader(f) if l["verdict"].strip()]
    unknown_ones = {v for _, v in verdicts} - VERDICTS
    if unknown_ones:
        raise SystemExit(f"verdicts inconnus : {unknown_ones}. Attendus : {sorted(VERDICTS)}")
    print(f"{len(verdicts)} verdicts sur {len(keys)}")
    counts = {s: Counter(c) for s, c in json.loads(COVERAGE.read_text(encoding="utf-8")).items()}
    print(json.dumps(compute([(keys[n]["strate"], v) for n, v in verdicts], counts), ensure_ascii=False, indent=1))
    print("par sort :", dict(Counter((keys[n]["sort"], v) for n, v in verdicts)))
    print("par source :", dict(Counter(("+".join(keys[n]["sources"]), v) for n, v in verdicts)))


PAGE_TEMPLATE = """<!doctype html>
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
    sub = parser.add_subparsers(dest="commande", required=True)
    sub.add_parser("lancer").set_defaults(function=launch)
    output = sub.add_parser("bilan")
    output.add_argument("verdicts")
    output.set_defaults(function=report)
    args = parser.parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
