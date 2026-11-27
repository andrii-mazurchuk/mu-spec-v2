// dashboard/src/effects.ts (S-479): each action's effect reads, the views that draw each control,
// the proposal target re-exported from the action port, carriesEffect and shows. T-454, the effect
// reads and the per-view rules; T-475, that shows asks only whether the state shows what was pressed;
// T-458, that the recorded fixtures are whole; T-459, that a proposal target is the port's one text;
// T-460 and T-461, ratify and reject judged on the recorded proposal and spine reads.
//
// The modules under test are imported inside each test, so a module not yet written fails only the
// tests that need it. HEAD and BUNDLE are read from mu_spec.dash.views, and the recorded reads from
// fixtures/effects.json, written by tests/record_effect_fixtures.py from the running unit; never copied.
// A unit is read as cut when its key is in the units read's cut, and as out when the emission read's
// 'already' holds it, as today's Ship screen reads them.

import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

type Fields = { [read: string]: unknown };
type Parts = { head: Fields | null; rest: Fields | null; run: { id: string } | null };
type Mapping = { [slice: string]: string[] };
type Proposal = { status: string; proposal: Mapping; note: string; last_rejection: unknown };

const ROOT = join(import.meta.dir, "..", "..");
const STATED = '[[["a",[]],["b",["S-10 (was S-01)","S-17 (was S-02)"]]],null]';
const FIXTURE_KEYS = [
  "repo_on", "repo_om", "SET", "prop_before", "prop_ratified", "spine_ratified", "prop_rejected",
  "units_before", "units_cut", "emission_before",
];

/** S-476's HEAD and the names of its BUNDLE, read from mu_spec.dash.views. */
function reads(): { HEAD: Record<string, string[]>; BUNDLE: string[] } {
  const code = "import json; from mu_spec.dash.views import BUNDLE, HEAD; "
    + "print(json.dumps({'HEAD': {k: list(v) for k, v in HEAD.items()}, 'BUNDLE': list(BUNDLE)}))";
  const r = Bun.spawnSync(["python", "-c", code], { cwd: ROOT });
  expect([r.exitCode, r.stderr.toString()]).toEqual([0, ""]);
  return JSON.parse(r.stdout.toString());
}

/** The parsed recorded fixtures. */
function recorded(): any {
  return JSON.parse(readFileSync(join(ROOT, "dashboard", "test", "fixtures", "effects.json"), "utf-8"));
}

/** A part holding each name with value null, or null when names is null. */
function nulls(names: readonly string[] | null): Fields | null {
  if (names === null) return null;
  const f: Fields = {};
  for (const n of names) f[n] = null;
  return f;
}

function parts(headNames: readonly string[] | null, restNames: readonly string[] | null, run: { id: string } | null): Parts {
  return { head: nulls(headNames), rest: nulls(restNames), run };
}

/** The keys a units read shows as cut. */
function cutKeys(units: any): Set<string> {
  return new Set(((units?.cut?.units ?? []) as any[]).map((u) => u.key as string));
}

/** The keys an emission read shows as out ('already'). */
function outKeys(emission: any): Set<string> {
  const already = (emission?.already ?? {}) as Record<string, unknown>;
  return new Set(Object.keys(already).filter((k) => already[k]));
}

const asSet = (xs: Iterable<string>) => [...new Set(xs)].sort();

async function effects() {
  const mod = await import("../src/effects.ts");
  return {
    mod,
    carriesEffect: mod.carriesEffect as (a: string, p: Parts) => boolean,
    shows: mod.shows as (a: string, t: unknown, p: Parts) => boolean,
    proposalTarget: mod.proposalTarget as (m: Mapping, note: string | null) => string,
  };
}

/** The ratify and reject fixtures and the mappings built from mP, the waiting mapping shown back. */
function proposals() {
  const F = recorded();
  const prop_before = F.prop_before as Proposal;
  const prop_ratified = F.prop_ratified as Proposal;
  const prop_rejected = F.prop_rejected as Proposal;
  const mP = prop_before.proposal;
  const s0 = Object.keys(mP).sort().find((s) => mP[s].length > 0);
  expect(s0).toBeDefined();
  const mX: Mapping = { ...mP, [s0!]: [...mP[s0!], "zz-no-entry"] };
  return { F, prop_before, prop_ratified, prop_rejected, spine_ratified: F.spine_ratified, mP, s0: s0!, mX };
}

test("carrying the effect reads and the per-view rules", async () => {
  const { carriesEffect, mod } = await effects();
  const EFFECT_READS = mod.EFFECT_READS as Record<string, readonly string[]>;
  const CONTROL_VIEWS = mod.CONTROL_VIEWS as Record<string, readonly string[]>;
  const { DRAWN_CONTROLS } = await import("../src/actionport.ts");
  const drawn = DRAWN_CONTROLS as Record<string, readonly string[]>;
  const { ACTION_NAMES } = await import("../src/actions.ts");
  const { HEAD, BUNDLE } = reads();

  // preconditions
  expect(HEAD.spine.includes("repo")).toBe(false);
  expect(HEAD.ship.includes("emission")).toBe(true);
  expect(HEAD.queues.includes("proposal")).toBe(true);
  expect(HEAD.queues.includes("spine")).toBe(true);

  expect(carriesEffect("set_repo", parts(HEAD.spine, null, null))).toBe(false);
  expect(carriesEffect("cut_units", parts(HEAD.order, null, null))).toBe(true);
  expect(carriesEffect("emit_tickets", parts(HEAD.ship, null, null))).toBe(true);
  expect(carriesEffect("set_repo", parts(null, ["repo"], null))).toBe(true);
  expect(carriesEffect("emit_tickets", parts(["units"], null, { id: "n:1" }))).toBe(false);
  expect(carriesEffect("ratify", parts(["proposal"], null, null))).toBe(false);
  expect(carriesEffect("ratify", parts(["proposal"], ["spine"], null))).toBe(true);
  expect(carriesEffect("ratify", parts(HEAD.queues, null, null))).toBe(true);
  expect(carriesEffect("reject", parts(["proposal"], null, null))).toBe(true);

  // the static rules
  expect(asSet(EFFECT_READS.ratify)).toEqual(["proposal", "spine"]);
  expect(asSet(Object.keys(EFFECT_READS))).toEqual(asSet(ACTION_NAMES));
  expect(asSet(Object.keys(CONTROL_VIEWS))).toEqual(asSet(ACTION_NAMES));
  for (const a of Object.keys(CONTROL_VIEWS)) {
    for (const v of CONTROL_VIEWS[a]) {
      for (const n of EFFECT_READS[a]) expect([a, v, n, HEAD[v]?.includes(n)]).toEqual([a, v, n, true]);
    }
  }
  for (const a of Object.keys(EFFECT_READS)) {
    for (const n of EFFECT_READS[a]) expect([a, n, BUNDLE.includes(n)]).toEqual([a, n, true]);
  }

  const inverted: Record<string, Set<string>> = {};
  for (const a of Object.keys(CONTROL_VIEWS)) {
    for (const v of CONTROL_VIEWS[a]) (inverted[v] ??= new Set()).add(a);
  }
  expect(asSet(Object.keys(inverted))).toEqual(asSet(Object.keys(drawn)));
  for (const v of Object.keys(drawn)) expect([v, asSet(inverted[v] ?? [])]).toEqual([v, asSet(drawn[v])]);
  expect(asSet(inverted.queues)).toEqual(asSet(["ratify", "reject"]));
  expect(asSet(inverted.order)).toEqual(["cut_units"]);
  expect(asSet(inverted.ship)).toEqual(asSet(["cut_units", "set_repo", "set_labels", "set_delegation", "emit_tickets"]));

  expect(asSet(CONTROL_VIEWS.cut_units)).toEqual(["order", "ship"]);
  for (const a of Object.keys(CONTROL_VIEWS)) {
    if (a !== "cut_units") expect([a, new Set(CONTROL_VIEWS[a]).size]).toEqual([a, 1]);
  }
});

test("shows asks only whether the state shows what was pressed", async () => {
  const { shows } = await effects();
  const { HEAD } = reads();
  const F = recorded();
  const ship = (x: Fields, run: { id: string } | null = null): Parts => ({ head: { ...nulls(HEAD.ship), ...x }, rest: null, run });

  // C: the keys units_cut shows as cut that units_before shows neither cut nor out, nor emission_before out
  const before = cutKeys(F.units_before);
  const out = new Set([...outKeys(F.units_before), ...outKeys(F.emission_before)]);
  const C = [...cutKeys(F.units_cut)].filter((k) => !before.has(k) && !out.has(k)).sort();
  expect(C.length).toBeGreaterThan(0);

  const on = { kind: "value", value: "o/n" };
  expect(shows("set_repo", on, ship({ repo: F.repo_on }))).toBe(true);
  expect(shows("set_repo", on, ship({ repo: F.repo_om }))).toBe(false);

  const cutOf = (units: unknown): Parts => ({ head: { units }, rest: null, run: null });
  expect(shows("cut_units", { kind: "units", keys: C }, cutOf(F.units_cut))).toBe(true);
  expect(shows("cut_units", { kind: "units", keys: [...C, "zz-no-unit"] }, cutOf(F.units_cut))).toBe(false);
  expect(shows("cut_units", { kind: "units", keys: C }, cutOf(F.units_before))).toBe(false);

  const shipped = { units: F.units_before, emission: F.emission_before };
  expect(shows("emit_tickets", { kind: "units", keys: ["a"] }, ship(shipped, { id: "n:1" }))).toBe(true);
  expect(shows("emit_tickets", { kind: "units", keys: [] }, ship(shipped))).toBe(false);
  expect(shows("emit_tickets", { kind: "units", keys: C }, ship(shipped))).toBe(false);
});

test("the recorded fixtures are whole", async () => {
  const F = recorded();
  for (const k of FIXTURE_KEYS) expect([k, F[k] === undefined]).toEqual([k, false]);

  const SET = F.SET as { status: unknown; body: unknown };
  expect(typeof SET.status).toBe("number");
  expect((SET.status as number) >= 200 && (SET.status as number) <= 299).toBe(true);
  expect(typeof SET.body === "object" && SET.body !== null && !Array.isArray(SET.body)).toBe(true);
  const result = { kind: "answer", status: SET.status as number, body: SET.body };

  const { classifyAction } = await import("../src/outcome.ts");
  const classify = classifyAction as (a: string, r: unknown) => { kind: string };
  expect(classify("set_repo", result).kind).toBe("confirmed");

  const mod = await import("../src/tracker.ts");
  const track = mod.track as (s: unknown, e: unknown) => { state: unknown };
  const control = mod.control as (s: unknown, p: string, a: string) => any;
  let s = (mod.initTracker as () => unknown)();
  s = track(s, { type: "press", project: "c", action: "set_repo", screen: "ship",
                 target: { kind: "value", value: "o/n" }, body: { repo: "o/n" }, issued: 1 }).state;
  s = track(s, { type: "outcome", id: 1, result, issued: 2 }).state;
  expect(control(s, "c", "set_repo").live.status).toBe("done");
  expect(control(s, "c", "set_repo").value).toBe("o/n");

  expect(F.repo_on).not.toEqual(F.repo_om);
});

test("proposal targets are the port's one text", async () => {
  const { proposalTarget } = await effects();
  const port = await import("../src/actionport.ts");
  expect(proposalTarget).toBe(port.proposalTarget);

  const source = readFileSync(join(ROOT, "dashboard", "src", "effects.ts"), "utf-8");
  expect(source.includes("function proposalTarget")).toBe(false);
  expect(/\bproposalTarget\s*=(?!=)/.test(source)).toBe(false);
  expect(source.includes("JSON.stringify")).toBe(false);

  const two = ["S-10 (was S-01)", "S-17 (was S-02)"];
  expect(proposalTarget({ b: ["S-17 (was S-02)", "S-10 (was S-01)", "S-10 (was S-01)"], a: [] }, null)).toBe(STATED);
  expect(proposalTarget({ a: [], b: two }, null)).toBe(STATED);
  const withN = proposalTarget({ b: two, a: [] }, "n");
  expect(withN).not.toBe(proposalTarget({ b: two, a: [] }, null));
  expect(withN).not.toBe(proposalTarget({ b: two, a: [] }, "m"));
  expect(proposalTarget({ b: ["S-10 (was S-01)"], a: [] }, null)).not.toBe(STATED);
});

test("ratify shows on the spine and the proposal read", async () => {
  const { carriesEffect, shows, proposalTarget } = await effects();
  const { prop_before, prop_ratified, prop_rejected, spine_ratified, mP, s0, mX } = proposals();
  const R = (prop: unknown, spine: unknown): Parts => ({ head: { proposal: prop, spine }, rest: null, run: null });
  const rat = (m: Mapping) => ({ kind: "proposal", proposal: proposalTarget(m, null) });

  const mY: Mapping = { ...mP, [s0]: mP[s0].slice(1) };
  const mZ: Mapping = { ...mP, "zz-no-slice": ["zz-no-entry"] };
  const mR: Mapping = {};
  for (const s of Object.keys(mP)) {
    const rev = [...mP[s]].reverse();
    mR[s] = rev.length ? [...rev, rev[0]] : rev;
  }
  expect(prop_rejected.last_rejection).not.toBeNull();
  const withRej = { ...prop_ratified, last_rejection: prop_rejected.last_rejection };

  expect(carriesEffect("ratify", R(prop_ratified, spine_ratified))).toBe(true);
  expect(shows("ratify", rat(mP), R(prop_ratified, spine_ratified))).toBe(true);
  expect(shows("ratify", rat(mR), R(prop_ratified, spine_ratified))).toBe(true);
  expect(shows("ratify", rat(mX), R(prop_ratified, spine_ratified))).toBe(false);
  expect(shows("ratify", rat(mY), R(prop_ratified, spine_ratified))).toBe(false);
  expect(shows("ratify", rat(mZ), R(prop_ratified, spine_ratified))).toBe(false);
  expect(shows("ratify", rat(mP), R(prop_before, spine_ratified))).toBe(false);
  expect(shows("ratify", rat(mP), R(withRej, spine_ratified))).toBe(false);
  expect(shows("ratify", rat(mP), R(prop_rejected, spine_ratified))).toBe(false);
});

test("reject shows only the pressed mapping and note", async () => {
  const { carriesEffect, shows, proposalTarget } = await effects();
  const { prop_ratified, prop_rejected, mP, mX } = proposals();
  const J = (prop: unknown): Parts => ({ head: { proposal: prop }, rest: null, run: null });
  const rej = (m: Mapping, note: string) => ({ kind: "proposal", proposal: proposalTarget(m, note) });
  const rat = (m: Mapping) => ({ kind: "proposal", proposal: proposalTarget(m, null) });
  expect(rej(mP, "n").proposal).not.toBe(rat(mP).proposal);

  expect(carriesEffect("reject", J(prop_rejected))).toBe(true);
  expect(shows("reject", rej(mP, "n"), J(prop_rejected))).toBe(true);
  expect(shows("reject", rat(mP), J(prop_rejected))).toBe(false);
  expect(shows("reject", rej(mP, "other"), J(prop_rejected))).toBe(false);
  expect(shows("reject", rej(mX, "n"), J(prop_rejected))).toBe(false);
  expect(shows("reject", rej(mP, "n"), J(prop_ratified))).toBe(false);
});
