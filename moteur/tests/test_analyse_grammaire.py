"""analyser_par_grammaire sur les 29 amendements de la PPL 822 (commission sur B0822, séance sur BTC1003).

Fixture : fixtures/ppl822_amendements.json, export de la table amendements de CLAIR (sort Charge exclu).
Les attendus suivent docs/amendment_tracking_worked_examples_fr.md.
"""

import json
from pathlib import Path

import pytest

from moteur.analyse import (AmendementBrut, Cible, Instruction, Operation, Portee, analyser_amendement,
                            analyser_par_grammaire, decouper_instructions, message_llm)

AMENDEMENTS = {a["numero"]: a for a in json.loads((Path(__file__).parent / "fixtures/ppl822_amendements.json")
                                                     .read_text(encoding="utf-8"))}


def portee(article_vise):
    # analyser_portee n'est pas encore écrit : les trois formes présentes dans la PPL 822 suffisent ici.
    if article_vise == "TITRE":
        return Portee("titre", None)
    if article_vise.startswith("APRÈS ART."):
        return Portee("article_additionnel", article_vise.removeprefix("APRÈS ART.").strip(), "apres")
    return Portee("article", article_vise.removeprefix("ART.").strip())


def analyser(numero):
    """Opérations de toutes les instructions, chaque instruction recevant la cible de la précédente."""
    amendement, operations, precedente = AMENDEMENTS[numero], [], None
    for instruction in decouper_instructions(amendement["dispositif"].split("\n\n")):
        ops = analyser_par_grammaire(instruction, portee(amendement["article_vise"]), precedente)
        assert ops is not None, f"{numero} : instruction {instruction.numero} non reconnue"
        operations += ops
        precedente = ops[-1].cible
    return operations


def test_tous_les_amendements_sont_reconnus():
    for numero in AMENDEMENTS:
        assert analyser(numero)


@pytest.mark.parametrize("numero, attendu", [
    ("7", [Operation("substitute", Cible(alinea=3), anciens_mots="habilités",
                     texte="mentionnés au premier alinéa de l’article L. 250‑3")]),
    ("8", [Operation("substitute", Cible(alinea=3, position="fin"),
                     anciens_mots=", en lui impartissant un délai raisonnable qu’ils fixent, de se conformer à ses "
                                  "obligations",
                     texte="de se conformer à ses obligations dans un délai qu’ils fixent")]),
    ("6", [Operation("delete", Cible(alinea=10, position="fin"),
                     anciens_mots="si une possibilité de financement de l’arrachage a été préalablement portée à la "
                                  "connaissance du détenteur de la parcelle")]),
    ("10", [Operation("delete_node", Cible(noeud="article"))]),
    ("CE5", [Operation("complete", Cible(alinea=2),
                       texte=", si une possibilité de financement de l’arrachage a été portée à la connaissance du "
                             "détenteur de la parcelle au préalable.")]),
    ("15", [Operation("rewrite", Cible(noeud="titre"),
                      texte="visant à instaurer des sanctions adaptées et proportionnées pour prévenir le "
                            "développement des vignes non cultivées")]),
    ("16", [Operation("substitute", Cible(alinea=2), anciens_mots="visant à instaurer des sanctions",
                      texte="instaurant des réponses")]),
    ("17", [Operation("insert", Cible(alinea=2, mots="prévenir", position="apres"), texte="notamment")]),
])
def test_formules_en_ligne(numero, attendu):
    assert analyser(numero) == attendu


def test_identiques_meme_analyse():
    # Séries identiques 75790 et 75886 : même analyse, donc même effet.
    assert analyser("6") == analyser("9")
    assert analyser("17") == analyser("18")


def test_sous_amendement_garde_le_numero_du_parent():
    # « le dixième alinéa » de CE11 : le numéro reste tel quel, le décompte chapeau compris se fait à l'étape 2.
    assert analyser("CE12")[0].cible == Cible(alinea=10)


def test_ce_meme_alinea_reprend_la_cible_precedente():
    substitute, complete = analyser("CE10")
    assert (substitute.verbe, substitute.cible, substitute.anciens_mots, substitute.texte) == \
        ("substitute", Cible(alinea=2), "puni", "passible")
    assert complete.verbe == "complete" and complete.cible == Cible(alinea=2)
    assert complete.texte.startswith("L’autorité administrative compétente")


def test_en_consequence_le_meme_alinea_numerote():
    instructions = decouper_instructions(AMENDEMENTS["1"]["dispositif"].split("\n\n"))
    assert [i.en_consequence for i in instructions] == [False, True]
    assert [op.cible for op in analyser("1")] == [Cible(alinea=10), Cible(alinea=10)]


def test_rediger_ainsi_cet_article_garde_les_alineas_sans_guillemets():
    (op,) = analyser("CE11")
    lignes = op.texte.split("\n")
    assert (op.verbe, op.cible, len(lignes)) == ("rewrite", Cible(noeud="article"), 9)
    assert lignes[0] == "Le code rural et de la pêche maritime est ainsi modifié :"
    assert lignes[-1].startswith("« Art. L. 251‑22.")


def test_blocs_d_alineas_cites():
    (rewrite,) = analyser("4")
    assert (rewrite.verbe, rewrite.cible, rewrite.texte.count("\n") + 1) == ("rewrite", Cible(alinea=3), 3)
    assert rewrite.texte.startswith("Art. L. 250‑10. –") and not rewrite.texte.endswith("»")
    (insert,) = analyser("5")
    assert (insert.verbe, insert.cible, insert.texte.count("\n") + 1) == \
        ("insert_node", Cible(alinea=3, position="apres"), 6)
    (complete_article,) = analyser("CE4")
    assert (complete_article.verbe, complete_article.cible) == \
        ("insert_node", Cible(noeud="article", position="fin"))


def test_article_additionnel():
    (op,) = analyser("CE2")
    assert (op.verbe, op.cible) == ("insert_node", Cible(noeud="article", position="apres"))
    assert op.texte.startswith("Le Gouvernement remet chaque année au Parlement")


@pytest.mark.parametrize("chapeau, citations", [
    ("Substituer aux alinéas 4 et 7 l'alinéa suivant :", ["« Un. »"]),         # alinéas épars : pas une plage
    ("Rédiger ainsi cet alinéa :", ["« Un. »"]),                               # aucun alinéa connu
    ("Rédiger ainsi l'article 6 :", ["« Un. »"]),                              # l'amendement vise l'article premier
    ("Compléter la même phrase par les mots : « , etc. »", []),                # renvoi sans antécédent
    ("Après l’alinéa 3, insérer les deux alinéas suivants :", ["« Un. »"]),    # 2 annoncés, 1 cité
    ("Compléter ce même alinéa par les mots : « , etc. »", []),                # renvoi sans antécédent
])
def test_ne_devine_pas(chapeau, citations):
    assert analyser_par_grammaire(Instruction(None, chapeau, citations), Portee("article", "PREMIER")) is None


# AC116 sur PIONANR5L17B0118 (ART. 13 BIS), tel que stocké dans CLAIR : le dernier alinéa cité n'a pas de « » »
# fermant. Le texte inséré modifie lui-même la loi n° 86-1067 : c'est l'opération imbriquée de la phase 2.
AC116 = ("Rédiger ainsi cet article :\n\n"
         "« Le premier alinéa de l’article 73 de la loi n° 86‑1067 du 30 septembre 1986 précitée est ainsi modifié :\n\n"
         "« 1° À la fin de la première phrase, les mots : « de deux interruptions publicitaires » sont remplacés par "
         "les mots : « d’une interruption publicitaire » ;\n\n"
         "« 2° La seconde phrase est supprimée.\n")


def test_rediger_ainsi_bloc_cite_sans_guillemet_fermant():
    (instruction,) = decouper_instructions(AC116.split("\n\n"))
    (op,) = analyser_par_grammaire(instruction, Portee("article", "13 BIS"))
    assert (op.verbe, op.cible) == ("rewrite", Cible(noeud="article"))
    assert op.texte.split("\n") == [
        "Le premier alinéa de l’article 73 de la loi n° 86‑1067 du 30 septembre 1986 précitée est ainsi modifié :",
        "1° À la fin de la première phrase, les mots : « de deux interruptions publicitaires » sont remplacés par "
        "les mots : « d’une interruption publicitaire » ;",
        "2° La seconde phrase est supprimée.",
    ]


def test_dispositif_aplati_rejete():
    # Copie de evals/amendements à la racine du dépôt (espaces écrasés) : sans sauts d'alinéa, les guillemets ouvrants ne sont plus
    # appariés et on ne sait plus où finit le chapeau. La grammaire refuse plutôt que de deviner.
    aplati = " ".join(AC116.split())
    (instruction,) = decouper_instructions([aplati])
    assert analyser_par_grammaire(instruction, Portee("article", "13 BIS")) is None


def test_apostrophe_droite_ou_typographique():
    portee_ = Portee("article", "PREMIER")
    for chapeau in ("À l'alinéa 2, supprimer le mot : « puni ».", "À l’alinéa 2, supprimer le mot : « puni »."):
        assert analyser_par_grammaire(Instruction(None, chapeau, []), portee_) == \
            [Operation("delete", Cible(alinea=2), anciens_mots="puni")]


# ---- niveau LLM, avec un faux modèle : aucun appel réseau


def faux_modele(sortie):
    return lambda consignes, message, schema: sortie


OPERATION_LLM = {"verbe": "substitute", "cible": {"alinea": 2, "jusqua": None, "phrase": None, "mots": None,
                                                  "occurrence": None, "position": "dans", "noeud": "alinea"},
                 "anciens_mots": "a", "texte": "b"}


def amendement_brut(dispositif, vise="ART. PREMIER"):
    return AmendementBrut("u", "1", "T", vise, None, dispositif, None, None, None)


def test_llm_seulement_si_la_grammaire_rejette():
    def interdit(*args):
        raise AssertionError("le LLM ne doit pas être appelé")
    analyse = analyser_amendement(amendement_brut("Supprimer cet article."), appel_llm=interdit)
    assert analyse.source == "grammar" and analyse.confiance == 1.0


def test_llm_prend_le_relais():
    sortie = {"operations": [OPERATION_LLM], "confiance": 0.8, "rejet": None}
    analyse = analyser_amendement(amendement_brut("Formule inconnue du légiste."), "Un.", faux_modele(sortie))
    assert analyse.source == "llm" and analyse.confiance == 0.8 and not analyse.erreurs
    assert analyse.operations == [Operation("substitute", Cible(alinea=2), anciens_mots="a", texte="b")]


@pytest.mark.parametrize("sortie", [
    None,
    {"operations": [], "confiance": 0.9, "rejet": "tableau"},
    {"operations": [{**OPERATION_LLM, "texte": None}], "confiance": 0.9, "rejet": None},
    {"operations": [{"verbe": "substitute"}], "confiance": 0.9, "rejet": None},
])
def test_sortie_llm_invalide_rapportee(sortie):
    analyse = analyser_amendement(amendement_brut("Formule inconnue du légiste."), "Un.", faux_modele(sortie))
    assert analyse.erreurs and analyse.operations == []


def test_message_llm_numerote_les_alineas():
    message = message_llm(Instruction(None, "Formule", ["« x »"]), Portee("article", "2"), "Un.\nDeux.", Cible(alinea=3))
    assert "(2) Deux." in message and "Cible précédente : alinéa 3" in message and message.endswith("« x »")


def _analyser(dispositif):
    analyse = analyser_amendement(AmendementBrut("a", "1", "T", "ART. PREMIER", None, dispositif, None, None, None))
    assert not analyse.erreurs, analyse.erreurs
    return analyse.operations


def _cibles(dispositif):
    return [(op.verbe, op.cible.alinea, op.cible.phrase, op.cible.position) for op in _analyser(dispositif)]


@pytest.mark.parametrize("suite, attendu", [
    # Renvois à la phrase ou à l'alinéa de l'instruction précédente.
    ("à la même première phrase du même alinéa 4, supprimer le mot : « b ».", ("delete", 4, 1, "dans")),
    ("à la même phrase du même alinéa, supprimer le mot : « b ».", ("delete", 4, 1, "dans")),
    ("compléter ladite phrase dudit alinéa par les mots : « b ».", ("complete", 4, 1, "dans")),
    ("audit alinéa 4, supprimer le mot : « b ».", ("delete", 4, None, "dans")),
    ("à la fin dudit alinéa 4, supprimer le mot : « b ».", ("delete", 4, None, "fin")),
    ("supprimer la seconde phrase dudit alinéa.", ("delete_node", 4, 2, "dans")),
    ("à la seconde phrase, supprimer le mot : « b ».", ("delete", 4, 2, "dans")),
    ("compléter cet alinéa par les mots : « b ».", ("complete", 4, None, "dans")),
    ("après le mot : « b », rédiger ainsi la fin de l'alinéa : « c ».", ("rewrite", 4, None, "apres")),
])
def test_renvois_a_l_instruction_precedente(suite, attendu):
    premiere = "I. – À la première phrase de l'alinéa 4, supprimer le mot : « a »."
    assert _cibles(f"{premiere}\n\nII. – En conséquence, {suite}")[1] == attendu


def test_phrase_sans_alinea_ni_antecedent():
    assert _cibles("À la seconde phrase, supprimer le mot : « b ».") == [("delete", None, 2, "dans")]


@pytest.mark.parametrize("dispositif, attendu", [
    ("Supprimer les alinéas 4, 9 et 12.", [4, 9, 12]),          # épars : une suppression par alinéa
    ("Supprimer les alinéas 4 et 7.", [4, 7]),
])
def test_suppression_d_alineas_epars(dispositif, attendu):
    assert [(op.verbe, op.cible.alinea, op.cible.jusqua) for op in _analyser(dispositif)] == \
        [("delete_node", n, None) for n in attendu]


def test_alineas_contigus_restent_une_plage():
    (op,) = _analyser("Supprimer les alinéas 4, 5 et 6.")
    assert (op.cible.alinea, op.cible.jusqua) == (4, 6)


@pytest.mark.parametrize("renvoi, attendu", [
    ("aux alinéas 7 et 8", [(7, "dans"), (8, "dans")]),
    ("aux alinéas 7, 9 et 12", [(7, "dans"), (9, "dans"), (12, "dans")]),
    ("à la fin des alinéas 41 et 71", [(41, "fin"), (71, "fin")]),
    ("au début de l'alinéa 6", [(6, "debut")]),
])
def test_renvoi_a_plusieurs_alineas(renvoi, attendu):
    operations = _analyser("I. – À l'alinéa 2, substituer au mot : « a » le mot : « b ».\n\n"
                           f"II. – En conséquence, procéder à la même substitution {renvoi}.")
    assert [(op.verbe, op.anciens_mots, op.texte) for op in operations[1:]] == [("substitute", "a", "b")] * len(attendu)
    assert [(op.cible.alinea, op.cible.position) for op in operations[1:]] == attendu


@pytest.mark.parametrize("dispositif", [
    "À l'alinéa 2, substituer au mot : « a » ; le mot : « b ».",
    "À l'alinéa 2, substituer au mot : « a ». le mot : « b ».",
    "À l'alinéa 2, substituer au mot ; « a », le mot : « b ».",
    "À l'alinéa 2, remplacer le mot : « a » par le mot : « b ».",
    "À l'alinéa 2, substituer au délai : « a » le délai : « b ».",
])
def test_variantes_de_ponctuation_et_d_objet(dispositif):
    (op,) = _analyser(dispositif)
    assert (op.verbe, op.cible.alinea, op.anciens_mots, op.texte) == ("substitute", 2, "a", "b")


def test_mention_en_tete_d_alinea():
    (op,) = _analyser("Au début de l'alinéa 1, insérer la mention : « I. – »")
    assert (op.verbe, op.cible.alinea, op.cible.position, op.texte) == ("insert", 1, "debut", "I. –")


def test_sous_instructions_sous_un_en_tete():
    operations = _analyser("À l'alinéa 3 :\n\n1° Supprimer les mots :\n\n« a » ;\n\n"
                           "2° Compléter l'alinéa par la phrase suivante :\n\n« B. »")
    assert [(op.verbe, op.cible.alinea) for op in operations] == [("delete", 3), ("complete", 3)]


def test_alinea_cite_numerote_n_est_pas_une_sous_instruction():
    # « 1° Après… » dans le texte cité d'un article qui modifie un code : un alinéa inséré, pas une instruction.
    (op,) = _analyser("Après l'alinéa 1, insérer les deux alinéas suivants :\n\n« I. – Le code est ainsi modifié :\n\n"
                      "1° Après le mot : « a », sont insérés les mots : « b ». »")
    assert (op.verbe, len(op.texte.split("\n"))) == ("insert_node", 2)


def test_retablir_une_subdivision_et_substituer_avec_virgule():
    (retabli,) = _analyser("Rétablir le I de l'alinéa 3 dans la rédaction suivante :\n\n« I. – Un. »")
    assert (retabli.verbe, retabli.cible.alinea) == ("rewrite", 3)
    (substitue,) = _analyser("Substituer à l'alinéa 18, les douze alinéas suivants :\n\n" + "\n\n".join(["« Un. »"] * 12))
    assert (substitue.verbe, substitue.cible.alinea, len(substitue.texte.split("\n"))) == ("rewrite", 18, 12)


def test_article_designe_par_son_numero():
    (op,) = _analyser("Rédiger ainsi l'article 1er :\n\n« Un. »")
    assert (op.verbe, op.cible.noeud) == ("rewrite", "article")
