// The one module that touches the page's own address and the browser's memory (S-255): T-174, that
// parse and format round-trip every view's kept selections, an address naming an unknown view opens
// spine keeping the rest, opening reads the stored project and the kept filter, keep replaces the
// address in place once per change, remember writes the project key, and faults are silent.
//
// The modules under test are imported inside each test, so a module not yet written fails only the
// tests that need it.

import { afterEach, beforeEach, expect, test } from "bun:test";

/** The page's own address with no kept part. */
const H = "https://node.example/mu/dash/index.html";

type Globals = { location: unknown; history: unknown; localStorage: unknown };
const g = globalThis as unknown as Globals;
let saved: Globals;

let replaced: { state: unknown; title: string; url: string }[];
let pushed: unknown[][];
const historyState = { kept: "by the browser" };

/** A localStorage stub over a plain map. */
function storage(): Storage {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => (m.has(k) ? (m.get(k) as string) : null),
    setItem: (k: string, v: string) => void m.set(k, String(v)),
    removeItem: (k: string) => void m.delete(k),
    clear: () => m.clear(),
    key: (i: number) => [...m.keys()][i] ?? null,
    get length() {
      return m.size;
    },
  } as Storage;
}

beforeEach(() => {
  saved = { location: g.location, history: g.history, localStorage: g.localStorage };
  replaced = [];
  pushed = [];
  g.location = { href: H };
  g.history = {
    state: historyState,
    replaceState(state: unknown, title: string, url: string) {
      replaced.push({ state, title, url });
      (g.location as { href: string }).href = url;
    },
    pushState(...args: unknown[]) {
      pushed.push(args);
    },
  };
  g.localStorage = storage();
});

afterEach(() => {
  g.location = saved.location;
  g.history = saved.history;
  g.localStorage = saved.localStorage;
});

async function local() {
  const { initialLocal } = await import("../src/views.ts");
  return initialLocal({ find: "needle", jump: "S-17 (was S-12)", file: "mu_spec/x.py", list: "issues" });
}

/** The kept selections each view keeps, under L. */
const KEPT_BY_VIEW: Record<string, Record<string, string>> = {
  spine: { entry: "B-90 (was B-03)" },
  read: { find: "needle", jump: "S-17 (was S-12)", layer: "A" },
  modules: { file: "mu_spec/x.py" },
  queues: { list: "issues" },
};
const FILTER_BY_VIEW: Record<string, string> = { spine: "B-90 (was B-03)", read: "A" };

test("parse and format round-trip every view's kept selections", async () => {
  const { parse, format } = await import("../src/address.ts");
  const { VIEW_NAMES } = await import("../src/views.ts");
  const L = await local();

  // 1. every view and the selections it keeps come back
  for (const v of VIEW_NAMES) {
    const a = format({ project: "p", view: v, filter: FILTER_BY_VIEW[v] ?? null }, L);
    const back = parse(a);
    expect([v, back.view]).toEqual([v, v]);
    expect([v, back.kept]).toMatchObject([v, KEPT_BY_VIEW[v] ?? {}]);
    // the page's own origin and path are unchanged
    const u = new URL(a, H);
    expect([v, u.origin, u.pathname]).toEqual([v, new URL(H).origin, new URL(H).pathname]);
  }

  // 2. an address naming no view opens spine with nothing kept
  expect(parse(H)).toEqual({ view: "spine", kept: {} });
});

test("an unknown view written by format opens spine keeping the rest", async () => {
  const { parse, format } = await import("../src/address.ts");
  const L = await local();

  // 3.
  const a = format({ project: "p", view: "nosuchview", filter: null }, L);
  const back = parse(a);
  expect(back.view).toBe("spine");
  expect(back.kept.file).toBe("mu_spec/x.py");
  expect(back.kept.list).toBe("issues");
});

test("opening reads the stored project, silently", async () => {
  const { opening, PROJECT_KEY } = await import("../src/address.ts");

  // 4. nothing stored
  expect(opening().choice.project).toBeNull();

  // storage that cannot be read reads as nothing remembered, and nothing is thrown
  g.localStorage = {
    ...storage(),
    getItem: () => {
      throw new Error("SecurityError");
    },
  };
  let o: ReturnType<typeof opening> | undefined;
  expect(() => {
    o = opening();
  }).not.toThrow();
  expect(o?.choice.project).toBeNull();

  // a remembered project
  g.localStorage = storage();
  (g.localStorage as Storage).setItem(PROJECT_KEY, "q");
  expect(opening().choice.project).toBe("q");
});

test("opening reads the kept filter", async () => {
  const { opening, format } = await import("../src/address.ts");
  const L = await local();
  const loc = g.location as { href: string };

  // 5. read: the kept layer, else behaviour
  loc.href = format({ project: "p", view: "read", filter: "A" }, L);
  expect(opening().choice.view).toBe("read");
  expect(opening().choice.filter).toBe("A");

  loc.href = format({ project: "p", view: "read", filter: null }, L);
  expect(opening().choice.filter).toBe("B");

  // spine: the kept entry
  loc.href = format({ project: "p", view: "spine", filter: "B-90 (was B-03)" }, L);
  expect(opening().choice.view).toBe("spine");
  expect(opening().choice.filter).toBe("B-90 (was B-03)");

  // every other view keeps no filter
  loc.href = format({ project: "p", view: "modules", filter: null }, L);
  expect(opening().choice.view).toBe("modules");
  expect(opening().choice.filter).toBeNull();
  expect(opening().kept.file).toBe("mu_spec/x.py");
});

test("keep replaces the address in place once per change", async () => {
  const { keep } = await import("../src/address.ts");
  const L = await local();
  const c2 = { project: "p", view: "waves", filter: null };

  // 6.
  keep(c2, L);
  keep(c2, L);
  expect(pushed).toEqual([]);
  expect(replaced.length).toBe(1);
  expect(replaced[0].state).toBe(historyState);
  const u = new URL(replaced[0].url, H);
  expect(u.origin).toBe(new URL(H).origin);
  expect(u.pathname).toBe(new URL(H).pathname);
});

test("remember writes the project key, silently", async () => {
  const { remember, PROJECT_KEY } = await import("../src/address.ts");

  // 7.
  remember("q");
  expect((g.localStorage as Storage).getItem(PROJECT_KEY)).toBe("q");

  g.localStorage = {
    ...storage(),
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
  };
  expect(() => remember("q")).not.toThrow();
});
