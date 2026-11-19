"""Lot 4 : scores d'une ou plusieurs versions (section 4 du document de conception).

Deux textes sont comparés mot à mot, sans les espaces ni les mentions de service « (nouveau) », « (Non modifié) ».
Les articles touchés sont ceux que le texte officiel, un amendement adopté ou la prédiction modifie : ce sont les seuls
qui disent quelque chose, puisque recopier l'entrée réussit sur tous les autres.

Usage : uv run python -m evals.engine.score --split dev V0-noop V1-gram
"""

import argparse
import difflib
import json
import re
import unicodedata
from collections import Counter, defaultdict

from evals.engine.corpus import MANIFESTE
from evals.engine.run import RESULTATS, etapes_du_split
from engine.analysis import RawAmendment, analyze_scope
from engine.text import number_key, split_tokens, normalize

ETATS_ADOPTES_ECHEC = {"pendant": "résolution", "ambigu": "résolution", "non_analyse": "analyse"}
ETATS_APPLIQUES = {"applique", "identique", "sous_amendement"}
CHAMPS = set(RawAmendment.__dataclass_fields__)


def mots_officiels(lignes):
    words = []
    for rank, line in enumerate(lignes):
        if rank:
            words.append("¶")
        words += [normalize(t) for t in split_tokens(line) if not t.isspace()]
    return words


def sans_mentions(words, flags=None):
    """Retire « (nouveau) » et « (Non modifié) », que les services ajoutent sans qu'aucun amendement ne les écrive."""
    garde, garde_drapeaux, i = [], [], 0
    while i < len(words):
        for mention in (("(", "nouveau", ")"), ("(", "Non", "modifié", ")")):
            if tuple(words[i:i + len(mention)]) == mention:
                i += len(mention)
                break
        else:
            garde.append(words[i])
            if flags is not None:
                garde_drapeaux.append(flags[i])
            i += 1
    return (garde, garde_drapeaux) if flags is not None else garde


def apparier(predicted_items, official_ones):
    """{id prédit : article officiel}. Un article existant se retrouve par son numéro, un additionnel par son contenu."""
    par_numero = {number_key(o["numero"]): o for o in official_ones}
    if "UNIQUE" not in par_numero and "1" in par_numero:
        par_numero["UNIQUE"] = par_numero["1"]
    pairs, libres = {}, {id(o): o for o in official_ones}
    for p in predicted_items:
        o = par_numero.get(number_key(p["numero_depart"] or "\0"))
        if o is not None and id(o) in libres:
            pairs[p["id"]] = o
            del libres[id(o)]
    additionnels = [p for p in predicted_items if p["numero_depart"] is None]
    candidates = []
    for p in additionnels:
        a = sans_mentions(p["mots"])
        for o in libres.values():
            ratio = difflib.SequenceMatcher(None, a, mots_officiels(o["lignes"]), autojunk=False).quick_ratio()
            if ratio >= 0.5:
                candidates.append((ratio, p["id"], id(o), o))
    for _, pid, oid, o in sorted(candidates, key=lambda c: -c[0]):
        if pid not in pairs and oid in libres:
            pairs[pid] = o
            del libres[oid]
    return pairs, list(libres.values())


def changes(before, after):
    """Multiensemble des mots retirés (-) et ajoutés (+) entre deux textes."""
    compte = Counter()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag != "equal":
            compte.update(("-", m) for m in before[i1:i2])
            compte.update(("+", m) for m in after[j1:j2])
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


def sans_accents(word):
    decomposed = unicodedata.normalize("NFD", word.casefold().replace("œ", "oe").replace("æ", "ae"))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _lettres(words):
    """Les lettres d'une suite de mots, sans ponctuation ni espaces : « communaute ́ s » égale « communautés »."""
    return "".join(sans_accents(m) for m in words if not PONCTUATION.match(m) and m != "¶")


def _meme_mot_retouche(retires, ajoutes):
    """Un seul mot de chaque côté, presque le même : « rédigé » et « rédigés »."""
    return (len(retires) == len(ajoutes) == 1
            and difflib.SequenceMatcher(None, sans_accents(retires[0]), sans_accents(ajoutes[0])).ratio() >= 0.7)


def _numero_seul(words, following):
    """Une lettre seule n'est un numéro que suivie de sa marque : « a ) » oui, le verbe « a » non."""
    if len(words) == 1 and words[0].isalpha() and len(words[0]) == 1 and words[0] not in "IVXLCDM":
        return following in APRES_LETTRE
    return bool(words) and all(MARQUE.match(m) for m in words) and any(RANG.match(m) for m in words)


def _renumerotation(predicted, official, i1, i2, j1, j2):
    """Deux numéros différents en tête d'alinéa. Au milieu d'une phrase, le même écart est une référence : on n'y touche pas."""
    return ((i1 == 0 or predicted[i1 - 1] in ("¶", "«"))
            and _numero_seul(predicted[i1:i2], predicted[i2] if i2 < len(predicted) else None)
            and _numero_seul(official[j1:j2], official[j2] if j2 < len(official) else None))


def _passages(flags):
    """Suites de mots écrits par un amendement : {index de mot : (début, fin)}."""
    places, i = {}, 0
    while i < len(flags):
        if flags[i]:
            j = i
            while j < len(flags) and flags[j]:
                j += 1
            for k in range(i, j):
                places[k] = (i, j)
            i = j
        else:
            i += 1
    return places


def classer_ecarts(predicted, flags, official):
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
    if predicted == official:
        return []
    operations = difflib.SequenceMatcher(None, predicted, official, autojunk=False).get_opcodes()
    gardes = [False] * len(predicted)
    for tag, i1, i2, _, _ in operations:
        if tag == "equal":
            gardes[i1:i2] = [True] * (i2 - i1)
    passages = _passages(flags)
    ecarts = []
    for tag, i1, i2, j1, j2 in operations:
        if tag == "equal":
            continue
        retires, ajoutes = predicted[i1:i2], official[j1:j2]
        structure = [m for m in retires + ajoutes if m in ("«", "»", "¶")]
        voisins = {passages[k] for k in range(max(0, i1 - 1), min(len(predicted), i2 + 1)) if k in passages}
        if "Supprimé" in ajoutes or "Supprimés" in ajoutes:
            etiquette = "mention"
        elif _lettres(retires) == _lettres(ajoutes):
            etiquette = "guillemets ou alinéas" if structure else "typographie"
        elif _renumerotation(predicted, official, i1, i2, j1, j2):
            etiquette = "renumérotation"
        elif not voisins:
            etiquette = "hors amendement"
        else:
            beginning, end = max(voisins, key=lambda p: p[1] - p[0])
            longueur = end - beginning
            garde = sum(gardes[beginning:end]) / longueur
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


def adoptes_par_article(step):
    """{clé d'article : [amendements adoptés qui le visent]}. Les articles additionnels sont à part."""
    result = defaultdict(list)
    for a in step["amendements"]:
        if a["sort"] != "Adopté" or a["parent_uid"]:
            continue
        try:
            scope = analyze_scope(a["article_vise"])
        except ValueError:
            continue
        if scope.node == "article":
            result[number_key(scope.article)].append(a)
    return result


def pct(n, d):
    return round(100 * n / d, 1) if d else None


def f1(vp, predicted_items, reels):
    return round(200 * vp / (predicted_items + reels), 1) if predicted_items + reels else None


def mesurer(entree, predicted, official):
    """Comparaison d'un article. Calculée une fois : un article compte dans plusieurs compteurs, et le diff coûte cher."""
    exact, indulgent = predicted == official, _lettres(predicted) == _lettres(official)
    reels, predicted_items = changes(entree, official), changes(entree, predicted)
    if not reels and not predicted_items:
        return {"exact": exact, "indulgent": indulgent, "touche": False}
    mesure = {"exact": exact, "indulgent": indulgent, "touche": True, "officiel": bool(reels), "predit": bool(predicted_items)}
    communs = reels & predicted_items
    for signe in "+-":
        mesure[signe] = tuple(sum(n for (s, _), n in c.items() if s == signe) for c in (communs, predicted_items, reels))
    return mesure


class Compteur:
    """Une ligne du tableau. Tout se mesure sur les articles touchés : les articles inchangés gonfleraient le score.

    Les taux d'exacts se rapportent aux articles que le texte officiel modifie : le même ensemble pour toutes les
    versions d'un corpus. Les articles que le moteur modifie à tort (« abîmés ») sont comptés à part.
    """

    def __init__(self):
        self.touches = self.official_ones = self.predicted_items = self.communs = self.exact_ones = self.indulgents = 0
        self.words = {"+": [0, 0, 0], "-": [0, 0, 0]}  # mots communs, prédits, réels

    def ajouter(self, entree, predicted, official):
        self.ajouter_mesure(mesurer(entree, predicted, official))

    def ajouter_mesure(self, m):
        if not m["touche"]:
            return
        self.touches += 1
        self.official_ones += m["officiel"]
        self.predicted_items += m["predit"]
        self.communs += m["officiel"] and m["predit"]
        self.exact_ones += m["officiel"] and m["exact"]
        self.indulgents += m["officiel"] and m["indulgent"]
        for signe, total in self.words.items():
            for i, n in enumerate(m[signe]):
                total[i] += n

    def summarize(self):
        summarize = {
            "touches": self.touches, "officiels": self.official_ones, "abimes": self.predicted_items - self.communs,
            "exacts_%": pct(self.exact_ones, self.official_ones),
            "exacts_indulgents_%": pct(self.indulgents, self.official_ones),
            "articles_precision_%": pct(self.communs, self.predicted_items),
            "articles_rappel_%": pct(self.communs, self.official_ones),
        }
        for signe, name in (("+", "ajouts"), ("-", "retraits")):
            communs, predicted_items, reels = self.words[signe]
            summarize |= {f"f1_{name}_%": f1(communs, predicted_items, reels), f"precision_{name}_%": pct(communs, predicted_items),
                       f"rappel_{name}_%": pct(communs, reels)}
        return summarize


def strate_longueur(n):
    return "1-4 alinéas" if n < 5 else "5-19 alinéas" if n < 20 else "20+ alinéas"


def diagnostiquer_article(p, o, predicted, flags, official, statuses, adopted, autre_ordre=None):
    """(amendements qui visent l'article, écarts étiquetés, catégorie). Catégorie None si l'article est exact."""
    vises = adopted.get(number_key(p["numero_depart"] or "\0"), [])
    if p["numero_depart"] is None:
        vises = [{"uid": u} for u in p["origines"]]
    etats = [statuses.get(a["uid"], {}).get("etat") for a in vises]
    motifs = [ETATS_ADOPTES_ECHEC.get(e, "non appliqué") for e in etats if e not in ETATS_APPLIQUES]
    ecarts = classer_ecarts(predicted, flags, official) if o is not None else []
    if predicted == official:
        return vises, ecarts, None
    if o is None:
        categorie = "article en trop"
    elif motifs:
        categorie = "analyse" if "analyse" in motifs else "résolution" if "résolution" in motifs else "non appliqué"
    elif _exact_dans_l_autre_ordre(autre_ordre, p, official):
        categorie = "ordre"
    else:
        categorie = categorie_article(ecarts)
    return vises, ecarts, categorie


def echecs_etape(amendments, articles, statuses):
    """Les échecs qu'on peut attribuer à un amendement précis. articles : {id : {uids, exact, categorie}}.

    Certain : le moteur n'a pas pu analyser ou appliquer cet amendement adopté.
    Probable : il a été appliqué, il est le seul vote sur l'article, et l'article n'est pas exact. L'écart peut encore
    venir d'une retouche du texte sans amendement : l'étiquette le dit quand on sait le voir.
    """
    article_de = {u: i for i, a in articles.items() for u in a["uids"]}
    failures = []
    for a in amendments:
        etat = statuses.get(a["uid"], {}).get("etat")
        if a["sort"] == "Adopté" and etat in ETATS_ADOPTES_ECHEC:
            failures.append({"uid": a["uid"], "numero": a["numero"], "article": article_de.get(a["uid"]),
                           "type": ETATS_ADOPTES_ECHEC[etat], "attribution": "certaine",
                           "detail": statuses[a["uid"]].get("detail", "")})
    par_uid = {a["uid"]: a for a in amendments}
    for i, article in articles.items():
        votes = [u for u in article["uids"] if statuses.get(u, {}).get("etat") != "identique"]
        if article["exact"] or len(votes) != 1 or votes[0] not in par_uid:
            continue
        if statuses.get(votes[0], {}).get("etat") in ETATS_APPLIQUES:
            failures.append({"uid": votes[0], "numero": par_uid[votes[0]]["numero"], "article": i,
                           "type": article["categorie"], "attribution": "probable", "detail": ""})
    return failures


def lire_resultats(version, split):
    with (RESULTATS / version / f"{split}.jsonl").open(encoding="utf-8") as f:
        header_line = json.loads(f.readline())
        return header_line, {r["id"]: r for r in map(json.loads, f)}


def scorer(version, split, version_ordre=None):
    """version_ordre : la même version avec un autre ordre, pour reconnaître les erreurs d'ordre."""
    header_line, resultats = lire_resultats(version, split)
    actuel = json.loads(MANIFESTE.read_text(encoding="utf-8"))["sha256"]
    if header_line.get("corpus_sha256") != actuel:
        print(f"attention : {version} a tourné sur un autre corpus ; son score compare des textes qui ne vont pas ensemble")
    autre_ordre = lire_resultats(version_ordre, split)[1] if version_ordre else {}
    global_, strates = Compteur(), defaultdict(Compteur)
    bins = Counter()
    ecarts_par_etiquette = Counter()
    etats = Counter()
    tombes = Counter()
    texts = []
    for step in etapes_du_split(split):
        r = resultats.get(step["id"])
        if r is None:
            continue
        keys = "commission" if step["etape"] == "commission" else "seance"
        famille = "finances" if step["finances"] else "hors finances"
        entrees = {number_key(a["numero"]): a for a in step["entree"]["articles"]}
        adopted = adoptes_par_article(step)
        pairs, missing = apparier(r["articles"], step["sortie"]["articles"])
        local = Compteur()
        for p in r["articles"]:
            o = pairs.get(p["id"])
            predicted, flags = sans_mentions(p["mots"], p["amendement"])
            entree_art = entrees.get(number_key(p["numero_depart"] or "\0"))
            entree = sans_mentions(mots_officiels(entree_art["lignes"])) if entree_art else []
            official = sans_mentions(mots_officiels(o["lignes"])) if o is not None else []
            if entree_art is None and o is None and not predicted:
                continue
            mesure = mesurer(entree, predicted, official)
            for c in (global_, local, strates[f"étape : {keys}"], strates[f"famille : {famille}"],
                      strates[f"longueur : {strate_longueur(predicted.count('¶') + 1)}"]):
                c.ajouter_mesure(mesure)
            _, ecarts, categorie = diagnostiquer_article(p, o, predicted, flags, official, r["statuts"], adopted,
                                                             autre_ordre.get(step["id"]))
            ecarts_par_etiquette.update(e[0] for e in ecarts)
            if categorie is not None:
                bins[categorie] += 1
        for o in missing:
            mesure = mesurer([], [], sans_mentions(mots_officiels(o["lignes"])))
            global_.ajouter_mesure(mesure)
            local.ajouter_mesure(mesure)
            bins["article manquant"] += 1
        for a in step["amendements"]:
            s = r["statuts"].get(a["uid"])
            if s is None:
                continue
            etats[s["etat"]] += 1
            if s["etat"] in ("predit_tombe", "predit_vivant"):
                tombes[(s["etat"] == "predit_tombe", s["sort"] == "Tombé")] += 1
        texts.append({"id": step["id"], **local.summarize()})
    vp, fp, fn = tombes[(True, True)], tombes[(True, False)], tombes[(False, True)]
    return {
        "version": version, "split": split, "commit": header_line.get("commit"), "date": header_line.get("date"),
        "corpus": header_line.get("corpus_sha256"),
        "consolidation": global_.summarize(),
        "strates": {k: strates[k].summarize() for k in sorted(strates)},
        "echecs": dict(bins.most_common()),
        "ecarts_par_etiquette": dict(ecarts_par_etiquette.most_common()),
        "etats": dict(etats.most_common()),
        "tombes": {"f1_%": f1(vp, vp + fp, vp + fn), "vp": vp, "fp": fp, "fn": fn},
        "textes": texts,
    }


def _exact_dans_l_autre_ordre(result, article, official):
    if result is None or article["numero_depart"] is None:
        return False
    other = next((a for a in result["articles"] if a["numero_depart"] == article["numero_depart"]), None)
    return other is not None and sans_mentions(other["mots"]) == official


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
        for key in ("echecs", "ecarts_par_etiquette", "etats", "tombes", "strates"):
            print(key, ":", json.dumps(s[key], ensure_ascii=False))


if __name__ == "__main__":
    main()
