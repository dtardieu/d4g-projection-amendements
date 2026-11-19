"""L'évaluation elle-même : lecture des textes de l'AN, appariement des articles, comparaison, sur la PPL 822."""

import json
from collections import Counter
import unicodedata
from pathlib import Path

import pytest

from evals.moteur.corpus import split_de
from evals.moteur.run import executer
from evals.moteur.score import (Compteur, apparier, categorie_article, changements, classer_ecarts, mots_officiels,
                                sans_mentions)
from evals.moteur.run import version_d_entree
from evals.moteur.revue_amendements import calculer, wilson
from evals.moteur.juge import JugeDecisions, probabilite_meme_effet, regle
from evals.moteur.pilote_juge import bornes_phrase, taux_par_seuil
from evals.moteur.score import echecs_etape
from evals.moteur.visu.app import analyse_detaillee, effet_amendement, morceaux, segments_diff, segments_ecarts
from moteur.texte import cle_numero

FIXTURES = Path(__file__).parent / "fixtures"
AMENDEMENTS = json.loads((FIXTURES / "ppl822_amendements.json").read_text(encoding="utf-8"))
TEXTES = json.loads((FIXTURES / "ppl822_textes.json").read_text(encoding="utf-8"))
COMMISSION, ADOPTE = "PIONANR5L17BTC1003", "PIONANR5L17BTA0062"


def test_ppl_822_ni_en_developpement_ni_en_test():
    assert split_de("DLR5L17N51328") == "exclu"


def test_split_stable_et_proche_de_70_30():
    assert split_de("DLR5L17N51328") == split_de("DLR5L17N51328")
    dev = sum(split_de(f"D{i}") == "dev" for i in range(2000))
    assert 1300 < dev < 1500


def test_texte_officiel_aux_accents_decomposes():
    assert mots_officiels([unicodedata.normalize("NFD", "Est créée.")]) == ["Est", "créée", "."]


def test_mentions_de_service_ignorees():
    assert sans_mentions(mots_officiels(["1° (nouveau) Le code", "(Non modifié) Suite"])) == \
        mots_officiels(["1° Le code", "Suite"])
    assert "Supprimé" in sans_mentions(mots_officiels(["3° (Supprimé)"]))


def test_apparier_par_numero_puis_par_contenu():
    predits = [{"id": "a", "numero_depart": "UNIQUE", "mots": ["x"], "amendement": [0]},
               {"id": "b", "numero_depart": None, "mots": ["rapport", "au", "Parlement"], "amendement": [1, 1, 1]},
               {"id": "c", "numero_depart": None, "mots": ["sans", "rapport", "aucun"], "amendement": [1, 1, 1]}]
    officiels = [{"numero": "PREMIER", "lignes": ["y"]}, {"numero": "1 BIS", "lignes": ["rapport au Parlement"]}]
    paires, manquants = apparier(predits, officiels)
    assert paires["a"]["numero"] == "PREMIER" and paires["b"]["numero"] == "1 BIS"
    assert "c" not in paires and manquants == []


def test_les_cinq_metriques():
    compteur = Compteur()
    compteur.ajouter(["a", "b"], ["a", "c"], ["a", "d"])  # b retiré trouvé ; c ajouté à tort, d manqué
    compteur.ajouter(["a"], ["a", "É", ","], ["a", "e"])  # mêmes lettres : exact seulement pour la mesure indulgente
    compteur.ajouter(["a"], ["a"], ["a"])                 # inchangé : ne compte pas
    compteur.ajouter(["x"], ["x", "y"], ["x"])            # modifié à tort : abîmé, hors du dénominateur des exacts
    resume = compteur.resume()
    assert (resume["touches"], resume["officiels"], resume["abimes"]) == (3, 2, 1)
    assert (resume["exacts_%"], resume["exacts_indulgents_%"]) == (0.0, 50.0)
    assert (resume["articles_precision_%"], resume["articles_rappel_%"]) == (66.7, 100.0)
    assert (resume["f1_ajouts_%"], resume["f1_retraits_%"]) == (0.0, 100.0)
    assert (resume["precision_ajouts_%"], resume["rappel_ajouts_%"]) == (0.0, 0.0)
    assert (resume["precision_retraits_%"], resume["rappel_retraits_%"]) == (100.0, 100.0)
    assert changements(["a"], ["a"]) == {}


def etiquettes(predit, drapeaux, officiel):
    return [e[0] for e in classer_ecarts(predit.split(), drapeaux, officiel.split())]


def test_ecart_loin_d_un_amendement():
    assert etiquettes("a b c d e", [0, 0, 0, 0, 1], "z b c d e") == ["hors amendement"]


def test_retouche_dans_le_passage_de_l_amendement():
    assert etiquettes("a b c d e f", [0, 1, 1, 1, 1, 1], "a b c z e f") == ["retouche"]


def test_passage_d_amendement_absent_du_texte_officiel():
    assert etiquettes("a x y z b", [0, 1, 1, 1, 0], "a p q r b") == ["effet absent"]


def test_gros_ecart_dans_un_long_passage():
    # Le passage reste presque entier, mais l'écart est trop gros pour être une retouche.
    predit = " ".join(f"m{i}" for i in range(40))
    officiel = " ".join(f"m{i}" for i in range(30)) + " " + " ".join(f"n{i}" for i in range(10))
    assert etiquettes(predit, [1] * 40, officiel) == ["retouche"]
    officiel = " ".join(f"m{i}" for i in range(25)) + " " + " ".join(f"n{i}" for i in range(15))
    assert etiquettes(predit, [1] * 40, officiel) == ["effet absent"]


def test_typographie_mention_et_guillemets():
    assert etiquettes("a , b", [0, 0, 0], "a b") == ["typographie"]
    assert etiquettes("sureté b", [1, 1], "sûreté b") == ["typographie"]
    assert etiquettes("a b", [0, 0], "a ( Supprimé ) b") == ["mention"]
    assert etiquettes("a b", [1, 1], "« a b") == ["guillemets ou alinéas"]


def test_ecarts_reperes_a_la_relecture():
    # Accent décomposé et ligature : mêmes lettres.
    assert etiquettes("communaute \u0301 s de", [1, 1, 1, 0], "communautés de") == ["typographie"]
    assert etiquettes("mise en oeuvre", [0, 0, 1], "mise en œuvre") == ["typographie"]
    # Un seul mot presque identique dans un passage court.
    assert etiquettes("ainsi rédigé :", [1, 1, 1], "ainsi rédigés :") == ["retouche"]
    # Petit ajout officiel près d'un amendement : retouche. Gros : le moteur a peut-être tronqué le passage.
    assert etiquettes("a b c", [0, 1, 0], "a b la propriété de c") == ["retouche"]
    assert etiquettes("a b c", [0, 1, 0], "a b " + " ".join(f"n{i}" for i in range(12)) + " c") == ["effet absent"]


def test_categorie_d_article_prend_la_plus_grave():
    assert categorie_article([("typographie",), ("effet absent",), ("retouche",)]) == "effet absent"
    assert categorie_article([("typographie",), ("hors amendement",)]) == "typographie"
    assert categorie_article([]) == "exact"


def test_ecarts_connus_de_la_ppl_822():
    # Séance, article 1er : « un 3° » et « adressée » ne touchent aucun mot d'amendement.
    etape = etape_ppl()
    resultat = executer(etape, "V1-gram")
    article = next(a for a in resultat["articles"] if a["numero"] == "PREMIER")
    predit, drapeaux = sans_mentions(article["mots"], article["amendement"])
    officiel = sans_mentions(mots_officiels(etape["sortie"]["articles"][0]["lignes"]))
    assert [e[0] for e in classer_ecarts(predit, drapeaux, officiel)] == ["hors amendement", "hors amendement"]


def etape_ppl():
    def article(numero, contenu):
        return {"numero": numero, "lignes": contenu.split("\n"), "supprime": False}
    sortie = [article(n, c) for n, c in TEXTES[ADOPTE]["articles"]]
    sortie += [{"numero": n, "lignes": [], "supprime": True} for n in TEXTES[ADOPTE]["supprimes"]]
    return {"id": "ppl822:seance", "entree": {"articles": [article(n, c) for n, c in TEXTES[COMMISSION]["articles"]]},
            "sortie": {"articles": sortie}, "amendements": [dict(a) for a in AMENDEMENTS if a["texte_ref"] == COMMISSION]}


@pytest.mark.parametrize("nom, exacts", [("V1-gram", {"2", "3", "4", "5", "6"}), ("V0-noop", set())])
def test_execution_sur_la_seance(nom, exacts):
    etape = etape_ppl()
    resultat = executer(etape, nom)
    paires, manquants = apparier(resultat["articles"], etape["sortie"]["articles"])
    assert manquants == []
    obtenus = {p["numero"] for p in resultat["articles"]
               if sans_mentions(p["mots"]) == sans_mentions(mots_officiels(paires[p["id"]]["lignes"]))}
    assert obtenus == exacts


def test_amendements_du_titre_ecartes_de_l_execution():
    resultat = executer(etape_ppl(), "V1-gram")
    numeros = {s["numero"] for s in resultat["statuts"].values()}
    assert "15" not in numeros and "6" in numeros
    assert cle_numero("PREMIER") == "1"


# ---- visionneuse


def test_visionneuse_recolle_le_texte():
    assert "".join(morceaux("Le code , ( modifié ) de l ' article L . 5 ¶ Suite".split())) == \
        "Le code, (modifié) de l'article L. 5\nSuite"


def test_visionneuse_diff_et_marque_les_mots_d_amendement():
    segments = segments_diff(["a", "b"], ["a", "c", "d"], [0, 1, 1])
    assert [(s["t"], s["s"]) for s in segments] == [("=", "a"), ("-", " b"), ("+", " c d")]
    assert segments_diff(["a", "b"], ["a", "b"], [0, 1])[1] == {"t": "=", "s": " b", "amdt": True}


def test_visionneuse_garde_les_etiquettes_d_ecart():
    predit, officiel = ["a", "b", "c"], ["a", "x", "c"]
    segments = segments_ecarts(predit, officiel, [0, 0, 0], [("hors amendement", 1, 2, 1, 2)])
    assert [(s["t"], s["cat"]) for s in segments] == [("=", None), ("-", "hors amendement"), ("+", "hors amendement"), ("=", None)]


def etape_commission():
    def article(numero, contenu):
        return {"numero": numero, "lignes": contenu.split("\n"), "supprime": False}
    return {"id": "ppl822:commission",
            "entree": {"articles": [article(n, c) for n, c in TEXTES["PIONANR5L17B0822"]["articles"]]},
            "sortie": {"articles": [article(n, c) for n, c in TEXTES[COMMISSION]["articles"]]},
            "amendements": [{"numero_ordre": None, **a} for a in AMENDEMENTS if a["texte_ref"] == "PIONANR5L17B0822"]}


def amendement_de(etape, numero):
    return next(a["uid"] for a in etape["amendements"] if a["numero"] == numero)


def article_vue(etape, numero):
    lignes = next(a["lignes"] for a in etape["entree"]["articles"] if a["numero"] == numero)
    officiel = next(a["lignes"] for a in etape["sortie"]["articles"] if a["numero"] == ("PREMIER" if numero == "UNIQUE" else numero))
    return {"entree": sans_mentions(mots_officiels(lignes)), "officiel": sans_mentions(mots_officiels(officiel)), "uids": []}


def texte_segments(segments, *types):
    return "".join(s["s"] for s in segments if s["t"] in types)


def test_effet_d_un_amendement_seul():
    etape = etape_ppl()
    x = effet_amendement(etape, amendement_de(etape, "8"), article_vue(etape, "PREMIER"), {})
    assert x["erreur"] is None and x["sort"] == "Adopté"
    assert "impartissant" in texte_segments(x["effet"], "-")
    assert "dans un délai" in texte_segments(x["effet"], "+")
    assert "Article" not in texte_segments(x["effet"], "+", "-")


def test_effet_d_un_amendement_tombe_sur_le_texte_d_entree():
    # CE10 est tombé, mais son texte s'applique au texte d'entrée : on voit ce qu'il aurait donné.
    etape = etape_commission()
    x = effet_amendement(etape, amendement_de(etape, "CE10"), article_vue(etape, "UNIQUE"), {})
    assert x["erreur"] is None and x["sort"] == "Tombé"
    assert "puni" in texte_segments(x["effet"], "-") and "passible" in texte_segments(x["effet"], "+")


def test_sous_amendement_montre_a_travers_son_parent():
    etape = etape_commission()
    x = effet_amendement(etape, amendement_de(etape, "CE12"), article_vue(etape, "UNIQUE"), {})
    assert x["parent"] == "CE11" and x["erreur"] is None
    assert "au préalable" in texte_segments(x["effet"], "+", "=")


def test_parent_inclut_ses_sous_amendements_adoptes():
    etape = etape_commission()
    x = effet_amendement(etape, amendement_de(etape, "CE11"), article_vue(etape, "UNIQUE"), {})
    assert "au préalable" in texte_segments(x["effet"], "+", "=")


def test_amendement_qui_ne_s_applique_pas_rapporte_l_erreur():
    etape = etape_ppl()
    uid = amendement_de(etape, "6")
    for a in etape["amendements"]:
        if a["uid"] == uid:
            a["dispositif"] = "À l’alinéa 10, supprimer les mots : « absents du texte »."
    x = effet_amendement(etape, uid, article_vue(etape, "PREMIER"), {})
    assert "mots absents" in x["erreur"] and x["effet"] == []


def test_article_additionnel_seul():
    etape = etape_commission()
    x = effet_amendement(etape, amendement_de(etape, "CE2"), article_vue(etape, "UNIQUE"), {})
    assert "Le Gouvernement remet chaque année" in texte_segments(x["effet"], "+")


def analyse_de(etape, numero):
    brut = next(a for a in etape["amendements"] if a["numero"] == numero)
    return analyse_detaillee(brut, version_d_entree(etape))


def test_analyse_montre_la_grammaire_et_ce_qu_elle_a_trouve():
    (instruction,) = analyse_de(etape_ppl(), "8")
    assert instruction["source"] == "grammaire"
    (op,) = instruction["operations"]
    assert (op["verbe"], op["alinea"], op["position"]) == ("substitute", 3, "fin")
    assert op["trouve"]["lieu"] == "alinéa 3" and op["trouve"]["extrait"].startswith(", en lui impartissant")


def test_analyse_une_instruction_par_numero():
    instructions = analyse_de(etape_commission(), "CE10")
    assert [i["source"] for i in instructions] == ["grammaire", "grammaire"]
    assert [i["operations"][0]["verbe"] for i in instructions] == ["substitute", "complete"]


def test_analyse_signale_formule_inconnue_et_cible_absente():
    etape = etape_ppl()
    brut = next(a for a in etape["amendements"] if a["numero"] == "6")
    brut["dispositif"] = "Formule inconnue du légiste."
    assert analyse_detaillee(brut, version_d_entree(etape))[0]["source"] == "non reconnu"
    brut["dispositif"] = "À l’alinéa 10, supprimer les mots : « absents du texte »."
    assert "mots absents" in analyse_detaillee(brut, version_d_entree(etape))[0]["operations"][0]["erreur"]


def test_analyse_article_additionnel_et_sous_amendement():
    etape = etape_commission()
    assert "Nouvel article" in analyse_de(etape, "CE2")[0]["operations"][0]["note"]
    assert "amendement parent" in analyse_de(etape, "CE12")[0]["operations"][0]["note"]


def test_echecs_attribues_a_un_amendement():
    amendements = [{"uid": u, "numero": u, "sort": "Adopté"} for u in ("pendant", "seul", "a", "b", "serie1", "serie2", "juste")]
    statuts = {"pendant": {"etat": "pendant", "detail": "mots absents"}, "seul": {"etat": "applique"}, "a": {"etat": "applique"},
               "b": {"etat": "applique"}, "serie1": {"etat": "applique"}, "serie2": {"etat": "identique"}, "juste": {"etat": "applique"}}
    article = lambda uids, exact=False: {"uids": uids, "exact": exact, "categorie": None if exact else "effet absent"}
    articles = {"1": article(["seul"]), "2": article(["a", "b"]), "3": article(["serie1", "serie2"]), "4": article(["juste"], True)}
    echecs = {e["uid"]: e for e in echecs_etape(amendements, articles, statuts)}
    assert set(echecs) == {"pendant", "seul", "serie1"}  # plusieurs votes ou article exact : rien à attribuer
    assert (echecs["pendant"]["type"], echecs["pendant"]["attribution"], echecs["pendant"]["article"]) == ("résolution", "certaine", None)
    assert (echecs["seul"]["type"], echecs["seul"]["attribution"]) == ("effet absent", "probable")


def test_regles_du_juge():
    assert regle("Le Maire ,".split(), "le maire".split()) == "forme_seule"
    assert regle("peut demander".split(), "doit demander".split()) == "effet_different"
    assert regle("un délai de 3 mois".split(), "un délai de 6 mois".split()) == "effet_different"
    assert regle("à l' article 2".split(), "au titre".split()) == "effet_different"
    assert regle("chargé de la santé".split(), "en charge de la santé".split()) is None


def test_juge_lit_les_reponses_et_les_refus():
    juge = JugeDecisions()
    jugement = juge._lire({"type": "choice", "choice": "meme_effet", "confidence": 0.8, "probabilities": [
        {"value": "forme_seule", "probability": 0.1}, {"value": "meme_effet", "probability": 0.7}, {"value": "effet_different", "probability": 0.2}]})
    assert jugement["choix"] == "meme_effet" and round(probabilite_meme_effet(jugement), 3) == 0.8
    assert juge._lire({"type": "refusal", "refusal": "non"})["choix"] == "indetermine" and juge.refus == 1


def test_seuils_du_pilote():
    resultat = taux_par_seuil([(0.95, "meme"), (0.95, "different"), (0.6, "forme"), (0.2, "different"), (0.99, "indetermine")])
    assert taux_par_seuil([(0.95, "renumerotation")])[0.9]["vrais même effet retrouvés"] == 1
    assert resultat[0.9]["acceptés"] == 2 and resultat[0.9]["taux de faux %"] == 50.0
    assert resultat[0.5]["vrais même effet retrouvés"] == 2 and resultat[0.5]["sur"] == 2


def test_renumerotation_en_tete_d_alinea_seulement():
    officiel = "la phrase . ¶ 2 ° Le texte".split()
    assert etiquettes("la phrase . ¶ b ) Le texte", [0] * 8, " ".join(officiel)) == ["renumérotation"]
    assert etiquettes("a ) Le texte", [0] * 4, "1 ° Le texte") == ["renumérotation"]
    assert etiquettes("mentionnées au III de l' article", [0] * 7, "mentionnées au II de l' article") == ["hors amendement"]
    assert etiquettes("¶ Au texte", [0] * 3, "¶ Le texte") == ["hors amendement"]
    assert etiquettes("¶ a ) Le texte", [0] * 5, "¶ b ) Le texte") == ["renumérotation"]
    assert etiquettes("¶ a Le texte", [0] * 4, "¶ b Le texte") == ["hors amendement"]


def test_bornes_de_la_phrase():
    mots = "Fin . Selon l' article L . 12 , le maire peut agir . Autre".split()
    assert " ".join(mots[slice(*bornes_phrase(mots, 11, 12))]) == "Selon l' article L . 12 , le maire peut agir ."


def test_metrique_produit_par_amendement():
    comptes = {"adopté": Counter({"affiché": 80, "non analysé": 20}), "non adopté": Counter({"affiché": 150, "cible introuvable": 150})}
    lignes = [("adopté", "juste")] * 3 + [("adopté", "faux")] + [("non adopté", "mineur"), ("non adopté", "faux"), ("non adopté", "indetermine")]
    bilan = calculer(lignes, comptes)
    assert bilan["strates"]["adopté"]["effet juste au total %"] == 60.0
    assert bilan["strates"]["non adopté"]["effet juste au total %"] == 25.0
    assert bilan["effet juste, tous amendements %"] == 33.8  # pondéré : 100 adoptés, 300 non adoptés
    assert bilan["effet affiché mais faux, tous amendements %"] == 23.8
    assert wilson(0, 0) is None and wilson(5, 10)[0] < 50 < wilson(5, 10)[1]


def test_comparaison_des_versions(tmp_path):
    from evals.moteur.visu.app import comparer_versions

    def ecrire(version, entete, score=None):
        (tmp_path / version).mkdir()
        (tmp_path / version / "dev.jsonl").write_text(json.dumps(entete) + "\n", encoding="utf-8")
        if score:
            (tmp_path / version / "score_dev.json").write_text(json.dumps(score), encoding="utf-8")

    ecrire("V1", {"entete": True, "commit": "abc", "date": "2026-11-11T10:00:00"},
           {"consolidation": {"touches": 10, "exacts_%": 50.0}, "tombes": {"f1_%": 70.0}, "etats": {"pendant": 3}})
    ecrire("V0", {"entete": True, "commit": "abc"})          # run d'avant la date, jamais scoré
    (tmp_path / "juge_pilote").mkdir()                       # un dossier sans résultat n'est pas une version
    v0, v1 = comparer_versions(tmp_path, corpus_actuel="sha-v1")
    assert (v0["corpus_ancien"], v1["corpus_ancien"]) == (True, True)   # aucun des deux n'a tourné sur « sha-v1 »
    assert (v1["version"], v1["date"], v1["date_fichier"]) == ("V1", "2026-11-11T10:00:00", False)
    assert (v1["exacts_%"], v1["f1_tombes_%"], v1["pendant"], v1["applique"]) == (50.0, 70.0, 3, None)
    assert v0["date_fichier"] and v0["date"] and v0.get("exacts_%") is None


def test_le_run_est_date(tmp_path, monkeypatch):
    from evals.moteur import run

    monkeypatch.setattr(run, "RESULTATS", tmp_path)
    monkeypatch.setattr(run, "etapes_du_split", lambda split: [])
    monkeypatch.setattr("sys.argv", ["run", "--version", "V0-noop"])
    run.main()
    entete = json.loads((tmp_path / "V0-noop" / "dev.jsonl").read_text(encoding="utf-8"))
    assert entete["date"][:4].isdigit() and "T" in entete["date"]


def _etape_tisseuse():
    def amendement(uid, sort="Adopté", **champs):
        return {"uid": uid, "numero": uid, "texte_ref": "T", "article_vise": "ART. PREMIER", "alinea": "Alinéa 2",
                "dispositif": "d", "parent_uid": None, "tri_amendement": uid, "date_sort": None, "sort": sort,
                "id_identique": None, **champs}

    return {"id": "e", "entree": {"articles": [{"numero": "PREMIER", "lignes": ["Un mot ancien."], "supprime": False}]},
            "amendements": [amendement("a", id_identique="s"), amendement("b", id_identique="s"),
                            amendement("c", parent_uid="a"), amendement("d", sort="Rejeté"),
                            amendement("e", article_vise="APRÈS ART. PREMIER"), amendement("f", article_vise="ART. 9")]}


def test_tisseuse_ce_qui_part_au_pont():
    from evals.moteur.run_tisseuse import preparer

    envoi, statuts, suiveurs = preparer(_etape_tisseuse())
    assert [(a["uid"], a["article"], a["position"], a["alinea"]) for a in envoi["amendements"]] == \
        [("a", 0, "on_article", 2), ("e", 0, "after_article", 2)]
    assert suiveurs == {"b": "a"}
    assert {u: s["etat"] for u, s in statuts.items()} == \
        {"a": "applique", "b": "identique", "c": "non_analyse", "d": "ignore", "e": "applique", "f": "pendant"}


def test_tisseuse_reponse_du_pont_au_format_du_score():
    from evals.moteur.run_tisseuse import convertir, preparer

    etape = _etape_tisseuse()
    _, statuts, suiveurs = preparer(etape)
    reponse = {"statuts": {"a": {"etat": "pendant", "detail": "cible introuvable"}, "e": {"etat": "applique", "detail": ""}},
               "articles": [{"depart": 0, "lignes": ["Un mot nouveau."], "supprime": False, "origines": []},
                            {"depart": None, "lignes": ["Article ajouté."], "supprime": False, "origines": ["e"]}]}
    resultat = convertir(etape, reponse, statuts, suiveurs)
    premier, ajoute = resultat["articles"]
    assert (premier["numero_depart"], premier["mots"], premier["amendement"]) == \
        ("PREMIER", ["Un", "mot", "nouveau", "."], [0, 0, 1, 0])
    assert (ajoute["numero_depart"], ajoute["amendement"], ajoute["origines"]) == (None, [1, 1, 1], ["e"])
    # La série dont le premier membre a échoué échoue en entier.
    assert resultat["statuts"]["b"]["etat"] == "pendant"

