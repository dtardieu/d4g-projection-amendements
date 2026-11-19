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

from evals.engine.corpus import MANIFEST
from evals.engine.run import RESULTS, split_steps
from engine.analysis import RawAmendement, analyze_scope
from engine.text import number_key, split_tokens, normalize

FAILED_ADOPTED_STATES = {"pendant": "résolution", "ambigu": "résolution", "non_analyse": "analyse"}
APPLIED_STATES = {"applique", "identique", "sous_amendement"}
FIELDS = set(RawAmendement.__dataclass_fields__)


def official_words(lignes):
    words = []
    for rank, line in enumerate(lignes):
        if rank:
            words.append("¶")
        words += [normalize(t) for t in split_tokens(line) if not t.isspace()]
    return words


def without_mentions(words, flags=None):
    """Retire « (nouveau) » et « (Non modifié) », que les services ajoutent sans qu'aucun amendement ne les écrive."""
    kept_words, kept_flags, i = [], [], 0
    while i < len(words):
        for mention in (("(", "nouveau", ")"), ("(", "Non", "modifié", ")")):
            if tuple(words[i:i + len(mention)]) == mention:
                i += len(mention)
                break
        else:
            kept_words.append(words[i])
            if flags is not None:
                kept_flags.append(flags[i])
            i += 1
    return (kept_words, kept_flags) if flags is not None else kept_words


def match_articles(predicted_items, official_ones):
    """{id prédit : article officiel}. Un article existant se retrouve par son numéro, un additionnel par son contenu."""
    by_number = {number_key(o["numero"]): o for o in official_ones}
    if "UNIQUE" not in by_number and "1" in by_number:
        by_number["UNIQUE"] = by_number["1"]
    pairs, free = {}, {id(o): o for o in official_ones}
    for p in predicted_items:
        o = by_number.get(number_key(p["numero_depart"] or "\0"))
        if o is not None and id(o) in free:
            pairs[p["id"]] = o
            del free[id(o)]
    additionnels = [p for p in predicted_items if p["numero_depart"] is None]
    candidates = []
    for p in additionnels:
        a = without_mentions(p["mots"])
        for o in free.values():
            ratio = difflib.SequenceMatcher(None, a, official_words(o["lignes"]), autojunk=False).quick_ratio()
            if ratio >= 0.5:
                candidates.append((ratio, p["id"], id(o), o))
    for _, pid, oid, o in sorted(candidates, key=lambda c: -c[0]):
        if pid not in pairs and oid in free:
            pairs[pid] = o
            del free[oid]
    return pairs, list(free.values())


def changes(before, after):
    """Multiensemble des mots retirés (-) et ajoutés (+) entre deux textes."""
    counted = Counter()
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
        if tag != "equal":
            counted.update(("-", m) for m in before[i1:i2])
            counted.update(("+", m) for m in after[j1:j2])
    return counted


PUNCTUATION = re.compile(r"^[^\w¶]+$")
# Seuils réglés sur le développement : voir validate_classification.py pour ce qu'ils détectent.
MIN_KEPT_SHARE = 0.6     # part d'un passage d'amendement retrouvée dans le texte officiel
SMALL_GAP = 5           # mots
MAX_GAP_SHARE = 0.25     # taille de l'écart rapportée au passage

ENGINE = {"effet absent", "guillemets ou alinéas"}
LABEL_ORDER = ("effet absent", "guillemets ou alinéas", "retouche", "renumérotation", "typographie", "mention",
                        "hors amendement")
AFTER_LETTER = {")", ".", "-", "–", "‑", "‐", "bis", "ter", "quater", "quinquies", "sexies", "septies", "octies", "nonies", "decies"}
RANK = re.compile(r"^(?:[IVXLCDM]+|\d+|[A-Za-z])$")
MARK = re.compile(r"^(?:[IVXLCDM]+|\d+|[A-Za-z]|°|\)|\.|-|–|‑|‐|bis|ter|quater|quinquies|sexies|septies|octies|nonies|decies|Ier|er)$")


def without_accents(word):
    decomposed = unicodedata.normalize("NFD", word.casefold().replace("œ", "oe").replace("æ", "ae"))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _letters(words):
    """Les lettres d'une suite de mots, sans ponctuation ni espaces : « communaute ́ s » égale « communautés »."""
    return "".join(without_accents(m) for m in words if not PUNCTUATION.match(m) and m != "¶")


def _same_word_touched_up(removed_words, added_words):
    """Un seul mot de chaque côté, presque le même : « rédigé » et « rédigés »."""
    return (len(removed_words) == len(added_words) == 1
            and difflib.SequenceMatcher(None, without_accents(removed_words[0]), without_accents(added_words[0])).ratio() >= 0.7)


def _lone_number(words, following):
    """Une lettre seule n'est un numéro que suivie de sa marque : « a ) » oui, le verbe « a » non."""
    if len(words) == 1 and words[0].isalpha() and len(words[0]) == 1 and words[0] not in "IVXLCDM":
        return following in AFTER_LETTER
    return bool(words) and all(MARK.match(m) for m in words) and any(RANK.match(m) for m in words)


def _renumbering(predicted, official, i1, i2, j1, j2):
    """Deux numéros différents en tête d'alinéa. Au milieu d'une phrase, le même écart est une référence : on n'y touche pas."""
    return ((i1 == 0 or predicted[i1 - 1] in ("¶", "«"))
            and _lone_number(predicted[i1:i2], predicted[i2] if i2 < len(predicted) else None)
            and _lone_number(official[j1:j2], official[j2] if j2 < len(official) else None))


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


def classify_gaps(predicted, flags, official):
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
    que le moteur a tronqué, donc effet absent (voir validate_classification.py).
    """
    if predicted == official:
        return []
    operations = difflib.SequenceMatcher(None, predicted, official, autojunk=False).get_opcodes()
    kept = [False] * len(predicted)
    for tag, i1, i2, _, _ in operations:
        if tag == "equal":
            kept[i1:i2] = [True] * (i2 - i1)
    passages = _passages(flags)
    gaps = []
    for tag, i1, i2, j1, j2 in operations:
        if tag == "equal":
            continue
        removed_words, added_words = predicted[i1:i2], official[j1:j2]
        structure = [m for m in removed_words + added_words if m in ("«", "»", "¶")]
        neighbors = {passages[k] for k in range(max(0, i1 - 1), min(len(predicted), i2 + 1)) if k in passages}
        if "Supprimé" in added_words or "Supprimés" in added_words:
            label = "mention"
        elif _letters(removed_words) == _letters(added_words):
            label = "guillemets ou alinéas" if structure else "typographie"
        elif _renumbering(predicted, official, i1, i2, j1, j2):
            label = "renumérotation"
        elif not neighbors:
            label = "hors amendement"
        else:
            beginning, end = max(neighbors, key=lambda p: p[1] - p[0])
            length = end - beginning
            kept_words = sum(kept[beginning:end]) / length
            small = max(len(removed_words), len(added_words)) <= max(SMALL_GAP, MAX_GAP_SHARE * length)
            if not removed_words:
                label = "retouche" if small else "effet absent"
            elif _same_word_touched_up(removed_words, added_words) or (kept_words >= MIN_KEPT_SHARE and small):
                label = "retouche"
            else:
                label = "effet absent"
        gaps.append((label, i1, i2, j1, j2))
    return gaps


def article_category(gaps):
    """L'étiquette la plus grave des écarts d'un article."""
    present = {e[0] for e in gaps}
    return next((e for e in LABEL_ORDER if e in present), "exact")


def adopted_by_article(step):
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


def f1(vp, predicted_items, actual_items):
    return round(200 * vp / (predicted_items + actual_items), 1) if predicted_items + actual_items else None


def measure_article(input_, predicted, official):
    """Comparaison d'un article. Calculée une fois : un article compte dans plusieurs compteurs, et le diff coûte cher."""
    exact, lenient = predicted == official, _letters(predicted) == _letters(official)
    actual_items, predicted_items = changes(input_, official), changes(input_, predicted)
    if not actual_items and not predicted_items:
        return {"exact": exact, "indulgent": lenient, "touche": False}
    measure = {"exact": exact, "indulgent": lenient, "touche": True, "officiel": bool(actual_items), "predit": bool(predicted_items)}
    common = actual_items & predicted_items
    for sign in "+-":
        measure[sign] = tuple(sum(n for (s, _), n in c.items() if s == sign) for c in (common, predicted_items, actual_items))
    return measure


class Tally:
    """Une ligne du tableau. Tout se mesure sur les articles touchés : les articles inchangés gonfleraient le score.

    Les taux d'exacts se rapportent aux articles que le texte officiel modifie : le même ensemble pour toutes les
    versions d'un corpus. Les articles que le moteur modifie à tort (« abîmés ») sont comptés à part.
    """

    def __init__(self):
        self.touched = self.official_ones = self.predicted_items = self.common = self.exact_ones = self.lenient_count = 0
        self.words = {"+": [0, 0, 0], "-": [0, 0, 0]}  # mots communs, prédits, réels

    def add(self, input_, predicted, official):
        self.add_measure(measure_article(input_, predicted, official))

    def add_measure(self, m):
        if not m["touche"]:
            return
        self.touched += 1
        self.official_ones += m["officiel"]
        self.predicted_items += m["predit"]
        self.common += m["officiel"] and m["predit"]
        self.exact_ones += m["officiel"] and m["exact"]
        self.lenient_count += m["officiel"] and m["indulgent"]
        for sign, total in self.words.items():
            for i, n in enumerate(m[sign]):
                total[i] += n

    def summarize(self):
        summarize = {
            "touches": self.touched, "officiels": self.official_ones, "abimes": self.predicted_items - self.common,
            "exacts_%": pct(self.exact_ones, self.official_ones),
            "exacts_indulgents_%": pct(self.lenient_count, self.official_ones),
            "articles_precision_%": pct(self.common, self.predicted_items),
            "articles_rappel_%": pct(self.common, self.official_ones),
        }
        for sign, name in (("+", "ajouts"), ("-", "retraits")):
            common, predicted_items, actual_items = self.words[sign]
            summarize |= {f"f1_{name}_%": f1(common, predicted_items, actual_items), f"precision_{name}_%": pct(common, predicted_items),
                       f"rappel_{name}_%": pct(common, actual_items)}
        return summarize


def length_stratum(n):
    return "1-4 alinéas" if n < 5 else "5-19 alinéas" if n < 20 else "20+ alinéas"


def diagnose_article(p, o, predicted, flags, official, statuses, adopted, other_order=None):
    """(amendements qui visent l'article, écarts étiquetés, catégorie). Catégorie None si l'article est exact."""
    targeted_amendements = adopted.get(number_key(p["numero_depart"] or "\0"), [])
    if p["numero_depart"] is None:
        targeted_amendements = [{"uid": u} for u in p["origines"]]
    states = [statuses.get(a["uid"], {}).get("etat") for a in targeted_amendements]
    reasons = [FAILED_ADOPTED_STATES.get(e, "non appliqué") for e in states if e not in APPLIED_STATES]
    gaps = classify_gaps(predicted, flags, official) if o is not None else []
    if predicted == official:
        return targeted_amendements, gaps, None
    if o is None:
        category = "article en trop"
    elif reasons:
        category = "analyse" if "analyse" in reasons else "résolution" if "résolution" in reasons else "non appliqué"
    elif _exact_in_other_order(other_order, p, official):
        category = "ordre"
    else:
        category = article_category(gaps)
    return targeted_amendements, gaps, category


def step_failures(amendements, articles, statuses):
    """Les échecs qu'on peut attribuer à un amendement précis. articles : {id : {uids, exact, categorie}}.

    Certain : le moteur n'a pas pu analyser ou appliquer cet amendement adopté.
    Probable : il a été appliqué, il est le seul vote sur l'article, et l'article n'est pas exact. L'écart peut encore
    venir d'une retouche du texte sans amendement : l'étiquette le dit quand on sait le voir.
    """
    article_of = {u: i for i, a in articles.items() for u in a["uids"]}
    failures = []
    for a in amendements:
        etat = statuses.get(a["uid"], {}).get("etat")
        if a["sort"] == "Adopté" and etat in FAILED_ADOPTED_STATES:
            failures.append({"uid": a["uid"], "numero": a["numero"], "article": article_of.get(a["uid"]),
                           "type": FAILED_ADOPTED_STATES[etat], "attribution": "certaine",
                           "detail": statuses[a["uid"]].get("detail", "")})
    by_uid = {a["uid"]: a for a in amendements}
    for i, article in articles.items():
        votes = [u for u in article["uids"] if statuses.get(u, {}).get("etat") != "identique"]
        if article["exact"] or len(votes) != 1 or votes[0] not in by_uid:
            continue
        if statuses.get(votes[0], {}).get("etat") in APPLIED_STATES:
            failures.append({"uid": votes[0], "numero": by_uid[votes[0]]["numero"], "article": i,
                           "type": article["categorie"], "attribution": "probable", "detail": ""})
    return failures


def read_results(version, split):
    with (RESULTS / version / f"{split}.jsonl").open(encoding="utf-8") as f:
        header_line = json.loads(f.readline())
        return header_line, {r["id"]: r for r in map(json.loads, f)}


def score_version(version, split, order_version=None):
    """version_ordre : la même version avec un autre ordre, pour reconnaître les erreurs d'ordre."""
    header_line, results = read_results(version, split)
    latest = json.loads(MANIFEST.read_text(encoding="utf-8"))["sha256"]
    if header_line.get("corpus_sha256") != latest:
        print(f"attention : {version} a tourné sur un autre corpus ; son score compare des textes qui ne vont pas ensemble")
    other_order = read_results(order_version, split)[1] if order_version else {}
    global_, strata = Tally(), defaultdict(Tally)
    bins = Counter()
    gaps_by_label = Counter()
    states = Counter()
    tombes = Counter()
    texts = []
    for step in split_steps(split):
        r = results.get(step["id"])
        if r is None:
            continue
        keys = "commission" if step["etape"] == "commission" else "seance"
        family = "finances" if step["finances"] else "hors finances"
        inputs = {number_key(a["numero"]): a for a in step["entree"]["articles"]}
        adopted = adopted_by_article(step)
        pairs, missing = match_articles(r["articles"], step["sortie"]["articles"])
        local = Tally()
        for p in r["articles"]:
            o = pairs.get(p["id"])
            predicted, flags = without_mentions(p["mots"], p["amendement"])
            input_article = inputs.get(number_key(p["numero_depart"] or "\0"))
            input_ = without_mentions(official_words(input_article["lignes"])) if input_article else []
            official = without_mentions(official_words(o["lignes"])) if o is not None else []
            if input_article is None and o is None and not predicted:
                continue
            measure = measure_article(input_, predicted, official)
            for c in (global_, local, strata[f"étape : {keys}"], strata[f"famille : {family}"],
                      strata[f"longueur : {length_stratum(predicted.count('¶') + 1)}"]):
                c.add_measure(measure)
            _, gaps, category = diagnose_article(p, o, predicted, flags, official, r["statuts"], adopted,
                                                             other_order.get(step["id"]))
            gaps_by_label.update(e[0] for e in gaps)
            if category is not None:
                bins[category] += 1
        for o in missing:
            measure = measure_article([], [], without_mentions(official_words(o["lignes"])))
            global_.add_measure(measure)
            local.add_measure(measure)
            bins["article manquant"] += 1
        for a in step["amendements"]:
            s = r["statuts"].get(a["uid"])
            if s is None:
                continue
            states[s["etat"]] += 1
            if s["etat"] in ("predit_tombe", "predit_vivant"):
                tombes[(s["etat"] == "predit_tombe", s["sort"] == "Tombé")] += 1
        texts.append({"id": step["id"], **local.summarize()})
    vp, fp, fn = tombes[(True, True)], tombes[(True, False)], tombes[(False, True)]
    return {
        "version": version, "split": split, "commit": header_line.get("commit"), "date": header_line.get("date"),
        "corpus": header_line.get("corpus_sha256"),
        "consolidation": global_.summarize(),
        "strates": {k: strata[k].summarize() for k in sorted(strata)},
        "echecs": dict(bins.most_common()),
        "ecarts_par_etiquette": dict(gaps_by_label.most_common()),
        "etats": dict(states.most_common()),
        "tombes": {"f1_%": f1(vp, vp + fp, vp + fn), "vp": vp, "fp": fp, "fn": fn},
        "textes": texts,
    }


def _exact_in_other_order(result, article, official):
    if result is None or article["numero_depart"] is None:
        return False
    other = next((a for a in result["articles"] if a["numero_depart"] == article["numero_depart"]), None)
    return other is not None and without_mentions(other["mots"]) == official


def table(scores):
    columns = ["officiels", "abimes", "exacts_%", "exacts_indulgents_%", "articles_precision_%", "f1_ajouts_%",
                "f1_retraits_%"]
    lignes = ["| version | " + " | ".join(columns) + " | f1_tombés_% |", "|---" * (len(columns) + 2) + "|"]
    for s in scores:
        c = s["consolidation"]
        lignes.append(f"| {s['version']} | " + " | ".join(str(c[k]) for k in columns) + f" | {s['tombes']['f1_%']} |")
    return "\n".join(lignes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("versions", nargs="+")
    parser.add_argument("--split", default="dev", choices=["dev", "test"])
    args = parser.parse_args()
    available = {p.name for p in RESULTS.iterdir()}
    scores = [score_version(v, args.split, "V1-gram-tri" if v == "V1-gram" and "V1-gram-tri" in available else None)
              for v in args.versions]
    for s in scores:
        (RESULTS / s["version"] / f"score_{args.split}.json").write_text(
            json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    print(table(scores))
    for s in scores:
        print(f"\n## {s['version']}")
        for key in ("echecs", "ecarts_par_etiquette", "etats", "tombes", "strates"):
            print(key, ":", json.dumps(s[key], ensure_ascii=False))


if __name__ == "__main__":
    main()
