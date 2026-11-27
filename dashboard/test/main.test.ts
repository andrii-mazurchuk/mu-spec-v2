// dashboard/src/main.ts (S-251): the bundle entry, the one module importing both the page's start
// and the action wiring. T-190, that its one statement is start(document.body, wireActions), that no
// page module imports an action module, and that no action module imports the page's start or the
// entry.
//
// The modules under test are imported inside the test, so a module not yet written fails only the
// test that needs it. Sources are parsed with the TypeScript parser (typescript 5, the last line
// with a JavaScript compiler API), run in a child bun since bun test auto-installs nothing.

import { expect, mock, test } from "bun:test";
import { existsSync, readdirSync, statSync } from "node:fs";
import { join, posix, relative } from "node:path";

const DASHBOARD = join(import.meta.dir, "..");
const SRC = join(DASHBOARD, "src");
const MAIN = "src/main.ts";
const PAGE = "src/page.ts";
const ACTION_FILES = [
  "src/actions.ts", "src/outcome.ts", "src/effects.ts", "src/tracker.ts", "src/actionwire.ts",
];

type Statement = { kind: string; spec: string | null; callee: string | null; argc: number | null };

/** Each file's top-level statements, parsed by typescript 5 in a child bun (bun test installs
 * nothing): its syntax kind, a module specifier, and for an expression statement that is a call,
 * the callee identifier and the argument count. */
const PARSE = `
const ts = (await import('typescript@5')).default;
const { readFileSync } = await import('node:fs');
const out = {};
for (const path of process.argv.slice(1)) {
  const sf = ts.createSourceFile(path, readFileSync(path, 'utf-8'), ts.ScriptTarget.Latest, true);
  out[path] = sf.statements.map((s) => {
    const m = s.moduleSpecifier;
    const e = s.kind === ts.SyntaxKind.ExpressionStatement ? s.expression : null;
    const call = e && ts.isCallExpression(e) ? e : null;
    return {
      kind: ts.SyntaxKind[s.kind],
      spec: m && ts.isStringLiteral(m) ? m.text : null,
      callee: call ? (ts.isIdentifier(call.expression) ? call.expression.text : '') : null,
      argc: call ? call.arguments.length : null,
    };
  });
}
console.log(JSON.stringify(out));
`;

function parse(paths: string[]): Record<string, Statement[]> {
  // one line, no double quotes: the script passes through a Windows command line intact
  const r = Bun.spawnSync([process.execPath, "-e", PARSE.split("\n").join(" "), ...paths], { cwd: DASHBOARD });
  // stderr only on failure: a first run's install may report on it
  if (r.exitCode !== 0) expect([r.exitCode, r.stderr.toString()]).toEqual([0, ""]);
  return JSON.parse(r.stdout.toString());
}

function tsFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) out.push(...tsFiles(path));
    else if (entry.endsWith(".ts")) out.push(path);
  }
  return out.sort();
}

function resolveSpecifier(fromRel: string, spec: string): string {
  if (!spec.startsWith(".")) return spec;
  let target = posix.normalize(posix.join(posix.dirname(fromRel), spec));
  if (target.endsWith(".js")) target = target.slice(0, -3) + ".ts";
  else if (!target.endsWith(".ts")) target += ".ts";
  return target;
}

/** Every (importing file, resolved target) under dashboard/src, from import and export-from
 * declarations, type-only ones included; both relative to dashboard/ and POSIX. */
function importGraph(): { from: string; target: string }[] {
  const edges: { from: string; target: string }[] = [];
  for (const [path, statements] of Object.entries(parse(tsFiles(SRC)))) {
    const from = relative(DASHBOARD, path).split("\\").join("/");
    for (const s of statements) {
      if ((s.kind === "ImportDeclaration" || s.kind === "ExportDeclaration") && s.spec !== null) {
        edges.push({ from, target: resolveSpecifier(from, s.spec) });
      }
    }
  }
  return edges;
}

test("the bundle entry joins the two once", async () => {
  // 1. no page module imports an action module, and no action module imports the page or the entry
  const edges = importGraph();
  const intoActions = edges
    .filter((e) => ACTION_FILES.includes(e.target))
    .filter((e) => !ACTION_FILES.includes(e.from) && e.from !== MAIN)
    .map((e) => `${e.from} -> ${e.target}`);
  expect(intoActions).toEqual([]);
  const outOfActions = edges
    .filter((e) => ACTION_FILES.includes(e.from))
    .filter((e) => e.target === PAGE || e.target === MAIN)
    .map((e) => `${e.from} -> ${e.target}`);
  expect(outOfActions).toEqual([]);

  // 2. main.ts is import declarations and one statement: start called with two arguments
  const mainPath = join(SRC, "main.ts");
  expect(existsSync(mainPath)).toBe(true);
  const others = parse([mainPath])[mainPath].filter((s) => s.kind !== "ImportDeclaration");
  expect(others).toEqual([{ kind: "ExpressionStatement", spec: null, callee: "start", argc: 2 }]);

  // 3. loading the entry, with start a recorder, records one call: (document.body, wireActions)
  const { wireActions } = await import("../src/actionwire.ts");
  const calls: unknown[][] = [];
  mock.module("../src/page.ts", () => ({
    start: (...args: unknown[]) => {
      calls.push(args);
    },
  }));
  const g = globalThis as any;
  const saved = g.document;
  const B = {};
  g.document = { body: B };
  try {
    await import("../src/main.ts");
  } finally {
    g.document = saved;
  }
  expect(calls.length).toBe(1);
  expect(calls[0][0]).toBe(B);
  expect(calls[0][1]).toBe(wireActions);
}, 120000);
