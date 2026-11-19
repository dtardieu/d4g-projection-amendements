"""L'évaluation elle-même : lecture des textes de l'AN, appariement des articles, comparaison, sur la PPL 822."""

import json
from collections import Counter
import unicodedata
from pathlib import Path

import pytest

from evals.engine.corpus import split_de
from evals.engine.run import execute
from evals.engine.score import (Tally, match_articles, article_category, changes, classify_gaps, official_words,
                                without_mentions)
from evals.engine.run import input_version
from evals.engine.amendement_review import compute, wilson
from evals.engine.judge import DecisionsJudge, same_effect_probability, rule
from evals.engine.judge_pilot import phrase_bounds, rate_by_threshold
from evals.engine.score import step_failures
from evals.engine.viewer.app import detailed_analysis, amendement_effect, pieces, segments_diff, segments_ecarts
from engine.text import number_key

FIXTURES = Path(__file__).parent / "fixtures"
AMENDEMENTS = json.loads((FIXTURES / "ppl822_amendements.json").read_text(encoding="utf-8"))
TEXTS = json.loads((FIXTURES / "ppl822_textes.json").read_text(encoding="utf-8"))
COMMISSION, ADOPTED = "PIONANR5L17BTC1003", "PIONANR5L17BTA0062"


def test_ppl_822_neither_dev_nor_test():
    assert split_de("DLR5L17N51328") == "exclu"


def test_split_stable_and_close_to_70_30():
    assert split_de("DLR5L17N51328") == split_de("DLR5L17N51328")
    dev = sum(split_de(f"D{i}") == "dev" for i in range(2000))
    assert 1300 < dev < 1500


def test_official_text_with_decomposed_accents():
    assert official_words([unicodedata.normalize("NFD", "Est créée.")]) == ["Est", "créée", "."]


def test_service_mentions_ignored():
    assert without_mentions(official_words(["1° (nouveau) Le code", "(Non modifié) Suite"])) == \
        official_words(["1° Le code", "Suite"])
    assert "Supprimé" in without_mentions(official_words(["3° (Supprimé)"]))


def test_match_by_number_then_by_content():
    predicted_items = [{"id": "a", "numero_depart": "UNIQUE", "mots": ["x"], "amendement": [0]},
               {"id": "b", "numero_depart": None, "mots": ["rapport", "au", "Parlement"], "amendement": [1, 1, 1]},
               {"id": "c", "numero_depart": None, "mots": ["sans", "rapport", "aucun"], "amendement": [1, 1, 1]}]
    official_ones = [{"numero": "PREMIER", "lignes": ["y"]}, {"numero": "1 BIS", "lignes": ["rapport au Parlement"]}]
    pairs, missing = match_articles(predicted_items, official_ones)
    assert pairs["a"]["numero"] == "PREMIER" and pairs["b"]["numero"] == "1 BIS"
    assert "c" not in pairs and missing == []


def test_the_metrics():
    counter = Tally()
    counter.add(["a", "b"], ["a", "c"], ["a", "d"])  # b retiré trouvé ; c ajouté à tort, d manqué
    counter.add(["a"], ["a", "É", ","], ["a", "e"])  # mêmes lettres : exact seulement pour la mesure indulgente
    counter.add(["a"], ["a"], ["a"])                 # inchangé : ne compte pas
    counter.add(["x"], ["x", "y"], ["x"])            # modifié à tort : abîmé, hors du dénominateur des exacts
    summarize = counter.summarize()
    assert (summarize["touches"], summarize["officiels"], summarize["abimes"]) == (3, 2, 1)
    assert (summarize["exacts_%"], summarize["exacts_indulgents_%"]) == (0.0, 50.0)
    assert (summarize["articles_precision_%"], summarize["articles_rappel_%"]) == (66.7, 100.0)
    assert (summarize["f1_ajouts_%"], summarize["f1_retraits_%"]) == (0.0, 100.0)
    assert (summarize["precision_ajouts_%"], summarize["rappel_ajouts_%"]) == (0.0, 0.0)
    assert (summarize["precision_retraits_%"], summarize["rappel_retraits_%"]) == (100.0, 100.0)
    assert changes(["a"], ["a"]) == {}


def labels(predicted, flags, official):
    return [e[0] for e in classify_gaps(predicted.split(), flags, official.split())]


def test_gap_far_from_an_amendement():
    assert labels("a b c d e", [0, 0, 0, 0, 1], "z b c d e") == ["hors amendement"]


def test_touch_up_in_amendement_passage():
    assert labels("a b c d e f", [0, 1, 1, 1, 1, 1], "a b c z e f") == ["retouche"]


def test_amendement_passage_missing_from_official_text():
    assert labels("a x y z b", [0, 1, 1, 1, 0], "a p q r b") == ["effet absent"]


def test_large_gap_in_long_passage():
    # Le passage reste presque entier, mais l'écart est trop gros pour être une retouche.
    predicted = " ".join(f"m{i}" for i in range(40))
    official = " ".join(f"m{i}" for i in range(30)) + " " + " ".join(f"n{i}" for i in range(10))
    assert labels(predicted, [1] * 40, official) == ["retouche"]
    official = " ".join(f"m{i}" for i in range(25)) + " " + " ".join(f"n{i}" for i in range(15))
    assert labels(predicted, [1] * 40, official) == ["effet absent"]


def test_typography_mention_and_quotes():
    assert labels("a , b", [0, 0, 0], "a b") == ["typographie"]
    assert labels("sureté b", [1, 1], "sûreté b") == ["typographie"]
    assert labels("a b", [0, 0], "a ( Supprimé ) b") == ["mention"]
    assert labels("a b", [1, 1], "« a b") == ["guillemets ou alinéas"]


def test_gaps_found_on_review():
    # Accent décomposé et ligature : mêmes lettres.
    assert labels("communaute \u0301 s de", [1, 1, 1, 0], "communautés de") == ["typographie"]
    assert labels("mise en oeuvre", [0, 0, 1], "mise en œuvre") == ["typographie"]
    # Un seul mot presque identique dans un passage court.
    assert labels("ainsi rédigé :", [1, 1, 1], "ainsi rédigés :") == ["retouche"]
    # Petit ajout officiel près d'un amendement : retouche. Gros : le moteur a peut-être tronqué le passage.
    assert labels("a b c", [0, 1, 0], "a b la propriété de c") == ["retouche"]
    assert labels("a b c", [0, 1, 0], "a b " + " ".join(f"n{i}" for i in range(12)) + " c") == ["effet absent"]


def test_article_category_takes_most_severe():
    assert article_category([("typographie",), ("effet absent",), ("retouche",)]) == "effet absent"
    assert article_category([("typographie",), ("hors amendement",)]) == "typographie"
    assert article_category([]) == "exact"


def test_known_gaps_of_ppl_822():
    # Séance, article 1er : « un 3° » et « adressée » ne touchent aucun mot d'amendement.
    step = ppl_step()
    result = execute(step, "V1-gram")
    article = next(a for a in result["articles"] if a["numero"] == "PREMIER")
    predicted, flags = without_mentions(article["mots"], article["amendement"])
    official = without_mentions(official_words(step["sortie"]["articles"][0]["lignes"]))
    assert [e[0] for e in classify_gaps(predicted, flags, official)] == ["hors amendement", "hors amendement"]


def ppl_step():
    def article(numero, content):
        return {"numero": numero, "lignes": content.split("\n"), "supprime": False}
    output = [article(n, c) for n, c in TEXTS[ADOPTED]["articles"]]
    output += [{"numero": n, "lignes": [], "supprime": True} for n in TEXTS[ADOPTED]["supprimes"]]
    return {"id": "ppl822:seance", "entree": {"articles": [article(n, c) for n, c in TEXTS[COMMISSION]["articles"]]},
            "sortie": {"articles": output}, "amendements": [dict(a) for a in AMENDEMENTS if a["texte_ref"] == COMMISSION]}


@pytest.mark.parametrize("name, exact_ones", [("V1-gram", {"2", "3", "4", "5", "6"}), ("V0-noop", set())])
def test_run_on_seance(name, exact_ones):
    step = ppl_step()
    result = execute(step, name)
    pairs, missing = match_articles(result["articles"], step["sortie"]["articles"])
    assert missing == []
    obtained_items = {p["numero"] for p in result["articles"]
               if without_mentions(p["mots"]) == without_mentions(official_words(pairs[p["id"]]["lignes"]))}
    assert obtained_items == exact_ones


def test_title_amendements_excluded_from_run():
    result = execute(ppl_step(), "V1-gram")
    numbers = {s["numero"] for s in result["statuts"].values()}
    assert "15" not in numbers and "6" in numbers
    assert number_key("PREMIER") == "1"


# ---- visionneuse


def test_viewer_reassembles_text():
    assert "".join(pieces("Le code , ( modifié ) de l ' article L . 5 ¶ Suite".split())) == \
        "Le code, (modifié) de l'article L. 5\nSuite"


def test_viewer_diffs_and_marks_amendement_words():
    segments = segments_diff(["a", "b"], ["a", "c", "d"], [0, 1, 1])
    assert [(s["t"], s["s"]) for s in segments] == [("=", "a"), ("-", " b"), ("+", " c d")]
    assert segments_diff(["a", "b"], ["a", "b"], [0, 1])[1] == {"t": "=", "s": " b", "amdt": True}


def test_viewer_keeps_gap_labels():
    predicted, official = ["a", "b", "c"], ["a", "x", "c"]
    segments = segments_ecarts(predicted, official, [0, 0, 0], [("hors amendement", 1, 2, 1, 2)])
    assert [(s["t"], s["cat"]) for s in segments] == [("=", None), ("-", "hors amendement"), ("+", "hors amendement"), ("=", None)]


def commission_step():
    def article(numero, content):
        return {"numero": numero, "lignes": content.split("\n"), "supprime": False}
    return {"id": "ppl822:commission",
            "entree": {"articles": [article(n, c) for n, c in TEXTS["PIONANR5L17B0822"]["articles"]]},
            "sortie": {"articles": [article(n, c) for n, c in TEXTS[COMMISSION]["articles"]]},
            "amendements": [{"numero_ordre": None, **a} for a in AMENDEMENTS if a["texte_ref"] == "PIONANR5L17B0822"]}


def amendement_of(step, numero):
    return next(a["uid"] for a in step["amendements"] if a["numero"] == numero)


def article_view(step, numero):
    lignes = next(a["lignes"] for a in step["entree"]["articles"] if a["numero"] == numero)
    official = next(a["lignes"] for a in step["sortie"]["articles"] if a["numero"] == ("PREMIER" if numero == "UNIQUE" else numero))
    return {"entree": without_mentions(official_words(lignes)), "officiel": without_mentions(official_words(official)), "uids": []}


def segment_text(segments, *types):
    return "".join(s["s"] for s in segments if s["t"] in types)


def test_effect_of_single_amendement():
    step = ppl_step()
    x = amendement_effect(step, amendement_of(step, "8"), article_view(step, "PREMIER"), {})
    assert x["erreur"] is None and x["sort"] == "Adopté"
    assert "impartissant" in segment_text(x["effet"], "-")
    assert "dans un délai" in segment_text(x["effet"], "+")
    assert "Article" not in segment_text(x["effet"], "+", "-")


def test_effect_of_fallen_amendement_on_input_text():
    # CE10 est tombé, mais son texte s'applique au texte d'entrée : on voit ce qu'il aurait donné.
    step = commission_step()
    x = amendement_effect(step, amendement_of(step, "CE10"), article_view(step, "UNIQUE"), {})
    assert x["erreur"] is None and x["sort"] == "Tombé"
    assert "puni" in segment_text(x["effet"], "-") and "passible" in segment_text(x["effet"], "+")


def test_sous_amendement_shown_through_parent():
    step = commission_step()
    x = amendement_effect(step, amendement_of(step, "CE12"), article_view(step, "UNIQUE"), {})
    assert x["parent"] == "CE11" and x["erreur"] is None
    assert "au préalable" in segment_text(x["effet"], "+", "=")


def test_parent_includes_its_adopted_sous_amendements():
    step = commission_step()
    x = amendement_effect(step, amendement_of(step, "CE11"), article_view(step, "UNIQUE"), {})
    assert "au préalable" in segment_text(x["effet"], "+", "=")


def test_amendement_that_does_not_apply_reports_the_error():
    step = ppl_step()
    uid = amendement_of(step, "6")
    for a in step["amendements"]:
        if a["uid"] == uid:
            a["dispositif"] = "À l’alinéa 10, supprimer les mots : « absents du texte »."
    x = amendement_effect(step, uid, article_view(step, "PREMIER"), {})
    assert "mots absents" in x["erreur"] and x["effet"] == []


def test_article_additionnel_alone():
    step = commission_step()
    x = amendement_effect(step, amendement_of(step, "CE2"), article_view(step, "UNIQUE"), {})
    assert "Le Gouvernement remet chaque année" in segment_text(x["effet"], "+")


def analysis_of(step, numero):
    raw = next(a for a in step["amendements"] if a["numero"] == numero)
    return detailed_analysis(raw, input_version(step))


def test_analysis_shows_grammar_and_what_it_found():
    (instruction,) = analysis_of(ppl_step(), "8")
    assert instruction["source"] == "grammaire"
    (op,) = instruction["operations"]
    assert (op["verbe"], op["alinea"], op["position"]) == ("substitute", 3, "fin")
    assert op["trouve"]["lieu"] == "alinéa 3" and op["trouve"]["extrait"].startswith(", en lui impartissant")


def test_analysis_one_instruction_per_number():
    instructions = analysis_of(commission_step(), "CE10")
    assert [i["source"] for i in instructions] == ["grammaire", "grammaire"]
    assert [i["operations"][0]["verbe"] for i in instructions] == ["substitute", "complete"]


def test_analysis_flags_unknown_wording_and_missing_target():
    step = ppl_step()
    raw = next(a for a in step["amendements"] if a["numero"] == "6")
    raw["dispositif"] = "Formule inconnue du légiste."
    assert detailed_analysis(raw, input_version(step))[0]["source"] == "non reconnu"
    raw["dispositif"] = "À l’alinéa 10, supprimer les mots : « absents du texte »."
    assert "mots absents" in detailed_analysis(raw, input_version(step))[0]["operations"][0]["erreur"]


def test_analysis_article_additionnel_and_sous_amendement():
    step = commission_step()
    assert "Nouvel article" in analysis_of(step, "CE2")[0]["operations"][0]["note"]
    assert "amendement parent" in analysis_of(step, "CE12")[0]["operations"][0]["note"]


def test_failures_attributed_to_an_amendement():
    amendements = [{"uid": u, "numero": u, "sort": "Adopté"} for u in ("pendant", "seul", "a", "b", "serie1", "serie2", "juste")]
    statuses = {"pendant": {"etat": "pendant", "detail": "mots absents"}, "seul": {"etat": "applique"}, "a": {"etat": "applique"},
               "b": {"etat": "applique"}, "serie1": {"etat": "applique"}, "serie2": {"etat": "identique"}, "juste": {"etat": "applique"}}
    article = lambda uids, exact=False: {"uids": uids, "exact": exact, "categorie": None if exact else "effet absent"}
    articles = {"1": article(["seul"]), "2": article(["a", "b"]), "3": article(["serie1", "serie2"]), "4": article(["juste"], True)}
    failures = {e["uid"]: e for e in step_failures(amendements, articles, statuses)}
    assert set(failures) == {"pendant", "seul", "serie1"}  # plusieurs votes ou article exact : rien à attribuer
    assert (failures["pendant"]["type"], failures["pendant"]["attribution"], failures["pendant"]["article"]) == ("résolution", "certaine", None)
    assert (failures["seul"]["type"], failures["seul"]["attribution"]) == ("effet absent", "probable")


def test_judge_rules():
    assert rule("Le Maire ,".split(), "le maire".split()) == "forme_seule"
    assert rule("peut demander".split(), "doit demander".split()) == "effet_different"
    assert rule("un délai de 3 mois".split(), "un délai de 6 mois".split()) == "effet_different"
    assert rule("à l' article 2".split(), "au titre".split()) == "effet_different"
    assert rule("chargé de la santé".split(), "en charge de la santé".split()) is None


def test_judge_reads_answers_and_refusals():
    judge = DecisionsJudge()
    judgment = judge._read({"type": "choice", "choice": "meme_effet", "confidence": 0.8, "probabilities": [
        {"value": "forme_seule", "probability": 0.1}, {"value": "meme_effet", "probability": 0.7}, {"value": "effet_different", "probability": 0.2}]})
    assert judgment["choix"] == "meme_effet" and round(same_effect_probability(judgment), 3) == 0.8
    assert judge._read({"type": "refusal", "refusal": "non"})["choix"] == "indetermine" and judge.refusals == 1


def test_pilot_thresholds():
    result = rate_by_threshold([(0.95, "meme"), (0.95, "different"), (0.6, "forme"), (0.2, "different"), (0.99, "indetermine")])
    assert rate_by_threshold([(0.95, "renumerotation")])[0.9]["vrais même effet retrouvés"] == 1
    assert result[0.9]["acceptés"] == 2 and result[0.9]["taux de faux %"] == 50.0
    assert result[0.5]["vrais même effet retrouvés"] == 2 and result[0.5]["sur"] == 2


def test_renumbering_at_alinea_start_only():
    official = "la phrase . ¶ 2 ° Le texte".split()
    assert labels("la phrase . ¶ b ) Le texte", [0] * 8, " ".join(official)) == ["renumérotation"]
    assert labels("a ) Le texte", [0] * 4, "1 ° Le texte") == ["renumérotation"]
    assert labels("mentionnées au III de l' article", [0] * 7, "mentionnées au II de l' article") == ["hors amendement"]
    assert labels("¶ Au texte", [0] * 3, "¶ Le texte") == ["hors amendement"]
    assert labels("¶ a ) Le texte", [0] * 5, "¶ b ) Le texte") == ["renumérotation"]
    assert labels("¶ a Le texte", [0] * 4, "¶ b Le texte") == ["hors amendement"]


def test_phrase_bounds():
    words = "Fin . Selon l' article L . 12 , le maire peut agir . Autre".split()
    assert " ".join(words[slice(*phrase_bounds(words, 11, 12))]) == "Selon l' article L . 12 , le maire peut agir ."


def test_product_metric_per_amendement():
    counts = {"adopté": Counter({"affiché": 80, "non analysé": 20}), "non adopté": Counter({"affiché": 150, "cible introuvable": 150})}
    lignes = [("adopté", "juste")] * 3 + [("adopté", "faux")] + [("non adopté", "mineur"), ("non adopté", "faux"), ("non adopté", "indetermine")]
    report = compute(lignes, counts)
    assert report["strates"]["adopté"]["effet juste au total %"] == 60.0
    assert report["strates"]["non adopté"]["effet juste au total %"] == 25.0
    assert report["effet juste, tous amendements %"] == 33.8  # pondéré : 100 adoptés, 300 non adoptés
    assert report["effet affiché mais faux, tous amendements %"] == 23.8
    assert wilson(0, 0) is None and wilson(5, 10)[0] < 50 < wilson(5, 10)[1]


def test_version_comparison(tmp_path):
    from evals.engine.viewer.app import compare_versions

    def write(version, header_line, score=None):
        (tmp_path / version).mkdir()
        (tmp_path / version / "dev.jsonl").write_text(json.dumps(header_line) + "\n", encoding="utf-8")
        if score:
            (tmp_path / version / "score_dev.json").write_text(json.dumps(score), encoding="utf-8")

    write("V1", {"entete": True, "commit": "abc", "date": "2026-11-11T10:00:00"},
           {"consolidation": {"touches": 10, "exacts_%": 50.0}, "tombes": {"f1_%": 70.0}, "etats": {"pendant": 3}})
    write("V0", {"entete": True, "commit": "abc"})          # run d'avant la date, jamais scoré
    (tmp_path / "juge_pilote").mkdir()                       # un dossier sans résultat n'est pas une version
    v0, v1 = compare_versions(tmp_path, current_corpus="sha-v1")
    assert (v0["corpus_ancien"], v1["corpus_ancien"]) == (True, True)   # aucun des deux n'a tourné sur « sha-v1 »
    assert (v1["version"], v1["date"], v1["date_fichier"]) == ("V1", "2026-11-11T10:00:00", False)
    assert (v1["exacts_%"], v1["f1_tombes_%"], v1["pendant"], v1["applique"]) == (50.0, 70.0, 3, None)
    assert v0["date_fichier"] and v0["date"] and v0.get("exacts_%") is None


def test_run_is_dated(tmp_path, monkeypatch):
    from evals.engine import run

    monkeypatch.setattr(run, "RESULTS", tmp_path)
    monkeypatch.setattr(run, "split_steps", lambda split: [])
    monkeypatch.setattr("sys.argv", ["run", "--version", "V0-noop"])
    run.main()
    header_line = json.loads((tmp_path / "V0-noop" / "dev.jsonl").read_text(encoding="utf-8"))
    assert header_line["date"][:4].isdigit() and "T" in header_line["date"]


def _tisseuse_step():
    def amendement(uid, sort="Adopté", **fields):
        return {"uid": uid, "numero": uid, "texte_ref": "T", "article_vise": "ART. PREMIER", "alinea": "Alinéa 2",
                "dispositif": "d", "parent_uid": None, "tri_amendement": uid, "date_sort": None, "sort": sort,
                "id_identique": None, **fields}

    return {"id": "e", "entree": {"articles": [{"numero": "PREMIER", "lignes": ["Un mot ancien."], "supprime": False}]},
            "amendements": [amendement("a", id_identique="s"), amendement("b", id_identique="s"),
                            amendement("c", parent_uid="a"), amendement("d", sort="Rejeté"),
                            amendement("e", article_vise="APRÈS ART. PREMIER"), amendement("f", article_vise="ART. 9")]}


def test_tisseuse_what_goes_to_bridge():
    from evals.engine.run_tisseuse import prepare

    sending, statuses, followers = prepare(_tisseuse_step())
    assert [(a["uid"], a["article"], a["position"], a["alinea"]) for a in sending["amendements"]] == \
        [("a", 0, "on_article", 2), ("e", 0, "after_article", 2)]
    assert followers == {"b": "a"}
    assert {u: s["etat"] for u, s in statuses.items()} == \
        {"a": "applique", "b": "identique", "c": "non_analyse", "d": "ignore", "e": "applique", "f": "pendant"}


def test_tisseuse_bridge_response_in_score_format():
    from evals.engine.run_tisseuse import convert, prepare

    step = _tisseuse_step()
    _, statuses, followers = prepare(step)
    response = {"statuts": {"a": {"etat": "pendant", "detail": "cible introuvable"}, "e": {"etat": "applique", "detail": ""}},
               "articles": [{"depart": 0, "lignes": ["Un mot nouveau."], "supprime": False, "origines": []},
                            {"depart": None, "lignes": ["Article ajouté."], "supprime": False, "origines": ["e"]}]}
    result = convert(step, response, statuses, followers)
    first, added = result["articles"]
    assert (first["numero_depart"], first["mots"], first["amendement"]) == \
        ("PREMIER", ["Un", "mot", "nouveau", "."], [0, 0, 1, 0])
    assert (added["numero_depart"], added["amendement"], added["origines"]) == (None, [1, 1, 1], ["e"])
    # La série dont le premier membre a échoué échoue en entier.
    assert result["statuts"]["b"]["etat"] == "pendant"

