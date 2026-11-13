# Moteur de projection des amendements et son évaluation

Synthèse thématique des learnings des 9, 11 et 12 novembre 2026. Code : ce dossier (`moteur/` pour le moteur,
`evals/moteur/` pour l'évaluation). Learnings généraux sur les sources : [`learnings/categories/`](../../../learnings/categories/).

## Où sont les résultats

- Un dossier par version : `evals/moteur/resultats/<version>/dev.jsonl` (texte produit, statut de chaque
  amendement) et `score_dev.json` (scores). Hors git (`*.jsonl`).
- L'en-tête de `dev.jsonl` donne la date du run, le commit et l'empreinte du corpus (`corpus_sha256`). Deux runs
  ne se comparent que sur le même corpus.
- Relancer une version écrase son résultat. Le moteur n'a qu'une grammaire et une résolution : relancer `V1` ou
  `V3` donne le moteur actuel. Chaque changement du moteur prend donc un nouveau nom dans `versions.py`.
- Visionneuse : `evals/moteur/visu/`, ouverte sur un tableau qui compare toutes les versions (‡ : run fait sur
  un corpus antérieur ; † : score plus ancien que le résultat).
- Le dossier est un projet uv autonome (`pyproject.toml` et `uv.lock` propres, depuis le 13 novembre 2026) :
  lancer `uv run …` depuis `data-science/projection-amendements/`, `uv run pytest` pour ses tests.

## Scores (développement, corpus `c6d41f77`, 12 novembre 2026)

| Version | Exacts % | F1 ajouts | F1 retraits | F1 tombés | Adoptés non analysés | Adoptés pendants |
| --- | --- | --- | --- | --- | --- | --- |
| `V0-noop` (rien) | 0,0 | 0,0 | 0,0 | — | — | — |
| `V4-tisseuse-legi-ui` | 11,5 | 74,8 | 65,7 | — | 379 | 802 |
| `V5-gram-etendue` | 11,8 | 76,8 | 68,6 | 77,6 | 354 | 353 |
| `V6-gram-etendue+llm` | 12,3 | 79,6 | 70,2 | 79,1 | 69 | 398 |

`V1`, `V2` et `V3` ont tourné sur le corpus d'avant le rapport annexé : ne pas les comparer à ce tableau.

## Ce qui a fait gagner (mesuré sur le développement)

| Changement | Effet |
| --- | --- |
| Résolution tolérante (typographie, coquille, autre alinéa, « à la fin » approché) | adoptés pendants 610 → 491 |
| Grammaire étendue | adoptés non analysés 602 → 354 |
| Rapport annexé lu dans le corpus | adoptés pendants 530 → 353, F1 tombés 74,9 → 77,6 |
| LLM sur ce que la grammaire étendue rejette | non analysés 354 → 69, pour 0,03 $ (cache) |

- Grammaire : le premier trou était « la même phrase », « ledit », « audit », « dudit » (126 amendements). Puis la
  ponctuation variable autour des citations, les alinéas épars (« les alinéas 4, 9 et 12 »), les renvois
  multiples, les sous-instructions sous « À l'alinéa 3 : ».
- Toute tolérance est notée dans le statut de l'amendement (`typographie`, `mots approchés`, `autre alinéa`,
  `pas au bord`) : c'est par là qu'on relit en priorité.

## Pièges

- **Rapport annexé** (lois de programmation) : les amendements le visent comme les alinéas de l'article qui
  l'approuve (« ART. PREMIER, alinéa 179 »). Ses alinéas sont numérotés à part, depuis 1 : un paragraphe par
  alinéa, intertitres compris, et un tableau entier pour un seul alinéa. Sans traitement, le rapport débordait sur
  le dernier article du texte.
- Les pastilles de numéro d'alinéa du HTML de l'AN sont des images PNG en base64 : la même pastille n'a pas
  toujours la même empreinte, on ne peut pas s'en servir pour numéroter.
- **« À la fin de l'alinéa »** est souvent approximatif chez les rédacteurs. Mais quand c'était vrai sur le texte
  de départ et que ça ne l'est plus, un amendement adopté a écrit au bord : l'amendement est tombé.
- **« 1° Après le mot… »** est le plus souvent un alinéa cité (un article qui modifie un code), pas une
  sous-instruction : ne le découper que sous un en-tête de cible ou en tête de dispositif.
- **« Rédiger ainsi la fin de la première phrase »** ne touche que cette phrase : les suivantes restent (erreur
  corrigée le 12 novembre, elle existait avant la grammaire étendue).
- **Séries d'amendements identiques sans `id_discussion_identique`** : le second échoue (« mots absents ») ou
  insère un article additionnel en double. Non traité.

## Lire les scores

- Un article est compté dès que le texte officiel ou la prédiction le modifie. Un amendement non lu laisse l'article
  inchangé : il compte comme non exact (0,3 % d'exacts sur ces articles).
- Les articles exacts bougent peu : un article porte souvent plusieurs amendements et des retouches des services.
  Les F1 comptent les mots et suivent mieux les progrès. Le plus direct reste le taux d'amendements adoptés lus puis
  appliqués.
- Lire plus fait monter les articles « effet absent ». Relecture des 170 amendements nouvellement lus par la grammaire
  étendue : une vraie erreur du moteur, le reste des retouches des services ou un amendement voté ensuite au même
  endroit. Les amendements appliqués grâce au LLM n'ont pas été relus.

## Tisseuse et leximpact-legi-ui

- **Tisseuse** (`tricoteuses-juridique`, `packages/tisseuse`) analyse : `parseAmendment` traduit un dispositif en
  directives (`replace`, `insert_after`, `rewrite_portion`…). Pas de moteur d'application, pas de scores.
- **Le projecteur est dans `leximpact-legi-ui`** : `projectAmendmentOnPjlArticle` applique un amendement à
  l'article qu'il vise, pour un aperçu. Pas de consolidation (ordre des votes, enchaînement, séries identiques),
  pas de sous-amendements, application partielle quand une directive échoue.
- Pont d'évaluation : `evals/moteur/tisseuse/` (Node, esbuild) et `evals/moteur/run_tisseuse.py`. L'ordre des
  votes et l'enchaînement viennent de nous. Le projecteur est importé depuis le clone de legi-ui, qui n'est pas
  installé.
- Environnement : Tisseuse demande Node 24 ou plus, legi-ui Node 26. Avec Node 23, 55 amendements lèvent une
  exception d'expression régulière. `@tricoteuses/tisseuse` 0.22.3 importe `fs-extra` sans le déclarer.
- Rendu : une réécriture d'article entier sort dans un `<div class="pjl-amendment-rewrite">`, pas en `<p>` ; un
  article vide est refusé (« fragment HTML non disponible »).
- Points faibles mesurés : quand il ne situe pas la portion visée, le projecteur réécrit l'article entier
  (destructeur quand on enchaîne les amendements) ; une réécriture de plusieurs alinéas peut n'en garder qu'un ;
  pas de recherche tolérante.
- Point fort : son analyseur lisait plus de formules que notre grammaire d'origine. La grammaire étendue l'a
  rattrapé.

## Décisions

- **13 novembre 2026 : l'évaluation repose uniquement sur l'API Tricoteuses** (`parlement.tricoteuses.fr`) pour
  les actes, les textes découpés et les amendements. CLAIR n'est plus utilisé pour ce projet. Raisons : données en
  Licence Ouverte, textes adoptés en séance (`BTA`) disponibles, corpus reconstructible par tous. Le projet repart de
  zéro sur cette base (tâches : `docs/taches-benevoles-projection.xlsx`) ; les scores ci-dessus portent sur l'ancien
  corpus (open data AN + CLAIR) et ne se comparent pas aux prochains.
- 13 novembre 2026 : Tricoteuses accepte qu'on utilise ses données (données en Licence Ouverte).
- Historique : dump AN brut jugé trop compliqué ; Tricoteuses envisagé, puis corpus CLAIR figé (textes de l'open
  data AN, amendements CLAIR), remplacé par la décision précédente.
- Le rapport annexé d'une loi de programmation est rattaché à l'article qui l'approuve (voir plus haut).
- Chaque changement du moteur prend un nouveau nom de version : relancer un ancien nom donne le moteur actuel.

## Questions ouvertes

- Texte exact de la mention des sources à faire figurer avec le corpus publié.
- Qualité du découpage Tricoteuses pour les textes de commission (`BTC`) : vérifié seulement sur le PLF.
- PLF en nouvelle lecture dans le périmètre v0 ou non ? (Avec Tricoteuses, PLF et lois de fin de gestion ont un
  texte adopté vide : à exclure.)

## À faire

- Séries d'amendements identiques sans `id_discussion_identique` (dédoublonner par effet).
- Articles absents ou vides dans le texte d'entrée (`DLR5L17N53980` en commission : mise en page HTML différente ;
  articles 11 à 15 de `DLR5L17N54372`).
- « Rédiger ainsi le début de l'alinéa », lignes de tableau, nombres d'alinéas annoncés différents des alinéas cités.
- Relire les amendements appliqués grâce au LLM ; mesurer le LLM seul sur des instructions que la grammaire sait lire.
- Faire tourner la chaîne Tisseuse + legi-ui sous Node 26 pour écarter les exceptions dues à Node 23.
