# Suivi des effets des amendements — exemples détaillés sur de vrais amendements

Complément de *Tracking Amendment Effects on Law Text: Algorithm and Evaluation* (24/10/2026).
Chaque exemple déroule les cinq étapes (analyse → résolution → application → comparaison →
consolidation) sur de vrais amendements et indique, pour chaque étape, **d'où viennent les données**.

Tous les exemples portent sur un seul texte, choisi parce qu'il est court et couvre presque tous
les verbes et cas difficiles du document de conception, aux deux stades de la lecture à l'Assemblée :

| | |
|---|---|
| Texte | PPL n° 822 *Instaurer un dispositif de sanction contraventionnelle pour prévenir le développement des vignes non cultivées…* |
| Dossier | `DLR5L17N51328` — [page du dossier AN](https://www.assemblee-nationale.fr/dyn/17/dossiers/dispositif_sanction_contraventionnelle_prevenir_developpement_vignes_non_cultivees_17e) — promulguée : loi n° 2025‑533 |
| T₀ (entrée en commission) | `PIONANR5L17B0822` (PPL déposée) |
| Sortie de commission = entrée en séance | `PIONANR5L17BTC1003` (texte de la commission, 19 février 2025) |
| Sortie de séance | `PIONANR5L17BTA0062` (T.A. n° 62, 6 mars 2025) |
| Amendements | 12 en commission (CE1–CE12, sur B0822), 18 en séance (1–18, sur BTC1003) |

## Sources de données utilisées

| Code | Source | Ce qu'elle fournit | Comment la lire |
|---|---|---|---|
| **[DB‑A]** | Base CLAIR, table `amendements` (restaurée depuis l'open data de l'AN, voir [amendements.sql](../infra/meili_pg_sync/amendements.sql)) | `dispositif` (texte opératoire), `article_vise`, `alinea`, `sort`, `tri_amendement` (ordre d'appel), `amendement_parent_ref` (lien de sous‑amendement), `id_discussion_identique` / `id_discussion_commune`, `numero` (avec `(Rect)`) | `select … from amendements where texte_ref = 'PIONANR5L17B0822'` |
| **[DB‑T]** | Base CLAIR, table `textes_articles` | Une ligne par article : `numero`, `libelle`, `contenu` (un alinéa par ligne), `ordre` | `select … from textes_articles where texte_ref = … order by ordre` |
| **[AN‑TA]** | HTML open data de l'AN du texte adopté — `https://www.assemblee-nationale.fr/dyn/opendata/PIONANR5L17BTA0062.html` | Vérité terrain après la séance (absent de `textes_articles`) | téléchargement + suppression du HTML |
| **[AN‑TC]** | Idem pour `PIONANR5L17BTC1003.html` | Vérité terrain après la commission (également dans [DB‑T]) | |
| **[AN‑PDF]** | PDF publié de l'amendement, p. ex. `…/dyn/17/amendements/0822/CION-ECO/CE11.pdf` | Mise en page de référence du dispositif (découpage en alinéas), pour vérifier [DB‑A] | |
| **[AN‑JSON]** | `…/dyn/opendata/AMANR5L17PO419610B0822P0D1N000011.json` | Enregistrement open data brut (`numeroRect`, `triAmendement`, `sort`) | |
| **[LEGI]** | Légifrance / Archéo Lex, *code rural et de la pêche maritime* consolidé | Nécessaire seulement pour les opérations internes (au niveau du code) — phase 2 | |

Convention de numérotation des alinéas utilisée partout : découper `contenu` sur les sauts de
ligne ; l'alinéa 1 est la première ligne. Elle correspond au décompte de l'AN (vérifié pour toutes
les cibles de ce document).

---

## Exemple 1 — Commission : CE11 « Rédiger ainsi cet article » fait tomber trois amendements

**Entrée T₀** — [DB‑T] `PIONANR5L17B0822`, article `UNIQUE`, 2 alinéas :

```
al.1  L'article L. 251‑20 du code rural et de la pêche maritime est complété par un V ainsi rédigé :
al.2  « V. – Est puni d'une contravention de cinquième classe le fait de ne pas respecter le premier
      alinéa de l'article L. 251‑10 pour les parcelles plantées en vignes. »
```

**Amendements sur cet article** — [DB‑A], triés par `tri_amendement` :

| N° | tri_amendement (suffixe) | alinea | dispositif (début) | sort enregistré |
|---|---|---|---|---|
| CE11 | `…b` | — | « Rédiger ainsi cet article : … » (9 alinéas, rapporteur) | Adopté |
| CE12 | `…bi`, parent = CE11 | — | « Compléter le dixième alinéa par les mots : « , si une possibilité de financement … au préalable. » » | Adopté |
| CE10 | `…c` | Alinéa 2 | « I. – À l'alinéa 2, substituer au mot : « puni », le mot : « passible ». II. – Compléter ce même alinéa par la phrase suivante : … » | **Tombé** |
| CE5 | `…g` | Alinéa 2 | « Compléter l'alinéa 2 par les mots : « , si une possibilité de financement … » » | **Tombé** |
| CE4 | `…u` | Après l'alinéa 2 | « Compléter cet article par l'alinéa suivant : « Les produits de la contravention … » » | **Tombé** |

**Étape 1 — Analyse** (source : `dispositif` [DB‑A] ; toutes des formules canoniques → `source: grammar`)

```jsonc
CE11: [ rewrite(node=art.UNIQUE, text=<9 alinéas>) ]
CE10: [ substitute(occurrence(al.2, "puni", 1), "puni", "passible"),
        complete(al.2, "L'autorité administrative compétente étudie … L251‑10.") ]   // « ce même alinéa » → al.2 reporté
CE5:  [ complete(al.2, ", si une possibilité de financement … au préalable.") ]
CE4:  [ insert_node(after=last(art.UNIQUE), kind=alinea, text="Les produits …") ]
```

**Étape 2 — Résolution** sur T₀ (source : [DB‑T] B0822) : `al.2` → nœud `n2` ; « puni » apparaît
une seule fois dans `n2` → pas d'ambiguïté. « Compléter cet article » (CE4) se résout en « après
`n2` » (le champ `alinea` « Après l'alinéa 2 » concorde).

**Étape 3 — Application** : CE11 remplace les enfants de `art.UNIQUE` par 9 nouveaux nœuds
`m1…m9` ; `n1` et `n2` n'existent plus dans V₁.

**Étape 4 — Comparaison** : empreintes

| | écrit | lit |
|---|---|---|
| CE11 | {art.UNIQUE (sous‑arbre)} | — |
| CE10 | {n2} | {n2} |
| CE5 | {n2.fin} | {n2} |
| CE4 | {art.UNIQUE.fin} | {n2} |

`ancestor(writes(CE11)) ∩ writes(CE10|CE5|CE4) ≠ ∅` → les trois sont **en conflit** avec CE11.
CE11 est appelé en premier (`…b` < `…c`) et adopté ; résoudre à nouveau les autres sur V₁ donne
une **cible pendante** (`n2` n'existe plus) → prédits *tombés*.

**Vérité terrain / métrique** (détection de conflits) : [DB‑A] `sort = 'Tombé'` pour CE10, CE5,
CE4 → 3/3 vrais positifs sur cet article.

---

## Exemple 2 — Commission : le sous‑amendement CE12 et la résolution de son numéro d'alinéa

CE12 dit « Compléter le **dixième** alinéa » — de CE11, pas du texte. On confond facilement les
deux, et la règle de décompte n'est pas évidente :

* [DB‑A] / [AN‑PDF] CE11 compte **9** alinéas de texte après le chapeau « Rédiger ainsi cet article : ».
* En comptant **le chapeau comme alinéa 1** (convention de l'AN pour la cible d'un
  sous‑amendement), l'alinéa 10 = « Art. L. 251‑22. – Est puni de l'amende … en application du II
  de l'article L. 201‑4. » — la seule cible qui ait un sens. Sans le chapeau, on obtient une
  **cible pendante** (seulement 9 alinéas).

La même règle vaut en séance : les sous‑amendements 16 et 17 visent « l'alinéa 2 » de
l'amendement 15, dont l'alinéa 1 est « Rédiger ainsi le titre : » (exemple 5).

**Étape 1 — Analyse** : `CE12: { parent: CE11, ops: [ complete(CE11.al10, ", si une possibilité … au préalable.") ] }`

**Étape 3 — Application** (le sous‑amendement d'abord, sur les opérations du parent) : le nœud
`m9` de CE11 devient `"… L. 201‑4, si une possibilité de financement de l'arrachage a été portée
à la connaissance du détenteur de la parcelle au préalable. »"`. Attention à la ponctuation
finale : le texte ajouté se termine par `.` et la cible par `. »`. Un simple ajout en fin de chaîne
donne `L. 201‑4. », si…`. `complete` doit insérer avant le point final et le guillemet fermant.

**Détection de doublons** : CE12 et CE5 ont **exactement le même texte ajouté** mais visent des
nœuds différents (`CE11.m9` contre `n2`). Leurs empreintes de résultat diffèrent, donc la seule
égalité des effets ne détecte pas la paire. La détection de quasi‑doublons (MinHash sur le diff
normalisé, puis Smith‑Waterman sur le texte opératoire) la trouve : la suite de mots insérée est
identique. Cette paire montre que le cas « même idée qu'un amendement tombé, repêchée par un
sous‑amendement » existe et mérite d'être signalé.

---

## Exemple 3 — Commission : articles additionnels et le cas à deux niveaux (texte → code)

CE1, CE2, CE3, CE6, CE7 ([DB‑A] `article_vise = 'APRÈS ART. UNIQUE'`, tous Adopté) créent chacun
un nouvel article. CE8 et CE9 (même position) sont Rejeté.

**Étape 1 — Analyse**

```jsonc
CE2: [ insert_node(after=art.UNIQUE, kind=article,
        text="Le Gouvernement remet chaque année au Parlement un rapport sur le nombre de sanctions …") ]
CE1: [ insert_node(after=art.UNIQUE, kind=article,
        text=<contenu = opération sur un autre texte>) ]
     // interne : insert sur CRPM L. 201‑4, al.1 : insert(après « administrative »,
     //           ", dans les meilleurs délais au regard de la situation sanitaire,")
```

**Étape 5 — Consolidation** dans l'ordre de `tri_amendement` (`iaaa…c` CE1, `…ci` CE2, `…d` CE3,
`…f` CE6, `…g` CE7) : l'arbre des articles devient `[art.UNIQUE, a_CE1, a_CE2, a_CE3, a_CE6, a_CE7]`.
La vue `renumber` donne alors « Article 1er », « Article 2 (nouveau) » … « Article 6 (nouveau) ».
Aucun identifiant stocké ne change. C'est conforme à [DB‑T] `BTC1003` (`numero` = `PREMIER`, `2`…`6`).

**Niveau interne (phase 2)** : CE1 insère après « administrative » ; l'article 2 du texte de la
commission [AN‑TC] dit « Au premier alinéa **du I** de l'article L. 201‑4 …, après le mot :
« **prend** » ». CE1 n'a pas de `(Rect)` dans [DB‑A], `numeroRect = 0` dans [AN‑JSON], et aucun
autre amendement n'explique ce changement : c'est donc un **changement hors amendement** (une
correction rédactionnelle des services, faite au regard du code consolidé [LEGI]). Il va dans la
catégorie exclue et ne compte pas comme une erreur.

---

## Exemple 4 — Commission de bout en bout : consolidation contre BTC1003

`consolidate(T₀=B0822, adopted=[CE11←CE12, CE1, CE2, CE3, CE6, CE7])` et diff mot à mot avec
[DB‑T]/[AN‑TC] `BTC1003` :

| Article | Exact ? | Diff restant (prédit → officiel) | Catégorie |
|---|---|---|---|
| 1er (issu de CE11+CE12) | non | « Le code rural … est ainsi modifié » → « Le **titre V du livre II du** code rural … » et « du titre V du livre II » retiré des items ; « 1° » → « 1° (nouveau) » ; « Art. L. 250‑10 - » → « Art. L. 250‑10. – » ; « À la fin, est ajouté » → « Il est ajouté » ; « L. 250‑10. » → « L. 250‑10 du présent code. » ; « 3° La section 3 » → « 3° (Supprimé) / 4° (nouveau) La section 3 » ; « est complété » → « est complétée » ; « , si … a été portée … au préalable » → « si … a été **préalablement** portée … » | changement hors amendement (légistique) |
| 2 (CE1) | non | « après le mot « administrative » » → « du I … après le mot « prend » » | changement hors amendement |
| 3 (CE2) | **oui** | — | — |
| 4 (CE3) | non | « Dans les six mois » → « Dans un délai de six mois » ; « non respect » → « non-respect » | changement hors amendement |
| 5 (CE6) | non | « incluant » → « qui inclut » | changement hors amendement |
| 6 (CE7) | non | « et envisager » → « et d'envisager » | changement hors amendement |

Score principal pour cette lecture : 1/6 article exact (vérifié en exécutant le moteur ; une version
précédente de ce document indiquait à tort 4/6). Tous les diffs restants tombent dans la catégorie exclue,
donc le score sur les changements expliqués par des amendements est de 6/6. Ce texte montre que la catégorie
exclue n'est pas marginale : en commission, les services ont largement reformulé la réécriture du rapporteur
et ont aussi retouché les articles ajoutés.

---

## Exemple 5 — Séance : ancres de contenu, séries identiques, sous‑amendements sur le titre

Entrée V₀ = [DB‑T] `BTC1003`. L'article 1er compte 10 alinéas ; ceux qui sont visés :

```
al.3   « Art. L. 250‑10. – Lorsque les agents habilités constatent …, ils peuvent enjoindre à l'intéressé,
        en lui impartissant un délai raisonnable qu'ils fixent, de se conformer à ses obligations. » ;
al.10  « Art. L. 251‑22. – Est puni de l'amende … en application du II de l'article L. 201‑4 si une
        possibilité de financement de l'arrachage a été préalablement portée à la connaissance du
        détenteur de la parcelle. »
```

Amendements de séance [DB‑A] (`texte_ref = 'PIONANR5L17BTC1003'`, hors `sort = 'Charge'`), dans
l'ordre de `tri_amendement` :

| N° | tri | dispositif (début) | id_discussion_identique / parent | sort |
|---|---|---|---|---|
| 4, 7 | `…f`, `…g` | substitution / réécriture sur al.3 | — | Retiré après publication |
| 8 | `…jl` | « À la fin de l'alinéa 3, substituer aux mots : « , en lui impartissant un délai raisonnable qu'ils fixent, de se conformer à ses obligations » les mots : « de se conformer à ses obligations dans un délai qu'ils fixent » » | — | Adopté |
| 5 | `…jo` | « Après l'alinéa 3, insérer les six alinéas suivants : … » | — | Rejeté |
| 1 | `…js` | « I. – À l'alinéa 10, substituer au mot « puni » le mot « passible ». II. – **En conséquence**, compléter le même alinéa 10 par la phrase suivante : … » | — | Rejeté |
| 6, 9 | `…k`, `…l` | « À la fin de l'alinéa 10, supprimer les mots : « si une possibilité de financement … du détenteur de la parcelle » » | identique `75790` | Adopté |
| 10–14 | `caaa…`→`paaa…` | « Supprimer cet article. » sur ART. 2 … ART. 6 | — | Adopté |
| 15 | `saaa…i` | « Rédiger ainsi le titre : « visant à instaurer des sanctions adaptées et proportionnées pour prévenir le développement des vignes non cultivées » » | — | Adopté |
| 16 | `…o` | « À l'alinéa 2, substituer aux mots « visant à instaurer des sanctions » les mots « instaurant des réponses » » | parent 15 | Adopté |
| 17, 18 | `…q`, `…r` | « À l'alinéa 2, après le mot « prévenir », insérer le mot « notamment » » | parent 15, identique `75886` | Adopté |

**Étape 1 — Analyse** (toutes `source: grammar`)

```jsonc
8:  [ substitute(span(al.3, anchor_before="à l'intéressé", anchor_after=END),
                 ", en lui impartissant … à ses obligations", "de se conformer … qu'ils fixent") ]
6:  [ delete(span(al.10, "si une possibilité … de la parcelle", anchor_after=END)) ]   // = 9
1:  [ substitute(occurrence(al.10, "puni", 1), "puni", "passible"),
      complete(al.10, "L'autorité administrative compétente …") ]   // « En conséquence … même alinéa 10 » → même cible
10: [ delete_node(art.2) ]   … 14: [ delete_node(art.6) ]
15: [ rewrite(node=title, "visant à instaurer des sanctions adaptées …") ]
16: { parent: 15, ops: [ substitute(15.al2, "visant à instaurer des sanctions", "instaurant des réponses") ] }
17: { parent: 15, ops: [ insert(occurrence(15.al2, "prévenir", 1), after, "notamment") ] }
```

**Étape 2 — Résolution**, deux points :
* **Métadonnées contre texte.** L'amendement 1 a `alinea = 'Alinéa 2'` dans [DB‑A] mais son
  dispositif dit « alinéa 10 ». La résolution doit s'appuyer sur le dispositif. La colonne `alinea`
  n'est qu'un indice. Elle correspond à CE10 en commission, qui a le même texte et visait l'alinéa 2
  de B0822 : elle a donc probablement été reprise telle quelle.
* **Ancrage sur le contenu.** L'amendement 6 cite les mots exactement comme dans [AN‑TC] (« a été
  **préalablement** portée »), et non comme CE12 les avait écrits (« … au préalable »). Il faut
  résoudre sur la bonne version (le texte de la commission, pas T₀ + CE12).

**Étape 4 — Comparaison**

* **6 contre 9 (série identique `75790`)** : mêmes opérations sur V₀ → **même empreinte de
  résultat** → doublon détecté par l'effet, comme l'enregistre l'AN. La consolidation doit
  appliquer la série **une seule fois**. Appliquer 9 après 6 donne une cible pendante : 9 serait
  prédit *tombé* alors que [DB‑A] enregistre *Adopté*. Règle : regrouper par
  `id_discussion_identique` avant l'étape 5.
* **17 contre 18** (`75886`) : idem, au niveau des sous‑amendements.
* **16 contre 17** : portions disjointes de 15.al2 (« visant à instaurer des sanctions » contre
  après « prévenir ») → ils commutent ; les deux ordres donnent la même empreinte (métrique de
  commutation : 1/1).
* **1 contre 6** (contrefactuel, puisque 1 a été rejeté) : 1 complète la fin de l'al.10 ; 6
  supprime des mots « à la fin de l'alinéa 10 ». Leurs empreintes se recouvrent sur `al10.fin`, ils
  sont donc **en conflit**. Si on applique 1 d'abord, l'ancre « à la fin » de 6 n'est plus vraie. Si
  le contrôle par empreinte disait que ces deux‑là commutent, la règle d'empreinte ci‑dessus serait
  fausse.
* **8 contre 5** : 8 écrit la fin de l'al.3 ; 5 insère des nœuds *après* l'al.3 → pas de
  recouvrement, ils commutent.

**Étapes 3/5 — Application et consolidation** `[8, {6,9}, 10, 11, 12, 13, 14, 15←{16, {17,18}}]` :

```
al.3   « Art. L. 250‑10. – … ils peuvent enjoindre à l'intéressé de se conformer à ses obligations
        dans un délai qu'ils fixent. » ;
al.10  « Art. L. 251‑22. – Est puni … en application du II de l'article L. 201‑4. »
art.2–6  (Supprimés)          ← delete_node garde l'identifiant et laisse la mention « (Supprimé) »
titre  « instaurant des réponses adaptées et proportionnées pour prévenir notamment le
         développement des vignes non cultivées »
```

Provenance (à la `git blame`) : les mots de l'al.3 « de se conformer … qu'ils fixent » →
amendement 8 ; dans le titre, « instaurant des réponses » → 16, « notamment » → 17 (et 18) ; le
reste du titre → 15.

---

## Exemple 6 — Séance de bout en bout : consolidation contre le T.A. n° 62

Diff mot à mot de la sortie de l'exemple 5 avec [AN‑TA] `PIONANR5L17BTA0062` :

| Unité | Exact ? | Diff restant (prédit → officiel) | Catégorie |
|---|---|---|---|
| Article 1er | non | al.6 « b) Il est ajouté 3° » → « Il est ajouté **un** 3° » ; al.7 « injonction **prise** en application » → « injonction **adressée** en application » | changement hors amendement (aucun amendement de séance ne touche al.6/al.7 ; les 18 numéros de [DB‑A] se suivent, aucun ne manque) |
| Articles 2–6 | **oui** | « Articles 2 à 6 (nouveaux) (Supprimés) » | — |
| Titre | **oui** | — | — |

Application par amendement (articles avec exactement un amendement adopté) : articles 2–6
(amendements 10–14) → 5/5 exacts. L'article 1er a trois amendements adoptés (8, 6, 9) : il est
exclu de cette métrique et évalué seulement de bout en bout.

---

## Récapitulatif de ce que couvrent ces exemples

| Élément du document de conception | Où | Données qui le montrent |
|---|---|---|
| `rewrite` → conflit par ancêtre → tombé | Ex. 1 | [DB‑A] `sort = 'Tombé'` CE4, CE5, CE10 |
| Sous‑amendement appliqué aux opérations du parent ; décompte des alinéas chapeau compris | Ex. 2, 5 | [DB‑A] `amendement_parent_ref` ; [AN‑PDF] CE11 |
| Quasi‑doublon entre niveaux (CE5 ≈ CE12) | Ex. 2 | [DB‑A] dispositifs |
| `insert_node` d'article + renumérotation dérivée | Ex. 3 | [DB‑T] `BTC1003.numero` |
| Opération imbriquée texte → code | Ex. 3 | [DB‑A] CE1, [AN‑TC] art. 2, [LEGI] |
| « En conséquence … même alinéa » | Ex. 1 (CE10), Ex. 5 (1) | [DB‑A] |
| L'ancre de contenu doit suivre la rédaction de la dernière version | Ex. 5 (6/9) | [AN‑TC] contre CE12 |
| Série identique → un seul effet, appliqué une fois | Ex. 5 | [DB‑A] `id_discussion_identique` 75790, 75886 |
| Contrôle de commutation (16/17) et vrai conflit (1/6) | Ex. 5 | — |
| `delete_node` avec mention « (Supprimé) » | Ex. 5, 6 | [AN‑TA] « Articles 2 à 6 (Supprimés) » |
| Catégorie « changement hors amendement » | Ex. 3, 4, 6 | [AN‑TC], [AN‑TA] |
| Métadonnée (`alinea`) en désaccord avec le dispositif | Ex. 5 (1) | [DB‑A] |

Non couvert par ce texte : occurrences ambiguës (toutes les ancres ici sont uniques), amendements
rectifiés appliqués (aucun `(Rect)` parmi les amendements appliqués ; l'amendement de séance 3 est
`3 (Rect)` mais Rejeté), discussions communes, et la navette avec le Sénat (texte de la
commission n° 647 / T.A. 134 du Sénat, accessibles depuis la page du dossier). Il faudrait un
exemple sur une loi de finances pour couvrir ces cas.
