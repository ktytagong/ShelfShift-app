// The words a web source can put on screen, for the honesty scan (tests/web/test_honesty_web.py; 06.1 D-03, D-61).
// Reads each .ts/.tsx path given on the command line with the TypeScript parser from web/node_modules (no install)
// and prints JSON: [{file, line, kind, text}], one entry per
// - string literal or template literal ("string"; each `${…}` becomes "{}"), and
// - JSX text node ("jsx") or visible JSX attribute string (aria-label, title, placeholder, alt, aria-description).
// Comments, import and export paths, type-level literals ("manager" | "hq"), object keys and non-visible JSX
// attributes (className, data-*, href, id, role, …) are not on screen and are left out.
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.join(here, "..", "..", "web", "package.json"));
const ts = require("typescript");

const VISIBLE_ATTRS = new Set(["aria-label", "title", "placeholder", "alt", "aria-description", "aria-valuetext"]);

function visit(sf, node, out) {
  const k = ts.SyntaxKind;
  const p = node.parent;
  const line = () => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;
  if (node.kind === k.StringLiteral || node.kind === k.NoSubstitutionTemplateLiteral) {
    const skip = (p && (p.kind === k.ImportDeclaration || p.kind === k.ExportDeclaration || p.kind === k.LiteralType
      || p.kind === k.ExternalModuleReference || p.kind === k.ImportType
      || (p.kind === k.PropertyAssignment && p.name === node) || (p.kind === k.PropertySignature && p.name === node)
      || (p.kind === k.CallExpression && p.expression.kind === k.ImportKeyword)))
      || (p && p.kind === k.JsxAttribute && !VISIBLE_ATTRS.has(p.name.getText(sf)));
    if (!skip) out.push({ line: line(), kind: "string", text: node.text });
  } else if (node.kind === k.TemplateExpression) {
    const text = node.head.text + node.templateSpans.map((s) => "{}" + s.literal.text).join("");
    out.push({ line: line(), kind: "string", text });
  } else if (node.kind === k.JsxText) {
    const text = node.text.replace(/\s+/g, " ").trim();
    if (text !== "") out.push({ line: line(), kind: "jsx", text });
  }
  ts.forEachChild(node, (c) => visit(sf, c, out));
}

const found = [];
for (const file of process.argv.slice(2)) {
  const src = readFileSync(file, "utf8");
  const kind = file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(file, src, ts.ScriptTarget.Latest, true, kind);
  const out = [];
  visit(sf, sf, out);
  for (const o of out) found.push({ file, ...o });
}
process.stdout.write(JSON.stringify(found));
