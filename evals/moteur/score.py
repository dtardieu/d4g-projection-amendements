"""Lot 4 : scores d'une ou plusieurs versions (section 4 du document de conception).

Deux textes sont comparés mot à mot, sans les espaces ni les mentions de service « (nouveau) », « (Non modifié) ».
Les articles touchés sont ceux que le texte officiel, un amendement adopté ou la prédiction modifie : ce sont les seuls
qui disent quelque chose, puisque recopier l'entrée réussit sur tous les autres.

Usage : uv run python -m evals.moteur.score --split dev V0-noop V1-gram
"""

import argparse
import difflib
import json
import re
import unicodedata
from collections import Counter, defaultdict

from evals.moteur.corpus import MANIFESTE
from evals.moteur.run import RESULTATS, etapes_du_split
from moteur.analyse import AmendementBrut, analyser_portee
from moteur.texte import cle_numero, decouper, normaliser

ETATS_ADOPTES_ECHEC = {"pendant": "résolution", "ambigu": "résolution", "non_analyse": "analyse"}
ETATS_APPLIQUES = {"applique", "identique", "sous_amendement"}
CHAMPS = set(AmendementBrut.__dataclass_fields__)


def mots_officiels(lignes):
    mots = []
    for rang, ligne in enumerate(lignes):
        if rang:
            mots.append("¶")
        mots += [normaliser(t) for t in decouper(ligne) if not t.isspace()]
    return mots


def sans_mentions(mots, drapeaux=None):
    """Retire « (nouveau) » et « (Non modifié) », que les services ajoutent sans qu'aucun amendement ne les écrive."""
    garde, garde_drapeaux, i = [], [], 0
    while i < len(mots):
        for mention in (("(", "nouveau", ")"), ("(", "Non", "modifié", ")")):
            if tuple(mots[i:i + len(mention)]) == mention:
                i += len(mention)
                break
        else:
            garde.append(mots[i])
            if drapeaux is not None:
                garde_drapeaux.append(drapeaux[i])
            i += 1
    return (garde, garde_drapeaux) if drapeaux is not None else garde


def apparier(predits, officiels):
    """{id prédit : article officiel}. Un article existant se retrouve par son numéro, un additionnel par son contenu."""
    par_numero = {cle_numero(o["numero"]): o for o in officiels}
    if "UNIQUE" not in par_numero and "1" in par_numero:
        par_numero["UNIQUE"] = par_numero["1"]
    paires, libres = {}, {id(o): o for o in officiels}
    for p in predits:
        o = par_numero.get(cle_numero(p["numero_depart"] or "\0"))
        if o is not None and id(o) in libres:
            paires[p["id"]] = o
            del libres[id(o)]
    additionnels = [p for p in predits if p["numero_depart"] is None]
    candidats = []
    for p in additionnels:
        a = sans_mentions(p["mots"])
        for o in libres.values():
            ratio = difflib.SequenceMatcher(None, a, mots_officiels(o["lignes"]), autojunk=False).quick_ratio()
            if ratio >= 0.5:
                candidats.append((ratio, p["id"], id(o), o))
    for _, pid, oid, o in sorted(candidats, key=lambda c: -c[0]):
        if pid not in paires and oid in libres:
            paires[pid] = o
            del libres[oid]
    return paires, list(libres.values())


def changements(avant, apres):
    """Multiensemble des mots retirés (-) et ajoutés (+) entre deux textes."""
    compte = Counter()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, avant, apres, autojunk=False).get_opcodes():
        if tag != "equal":
            compte.update(("-", m) for m in avant[i1:i2])
            compte.update(("+", m) for m in apres[j1:j2])
    return compte


PONCTUATION = re.compile(r"^[^\w¶]+$")
# Seuils réglés sur le développement : voir valider_classement.py pour ce qu'ils détectent.
PART_GARDEE_MIN = 0.6     # part d'un passage d'amendement retrouvée dans le texte officiel
PETIT_ECART = 5           # mots
PART_ECART_MAX = 0.25     # taille de l'écart rapportée au passage

MOTEUR = {"effet absent", "guillemets ou alinéas"}
ORDRE_DES_ETIQUETTES = ("effet absent", "guillemets ou alinéas", "retouche", "renumérotation", "typographie", "mention",
                        "hors amendement")
APRES_LETTRE = {")", ".", "-", "–", "‑", "‐", "bis", "ter", "quater", "quinquies", "sexies", "septies", "octies", "nonies", "decies"}
RANG = re.compile(r"^(?:[IVXLCDM]+|\d+|[A-Za-z])$")
MARQUE = re.compile(r"^(?:[IVXLCDM]+|\d+|[A-Za-z]|°|\)|\.|-|–|‑|‐|bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies|Ier|er)$")


def sans_accents(mot):
    decompose = unicodedata.normalize("NFD", mot.casefold().replace("œ", "oe").replace("æ", "ae"))
    return "".join(c for c in decompose if not unicodedata.combining(c))


def _lettres(mots):
    """Les lettres d'une suite de mots, sans ponctuation ni espaces : « communaute ́ s » égale « communautés »."""
    return "".join(sans_accents(m) for m in mots if not PONCTUATION.match(m) and m != "¶")


def _meme_mot_retouche(retires, ajoutes):
    """Un seul mot de chaque côté, presque le même : « rédigé » et « rédigés »."""
    return (len(retires) == len(ajoutes) == 1
            and difflib.SequenceMatcher(None, sans_accents(retires[0]), sans_accents(ajoutes[0])).ratio() >= 0.7)


def _numero_seul(mots, suivant):
    """Une lettre seule n'est un numéro que suivie de sa marque : « a ) » oui, le verbe « a » non."""
    if len(mots) == 1 and mots[0].isalpha() and len(mots[0]) == 1 and mots[0] not in "IVXLCDM":
        return suivant in APRES_LETTRE
    return bool(mots) and all(MARQUE.match(m) for m in mots) and any(RANG.match(m) for m in mots)


def _renumerotation(predit, officiel, i1, i2, j1, j2):
    """Deux numéros différents en tête d'alinéa. Au milieu d'une phrase, le même écart est une référence : on n'y touche pas."""
    return ((i1 == 0 or predit[i1 - 1] in ("¶", "«"))
            and _numero_seul(predit[i1:i2], predit[i2] if i2 < len(predit) else None)
            and _numero_seul(officiel[j1:j2], officiel[j2] if j2 < len(officiel) else None))


def _passages(drapeaux):
    """Suites de mots écrits par un amendement : {index de mot : (début, fin)}."""
    places, i = {}, 0
    while i < len(drapeaux):
        if drapeaux[i]:
            j = i
            while j < len(drapeaux) and drapeaux[j]:
                j += 1
            for k in range(i, j):
                places[k] = (i, j)
            i = j
        else:
            i += 1
    return places


def classer_ecarts(predit, drapeaux, officiel):
    """Étiquette chaque écart entre le texte produit et le texte officiel : [(étiquette, i1, i2, j1, j2)].

    typographie : mêmes mots, à la ponctuation, aux majuscules et aux accents près.
    mention : le texte officiel ajoute « (Supprimé) ».
    guillemets ou alinéas : seuls des guillemets ou des sauts d'alinéa changent. Souvent le moteur, à vérifier.
    renumérotation : un numéro en tête d'alinéa en remplace un autre. Un mauvais numéro du moteur s'y confond.
    hors amendement : l'écart ne touche aucun mot écrit par un amendement.
    retouche : petit écart dans un passage d'amendement, dont le reste se retrouve dans le texte officiel. Le moteur a
    écrit ce que dit l'amendement, le texte officiel en diffère.
    effet absent : des mots écrits par l'amendement manquent au texte officiel. Le moteur s'est peut-être trompé ;
    c'est une borne haute des erreurs du moteur (les services ou un autre amendement peuvent aussi l'expliquer).
    Un ajout du texte officiel près d'un amendement : petit, c'est une retouche ; gros, il peut signaler un passage
    que le moteur a tronqué, donc effet absent (voir valider_classement.py).
    """
    if predit == officiel:
        return []
    operations = difflib.SequenceMatcher(None, predit, officiel, autojunk=False).get_opcodes()
    gardes = [False] * len(predit)
    for tag, i1, i2, _, _ in operations:
        if tag == "equal":
            gardes[i1:i2] = [True] * (i2 - i1)
    passages = _passages(drapeaux)
    ecarts = []
    for tag, i1, i2, j1, j2 in operations:
        if tag == "equal":
            continue
        retires, ajoutes = predit[i1:i2], officiel[j1:j2]
        structure = [m for m in retires + ajoutes if m in ("«", "»", "¶")]
        voisins = {passages[k] for k in range(max(0, i1 - 1), min(len(predit), i2 + 1)) if k in passages}
        if "Supprimé" in ajoutes or "Supprimés" in ajoutes:
            etiquette = "mention"
        elif _lettres(retires) == _lettres(ajoutes):
            etiquette = "guillemets ou alinéas" if structure else "typographie"
        elif _renumerotation(predit, officiel, i1, i2, j1, j2):
            etiquette = "renumérotation"
        elif not voisins:
            etiquette = "hors amendement"
        else:
            debut, fin = max(voisins, key=lambda p: p[1] - p[0])
            longueur = fin - debut
            garde = sum(gardes[debut:fin]) / longueur
            petit = max(len(retires), len(ajoutes)) <= max(PETIT_ECART, PART_ECART_MAX * longueur)
            if not retires:
                etiquette = "retouche" if petit else "effet absent"
            elif _meme_mot_retouche(retires, ajoutes) or (garde >= PART_GARDEE_MIN and petit):
                etiquette = "retouche"
            else:
                etiquette = "effet absent"
        ecarts.append((etiquette, i1, i2, j1, j2))
    return ecarts


def categorie_article(ecarts):
    """L'étiquette la plus grave des écarts d'un article."""
    presentes = {e[0] for e in ecarts}
    return next((e for e in ORDRE_DES_ETIQUETTES if e in presentes), "exact")


def adoptes_par_article(etape):
    """{clé d'article : [amendements adoptés qui le visent]}. Les articles additionnels sont à part."""
    resultat = defaultdict(list)
    for a in etape["amendements"]:
        if a["sort"] != "Adopté" or a["parent_uid"]:
            continue
        try:
            portee = analyser_portee(a["article_vise"])
        except ValueError:
            continue
        if portee.noeud == "article":
            resultat[cle_numero(portee.article)].append(a)
    return resultat


def pct(n, d):
    return round(100 * n / d, 1) if d else None


def f1(vp, predits, reels):
    return round(200 * vp / (predits + reels), 1) if predits + reels else None


def mesurer(entree, predit, officiel):
    """Comparaison d'un article. Calculée une fois : un article compte dans plusieurs compteurs, et le diff coûte cher."""
    exact, indulgent = predit == officiel, _lettres(predit) == _lettres(officiel)
    reels, predits = changements(entree, officiel), changements(entree, predit)
    if not reels and not predits:
        return {"exact": exact, "indulgent": indulgent, "touche": False}
    mesure = {"exact": exact, "indulgent": indulgent, "touche": True, "officiel": bool(reels), "predit": bool(predits)}
    communs = reels & predits
    for signe in "+-":
        mesure[signe] = tuple(sum(n for (s, _), n in c.items() if s == signe) for c in (communs, predits, reels))
    return mesure


class Compteur:
    """Une ligne du tableau. Tout se mesure sur les articles touchés : les articles inchangés gonfleraient le score.

    Les taux d'exacts se rapportent aux articles que le texte officiel modifie : le même ensemble pour toutes les
    versions d'un corpus. Les articles que le moteur modifie à tort (« abîmés ») sont comptés à part.
    """

    def __init__(self):
        self.touches = self.officiels = self.predits = self.communs = self.exacts = self.indulgents = 0
        self.mots = {"+": [0, 0, 0], "-": [0, 0, 0]}  # mots communs, prédits, réels

    def ajouter(self, entree, predit, officiel):
        self.ajouter_mesure(mesurer(entree, predit, officiel))

    def ajouter_mesure(self, m):
        if not m["touche"]:
            return
        self.touches += 1
        self.officiels += m["officiel"]
        self.predits += m["predit"]
        self.communs += m["officiel"] and m["predit"]
        self.exacts += m["officiel"] and m["exact"]
        self.indulgents += m["officiel"] and m["indulgent"]
        for signe, total in self.mots.items():
            for i, n in enumerate(m[signe]):
                total[i] += n

    def resume(self):
        resume = {
            "touches": self.touches, "officiels": self.officiels, "abimes": self.predits - self.communs,
            "exacts_%": pct(self.exacts, self.officiels),
            "exacts_indulgents_%": pct(self.indulgents, self.officiels),
            "articles_precision_%": pct(self.communs, self.predits),
            "articles_rappel_%": pct(self.communs, self.officiels),
        }
        for signe, nom in (("+", "ajouts"), ("-", "retraits")):
            communs, predits, reels = self.mots[signe]
            resume |= {f"f1_{nom}_%": f1(communs, predits, reels), f"precision_{nom}_%": pct(communs, predits),
                       f"rappel_{nom}_%": pct(communs, reels)}
        return resume


def strate_longueur(n):
    return "1-4 alinéas" if n < 5 else "5-19 alinéas" if n < 20 else "20+ alinéas"


def diagnostiquer_article(p, o, predit, drapeaux, officiel, statuts, adoptes, autre_ordre=None):
    """(amendements qui visent l'article, écarts étiquetés, catégorie). Catégorie None si l'article est exact."""
    vises = adoptes.get(cle_numero(p["numero_depart"] or "\0"), [])
    if p["numero_depart"] is None:
        vises = [{"uid": u} for u in p["origines"]]
    etats = [statuts.get(a["uid"], {}).get("etat") for a in vises]
    motifs = [ETATS_ADOPTES_ECHEC.get(e, "non appliqué") for e in etats if e not in ETATS_APPLIQUES]
    ecarts = classer_ecarts(predit, drapeaux, officiel) if o is not None else []
    if predit == officiel:
        return vises, ecarts, None
    if o is None:
        categorie = "article en trop"
    elif motifs:
        categorie = "analyse" if "analyse" in motifs else "résolution" if "résolution" in motifs else "non appliqué"
    elif _exact_dans_l_autre_ordre(autre_ordre, p, officiel):
        categorie = "ordre"
    else:
        categorie = categorie_article(ecarts)
    return vises, ecarts, categorie


def echecs_etape(amendements, articles, statuts):
    """Les échecs qu'on peut attribuer à un amendement précis. articles : {id : {uids, exact, categorie}}.

    Certain : le moteur n'a pas pu analyser ou appliquer cet amendement adopté.
    Probable : il a été appliqué, il est le seul vote sur l'article, et l'article n'est pas exact. L'écart peut encore
    venir d'une retouche du texte sans amendement : l'étiquette le dit quand on sait le voir.
    """
    article_de = {u: i for i, a in articles.items() for u in a["uids"]}
    echecs = []
    for a in amendements:
        etat = statuts.get(a["uid"], {}).get("etat")
        if a["sort"] == "Adopté" and etat in ETATS_ADOPTES_ECHEC:
            echecs.append({"uid": a["uid"], "numero": a["numero"], "article": article_de.get(a["uid"]),
                           "type": ETATS_ADOPTES_ECHEC[etat], "attribution": "certaine",
                           "detail": statuts[a["uid"]].get("detail", "")})
    par_uid = {a["uid"]: a for a in amendements}
    for i, article in articles.items():
        votes = [u for u in article["uids"] if statuts.get(u, {}).get("etat") != "identique"]
        if article["exact"] or len(votes) != 1 or votes[0] not in par_uid:
            continue
        if statuts.get(votes[0], {}).get("etat") in ETATS_APPLIQUES:
            echecs.append({"uid": votes[0], "numero": par_uid[votes[0]]["numero"], "article": i,
                           "type": article["categorie"], "attribution": "probable", "detail": ""})
    return echecs


def lire_resultats(version, split):
    with (RESULTATS / version / f"{split}.jsonl").open(encoding="utf-8") as f:
        entete = json.loads(f.readline())
        return entete, {r["id"]: r for r in map(json.loads, f)}


def scorer(version, split, version_ordre=None):
    """version_ordre : la même version avec un autre ordre, pour reconnaître les erreurs d'ordre."""
    entete, resultats = lire_resultats(version, split)
    actuel = json.loads(MANIFESTE.read_text(encoding="utf-8"))["sha256"]
    if entete.get("corpus_sha256") != actuel:
        print(f"attention : {version} a tourné sur un autre corpus ; son score compare des textes qui ne vont pas ensemble")
    autre_ordre = lire_resultats(version_ordre, split)[1] if version_ordre else {}
    global_, strates = Compteur(), defaultdict(Compteur)
    bins = Counter()
    ecarts_par_etiquette = Counter()
    etats = Counter()
    tombes = Counter()
    textes = []
    for etape in etapes_du_split(split):
        r = resultats.get(etape["id"])
        if r is None:
            continue
        cles = "commission" if etape["etape"] == "commission" else "seance"
        famille = "finances" if etape["finances"] else "hors finances"
        entrees = {cle_numero(a["numero"]): a for a in etape["entree"]["articles"]}
        adoptes = adoptes_par_article(etape)
        paires, manquants = apparier(r["articles"], etape["sortie"]["articles"])
        local = Compteur()
        for p in r["articles"]:
            o = paires.get(p["id"])
            predit, drapeaux = sans_mentions(p["mots"], p["amendement"])
            entree_art = entrees.get(cle_numero(p["numero_depart"] or "\0"))
            entree = sans_mentions(mots_officiels(entree_art["lignes"])) if entree_art else []
            officiel = sans_mentions(mots_officiels(o["lignes"])) if o is not None else []
            if entree_art is None and o is None and not predit:
                continue
            mesure = mesurer(entree, predit, officiel)
            for c in (global_, local, strates[f"étape : {cles}"], strates[f"famille : {famille}"],
                      strates[f"longueur : {strate_longueur(predit.count('¶') + 1)}"]):
                c.ajouter_mesure(mesure)
            _, ecarts, categorie = diagnostiquer_article(p, o, predit, drapeaux, officiel, r["statuts"], adoptes,
                                                             autre_ordre.get(etape["id"]))
            ecarts_par_etiquette.update(e[0] for e in ecarts)
            if categorie is not None:
                bins[categorie] += 1
        for o in manquants:
            mesure = mesurer([], [], sans_mentions(mots_officiels(o["lignes"])))
            global_.ajouter_mesure(mesure)
            local.ajouter_mesure(mesure)
            bins["article manquant"] += 1
        for a in etape["amendements"]:
            s = r["statuts"].get(a["uid"])
            if s is None:
                continue
            etats[s["etat"]] += 1
            if s["etat"] in ("predit_tombe", "predit_vivant"):
                tombes[(s["etat"] == "predit_tombe", s["sort"] == "Tombé")] += 1
        textes.append({"id": etape["id"], **local.resume()})
    vp, fp, fn = tombes[(True, True)], tombes[(True, False)], tombes[(False, True)]
    return {
        "version": version, "split": split, "commit": entete.get("commit"), "date": entete.get("date"),
        "corpus": entete.get("corpus_sha256"),
        "consolidation": global_.resume(),
        "strates": {k: strates[k].resume() for k in sorted(strates)},
        "echecs": dict(bins.most_common()),
        "ecarts_par_etiquette": dict(ecarts_par_etiquette.most_common()),
        "etats": dict(etats.most_common()),
        "tombes": {"f1_%": f1(vp, vp + fp, vp + fn), "vp": vp, "fp": fp, "fn": fn},
        "textes": textes,
    }


def _exact_dans_l_autre_ordre(resultat, article, officiel):
    if resultat is None or article["numero_depart"] is None:
        return False
    autre = next((a for a in resultat["articles"] if a["numero_depart"] == article["numero_depart"]), None)
    return autre is not None and sans_mentions(autre["mots"]) == officiel


def tableau(scores):
    colonnes = ["officiels", "abimes", "exacts_%", "exacts_indulgents_%", "articles_precision_%", "f1_ajouts_%",
                "f1_retraits_%"]
    lignes = ["| version | " + " | ".join(colonnes) + " | f1_tombés_% |", "|---" * (len(colonnes) + 2) + "|"]
    for s in scores:
        c = s["consolidation"]
        lignes.append(f"| {s['version']} | " + " | ".join(str(c[k]) for k in colonnes) + f" | {s['tombes']['f1_%']} |")
    return "\n".join(lignes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("versions", nargs="+")
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    args = parser.parse_args()
    disponibles = {p.name for p in RESULTATS.iterdir()}
    scores = [scorer(v, args.split, "V1-gram-tri" if v == "V1-gram" and "V1-gram-tri" in disponibles else None)
              for v in args.versions]
    for s in scores:
        (RESULTATS / s["version"] / f"score_{args.split}.json").write_text(
            json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    print(tableau(scores))
    for s in scores:
        print(f"\n## {s['version']}")
        for cle in ("echecs", "ecarts_par_etiquette", "etats", "tombes", "strates"):
            print(cle, ":", json.dumps(s[cle], ensure_ascii=False))


if __name__ == "__main__":
    main()
