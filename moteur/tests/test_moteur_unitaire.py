"""Ce que la PPL 822 n'exerce pas : ancres stables, ambiguïtés, phrases, plages d'alinéas, renvois."""

import unicodedata

import pytest

from moteur.analyse import (AmendementBrut, Cible, Operation, Portee, _texte_cite, analyser_amendement, analyser_portee,
                            nettoyer_dispositif)
from moteur.application import appliquer
from moteur.consolidation import consolider
from moteur.resolution import APPROCHE, AUTRE_ALINEA, PAS_AU_BORD, TYPOGRAPHIE, CibleAmbigue, CiblePendante
from moteur.texte import cle_numero, creer_version, empreinte, lignes_de, phrases, creer_jetons, Identifiants

PORTEE = Portee("article", "PREMIER")
TEXTE = creer_version([("PREMIER", "Un alinéa un.\nLe mot cible et le mot cible.\nDernier alinéa. Seconde phrase.")])


def operations(dispositif):
    analyse = analyser_amendement(AmendementBrut("a", "1", "T", "ART. PREMIER", None, dispositif, None, None, None))
    assert not analyse.erreurs, analyse.erreurs
    return analyse.operations


def appliquer_sur(dispositif, courant=TEXTE, reference=TEXTE):
    return lignes_de(appliquer(operations(dispositif), PORTEE, reference, courant, "a")[0].articles[0])


def test_le_numero_se_lit_dans_la_reference_pas_dans_la_version_courante():
    # Une insertion adoptée avant décale la numérotation courante, pas l'identité de l'alinéa visé.
    insere, _ = appliquer(operations("Au début, ajouter l'alinéa suivant :\n\n« Nouveau. »"), PORTEE, TEXTE, TEXTE, "x")
    assert lignes_de(insere.articles[0])[0] == "Nouveau."
    lignes = appliquer_sur("À l'alinéa 3, supprimer le mot : « Seconde ».", insere)
    assert lignes[3] == "Dernier alinéa. phrase."


def test_accents_decomposes_recomposes():
    decompose = unicodedata.normalize("NFD", "Est créée une commission.")
    assert [j.texte for j in creer_jetons(decompose, Identifiants("t")) if not j.texte.isspace()] == \
        ["Est", "créée", "une", "commission", "."]


def test_ancre_retrouvee_malgre_les_accents_decomposes():
    texte = creer_version([("PREMIER", unicodedata.normalize("NFD", "Est créée une commission."))])
    ops = operations("À l'alinéa 1, substituer au mot : « créée », le mot : « instituée ».")
    assert lignes_de(appliquer(ops, PORTEE, texte, texte, "a")[0].articles[0]) == ["Est instituée une commission."]


def test_mots_absents_cible_pendante():
    with pytest.raises(CiblePendante):
        appliquer_sur("À l'alinéa 1, supprimer le mot : « absent ».")


def test_completer_un_article_vide():
    vide = creer_version([("PREMIER", "")])
    ops = [Operation("complete", Cible(noeud="article"), texte="mots")]
    with pytest.raises(CiblePendante):
        appliquer(ops, PORTEE, vide, vide, "a")


def test_bloc_cite_garde_les_guillemets_de_l_article_insere():
    # Un seul guillemet par alinéa dans l'amendement, pour deux niveaux de citation.
    assert _texte_cite(["« I. – Le code est complété par un article L. 1 ainsi rédigé :", "« Art. L. 1. – Premier.",
                        "« Second. » ;", "« II. – Autre point."]).split("\n") == [
        "I. – Le code est complété par un article L. 1 ainsi rédigé :", "« Art. L. 1. – Premier.", "« Second. » ;",
        "II. – Autre point."]


def test_bloc_cite_entierement_dans_la_citation_garde_sa_fermeture():
    assert _texte_cite(["« Le code est complété par un article L. 1 ainsi rédigé :", "« Art. L. 1. – Texte. »"]).split("\n") == [
        "Le code est complété par un article L. 1 ainsi rédigé :", "« Art. L. 1. – Texte. »"]


def test_double_guillemet_de_l_amendement():
    assert _texte_cite(["« Un article ainsi rédigé :", "« « Art. L. 1. – Texte. » »"]).split("\n") == [
        "Un article ainsi rédigé :", "« Art. L. 1. – Texte. »"]


def test_simple_alinea_cite_perd_ses_guillemets():
    assert _texte_cite(["« Texte simple. »"]) == "Texte simple."
    assert _texte_cite(["« Un.", "« Deux. »"]) == "Un.\nDeux."


def test_insertion_apres_une_annonce_de_citation():
    texte = creer_version([("PREMIER", "Il est inséré un article ainsi rédigé :\n« Art. L. 1. – Texte. »\nFin.")])
    ops = operations("Après l'alinéa 1, insérer l'alinéa suivant :\n\n« Art. L. 0. – Avant. »")
    assert lignes_de(appliquer(ops, PORTEE, texte, texte, "a")[0].articles[0])[1] == "« Art. L. 0. – Avant."


def test_remplacement_du_dernier_alinea_cite_reprend_la_fermeture():
    texte = creer_version([("PREMIER", "Article ainsi rédigé :\n« Art. L. 1. – Texte. » ;\nFin.")])
    ops = operations("Rédiger ainsi l'alinéa 2 :\n\n« Art. L. 1. – Nouveau. »")
    assert lignes_de(appliquer(ops, PORTEE, texte, texte, "a")[0].articles[0])[1] == "« Art. L. 1. – Nouveau. » ;"


def test_alinea_absent_de_la_reference():
    with pytest.raises(CiblePendante):
        appliquer_sur("Supprimer l'alinéa 9.")


def test_alinea_supprime_entre_temps():
    sans, _ = appliquer(operations("Supprimer l'alinéa 1."), PORTEE, TEXTE, TEXTE, "x")
    with pytest.raises(CiblePendante):
        appliquer_sur("À l'alinéa 1, supprimer le mot : « alinéa ».", sans)


def test_mots_presents_deux_fois_sans_rang_ambigu():
    with pytest.raises(CibleAmbigue):
        appliquer_sur("À l'alinéa 2, supprimer le mot : « cible ».")


def test_rang_d_occurrence_leve_l_ambiguite():
    lignes = appliquer_sur("À l'alinéa 2, substituer à la seconde occurrence du mot : « cible », le mot : « but ».")
    assert lignes[1] == "Le mot cible et le mot but."


def test_a_la_fin_designe_la_derniere_occurrence_meme_loin_du_bord():
    # Les amendements disent « à la fin » de mots proches de la fin : on applique, et on le signale.
    version, effet = appliquer(operations("À la fin de l'alinéa 2, supprimer les mots : « et le mot »."),
                               PORTEE, TEXTE, TEXTE, "a")
    assert lignes_de(version.articles[0])[1] == "Le mot cible cible."
    assert effet.tolerances == {PAS_AU_BORD}


def test_a_la_fin_exacte_ne_signale_rien():
    _, effet = appliquer(operations("À la fin de l'alinéa 2, supprimer le mot : « cible »."), PORTEE, TEXTE, TEXTE, "a")
    assert not effet.tolerances


TEXTE_ECARTS = creer_version([("PREMIER", "Le délai va au‑delà de 4 000 euros.\n"
                                          "La substance létale est administrée par le médecin.\nRien ici.")])


def appliquer_avec_ecart(dispositif, courant=TEXTE_ECARTS):
    version, effet = appliquer(operations(dispositif), PORTEE, TEXTE_ECARTS, courant, "a")
    return lignes_de(version.articles[0]), effet.tolerances


@pytest.mark.parametrize("dispositif, rang, attendu, tolerance", [
    ("À l'alinéa 1, substituer au montant : « 4000 euros », le montant : « 8500 euros ».", 0,
     "Le délai va au‑delà de 8500 euros.", TYPOGRAPHIE),
    ("À l'alinéa 1, supprimer les mots : « au delà ».", 0, "Le délai va de 4 000 euros.", TYPOGRAPHIE),
    ("À l'alinéa 2, substituer aux mots : « substante létale », les mots : « dose ».", 1,
     "La dose est administrée par le médecin.", APPROCHE),
    ("À l'alinéa 3, supprimer le mot : « médecin ».", 1, "La substance létale est administrée par le.", AUTRE_ALINEA),
    ("À l'alinéa 9, supprimer le mot : « médecin ».", 1, "La substance létale est administrée par le.", AUTRE_ALINEA),
])
def test_citation_retrouvee_malgre_un_ecart(dispositif, rang, attendu, tolerance):
    lignes, tolerances = appliquer_avec_ecart(dispositif)
    assert lignes[rang] == attendu
    assert tolerances == {tolerance}


def test_citation_exacte_sans_tolerance():
    assert appliquer_avec_ecart("À l'alinéa 2, supprimer le mot : « létale ».")[1] == frozenset()


@pytest.mark.parametrize("dispositif", [
    # Un autre nombre n'est pas une coquille.
    "À l'alinéa 1, supprimer les mots : « au‑delà de 5 000 euros ».",
    # Trop court pour qu'une lettre de différence soit une coquille.
    "À l'alinéa 2, supprimer le mot : « médecine ».",
    # Une coquille et un mauvais numéro à la fois.
    "À l'alinéa 3, supprimer les mots : « substante létale ».",
])
def test_ecart_trop_grand_cible_pendante(dispositif):
    with pytest.raises(CiblePendante):
        appliquer_avec_ecart(dispositif)


def test_mots_retires_par_un_adopte_ne_se_cherchent_pas_ailleurs():
    sans, _ = appliquer(operations("À l'alinéa 2, supprimer le mot : « médecin »."), PORTEE, TEXTE_ECARTS,
                        TEXTE_ECARTS, "x")
    with pytest.raises(CiblePendante):
        appliquer_avec_ecart("À l'alinéa 2, substituer au mot : « médecin », le mot : « praticien ».", sans)


def test_le_statut_dit_ce_qui_a_ete_tolere():
    amendement = AmendementBrut("a", "1", "T", "ART. PREMIER", None,
                                "À l'alinéa 1, supprimer les mots : « au delà ».", None, None, None, "Adopté")
    statut = consolider(TEXTE_ECARTS, [amendement]).statuts["a"]
    assert (statut.etat, statut.detail) == ("applique", TYPOGRAPHIE)


def test_plage_d_alineas():
    assert appliquer_sur("Supprimer les alinéas 1 à 2.") == ["Dernier alinéa. Seconde phrase."]
    assert appliquer_sur("Supprimer les alinéas 2 et 3.") == ["Un alinéa un."]
    assert appliquer_sur("Substituer aux alinéas 1 à 2 l'alinéa suivant :\n\n« Fusion. »")[0] == "Fusion."


def test_phrase_visee():
    assert appliquer_sur("Supprimer la seconde phrase de l'alinéa 3.")[2] == "Dernier alinéa."
    assert appliquer_sur("Compléter la première phrase de l'alinéa 3 par les mots : « bis ».")[2] == \
        "Dernier alinéa bis. Seconde phrase."


def test_mot_cherche_dans_la_phrase_seulement():
    texte = creer_version([("PREMIER", "Le mot ici. Encore le mot là.")])
    ops = operations("À la seconde phrase de l'alinéa 1, substituer au mot : « mot », le mot : « terme ».")
    nouvelle, _ = appliquer(ops, PORTEE, texte, texte, "a")
    assert lignes_de(nouvelle.articles[0]) == ["Le mot ici. Encore le terme là."]


def test_renvoi_recopie_l_operation_precedente():
    texte = creer_version([("PREMIER", "Un mot.\nDeux mots.\nTrois mot.")])
    ops = operations("I. – À l'alinéa 1, substituer au mot : « mot », le mot : « terme ».\n\n"
                     "II. – En conséquence, procéder à la même substitution à l'alinéa 3.")
    nouvelle, _ = appliquer(ops, PORTEE, texte, texte, "a")
    assert lignes_de(nouvelle.articles[0]) == ["Un terme.", "Deux mots.", "Trois terme."]


def test_apres_le_mot_rediger_la_fin():
    assert appliquer_sur("À l'alinéa 1, après le mot : « alinéa », rédiger ainsi la fin de cet alinéa : « deux. »")[0] == \
        "Un alinéa deux."
    assert appliquer_sur("Après le mot : « alinéa », supprimer la fin de l'alinéa 1.")[0] == "Un alinéa."


def test_retablir_un_article_supprime():
    sans, _ = appliquer(operations("Supprimer cet article."), PORTEE, TEXTE, TEXTE, "x")
    assert sans.articles[0].supprime
    assert appliquer_sur("Rétablir cet article dans la rédaction suivante :\n\nNouveau texte.", sans) == ["Nouveau texte."]


def test_aucune_operation_appliquee_si_l_une_echoue():
    ops = [Operation("delete_node", Cible(alinea=1)), Operation("delete_node", Cible(alinea=9))]
    with pytest.raises(CiblePendante):
        appliquer(ops, PORTEE, TEXTE, TEXTE, "a")


def test_meme_contenu_meme_empreinte():
    un, _ = appliquer(operations("À l'alinéa 1, supprimer le mot : « alinéa »."), PORTEE, TEXTE, TEXTE, "a")
    autre, _ = appliquer(operations("À l'alinéa 1, supprimer le mot : « alinéa »."), PORTEE, TEXTE, TEXTE, "b")
    assert empreinte(un) == empreinte(autre) != empreinte(TEXTE)


@pytest.mark.parametrize("texte, attendu", [
    ("Une phrase. Une autre.", ["Une phrase.", "Une autre."]),
    ("Voir l’article L. 251‑10 du code. Fin.", ["Voir l’article L. 251‑10 du code.", "Fin."]),
    ("« Art. L. 250‑10. – Texte du I. Suite. »", ["« Art. L. 250‑10. – Texte du I.", "Suite. »"]),
    ("I. – Premier point. Second point.", ["I. – Premier point.", "Second point."]),
    ("Selon l’art. 5 du texte. Fin.", ["Selon l’art. 5 du texte.", "Fin."]),
    ("Les mots : « a. B » sont retirés. Fin.", ["Les mots : « a. B » sont retirés.", "Fin."]),
    ("Le règlement n° 1408/2013 s’applique.", ["Le règlement n° 1408/2013 s’applique."]),
])
def test_phrases(texte, attendu):
    jetons = creer_jetons(texte, Identifiants("t"))
    assert ["".join(j.texte for j in jetons[d:f]) for d, f in phrases(jetons)] == attendu


@pytest.mark.parametrize("vise, attendu", [
    ("ART. UNIQUE", Portee("article", "UNIQUE")),
    ("ART. 6 BIS", Portee("article", "6 BIS")),
    ("APRÈS ART. UNIQUE", Portee("article_additionnel", "UNIQUE", "apres")),
    ("AVANT L'ART. 3", Portee("article_additionnel", "3", "avant")),
    ("TITRE", Portee("titre", None)),
])
def test_portee(vise, attendu):
    assert analyser_portee(vise) == attendu


def test_portee_inconnue():
    with pytest.raises(ValueError):
        analyser_portee("ANNEXE B")


def test_cle_numero():
    assert cle_numero("PREMIER") == cle_numero("1") == "1"
    assert cle_numero("34FORFAITISER ET HARMONISER") == "34"
    assert cle_numero("6 bis") == "6 BIS"


def test_formule_aux_accents_decomposes_reconnue():
    decompose = unicodedata.normalize("NFD", "À l'alinéa 1, supprimer le mot : « alinéa ».")
    assert appliquer_sur(decompose)[0] == "Un un."


def test_nettoyer_dispositif():
    assert nettoyer_dispositif("<p>Un &amp; deux.</p><p><span>Trois</span></p>") == ["Un & deux.", "Trois"]
    assert nettoyer_dispositif("Un.\n\nDeux.\n") == ["Un.", "Deux."]


def test_phrase_sans_alinea_dans_un_article_d_un_seul_alinea():
    unique = creer_version([("PREMIER", "Première phrase. Seconde phrase.")])
    version, _ = appliquer(operations("À la seconde phrase, supprimer le mot : « Seconde »."), PORTEE, unique, unique, "a")
    assert lignes_de(version.articles[0]) == ["Première phrase. phrase."]


def test_phrase_sans_alinea_dans_un_article_de_plusieurs_alineas():
    with pytest.raises(CiblePendante):
        appliquer_sur("Supprimer la seconde phrase.")


@pytest.mark.parametrize("nouveau, attendu", [
    ("« mot »", "Dernier mot. Seconde phrase."),               # la ponctuation de la phrase reste
    ("« mot final. »", "Dernier mot final. Seconde phrase."),  # le texte nouveau apporte la sienne
])
def test_rediger_la_fin_d_une_phrase_garde_les_suivantes(nouveau, attendu):
    lignes = appliquer_sur(f"À la première phrase de l'alinéa 3, après le mot : « Dernier », "
                           f"rédiger ainsi la fin de la première phrase : {nouveau}.")
    assert lignes[2] == attendu
