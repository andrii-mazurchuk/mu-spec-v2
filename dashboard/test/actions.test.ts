// The page's person-only action names and routes (S-219): T-182, the names and relative routes, and
// T-208, that only the action tracker reads the routes and classifies an answer.
//
// The module under test is imported inside each test, so a module not yet written fails only the
// tests that need it.

import { expect, test } from "bun:test";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, posix, relative } from "node:path";

const DASHBOARD = join(import.meta.dir, "..");
const SRC = join(DASHBOARD, "src");
const ACTIONS = "src/actions.ts";
const OUTCOME = "src/outcome.ts";
const TRACKER = "src/tracker.ts";

const EXPECTED_NAMES = [
  "ratify", "reject", "cut_units", "set_repo", "set_labels", "emit_tickets", "set_delegation",
];

test("action names and relative routes", async () => {
  const mod = await import("../src/actions.ts");
  const names: readonly string[] = mod.ACTION_NAMES;
  const routes: Record<string, { method: string; path: (project: string) => string }> = mod.ACTION_ROUTES;

  expect(names.length).toBe(7);
  expect(new Set(names).size).toBe(7);
  expect([...names].sort()).toEqual([...EXPECTED_NAMES].sort());
  expect(names).not.toContain("rollback_tickets");

  expect(Object.keys(routes).sort()).toEqual([...names].sort());

  const paths: string[] = [];
  for (const a of names) {
    expect(routes[a].method).toBe("POST");
    const p = routes[a].path("zz9");
    expect(typeof p).toBe("string");
    expect(p.startsWith("/")).toBe(false);
    expect(p).not.toMatch(/^[A-Za-z][A-Za-z0-9+.-]*:/);
    expect(p).toContain("zz9");
    paths.push(p);
  }
  expect(new Set(paths).size).toBe(7);

  expect(Object.keys(mod).sort()).toEqual(["ACTION_NAMES", "ACTION_ROUTES"]);
});

// ---- the import graph of dashboard/src, from its import and export-from declarations

type Edge = {
  from: string; // the importing file, relative to dashboard/ and POSIX
  target: string; // the specifier resolved relative to the importing file, the same form
  names: string[]; // the imported bindings by their exported name; "*" for a namespace or export *
  reexport: boolean; // an export-from declaration
};

function tsFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) out.push(...tsFiles(path));
    else if (entry.endsWith(".ts")) out.push(path);
  }
  return out.sort();
}

/** The source with its comments blanked, string and template literals kept. */
function stripComments(text: string): string {
  let out = "";
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    const next = text[i + 1];
    if (c === "/" && next === "/") {
      while (i < text.length && text[i] !== "\n") i++;
    } else if (c === "/" && next === "*") {
      const end = text.indexOf("*/", i + 2);
      i = end < 0 ? text.length : end + 2;
      out += " ";
    } else if (c === '"' || c === "'" || c === "`") {
      let j = i + 1;
      while (j < text.length && text[j] !== c) j += text[j] === "\\" ? 2 : 1;
      out += text.slice(i, j + 1);
      i = j + 1;
    } else {
      out += c;
      i++;
    }
  }
  return out;
}

function resolveSpecifier(fromRel: string, spec: string): string {
  if (!spec.startsWith(".")) return spec;
  let target = posix.normalize(posix.join(posix.dirname(fromRel), spec));
  if (target.endsWith(".js")) target = target.slice(0, -3) + ".ts";
  else if (!target.endsWith(".ts")) target += ".ts";
  return target;
}

/** The exported names a clause like `X, { a as b, type c }` or `* as ns` takes. */
function clauseNames(clause: string): string[] {
  const names: string[] = [];
  let rest = clause.trim();
  const braced = rest.match(/\{([^}]*)\}/);
  if (braced) {
    for (const part of braced[1].split(",")) {
      const item = part.trim().replace(/^type\s+/, "");
      if (item) names.push(item.split(/\s+as\s+/)[0].trim());
    }
    rest = rest.replace(braced[0], "");
  }
  for (const part of rest.split(",")) {
    const item = part.trim();
    if (!item) continue;
    if (item.startsWith("*")) names.push("*");
    else names.push("default");
  }
  return names;
}

function importGraph(root: string): Edge[] {
  const edges: Edge[] = [];
  const declaration =
    /(?:^|[;\s}])(import|export)\s+(?:type\s+)?([\w$\s,{}*]*?)\s*from\s*(["'])([^"']+)\3|(?:^|[;\s}])import\s*(["'])([^"']+)\5/g;
  for (const path of tsFiles(join(root, "src"))) {
    const from = relative(root, path).split("\\").join("/");
    const text = stripComments(readFileSync(path, "utf-8"));
    for (const m of text.matchAll(declaration)) {
      if (m[6] !== undefined) {
        edges.push({ from, target: resolveSpecifier(from, m[6]), names: [], reexport: false });
      } else {
        edges.push({
          from,
          target: resolveSpecifier(from, m[4]),
          names: clauseNames(m[2]),
          reexport: m[1] === "export",
        });
      }
    }
  }
  return edges;
}

function importers(edges: Edge[], target: string, binding: string): Set<string> {
  return new Set(
    edges
      .filter((e) => e.target === target && (e.names.includes(binding) || e.names.includes("*")))
      .map((e) => e.from),
  );
}

test("routes and classification are the tracker's alone", () => {
  expect(existsSync(join(DASHBOARD, TRACKER))).toBe(true);
  const edges = importGraph(DASHBOARD);

  const routeImporters = importers(edges, ACTIONS, "ACTION_ROUTES");
  const classifyImporters = importers(edges, OUTCOME, "classifyAction");
  // precondition: the tracker imports both, so an empty sweep cannot pass
  expect(routeImporters.has(TRACKER)).toBe(true);
  expect(classifyImporters.has(TRACKER)).toBe(true);

  expect([...routeImporters].sort()).toEqual([TRACKER]);
  expect([...classifyImporters].sort()).toEqual([TRACKER]);

  const reexports = edges
    .filter((e) => e.reexport)
    .filter(
      (e) =>
        e.names.includes("ACTION_ROUTES") ||
        e.names.includes("classifyAction") ||
        (e.names.includes("*") && (e.target === ACTIONS || e.target === OUTCOME)),
    )
    .map((e) => e.from);
  expect(reexports).toEqual([]);

  expect(edges.filter((e) => e.from === ACTIONS)).toEqual([]);
});
