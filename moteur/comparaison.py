"""Étape 4 : comparer deux amendements par leurs effets, jamais par leur rédaction.

Conflit : ce que l'un écrit, l'autre l'écrit ou le lit. La réécriture d'un article écrit tous ses descendants,
donc le cas « a réécrit le paragraphe que b modifie » est couvert par la même intersection.
Les doublons se détectent sur le résultat (même empreinte) ; les quasi-doublons sur les mots ajoutés et retirés.
"""

import hashlib

from moteur.application import appliquer
from moteur.resolution import ErreurCible
from moteur.texte import decouper, empreinte, normaliser


def conflit(effet_a, effet_b):
    return bool(effet_a.ecritures & effet_b.ecritures
                or effet_a.ecritures & effet_b.lectures
                or effet_b.ecritures & effet_a.lectures)


def commutent(a, b, reference):
    """a et b : (operations, portee, uid). Vrai si les deux ordres donnent la même empreinte.

    C'est le test, pas une conséquence supposée de l'absence de conflit. Faux si un ordre ne s'applique pas.
    """
    try:
        return empreinte(_enchainer(a, b, reference)) == empreinte(_enchainer(b, a, reference))
    except ErreurCible:
        return False


def _enchainer(premier, second, reference):
    version = reference
    for operations, portee, uid in (premier, second):
        version, _ = appliquer(operations, portee, reference, version, uid)
    return version


def effet_identique(a, b, reference):
    """Vrai si a et b, appliqués chacun à reference, produisent exactement le même texte."""
    try:
        return (empreinte(appliquer(a[0], a[1], reference, reference, a[2])[0])
                == empreinte(appliquer(b[0], b[1], reference, reference, b[2])[0]))
    except ErreurCible:
        return False


def signature_texte(operations):
    """Les mots d'un amendement, normalisés : ajoutés et retirés, sans la cible."""
    morceaux = []
    for op in operations:
        morceaux += [op.anciens_mots or "", op.texte or ""]
    return [t.casefold() for t in decouper(normaliser(" ".join(morceaux))) if not t.isspace()]


def minhash(mots, taille=3, permutations=64):
    """Signature MinHash des suites de `taille` mots : deux textes proches partagent beaucoup de minimums."""
    suites = {" ".join(mots[i:i + taille]) for i in range(max(1, len(mots) - taille + 1))}
    return tuple(min(int.from_bytes(hashlib.blake2b(f"{k}:{s}".encode(), digest_size=8).digest()) for s in suites)
                 for k in range(permutations))


def similarite_minhash(a, b):
    return sum(x == y for x, y in zip(a, b)) / len(a)


def smith_waterman(a, b, correspondance=2, ecart=-1):
    """Alignement local de deux suites de mots, ramené à [0, 1] par la longueur de la plus courte."""
    if not a or not b:
        return 0.0
    ligne, meilleur = [0] * (len(b) + 1), 0
    for x in a:
        suivante = [0]
        for j, y in enumerate(b, 1):
            score = max(0, ligne[j - 1] + (correspondance if x == y else ecart), ligne[j] + ecart, suivante[-1] + ecart)
            suivante.append(score)
            meilleur = max(meilleur, score)
        ligne = suivante
    return meilleur / (correspondance * min(len(a), len(b)))


def quasi_doublons(amendements, seuil_minhash=0.5, seuil_alignement=0.8):
    """amendements : {uid : operations}. Paires de textes proches : MinHash d'abord, puis confirmation par alignement."""
    signatures = {uid: signature_texte(ops) for uid, ops in amendements.items()}
    empreintes = {uid: minhash(mots) for uid, mots in signatures.items() if mots}
    uids = sorted(empreintes)
    paires = []
    for i, a in enumerate(uids):
        for b in uids[i + 1:]:
            if similarite_minhash(empreintes[a], empreintes[b]) >= seuil_minhash:
                if smith_waterman(signatures[a], signatures[b]) >= seuil_alignement:
                    paires.append((a, b))
    return paires
