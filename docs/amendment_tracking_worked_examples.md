# Tracking amendment effects — worked examples on real amendments

Companion to *Tracking Amendment Effects on Law Text: Algorithm and Evaluation* (2026-10-24).
Each example runs the five steps (parse → resolve → apply → compare → consolidate) on real
amendments and says, for every step, **where the data comes from**.

All examples come from one bill, chosen because it is short and covers almost every verb and
hard case in the design doc, at both readings in the Assemblée:

| | |
|---|---|
| Bill | PPL n° 822 *Instaurer un dispositif de sanction contraventionnelle pour prévenir le développement des vignes non cultivées…* |
| Dossier | `DLR5L17N51328` — [AN dossier page](https://www.assemblee-nationale.fr/dyn/17/dossiers/dispositif_sanction_contraventionnelle_prevenir_developpement_vignes_non_cultivees_17e) — promulgated as loi n° 2025‑533 |
| T₀ (committee input) | `PIONANR5L17B0822` (PPL as filed) |
| Committee output = séance input | `PIONANR5L17BTC1003` (texte de la commission, 19 Feb 2025) |
| Séance output | `PIONANR5L17BTA0062` (T.A. n° 62, 6 Mar 2025) |
| Amendments | 12 in committee (CE1–CE12, on B0822), 18 in séance (1–18, on BTC1003) |

## Data sources used

| Code | Source | What it gives | How to read it |
|---|---|---|---|
| **[DB‑A]** | CLAIR DB, table `amendements` (restored from AN open data, see [amendements.sql](../infra/meili_pg_sync/amendements.sql)) | `dispositif` (operative text), `article_vise`, `alinea`, `sort`, `tri_amendement` (calling order), `amendement_parent_ref` (sub‑amendment link), `id_discussion_identique` / `id_discussion_commune`, `numero` (with `(Rect)`) | `select … from amendements where texte_ref = 'PIONANR5L17B0822'` |
| **[DB‑T]** | CLAIR DB, table `textes_articles` | One row per article: `numero`, `libelle`, `contenu` (one alinéa per line), `ordre` | `select … from textes_articles where texte_ref = …  order by ordre` |
| **[AN‑TA]** | AN open data HTML of the adopted text — `https://www.assemblee-nationale.fr/dyn/opendata/PIONANR5L17BTA0062.html` | Ground truth after séance (not in `textes_articles`) | download + strip HTML |
| **[AN‑TC]** | Same for `PIONANR5L17BTC1003.html` | Ground truth after committee (also in [DB‑T]) | |
| **[AN‑PDF]** | Published amendment PDF, e.g. `…/dyn/17/amendements/0822/CION-ECO/CE11.pdf` | Authoritative layout of the dispositif (alinéa breaks), used to check [DB‑A] | |
| **[AN‑JSON]** | `…/dyn/opendata/AMANR5L17PO419610B0822P0D1N000011.json` | Raw open‑data record (`numeroRect`, `triAmendement`, `sort`) | |
| **[LEGI]** | Légifrance / Archéo Lex consolidated *code rural et de la pêche maritime* | Needed only for the inner (code‑level) operations — phase 2 | |

Alinéa numbering convention used everywhere below: split `contenu` on newlines; alinéa 1 is the
first line. This matches the AN's own counting (checked on every target in this document).

---

## Example 1 — Committee: CE11 "Rédiger ainsi cet article" makes three amendments fall

**Input T₀** — [DB‑T] `PIONANR5L17B0822`, article `UNIQUE`, 2 alinéas:

```
al.1  L'article L. 251‑20 du code rural et de la pêche maritime est complété par un V ainsi rédigé :
al.2  « V. – Est puni d'une contravention de cinquième classe le fait de ne pas respecter le premier
      alinéa de l'article L. 251‑10 pour les parcelles plantées en vignes. »
```

**Amendments on this article** — [DB‑A], ordered by `tri_amendement`:

| # | tri_amendement (suffix) | alinea | dispositif (head) | recorded sort |
|---|---|---|---|---|
| CE11 | `…b` | — | « Rédiger ainsi cet article : … » (9 alinéas, rapporteur) | Adopté |
| CE12 | `…bi`, parent = CE11 | — | « Compléter le dixième alinéa par les mots : « , si une possibilité de financement … au préalable. » » | Adopté |
| CE10 | `…c` | Alinéa 2 | « I. – À l'alinéa 2, substituer au mot : « puni », le mot : « passible ». II. – Compléter ce même alinéa par la phrase suivante : … » | **Tombé** |
| CE5 | `…g` | Alinéa 2 | « Compléter l'alinéa 2 par les mots : « , si une possibilité de financement … » » | **Tombé** |
| CE4 | `…u` | Après l'alinéa 2 | « Compléter cet article par l'alinéa suivant : « Les produits de la contravention … » » | **Tombé** |

**Step 1 — Parse** (source: `dispositif` [DB‑A]; all canonical formulas → `source: grammar`)

```jsonc
CE11: [ rewrite(node=art.UNIQUE, text=<9 alinéas>) ]
CE10: [ substitute(occurrence(al.2, "puni", 1), "puni", "passible"),
        complete(al.2, "L'autorité administrative compétente étudie … L251‑10.") ]   // "ce même alinéa" → al.2 carried over
CE5:  [ complete(al.2, ", si une possibilité de financement … au préalable.") ]
CE4:  [ insert_node(after=last(art.UNIQUE), kind=alinea, text="Les produits …") ]
```

**Step 2 — Resolve** against T₀ (source: [DB‑T] B0822): `al.2` → node `n2`; "puni" occurs once in
`n2` → unambiguous. CE4's "Compléter cet article" resolves to "after `n2`" (the `alinea` field
"Après l'alinéa 2" agrees).

**Step 3 — Apply**: CE11 replaces the children of `art.UNIQUE` with 9 new nodes `m1…m9`; `n1`, `n2`
are now dead in V₁.

**Step 4 — Compare**: footprints

| | writes | reads |
|---|---|---|
| CE11 | {art.UNIQUE (subtree)} | — |
| CE10 | {n2} | {n2} |
| CE5 | {n2.end} | {n2} |
| CE4 | {art.UNIQUE.end} | {n2} |

`ancestor(writes(CE11)) ∩ writes(CE10|CE5|CE4) ≠ ∅` → all three **conflict** with CE11.
CE11 is called first (`…b` < `…c`), is adopted, so re‑resolving the others on V₁ yields
**dangling target** (`n2` no longer exists) → predicted *fallen*.

**Ground truth / metric** (Conflict detection): [DB‑A] `sort = 'Tombé'` for CE10, CE5, CE4 →
3/3 true positives on this article.

---

## Example 2 — Committee: sub‑amendment CE12, and how its alinéa number resolves

CE12 says « Compléter le **dixième** alinéa » — of CE11, not of the bill. The two are easy to
mix up, and the counting rule is not obvious:

* [DB‑A] / [AN‑PDF] CE11 has **9** alinéas of text after the chapeau « Rédiger ainsi cet article : ».
* Counting the **chapeau as alinéa 1** (the AN's convention for counting a sub‑amendment's
  target), alinéa 10 = « Art. L. 251‑22. – Est puni de l'amende … en application du II de l'article
  L. 201‑4. » — which is the only sensible target. Counting without the chapeau gives a
  **dangling target** (only 9 alinéas).

The same rule holds in séance: sub‑amendments 16 and 17 target « l'alinéa 2 » of amendment 15,
whose alinéa 1 is « Rédiger ainsi le titre : » (Example 5).

**Step 1 — Parse**: `CE12: { parent: CE11, ops: [ complete(CE11.al10, ", si une possibilité … au préalable.") ] }`

**Step 3 — Apply** (sub‑amendment first, onto the parent's ops): CE11's payload node `m9`
becomes `"… L. 201‑4, si une possibilité de financement de l'arrachage a été portée à la
connaissance du détenteur de la parcelle au préalable. »"`. Note the closing punctuation: the
payload ends with `.` and the target ends with `. »`. A naive append produces `L. 201‑4. », si…`.
`complete` must insert before the final full stop and closing guillemet.

**Duplicate detection**: CE12 and CE5 have **the same payload** but target different nodes (`CE11.m9` vs `n2`). Their effect hashes differ, so effect-equality
alone misses the pair. Near-duplicate detection (MinHash over the normalised diff, then
Smith‑Waterman on the operative text) catches it: the inserted token sequence is identical. This pair shows
that the "same idea as a fallen amendment, rescued by a sub-amendment" pattern exists and is worth
reporting.

---

## Example 3 — Committee: "articles additionnels" and the two‑level (bill → code) case

CE1, CE2, CE3, CE6, CE7 ([DB‑A] `article_vise = 'APRÈS ART. UNIQUE'`, all Adopté) each create a
new article. CE8, CE9 (same position) are Rejeté.

**Step 1 — Parse**

```jsonc
CE2: [ insert_node(after=art.UNIQUE, kind=article,
        text="Le Gouvernement remet chaque année au Parlement un rapport sur le nombre de sanctions …") ]
CE1: [ insert_node(after=art.UNIQUE, kind=article,
        text=<payload = op on another text>) ]
     // inner: substitute/insert on CRPM L. 201‑4, al.1: insert(after "administrative",
     //        ", dans les meilleurs délais au regard de la situation sanitaire,")
```

**Step 5 — Consolidate** in `tri_amendement` order (`iaaa…c` CE1, `…ci` CE2, `…d` CE3, `…f` CE6,
`…g` CE7): the article tree becomes `[art.UNIQUE, a_CE1, a_CE2, a_CE3, a_CE6, a_CE7]`. The
`renumber` view then gives "Article 1er", "Article 2 (nouveau)" … "Article 6 (nouveau)". No stored
id changes. This matches [DB‑T] `BTC1003` (`numero` = `PREMIER`, `2`…`6`).

**Inner level (phase 2)**: CE1 inserts after « administrative »; the committee text [AN‑TC]
article 2 reads « Au premier alinéa **du I** de l'article L. 201‑4 …, après le mot : « **prend** » ».
CE1 has no `(Rect)` in [DB‑A], `numeroRect = 0` in [AN‑JSON], and no other amendment explains
the change, so this is a **non‑amendment change** (a drafting fix by the services, made against
the consolidated code [LEGI]). It goes in the excluded bin and does not count as an error.

---

## Example 4 — Committee end‑to‑end: consolidation vs. BTC1003

`consolidate(T₀=B0822, adopted=[CE11←CE12, CE1, CE2, CE3, CE6, CE7])` and word‑diff against
[DB‑T]/[AN‑TC] `BTC1003`:

| Article | Exact? | Remaining diff (predicted → official) | Bin |
|---|---|---|---|
| 1er (from CE11+CE12) | no | « Le code rural … est ainsi modifié » → « Le **titre V du livre II du** code rural … » and the "du titre V du livre II" removed from the items; « 1° » → « 1° (nouveau) »; « Art. L. 250‑10 - » → « Art. L. 250‑10. – »; « À la fin, est ajouté » → « Il est ajouté »; « L. 250‑10. » → « L. 250‑10 du présent code. »; « 3° La section 3 » → « 3° (Supprimé) / 4° (nouveau) La section 3 »; « est complété » → « est complétée »; « , si … a été portée … au préalable » → « si … a été **préalablement** portée … » | non‑amendment change (légistique) |
| 2 (CE1) | no | « après le mot « administrative » » → « du I … après le mot « prend » » | non‑amendment change |
| 3 (CE2) | **yes** | — | — |
| 4 (CE3) | no | « Dans les six mois » → « Dans un délai de six mois »; « non respect » → « non-respect » | non‑amendment change |
| 5 (CE6) | no | « incluant » → « qui inclut » | non‑amendment change |
| 6 (CE7) | no | « et envisager » → « et d'envisager » | non‑amendment change |

Headline score for this reading: 1/6 articles exact (checked by running the engine; an earlier version
of this document wrongly said 4/6). All remaining diffs fall in the excluded bin, so the score on
amendment-explained changes is 6/6. This bill shows the excluded bin is not marginal: in committee the
services reworded the rapporteur's rewrite heavily and touched the added articles too.

---

## Example 5 — Séance: content anchors, identical series, sub‑amendments on the title

Input V₀ = [DB‑T] `BTC1003`. Article 1er has 10 alinéas; the ones targeted are:

```
al.3   « Art. L. 250‑10. – Lorsque les agents habilités constatent …, ils peuvent enjoindre à l'intéressé,
        en lui impartissant un délai raisonnable qu'ils fixent, de se conformer à ses obligations. » ;
al.10  « Art. L. 251‑22. – Est puni de l'amende … en application du II de l'article L. 201‑4 si une
        possibilité de financement de l'arrachage a été préalablement portée à la connaissance du
        détenteur de la parcelle. »
```

Séance amendments [DB‑A] (`texte_ref = 'PIONANR5L17BTC1003'`, excluding `sort = 'Charge'`), in
`tri_amendement` order:

| # | tri | dispositif (head) | id_discussion_identique / parent | sort |
|---|---|---|---|---|
| 4, 7 | `…f`, `…g` | substitute / rewrite on al.3 | — | Retiré après publication |
| 8 | `…jl` | « À la fin de l'alinéa 3, substituer aux mots : « , en lui impartissant un délai raisonnable qu'ils fixent, de se conformer à ses obligations » les mots : « de se conformer à ses obligations dans un délai qu'ils fixent » » | — | Adopté |
| 5 | `…jo` | « Après l'alinéa 3, insérer les six alinéas suivants : … » | — | Rejeté |
| 1 | `…js` | « I. – À l'alinéa 10, substituer au mot « puni » le mot « passible ». II. – **En conséquence**, compléter le même alinéa 10 par la phrase suivante : … » | — | Rejeté |
| 6, 9 | `…k`, `…l` | « À la fin de l'alinéa 10, supprimer les mots : « si une possibilité de financement … du détenteur de la parcelle » » | identique `75790` | Adopté |
| 10–14 | `caaa…`→`paaa…` | « Supprimer cet article. » on ART. 2 … ART. 6 | — | Adopté |
| 15 | `saaa…i` | « Rédiger ainsi le titre : « visant à instaurer des sanctions adaptées et proportionnées pour prévenir le développement des vignes non cultivées » » | — | Adopté |
| 16 | `…o` | « À l'alinéa 2, substituer aux mots « visant à instaurer des sanctions » les mots « instaurant des réponses » » | parent 15 | Adopté |
| 17, 18 | `…q`, `…r` | « À l'alinéa 2, après le mot « prévenir », insérer le mot « notamment » » | parent 15, identique `75886` | Adopté |

**Step 1 — Parse** (all `source: grammar`)

```jsonc
8:  [ substitute(span(al.3, anchor_before="à l'intéressé", anchor_after=END),
                 ", en lui impartissant … à ses obligations", "de se conformer … qu'ils fixent") ]
6:  [ delete(span(al.10, "si une possibilité … de la parcelle", anchor_after=END)) ]   // = 9
1:  [ substitute(occurrence(al.10, "puni", 1), "puni", "passible"),
      complete(al.10, "L'autorité administrative compétente …") ]   // "En conséquence … même alinéa 10" → reuse target
10: [ delete_node(art.2) ]   … 14: [ delete_node(art.6) ]
15: [ rewrite(node=title, "visant à instaurer des sanctions adaptées …") ]
16: { parent: 15, ops: [ substitute(15.al2, "visant à instaurer des sanctions", "instaurant des réponses") ] }
17: { parent: 15, ops: [ insert(occurrence(15.al2, "prévenir", 1), after, "notamment") ] }
```

**Step 2 — Resolve**, two points:
* **Metadata vs text.** Amendment 1 has `alinea = 'Alinéa 2'` in [DB‑A] but its dispositif says
  « alinéa 10 ». Resolution must use the dispositif. The `alinea` column is a hint only. It matches
  the committee-stage CE10, which has the same text and targeted alinéa 2 of B0822, so it was probably carried over.
* **Content anchoring.** Amendment 6 quotes the words exactly as [AN‑TC] has them (« a été
  **préalablement** portée »), not as CE12 wrote them (« … au préalable »). Resolving against
  the right version (the committee text, not T₀ + CE12) is required.

**Step 4 — Compare**

* **6 vs 9 (identical series `75790`)**: same ops on V₀ → **same result hash** → duplicate detected
  from the effect, as recorded by the AN. Consolidation must apply the series **once**.
  Applying 9 after 6 gives a dangling target, so 9 would be predicted *fallen* while [DB‑A] records
  *Adopté*. Rule: group by `id_discussion_identique` before step 5.
* **17 vs 18** (`75886`): same, at sub‑amendment level.
* **16 vs 17**: disjoint spans of 15.al2 (« visant à instaurer des sanctions » vs after
  « prévenir ») → commute; both orders give the same hash (Commutation metric: 1/1).
* **1 vs 6** (counterfactual, since 1 was rejected): 1 completes al.10 at the end; 6 deletes
  « à la fin de l'alinéa 10 ». Their footprints overlap at `al10.end`, so they **conflict**. Applying 1
  first means 6's anchor "à la fin" no longer holds. If a hash check said these two commute,
  the footprint rule above would be wrong.
* **8 vs 5**: 8 writes al.3's tail; 5 inserts nodes *after* al.3 → no overlap, commute.

**Step 3/5 — Apply & consolidate** `[8, {6,9}, 10, 11, 12, 13, 14, 15←{16, {17,18}}]`:

```
al.3   « Art. L. 250‑10. – … ils peuvent enjoindre à l'intéressé de se conformer à ses obligations
        dans un délai qu'ils fixent. » ;
al.10  « Art. L. 251‑22. – Est puni … en application du II de l'article L. 201‑4. »
art.2–6  (Supprimés)          ← delete_node keeps the id and leaves a "(Supprimé)" placeholder
titre  « instaurant des réponses adaptées et proportionnées pour prévenir notamment le
         développement des vignes non cultivées »
```

Provenance (git blame): al.3 tokens « de se conformer … qu'ils fixent » → amendment 8; title tokens
« instaurant des réponses » → 16, « notamment » → 17 (and 18); rest of title → 15.

---

## Example 6 — Séance end‑to‑end: consolidation vs. T.A. n° 62

Word‑diff of the Example 5 output against [AN‑TA] `PIONANR5L17BTA0062`:

| Unit | Exact? | Remaining diff (predicted → official) | Bin |
|---|---|---|---|
| Article 1er | no | al.6 « b) Il est ajouté 3° » → « Il est ajouté **un** 3° »; al.7 « injonction **prise** en application » → « injonction **adressée** en application » | non‑amendment change (no séance amendment touches al.6/al.7; the 18 numbers in [DB‑A] are contiguous, so none is missing) |
| Articles 2–6 | **yes** | « Articles 2 à 6 (nouveaux) (Supprimés) » | — |
| Titre | **yes** | — | — |

Per‑amendment application (articles with exactly one adopted amendment): articles 2–6 (10–14)
→ 5/5 exact. Article 1er has three adopted amendments (8, 6, 9), so it is excluded from that
metric and scored only end-to-end.

---

## Summary of what these examples exercise

| Design‑doc item | Where | Data that shows it |
|---|---|---|
| `rewrite` → ancestor conflict → fallen | Ex. 1 | [DB‑A] `sort = 'Tombé'` CE4, CE5, CE10 |
| Sub‑amendment applied to parent ops; alinéa counting includes chapeau | Ex. 2, 5 | [DB‑A] `amendement_parent_ref`; [AN‑PDF] CE11 |
| Near‑duplicate across levels (CE5 ≈ CE12) | Ex. 2 | [DB‑A] dispositifs |
| `insert_node` article + derived renumber | Ex. 3 | [DB‑T] `BTC1003.numero` |
| Bill → code nested operation | Ex. 3 | [DB‑A] CE1, [AN‑TC] art. 2, [LEGI] |
| "En conséquence … même alinéa" | Ex. 1 (CE10), Ex. 5 (1) | [DB‑A] |
| Content anchor must use the latest version's wording | Ex. 5 (6/9) | [AN‑TC] vs CE12 |
| Identical series → one effect, applied once | Ex. 5 | [DB‑A] `id_discussion_identique` 75790, 75886 |
| Commutation check (16/17) and a real conflict (1/6) | Ex. 5 | — |
| `delete_node` with placeholder | Ex. 5, 6 | [AN‑TA] « Articles 2 à 6 (Supprimés) » |
| Non‑amendment change bin | Ex. 3, 4, 6 | [AN‑TC], [AN‑TA] |
| Metadata (`alinea`) disagreeing with dispositif | Ex. 5 (1) | [DB‑A] |

Not covered by this bill: ambiguous occurrences (every anchor here is unique), rectified
amendments (no `(Rect)` among the applied ones; séance 3 is `3 (Rect)` but Rejeté), discussions
communes, and the Senate shuttle (Sénat texts n° 647 / T.A. 134 are linked from the dossier page).
A finance‑bill example would be needed for those.
