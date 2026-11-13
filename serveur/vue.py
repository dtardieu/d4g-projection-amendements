"""Mise en forme d'une projection pour l'interface : article par article, le texte de départ et le
texte projeté découpé en segments (inchangé, inséré, supprimé), chacun rattaché à son amendement.

Le diff ne compare pas des mots : un jeton du moteur garde son identifiant d'un bout à l'autre.
Un jeton du départ absent de la version projetée a été supprimé ; un jeton d'une autre origine que
« T0 » a été inséré par l'amendement dont il porte l'uid. L'auteur d'une suppression se lit dans les
effets des amendements adoptés (ce qu'ils écrivent : jeton, alinéa ou article).
"""

from moteur.analyse import analyser_portee
from moteur.texte import cle_numero, lignes_de, numeros

ORIGINE_DEPART = "T0"


def articles(depart, projete, effets, amendements, statuts):
    """[{numero, numero_depart, etat, depart, alineas, amendements}], dans l'ordre projeté."""
    anciens = {a.id: a for a in depart.articles}
    auteurs = _auteurs_des_ecritures(effets)
    vises = _amendements_par_article(amendements)
    vue = numeros(projete)
    resultat = []
    for article in projete.articles:
        ancien = anciens.get(article.id)
        alineas = _alineas(ancien, article, auteurs)
        touches = {s["amendement"] for a in alineas for s in a["segments"] if s["amendement"]}
        cle = cle_numero(ancien.numero) if ancien and ancien.numero else None
        lies = touches | set(vises.get(cle, []))
        resultat.append(
            {
                "numero": vue[article.id],
                "numero_depart": ancien.numero if ancien else None,
                "etat": _etat(ancien, article, alineas),
                "depart": [] if ancien is None or ancien.supprime else lignes_de(ancien),
                "alineas": alineas,
                "amendements": [a.uid for a in amendements if a.uid in lies and a.uid in statuts],
            }
        )
    return resultat


def _etat(ancien, article, alineas):
    if ancien is None:
        return "nouveau"
    if article.supprime and not ancien.supprime:
        return "supprime"
    if any(s["type"] != "eq" for a in alineas for s in a["segments"]):
        return "modifie"
    return "inchange"


def _auteurs_des_ecritures(effets):
    """{identifiant écrit (jeton, alinéa, article) : uid de l'amendement adopté qui l'écrit}."""
    auteurs = {}
    for uid, effet in effets.items():
        for identifiant in effet.ecritures:
            auteurs.setdefault(identifiant, uid)
    return auteurs


def _amendements_par_article(amendements):
    """{clé du numéro d'article visé : [uid]}, « APRÈS ART. 3 » compris dans l'article 3."""
    vises = {}
    for amendement in amendements:
        try:
            article = analyser_portee(amendement.article_vise).article
        except ValueError:
            continue
        if article:
            vises.setdefault(cle_numero(article), []).append(amendement.uid)
    return vises


def _alineas(ancien, article, auteurs):
    """Alinéas affichés : [{numero, segments}] ; un alinéa supprimé en entier n'a pas de numéro."""
    if article.supprime:
        if ancien is None or ancien.supprime:
            return []
        return [_alinea_supprime(a, ancien, auteurs) for a in ancien.alineas]
    vivants = {j.id for a in article.alineas for j in a.jetons}
    avant, apres, fin = _suppressions(ancien, vivants, auteurs) if ancien else ({}, {}, [])
    resultat = []
    for numero, alinea in enumerate(article.alineas, 1):
        segments = []
        for jeton in alinea.jetons:
            alineas_retires, mots_retires = avant.pop(jeton.id, ([], []))
            resultat += alineas_retires
            # Le texte retiré se lit avant ce qui le remplace : on le place devant les insertions
            # qui précèdent immédiatement ce jeton.
            debut = len(segments)
            while debut and segments[debut - 1]["type"] == "ins":
                debut -= 1
            segments[debut:debut] = mots_retires
            if jeton.origine == ORIGINE_DEPART:
                segments.append(_segment("eq", jeton.texte, None))
            else:
                segments.append(_segment("ins", jeton.texte, jeton.origine))
            segments += apres.pop(jeton.id, [])
        resultat.append({"numero": numero, "segments": _fusionner(segments)})
    if ancien and len(fin) == len(ancien.alineas):
        return fin + resultat  # article réécrit en entier : l'ancien texte d'abord, puis le nouveau
    return resultat + fin


def _suppressions(ancien, vivants, auteurs):
    """Place les jetons supprimés du départ dans le texte projeté.

    avant[id] : (alinéas supprimés en entier, mots supprimés) à montrer avant le jeton vivant id ;
    apres[id] : mots supprimés en fin d'alinéa, après le dernier jeton vivant ;
    fin : alinéas supprimés après le dernier jeton vivant de l'article.
    """
    avant, apres, alineas_retires = {}, {}, []
    for alinea in ancien.alineas:
        if not any(j.id in vivants for j in alinea.jetons):
            alineas_retires.append(_alinea_supprime(alinea, ancien, auteurs))
            continue
        mots, dernier = [], None
        for jeton in alinea.jetons:
            if jeton.id not in vivants:
                mots.append(_supprime(jeton, alinea, ancien, auteurs))
            else:
                if mots or alineas_retires:
                    avant[jeton.id] = (alineas_retires, mots)
                    mots, alineas_retires = [], []
                dernier = jeton.id
        if mots:
            apres[dernier] = mots
    return avant, apres, alineas_retires


def _alinea_supprime(alinea, article, auteurs):
    segments = [_supprime(j, alinea, article, auteurs) for j in alinea.jetons]
    return {"numero": None, "segments": _fusionner(segments)}


def _supprime(jeton, alinea, article, auteurs):
    auteur = auteurs.get(jeton.id) or auteurs.get(alinea.id) or auteurs.get(article.id)
    return _segment("del", jeton.texte, auteur)


def _segment(type_, texte, amendement):
    return {"type": type_, "texte": texte, "amendement": amendement}


def _fusionner(segments):
    """Fusionne les segments voisins de même type et de même amendement.

    Une espace supprimée n'est écrite par aucun amendement : elle rejoint le mot supprimé qu'elle
    suit.
    """
    fusion = []
    for s in segments:
        meme_type = fusion and fusion[-1]["type"] == s["type"]
        if meme_type and (fusion[-1]["amendement"] == s["amendement"] or not s["texte"].strip()):
            fusion[-1]["texte"] += s["texte"]
        else:
            fusion.append(dict(s))
    return fusion
