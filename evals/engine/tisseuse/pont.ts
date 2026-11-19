// Applique à chaque étape les amendements adoptés, dans l'ordre reçu, avec la chaîne Tisseuse + leximpact-legi-ui :
// parseAmendment (Tisseuse) puis projectAmendmentOnPjlArticle (legi-ui). Leur projecteur traite un amendement sur un
// article ; l'enchaînement est le nôtre : le texte produit par un amendement est l'entrée du suivant.
//
// Usage : node dist/pont.mjs entree.jsonl sortie.jsonl
// Entrée, une étape par ligne : {id, articles: [{lignes, supprime}], amendements: [{uid, numero, article (index),
//   position ("on_article" | "after_article" | "before_article"), alinea, alinea_libelle, dispositif}]}
// Sortie : {id, articles: [{depart (index d'entrée ou null), lignes, supprime, origines}], statuts: {uid: {etat, detail}}}
import { createReadStream, createWriteStream } from "node:fs"
import { createInterface } from "node:readline"

import { parseAmendment } from "@tricoteuses/tisseuse/server"
import { projectAmendmentOnPjlArticle } from "legi-ui/src/lib/pjl/amendment_projection.ts"

type Article = { depart: number | null; lignes: string[]; supprime: boolean; origines: string[]; ancre?: number }

const echapper = (texte: string) => texte.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
const dechapper = (texte: string) =>
  texte
    .replace(/&(?:nbsp|#160);/giu, " ")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&quot;", '"')
    .replaceAll("&#39;", "'")
    .replaceAll("&amp;", "&")

const SUPPRIME = "(Supprimé)"
const enHtml = (lignes: string[]) => lignes.map((ligne) => `<p>${echapper(ligne)}</p>`).join("\n")

/** Le dispositif du corpus est du texte aux alinéas séparés par une ligne vide ; legi-ui donne du HTML à Tisseuse. */
function dispositifHtml(dispositif: string): string {
  if (/<p\b/iu.test(dispositif)) return dispositif
  return enHtml(dispositif.split(/\n\s*\n/gu).map((alinea) => alinea.trim()).filter(Boolean))
}

/** HTML rendu par le projecteur → alinéas du nouveau texte : sans les passages barrés ni les alinéas remplacés. */
function lignesProjetees(html: string): string[] {
  const propre = html
    .replace(/<style\b[\s\S]*?<\/style>/giu, "")
    // Réécriture de l'article entier : un seul bloc, un alinéa par ligne.
    .replace(/<div\b[^>]*\bpjl-amendment-rewrite\b[^>]*>([\s\S]*?)<\/div>/giu, (_, texte: string) =>
      texte.split(/\n/gu).map((ligne) => `<p>${ligne}</p>`).join(""))
    .replace(/<span\b(?=[^>]*\bpjl-amendment-deletion\b)[^>]*>[\s\S]*?<\/span>/giu, "")
  const lignes: string[] = []
  for (const bloc of propre.matchAll(/<(p|li)\b([^>]*)>([\s\S]*?)<\/\1>/giu)) {
    if (/\bpjl-amendment-replaced-alinea\b/u.test(bloc[2])) continue
    const texte = dechapper(bloc[3].replace(/<[^>]+>/gu, "")).replace(/[ \t\r\n]+/gu, " ").trim()
    if (texte) lignes.push(texte)
  }
  return lignes
}

function traiter(etape: any) {
  const articles: Article[] = etape.articles.map((a: any, i: number) => ({
    depart: i, lignes: a.lignes, supprime: a.supprime, origines: [],
  }))
  const statuts: Record<string, { etat: string; detail: string }> = {}
  for (const a of etape.amendements) {
    let analyse
    try {
      analyse = parseAmendment({
        sourceFormat: "assemblee-amendement",
        amendmentId: a.uid,
        amendmentNumber: a.numero,
        targetArticleLabel: a.article_libelle,
        targetPosition: a.position,
        targetAlinea: a.alinea,
        targetAlineaLabel: a.alinea_libelle,
        dispositifHtml: dispositifHtml(a.dispositif ?? ""),
      })
    } catch (erreur) {
      statuts[a.uid] = { etat: "non_analyse", detail: `exception de l'analyseur : ${String(erreur).slice(0, 120)}` }
      continue
    }
    if (analyse.directives.length === 0) {
      statuts[a.uid] = { etat: "non_analyse", detail: analyse.diagnostics[0]?.message ?? "aucune directive" }
      continue
    }
    const rang = articles.findIndex((article) => article.depart === a.article)
    const cible = articles[rang]
    if (a.position !== "on_article") {
      // Leur projecteur affiche l'article additionnel sous l'article voisin ; on en fait un article du texte.
      const texte = analyse.directives.map((d) => d.insertText ?? "").join("\n")
      let place = a.position === "before_article" ? rang : rang + 1
      if (a.position === "after_article") {
        while (place < articles.length && articles[place].depart === null && articles[place].ancre === a.article) place++
      }
      const nouveau: Article = { depart: null, lignes: texte.split(/\n/gu).map((l) => l.trim()).filter(Boolean), supprime: false,
                        origines: [a.uid], ancre: a.article }
      articles.splice(place, 0, nouveau)
      statuts[a.uid] = { etat: "applique", detail: "" }
      continue
    }
    if (analyse.directives.some((d) => d.kind === "delete_article")) {
      cible.lignes = []
      cible.supprime = true
      cible.origines.push(a.uid)
      statuts[a.uid] = { etat: "applique", detail: "" }
      continue
    }
    let resultat
    try {
      // Un article supprimé n'a plus d'alinéa : on montre la mention, comme le texte officiel, pour qu'un amendement
      // puisse le rétablir.
      resultat = projectAmendmentOnPjlArticle(enHtml(cible.lignes.length ? cible.lignes : [SUPPRIME]), analyse)
    } catch (erreur) {
      statuts[a.uid] = { etat: "pendant", detail: `exception du projecteur : ${String(erreur).slice(0, 120)}` }
      continue
    }
    if (resultat.status !== "projected") {
      statuts[a.uid] = { etat: "pendant", detail: resultat.reason.slice(0, 160) }
      continue
    }
    const lignes = lignesProjetees(resultat.html)
    cible.lignes = lignes.length === 1 && lignes[0] === SUPPRIME ? [] : lignes
    cible.supprime = cible.lignes.length === 0
    cible.origines.push(a.uid)
    const partiel = resultat.appliedDirectiveCount < analyse.directives.length
    statuts[a.uid] = {
      etat: "applique",
      detail: partiel ? `partiel : ${resultat.appliedDirectiveCount}/${analyse.directives.length} directives` : "",
    }
  }
  return { id: etape.id, articles: articles.map(({ depart, lignes, supprime, origines }) => ({ depart, lignes, supprime, origines })), statuts }
}

const [entree, sortie] = process.argv.slice(2)
const ecriture = createWriteStream(sortie, { encoding: "utf-8" })
for await (const ligne of createInterface({ input: createReadStream(entree, { encoding: "utf-8" }) })) {
  if (ligne.trim()) ecriture.write(JSON.stringify(traiter(JSON.parse(ligne))) + "\n")
}
ecriture.end()
