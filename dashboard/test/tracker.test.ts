// dashboard/src/tracker.ts (S-471): the pure action tracker, track(state, event) and control.
// T-186, that one press sends once per (project, action) key whichever screen it comes from and each
// Ship and confirmation delivery is made once; T-187, that the press and unknown marks decide what
// judges and what releases an unknown press; and T-188, the displaced earlier press, the held
// confirmed value, a refusal status without the unit's words, and a switch.
//
// The modules under test are imported inside each test, so a module not yet written fails only the
// tests that need it. HEAD is read from mu_spec.dash.views and repo_on, repo_om and SET from the
// shows test's recorded fixtures, never copied.

import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

type RefusalForm = { name: string; status: number; field: "error" | "reason"; pattern: string };
type NetResult =
  | { kind: "answer"; status: number; body: unknown }
  | { kind: "no_answer"; reason: "aborted" | "network" };
type Fields = { [read: string]: unknown };
type Parts = { head: Fields | null; rest: Fields | null; run: { id: string } | null };
type Track = { state: unknown; sends: any[]; deliveries: any[] };

const ROOT = join(import.meta.dir, "..", "..");
const FIELD = "[\\s\\S]*?";
const FOREIGN = "zz-not-a-catalogue-text-zz";
const GIVE: NetResult = { kind: "answer", status: 504, body: { error: "upstream timed out" } };

function answer(status: number, body: unknown): NetResult {
  return { kind: "answer", status, body };
}

/** As in the classification test: the literal parts unescaped, each field filled with 'Q7'. */
function textOf(m: RefusalForm): string {
  let s = m.pattern;
  if (s.startsWith("^")) s = s.slice(1);
  if (s.endsWith("$")) s = s.slice(0, -1);
  s = s.split(FIELD).join("Q7");
  return s.replace(/\\([\\^$.*+?()[\]{}|/])/g, "$1");
}

/** S-142's HEAD, read from mu_spec.dash.views. */
function head(): Record<string, string[]> {
  const code = "import json; from mu_spec.dash.views import HEAD; print(json.dumps({k: list(v) for k, v in HEAD.items()}))";
  const r = Bun.spawnSync(["python", "-c", code], { cwd: ROOT });
  expect([r.exitCode, r.stderr.toString()]).toEqual([0, ""]);
  return JSON.parse(r.stdout.toString());
}

/** A head holding every name, null except the reads given in x. */
function parts(names: readonly string[], x: Fields): Parts {
  const h: Fields = {};
  for (const n of names) h[n] = null;
  return { head: { ...h, ...x }, rest: null, run: null };
}

/** The shows test's fixtures: repo_on, repo_om, SET, ship(x) and spine. */
function fixtures() {
  const effects = JSON.parse(readFileSync(join(ROOT, "dashboard", "test", "fixtures", "effects.json"), "utf-8"));
  const h = head();
  return {
    repo_on: effects.repo_on as unknown,
    repo_om: effects.repo_om as unknown,
    SET: effects.SET as { status: number; body: unknown },
    spineNames: h.spine,
    ship: (x: Fields) => parts(h.ship, x),
    spine: parts(h.spine, {}),
  };
}

/** The tracker under test, with a stepper that feeds each step the previous step's state. */
async function tracker() {
  const mod = await import("../src/tracker.ts");
  const track = mod.track as (s: unknown, e: unknown) => Track;
  const control = mod.control as (s: unknown, p: string, a: string) => any;
  const t = {
    s: (mod.initTracker as () => unknown)(),
    step(e: unknown): Track {
      const r = track(t.s, e);
      t.s = r.state;
      return r;
    },
    press(project: string, action: string, screen: string, target: unknown, body: unknown, issued: number) {
      return t.step({ type: "press", project, action, screen, target, body, issued });
    },
    outcome(id: number, result: NetResult, issued: number) {
      return t.step({ type: "outcome", id, result, issued });
    },
    state(project: string, seq: number, p: Parts, applied: boolean) {
      return t.step({ type: "state", project, seq, parts: p, applied });
    },
    control: (p: string, a: string) => control(t.s, p, a),
    status: (p: string, a: string) => control(t.s, p, a).live?.status,
  };
  return t;
}

test("one send per key and each delivery once", async () => {
  const { CONFIRM_KEYS, REFUSAL_FORMS, ACTION_DEADLINE_MS } = await import("../src/outcome.ts");
  const { ACTION_ROUTES } = await import("../src/actions.ts");
  const confirmKeys = CONFIRM_KEYS as Record<string, readonly string[]>;
  const refusalForms = REFUSAL_FORMS as Record<string, readonly RefusalForm[]>;
  const ok = (a: string): Record<string, unknown> => {
    const body: Record<string, unknown> = {};
    for (const k of confirmKeys[a]) body[k] = 1;
    body.zz_extra = 2;
    return body;
  };
  const t = await tracker();
  const k = { kind: "units", keys: ["k"] };

  // 1. the first press sends its action
  let r = t.press("p", "cut_units", "order", k, {}, 3);
  expect(r.sends).toEqual([
    { id: 1, method: "POST", path: ACTION_ROUTES.cut_units.path("p"), body: {}, deadlineMs: ACTION_DEADLINE_MS },
  ]);
  expect(r.deliveries).toEqual([]);
  const after1 = r.state;

  // 2. the same press again changes nothing
  r = t.press("p", "cut_units", "order", k, {}, 3);
  expect(r.sends).toEqual([]);
  expect(r.deliveries).toEqual([]);
  expect(r.state).toEqual(after1);

  // 3. the other screen offering the cut meets the same live record
  r = t.press("p", "cut_units", "ship", k, {}, 4);
  expect(r.sends).toEqual([]);

  // 4. another project is another key
  r = t.press("q", "cut_units", "order", k, {}, 4);
  expect(r.sends.length).toBe(1);
  expect(r.sends[0].id).toBe(2);

  // 5, 6. a Ship press delivers ship_sent in its own Track, once
  r = t.press("p", "emit_tickets", "ship", k, {}, 5);
  expect(r.sends.length).toBe(1);
  expect(r.sends[0].id).toBe(3);
  expect(r.deliveries).toEqual([{ type: "ship_sent", project: "p" }]);
  r = t.press("p", "emit_tickets", "ship", k, {}, 5);
  expect(r.sends).toEqual([]);
  expect(r.deliveries).toEqual([]);

  // 7, 8. its unknown outcome settles the Ship as not confirmed, once
  r = t.outcome(3, GIVE, 6);
  expect(r.deliveries).toEqual([{ type: "ship_settled", project: "p", outcome: "not_confirmed" }]);
  expect(t.control("p", "emit_tickets").available).toBe(false);
  r = t.outcome(3, GIVE, 6);
  expect(r.deliveries).toEqual([]);

  // 9. a confirmed cut delivers action_confirmed alone and frees its control
  r = t.outcome(1, answer(200, ok("cut_units")), 7);
  expect(r.deliveries).toEqual([{ type: "action_confirmed", project: "p" }]);
  expect(t.control("p", "cut_units").available).toBe(true);
  expect(t.status("p", "cut_units")).toBe("done");

  // 10. a confirmed Ship delivers both; a refused Ship delivers its failure alone, with the unit's words
  r = t.press("r", "emit_tickets", "ship", k, {}, 8);
  r = t.outcome(r.sends[0].id, answer(200, ok("emit_tickets")), 9);
  expect(r.deliveries.length).toBe(2);
  expect(r.deliveries).toContainEqual({ type: "action_confirmed", project: "r" });
  expect(r.deliveries).toContainEqual({ type: "ship_settled", project: "r", outcome: "done" });

  const m = refusalForms.emit_tickets[0];
  const text = textOf(m);
  r = t.press("t", "emit_tickets", "ship", k, {}, 10);
  r = t.outcome(r.sends[0].id, answer(m.status, { [m.field]: text }), 11);
  expect(r.deliveries).toEqual([{ type: "ship_settled", project: "t", outcome: "failed" }]);
  expect(t.control("t", "emit_tickets").live.reason === text).toBe(true);
});

test("marks decide judging and release", async () => {
  const { repo_on, repo_om, ship, spine, spineNames } = fixtures();
  // preconditions: the spine head lacks the repo read, and the two repo reads differ
  expect(spineNames.includes("repo")).toBe(false);
  expect(repo_on).not.toEqual(repo_om);
  const t = await tracker();
  const on = { kind: "value", value: "o/n" };
  const look = () => [t.status("p", "set_repo"), t.control("p", "set_repo").available];

  // pressMark 10, unknownMark 14
  let r = t.press("p", "set_repo", "ship", on, { repo: "o/n" }, 10);
  t.outcome(r.sends[0].id, GIVE, 14);
  expect(look()).toEqual(["nc_unavailable", false]);

  // a) a read sent before the press judges nothing, even showing the effect
  t.state("p", 9, ship({ repo: repo_on }), true);
  expect(look()).toEqual(["nc_unavailable", false]);
  // b) a read after the press but not after the unknown does not release
  t.state("p", 12, ship({ repo: repo_om }), true);
  expect(look()).toEqual(["nc_unavailable", false]);
  // c) an answer without the effect read neither judges nor releases
  t.state("p", 15, spine, true);
  expect(look()).toEqual(["nc_unavailable", false]);
  // d) a judged answer to a read after the unknown releases
  t.state("p", 15, ship({ repo: repo_om }), true);
  expect(look()).toEqual(["nc_available", true]);
  // e) any answer after the press that shows the effect settles it, applied or not
  t.state("p", 13, ship({ repo: repo_on }), false);
  expect(t.status("p", "set_repo")).toBe("done");

  // a waiting press is never settled by a state answer
  t.press("p2", "set_repo", "ship", on, { repo: "o/n" }, 20);
  t.state("p2", 21, ship({ repo: repo_on }), true);
  expect([t.status("p2", "set_repo"), t.control("p2", "set_repo").available]).toEqual(["waiting", false]);

  // an answer for another project judges nothing here
  r = t.press("p3", "set_repo", "ship", on, { repo: "o/n" }, 30);
  t.outcome(r.sends[0].id, GIVE, 31);
  t.state("p4", 40, ship({ repo: repo_on }), true);
  expect(t.status("p3", "set_repo")).toBe("nc_unavailable");
});

test("displacement, held value, foreign refusal, switch", async () => {
  const { repo_on, repo_om, SET, ship } = fixtures();
  const { REFUSAL_FORMS } = await import("../src/outcome.ts");
  const refusalForms = REFUSAL_FORMS as Record<string, readonly RefusalForm[]>;
  const t = await tracker();
  const on = { kind: "value", value: "o/n" };
  const om = { kind: "value", value: "o/m" };

  // displacement: a released 'not confirmed' press moves to earlier, and its effect settles it there
  let r = t.press("p", "set_repo", "ship", on, { repo: "o/n" }, 1);
  t.outcome(r.sends[0].id, GIVE, 2);
  t.state("p", 3, ship({ repo: repo_om }), true);
  expect(t.status("p", "set_repo")).toBe("nc_available");
  r = t.press("p", "set_repo", "ship", om, { repo: "o/m" }, 4);
  expect(r.sends.length).toBe(1);
  let c = t.control("p", "set_repo");
  expect(c.earlier.length).toBe(1);
  expect(c.earlier[0].target.value).toBe("o/n");
  expect(c.earlier[0].status).toBe("nc_available");
  expect(c.live.status).toBe("waiting");
  t.state("p", 5, ship({ repo: repo_on }), true);
  c = t.control("p", "set_repo");
  expect(c.earlier).toEqual([]);
  expect(c.settled.length).toBe(1);
  expect(c.settled[0].target.value).toBe("o/n");
  expect(c.settled[0].status).toBe("done");
  expect(c.live.status).toBe("waiting");

  // held value: the confirmed value stands until an applied answer to a read after the confirmation
  r = t.press("c", "set_repo", "ship", on, { repo: "o/n" }, 30);
  t.outcome(r.sends[0].id, answer(SET.status, SET.body), 33);
  expect(t.status("c", "set_repo")).toBe("done");
  expect(t.control("c", "set_repo").value).toBe("o/n");
  t.state("c", 32, ship({ repo: repo_om }), true);
  expect(t.control("c", "set_repo").value).toBe("o/n");
  t.state("c", 34, ship({ repo: repo_om }), false);
  expect(t.control("c", "set_repo").value).toBe("o/n");
  t.state("c", 34, ship({ repo: repo_om }), true);
  expect(t.control("c", "set_repo").value).toBe(undefined);

  // foreign refusal: a refusal status without the unit's words is not confirmed, never failed
  const m = refusalForms.set_labels[0];
  const taken = refusalForms.set_labels.some(
    (n) => n.status === m.status && n.field === m.field && new RegExp(n.pattern).test(FOREIGN),
  );
  expect(taken).toBe(false);
  r = t.press("f", "set_labels", "ship", { kind: "value", value: ["zz-label"] }, { labels: ["zz-label"] }, 40);
  t.outcome(r.sends[0].id, answer(m.status, { [m.field]: FOREIGN }), 41);
  const f = t.control("f", "set_labels").live;
  expect(f.status).toBe("nc_unavailable");
  expect(f.reason).toBe(null);

  // switch: nothing changes
  const before = t.s;
  r = t.step({ type: "switch" });
  expect(r.state).toEqual(before);
  expect(r.sends).toEqual([]);
  expect(r.deliveries).toEqual([]);
});
