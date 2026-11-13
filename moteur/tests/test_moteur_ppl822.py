"""Le moteur de bout en bout sur la PPL 822 : commission (B0822 → BTC1003) et séance (BTC1003 → T.A. 62).

Reprend les exemples 1 à 6 de docs/amendment_tracking_worked_examples_fr.md. Fixtures : exports de CLAIR, et
pour le T.A. n° 62 l'open data de l'AN (PIONANR5L17BTA0062.html), absent de textes_articles.
"""

import json
from pathlib import Path

import pytest

from moteur.analyse import AmendementBrut, analyser_amendement
from moteur.application import appliquer, integrer_sous_amendements
from moteur.comparaison import commutent, conflit, effet_identique, quasi_doublons
from moteur.consolidation import consolider, ordre_d_appel
from moteur.texte import creer_version, diff_mots, lignes_de, normaliser, provenance, rendre

FIXTURES = Path(__file__).parent / "fixtures"
AMENDEMENTS = [AmendementBrut(**{k: a[k] for k in AmendementBrut.__dataclass_fields__})
               for a in json.loads((FIXTURES / "ppl822_amendements.json").read_text(encoding="utf-8"))]
TEXTES = json.loads((FIXTURES / "ppl822_textes.json").read_text(encoding="utf-8"))
DEPOT, COMMISSION, ADOPTE = "PIONANR5L17B0822", "PIONANR5L17BTC1003", "PIONANR5L17BTA0062"


def version(ref):
    return creer_version(TEXTES[ref]["articles"], TEXTES[ref]["titre"])


def amendements(ref):
    return [a for a in AMENDEMENTS if a.texte_ref == ref]


def amendement(ref, numero):
    return next(a for a in amendements(ref) if a.numero == numero)


def officiel(ref):
    return {numero: contenu.split("\n") for numero, contenu in TEXTES[ref]["articles"]}


def preparer(ref, numero):
    a = amendement(ref, numero)
    analyse = analyser_amendement(a)
    assert not analyse.erreurs
    return analyse.operations, analyse.portee, a.uid


@pytest.fixture(scope="module")
def commission():
    return consolider(version(DEPOT), amendements(DEPOT))


@pytest.fixture(scope="module")
def seance():
    return consolider(version(COMMISSION), amendements(COMMISSION))


def etat(consolidation, ref, numero):
    return consolidation.statuts[amendement(ref, numero).uid].etat


def test_tous_les_amendements_analyses():
    assert [a.numero for a in AMENDEMENTS if analyser_amendement(a).erreurs] == []


# ---- exemple 1 : CE11 fait tomber CE10, CE5, CE4


@pytest.mark.parametrize("numero", ["CE10", "CE5", "CE4"])
def test_reecriture_fait_tomber(commission, numero):
    statut = commission.statuts[amendement(DEPOT, numero).uid]
    assert statut.etat == "predit_tombe" and statut.sort == "Tombé"
    assert statut.conflits == [amendement(DEPOT, "CE11").uid]


def test_tombes_predits_egaux_aux_tombes_enregistres(commission, seance):
    for consolidation in (commission, seance):
        for statut in consolidation.statuts.values():
            if statut.etat in ("predit_tombe", "predit_vivant"):
                assert (statut.etat == "predit_tombe") == (statut.sort == "Tombé"), statut.numero


def test_ce10_depuis_le_texte_d_origine_s_applique():
    # Sur T₀, CE10 est valide : c'est l'adoption de CE11 qui le fait tomber, pas son texte.
    operations, portee, uid = preparer(DEPOT, "CE10")
    nouvelle, _ = appliquer(operations, portee, version(DEPOT), version(DEPOT), uid)
    ligne = lignes_de(nouvelle.articles[0])[1]
    assert "Est passible d’une contravention" in ligne
    assert ligne.endswith("vignes. L’autorité administrative compétente étudie avec le propriétaire des parcelles "
                          "précitées les modalités de mise en œuvre des mesures mentionnées à l’article L251‑10. »")


# ---- exemple 2 : sous-amendement CE12, alinéa compté chapeau compris


def test_sous_amendement_vise_le_parent_chapeau_compris(commission):
    lignes = dict(rendre(commission.version))["PREMIER"]
    assert lignes[8].endswith("du II de l’article L. 201‑4, si une possibilité de financement de l’arrachage a été "
                              "portée à la connaissance du détenteur de la parcelle au préalable. »")
    assert etat(commission, DEPOT, "CE12") == "sous_amendement"


def test_sous_amendement_vote_avant_son_parent():
    numeros = [a.numero for a in ordre_d_appel(amendements(DEPOT))]
    assert numeros.index("CE12") < numeros.index("CE11")


def test_quasi_doublon_entre_niveaux():
    # CE5 (tombé) et CE12 (adopté) : mêmes mots, cibles différentes, effets différents.
    ce5, ce12 = amendement(DEPOT, "CE5"), amendement(DEPOT, "CE12")
    paires = quasi_doublons({a.uid: analyser_amendement(a).operations for a in (ce5, ce12)})
    assert paires == [tuple(sorted((ce5.uid, ce12.uid)))]


# ---- exemple 3 : articles additionnels et renumérotation dérivée


def test_articles_additionnels_dans_l_ordre_des_votes(commission):
    assert [numero for numero, _ in rendre(commission.version)] == ["PREMIER", "2", "3", "4", "5", "6"]
    assert dict(rendre(commission.version))["3"][0].startswith("Le Gouvernement remet chaque année")
    assert "identifiant la typologie" in dict(rendre(commission.version))["6"][0]


def test_rejetes_ne_changent_rien(commission):
    assert etat(commission, DEPOT, "CE8") == etat(commission, DEPOT, "CE9") == "predit_vivant"


def test_renumerotation_sans_changer_les_identifiants(commission):
    identifiants = {a.id for a in version(DEPOT).articles}
    assert commission.version.articles[0].id in identifiants


# ---- exemple 4 : consolidation de la commission contre BTC1003


def test_commission_contre_texte_officiel(commission):
    attendu, obtenu = officiel(COMMISSION), dict(rendre(commission.version))
    assert diff_mots(obtenu["3"], attendu["3"]) == []
    # Écarts qu'aucun amendement n'explique : tous viennent de la réécriture par les services.
    assert diff_mots(obtenu["2"], attendu["2"]) == [("insert", "", "du I"), ("replace", "administrative »", "prend »,")]
    assert diff_mots(obtenu["5"], attendu["5"]) == [("replace", "incluant", "qui inclut")]
    assert diff_mots(obtenu["6"], attendu["6"]) == [("replace", "envisager", "d'envisager")]
    assert diff_mots(obtenu["4"], attendu["4"]) == [("replace", "les", "un délai de"),
                                                     ("replace", "non respect", "non-respect")]


# ---- exemple 5 : séance


def test_substitution_a_la_fin(seance):
    ligne = dict(rendre(seance.version))["PREMIER"][2]
    assert ligne == ("« Art. L. 250‑10. – Lorsque les agents habilités constatent un manquement ou une infraction au "
                     "présent titre, ils peuvent enjoindre à l’intéressé de se conformer à ses obligations dans un "
                     "délai qu’ils fixent. » ;")


def test_identiques_appliques_une_seule_fois(seance):
    assert etat(seance, COMMISSION, "6") == "applique"
    assert etat(seance, COMMISSION, "9") == "identique"
    assert dict(rendre(seance.version))["PREMIER"][9].endswith("en application du II de l’article L. 201‑4. »")


def test_identiques_meme_empreinte():
    assert effet_identique(preparer(COMMISSION, "6"), preparer(COMMISSION, "9"), version(COMMISSION))
    assert not effet_identique(preparer(COMMISSION, "6"), preparer(COMMISSION, "8"), version(COMMISSION))


def test_sous_amendements_sur_le_titre(seance):
    assert lignes_de(seance.version.titre) == [TEXTES[ADOPTE]["titre"]]


def test_sous_amendements_commutent():
    operations, portee, uid = preparer(COMMISSION, "15")
    a, b = preparer(COMMISSION, "16"), preparer(COMMISSION, "17")
    un = integrer_sous_amendements(operations, portee, uid, [(a[2], a[0]), (b[2], b[0])])
    autre = integrer_sous_amendements(operations, portee, uid, [(b[2], b[0]), (a[2], a[0])])
    assert un[0].texte == autre[0].texte


def effet(numero):
    operations, portee, uid = preparer(COMMISSION, numero)
    return appliquer(operations, portee, version(COMMISSION), version(COMMISSION), uid)[1]


def test_conflit_sur_l_ancre_de_position():
    # L'amendement 1 écrit à la fin de l'alinéa 10 ; le 6 supprime les mots « à la fin » de ce même alinéa.
    assert conflit(effet("1"), effet("6"))
    assert not commutent(preparer(COMMISSION, "1"), preparer(COMMISSION, "6"), version(COMMISSION))


def test_sans_conflit_les_ordres_donnent_la_meme_empreinte():
    assert not conflit(effet("8"), effet("6"))
    assert commutent(preparer(COMMISSION, "8"), preparer(COMMISSION, "6"), version(COMMISSION))
    assert not conflit(effet("8"), effet("5"))
    assert commutent(preparer(COMMISSION, "8"), preparer(COMMISSION, "5"), version(COMMISSION))


def test_provenance_du_titre(seance):
    morceaux = provenance(seance.version.titre.alineas[0])
    auteurs = {texte: origine for origine, texte in morceaux}
    assert auteurs["instaurant des réponses"] == amendement(COMMISSION, "16").uid
    assert auteurs["notamment"] == amendement(COMMISSION, "17").uid
    assert morceaux[1][0] == amendement(COMMISSION, "15").uid


def test_provenance_d_une_substitution(seance):
    article = seance.version.articles[0]
    auteurs = provenance(article.alineas[2])
    assert (amendement(COMMISSION, "8").uid, "de se conformer à ses obligations dans un délai qu’ils fixent") in auteurs


# ---- exemple 6 : consolidation de la séance contre le T.A. n° 62


def test_seance_contre_texte_adopte(seance):
    obtenu = dict(rendre(seance.version))
    assert diff_mots(obtenu["PREMIER"], officiel(ADOPTE)["PREMIER"]) == [("insert", "", "un"),
                                                                         ("replace", "prise", "adressée")]
    for numero in TEXTES[ADOPTE]["supprimes"]:
        assert obtenu[numero] == []


def test_articles_supprimes_gardent_leur_identifiant(seance):
    assert [a.id for a in seance.version.articles] == [a.id for a in version(COMMISSION).articles]


def test_rejete_apres_un_article_supprime_n_est_pas_tombe(seance):
    # 3 (Rect) ajoutait un article après l'article 6, supprimé juste avant : rejeté, pas tombé.
    assert etat(seance, COMMISSION, "3 (Rect)") == "predit_vivant"
    assert normaliser("a  b") == "a b"
