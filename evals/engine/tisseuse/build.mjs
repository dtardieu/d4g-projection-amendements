// Assemble pont.ts et le projecteur d'amendements de leximpact-legi-ui (lu dans le clone, jamais copié) en un seul
// fichier. Le clone n'est pas installé (il exige Node 26) : on résout ses imports internes ici.
import { build } from "esbuild"
import { existsSync } from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"

const ici = path.dirname(fileURLToPath(import.meta.url))
const legiUi = path.resolve(process.env.LEGI_UI ?? path.join(ici, "../../../../../other_srcs/leximpact-legi-ui"))
if (!existsSync(path.join(legiUi, "src/lib/pjl/amendment_projection.ts"))) {
  throw new Error(`clone de leximpact-legi-ui introuvable : ${legiUi} (variable LEGI_UI)`)
}

await build({
  entryPoints: [path.join(ici, process.env.ENTREE ?? "pont.ts")],
  outfile: path.join(ici, "dist", (process.env.ENTREE ?? "pont.ts").replace(/\.ts$/, ".mjs")),
  bundle: true,
  platform: "node",
  format: "esm",
  logLevel: "warning",
  plugins: [{
    name: "legi-ui",
    setup(b) {
      b.onResolve({ filter: /^legi-ui\// }, (a) => ({ path: path.join(legiUi, a.path.slice("legi-ui/".length)) }))
      // Les messages de l'interface sont générés au build de legi-ui : ici, le nom du message suffit.
      b.onResolve({ filter: /^#lib\/paraglide\/messages/ }, () => ({ path: "messages", namespace: "bouchon" }))
      b.onResolve({ filter: /^#lib\// }, (a) => ({
        path: path.join(legiUi, "src/lib", a.path.slice("#lib/".length).replace(/\.js$/, ".ts")),
      }))
      b.onLoad({ filter: /.*/, namespace: "bouchon" }, () => ({
        contents: "export const m = new Proxy({}, { get: (_, nom) => () => String(nom) })",
      }))
      // Tisseuse reste un paquet : Node le trouve dans le node_modules de ce dossier.
      b.onResolve({ filter: /^@tricoteuses\// }, (a) => ({ path: a.path, external: true }))
    },
  }],
})
