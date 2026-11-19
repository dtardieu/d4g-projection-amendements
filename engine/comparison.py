"""Étape 4 : comparer deux amendements par leurs effets, jamais par leur rédaction.

Conflit : ce que l'un écrit, l'autre l'écrit ou le lit. La réécriture d'un article écrit tous ses descendants,
donc le cas « a réécrit le paragraphe que b modifie » est couvert par la même intersection.
Les doublons se détectent sur le résultat (même empreinte) ; les quasi-doublons sur les mots ajoutés et retirés.
"""

import hashlib

from engine.application import apply
from engine.resolution import TargetError
from engine.text import split_tokens, fingerprint, normalize


def conflict(effect_a, effect_b):
    return bool(effect_a.writings & effect_b.writings
                or effect_a.writings & effect_b.lectures
                or effect_b.writings & effect_a.lectures)


def commute(a, b, reference):
    """a et b : (operations, portee, uid). Vrai si les deux ordres donnent la même empreinte.

    C'est le test, pas une conséquence supposée de l'absence de conflit. Faux si un ordre ne s'applique pas.
    """
    try:
        return fingerprint(_chain(a, b, reference)) == fingerprint(_chain(b, a, reference))
    except TargetError:
        return False


def _chain(first, second, reference):
    version = reference
    for operations, scope, uid in (first, second):
        version, _ = apply(operations, scope, reference, version, uid)
    return version


def identical_effect(a, b, reference):
    """Vrai si a et b, appliqués chacun à reference, produisent exactement le même texte."""
    try:
        return (fingerprint(apply(a[0], a[1], reference, reference, a[2])[0])
                == fingerprint(apply(b[0], b[1], reference, reference, b[2])[0]))
    except TargetError:
        return False


def text_signature(operations):
    """Les mots d'un amendement, normalisés : ajoutés et retirés, sans la cible."""
    pieces = []
    for op in operations:
        pieces += [op.old_words or "", op.text or ""]
    return [t.casefold() for t in split_tokens(normalize(" ".join(pieces))) if not t.isspace()]


def minhash(words, size=3, permutations=64):
    """Signature MinHash des suites de `taille` mots : deux textes proches partagent beaucoup de minimums."""
    continuations = {" ".join(words[i:i + size]) for i in range(max(1, len(words) - size + 1))}
    return tuple(min(int.from_bytes(hashlib.blake2b(f"{k}:{s}".encode(), digest_size=8).digest()) for s in continuations)
                 for k in range(permutations))


def minhash_similarity(a, b):
    return sum(x == y for x, y in zip(a, b)) / len(a)


def smith_waterman(a, b, mapping=2, gap=-1):
    """Alignement local de deux suites de mots, ramené à [0, 1] par la longueur de la plus courte."""
    if not a or not b:
        return 0.0
    line, best = [0] * (len(b) + 1), 0
    for x in a:
        next_one = [0]
        for j, y in enumerate(b, 1):
            score = max(0, line[j - 1] + (mapping if x == y else gap), line[j] + gap, next_one[-1] + gap)
            next_one.append(score)
            best = max(best, score)
        line = next_one
    return best / (mapping * min(len(a), len(b)))


def near_duplicates(amendements, minhash_threshold=0.5, alignment_threshold=0.8):
    """amendements : {uid : operations}. Paires de textes proches : MinHash d'abord, puis confirmation par alignement."""
    signatures = {uid: text_signature(ops) for uid, ops in amendements.items()}
    fingerprints = {uid: minhash(words) for uid, words in signatures.items() if words}
    uids = sorted(fingerprints)
    pairs = []
    for i, a in enumerate(uids):
        for b in uids[i + 1:]:
            if minhash_similarity(fingerprints[a], fingerprints[b]) >= minhash_threshold:
                if smith_waterman(signatures[a], signatures[b]) >= alignment_threshold:
                    pairs.append((a, b))
    return pairs
