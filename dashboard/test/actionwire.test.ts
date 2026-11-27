// dashboard/src/actionwire.ts (S-250): wireActions, the action wiring behind the action port.
// T-191, that under injected deps a press sends once across both cut screens, its deadline timer
// aborts it at exactly ACTION_DEADLINE_MS into 'not confirmed', and an unchanged answer is judged on
// the held parts kept for its read; and T-212, the delivery filter, the kept-parts bookkeeping, one
// changed per applied event, and a press on a name that is no action.
//
// The modules under test are imported inside each test, so a module not yet written fails only the
// tests that need it. HEAD is read from mu_spec.dash.views and repo_on from the shows test's
// fixtures, never copied.

import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

type NetResult =
  | { kind: "answer"; status: number; body: unknown }
  | { kind: "no_answer"; reason: "aborted" | "network" };
type Fields = { [read: string]: unknown };
type Parts = { head: Fields | null; rest: Fields | null; run: { id: string } | null };

const ROOT = join(import.meta.dir, "..", "..");
const GIVE: NetResult = { kind: "answer", status: 504, body: { error: "upstream timed out" } };

/** Every pending promise callback and timer-0 task runs before this resolves. */
const flush = () => new Promise<void>((r) => setTimeout(r, 0));

/** S-142's HEAD, read from mu_spec.dash.views. */
function head(): Record<string, string[]> {
  const code = "import json; from mu_spec.dash.views import HEAD; print(json.dumps({k: list(v) for k, v in HEAD.items()}))";
  const r = Bun.spawnSync(["python", "-c", code], { cwd: ROOT });
  expect([r.exitCode, r.stderr.toString()]).toEqual([0, ""]);
  return JSON.parse(r.stdout.toString());
}

/** The shows test's fixtures: repo_on, and ship(x), a head holding every HEAD.ship read, null except x. */
function fixtures() {
  const effects = JSON.parse(readFileSync(join(ROOT, "dashboard", "test", "fixtures", "effects.json"), "utf-8"));
  const names = head().ship;
  const ship = (x: Fields): Parts => {
    const h: Fields = {};
    for (const n of names) h[n] = null;
    return { head: { ...h, ...x }, rest: null, run: null };
  };
  return { repo_on: effects.repo_on as unknown, ship };
}

type Sent = { method: string; path: string; body: unknown; signal: AbortSignal; resolve: (r: NetResult) => void };
type Timer = { handle: number; f: () => void; ms: number; at: number; fired: boolean };

/** Fake deps: a send the test answers or the signal aborts, a fake clock, and recorders. */
function fakeDeps() {
  const f = {
    n: 0,
    now: 0,
    sent: [] as Sent[],
    timers: [] as Timer[],
    cleared: [] as unknown[],
    delivered: [] as unknown[],
    changed: 0,
    deps: null as any,
    /** Moves the fake clock to t, firing every timer due by then that was not cleared. */
    advance(t: number) {
      f.now = t;
      for (const x of f.timers) {
        if (!x.fired && x.at <= t && !f.cleared.includes(x.handle)) {
          x.fired = true;
          x.f();
        }
      }
    },
  };
  f.deps = {
    send(method: string, path: string, body: unknown, signal: AbortSignal): Promise<NetResult> {
      return new Promise<NetResult>((resolve) => {
        f.sent.push({ method, path, body, signal, resolve });
        signal.addEventListener("abort", () => resolve({ kind: "no_answer", reason: "aborted" }));
      });
    },
    setTimer(fn: () => void, ms: number): unknown {
      const handle = f.timers.length + 1;
      f.timers.push({ handle, f: fn, ms, at: f.now + ms, fired: false });
      return handle;
    },
    clearTimer(h: unknown): void {
      f.cleared.push(h);
    },
    issued: () => f.n,
    deliver: (d: unknown) => {
      f.delivered.push(d);
    },
    shown: () => "p",
    changed: () => {
      f.changed++;
    },
  };
  return f;
}

test("deadline and held parts", async () => {
  const { wireActions } = await import("../src/actionwire.ts");
  const { ACTION_ROUTES } = await import("../src/actions.ts");
  const { ACTION_DEADLINE_MS } = await import("../src/outcome.ts");
  const { repo_on, ship } = fixtures();
  const f = fakeDeps();
  const w = (wireActions as any)(f.deps);
  const status = (a: string) => w.control("p", a).live?.status;

  // 1. one send across the order and ship screens, its deadline timer set at ACTION_DEADLINE_MS
  f.n = 1;
  w.press("p", "cut_units", "order", { kind: "units", keys: ["k"] }, {});
  expect(f.sent.length).toBe(1);
  expect(f.sent[0].path).toBe(ACTION_ROUTES.cut_units.path("p"));
  expect(f.timers.map((x) => x.ms)).toEqual([ACTION_DEADLINE_MS]);
  w.press("p", "cut_units", "ship", { kind: "units", keys: ["k"] }, {});
  expect(f.sent.length).toBe(1);
  expect(f.timers.length).toBe(1);

  // 2. still waiting one millisecond before the deadline; aborted into 'not confirmed' at it
  f.advance(ACTION_DEADLINE_MS - 1);
  await flush();
  expect(f.sent[0].signal.aborted).toBe(false);
  expect(status("cut_units")).toBe("waiting");
  f.advance(ACTION_DEADLINE_MS);
  await flush();
  expect(f.sent[0].signal.aborted).toBe(true);
  expect(status("cut_units")).toBe("nc_unavailable");

  // 3. a give-up answer clears its timer and leaves set_repo 'not confirmed'
  f.n = 3;
  w.press("p", "set_repo", "ship", { kind: "value", value: "o/n" }, { repo: "o/n" });
  expect(f.sent.length).toBe(2);
  expect(f.timers.length).toBe(2);
  const timer = f.timers[1].handle;
  f.n = 5;
  f.sent[1].resolve(GIVE);
  await flush();
  expect(f.cleared).toContain(timer);
  expect(status("set_repo")).toBe("nc_unavailable");

  // 4. an unchanged answer is judged on the head kept for its read, which shows 'o/n'
  f.n = 6;
  w.screenSent(6, "p", { head: ship({ repo: repo_on }).head, rest: null });
  w.screenAnswered(6, { cls: "unchanged", project: "p", head: null, rest: null, run: null }, true);
  expect(status("set_repo")).toBe("done");
});

test("deliveries, arrivals, changed and non-actions", async () => {
  const { wireActions } = await import("../src/actionwire.ts");
  const { repo_on, ship } = fixtures();
  const f = fakeDeps();
  const w = (wireActions as any)(f.deps);
  const last = () => f.sent[f.sent.length - 1];

  // 1. ship_sent is handed out for a project not shown; its ship_settled is not
  f.n = 1;
  w.press("q", "emit_tickets", "ship", { kind: "units", keys: ["a"] }, {});
  expect(f.delivered).toContainEqual({ type: "ship_sent", project: "q" });
  last().resolve(GIVE);
  await flush();
  expect(f.delivered.filter((d: any) => d.type === "ship_settled" && d.project === "q")).toEqual([]);

  // 2. ship_settled is handed out for the shown project
  f.n = 1;
  w.press("p", "emit_tickets", "ship", { kind: "units", keys: ["a"] }, {});
  last().resolve(GIVE);
  await flush();
  expect(f.delivered).toContainEqual({ type: "ship_settled", project: "p", outcome: "not_confirmed" });

  // 3. a preparing arrival applies nothing and forgets the parts kept for seq 7
  f.n = 1;
  w.press("p", "set_repo", "ship", { kind: "value", value: "o/n" }, { repo: "o/n" });
  f.n = 2;
  last().resolve(GIVE);
  await flush();
  expect(w.control("p", "set_repo").live?.status).toBe("nc_unavailable");
  const c0 = f.changed;
  w.screenSent(7, "p", { head: ship({ repo: repo_on }).head, rest: null });
  w.screenAnswered(7, { cls: "preparing", project: "p" }, false);
  expect(w.control("p", "set_repo").live?.status).toBe("nc_unavailable");
  expect(f.changed).toBe(c0);
  w.screenAnswered(7, { cls: "unchanged", project: "p", head: null, rest: null, run: null }, true);
  expect(w.control("p", "set_repo").live?.status).toBe("nc_unavailable");
  expect(f.changed).toBe(c0);

  // 4. changed once per applied event
  let c = f.changed;
  let s = f.sent.length;
  w.press("p", "cut_units", "order", { kind: "units", keys: ["k"] }, {});
  expect([f.sent.length - s, f.changed - c]).toEqual([1, 1]);
  c = f.changed;
  s = f.sent.length;
  w.press("p", "cut_units", "ship", { kind: "units", keys: ["k"] }, {});
  expect([f.sent.length - s, f.changed - c]).toEqual([0, 1]);
  c = f.changed;
  last().resolve(GIVE);
  await flush();
  expect(f.changed - c).toBe(1);
  c = f.changed;
  w.switched();
  expect(f.changed - c).toBe(1);

  // 5. a name that is no action sends nothing, redraws nothing and has no control
  c = f.changed;
  s = f.sent.length;
  w.press("p", "rollback_tickets", "ship", { kind: "units", keys: [] }, {});
  expect([f.sent.length - s, f.changed - c]).toEqual([0, 0]);
  expect(w.control("p", "rollback_tickets")).toEqual({
    available: false, live: null, earlier: [], settled: [], value: undefined,
  });

  // 6. the wiring conforms to the port, and the tracker's control to its control view
  const r = Bun.spawnSync(["bunx", "tsc", "--noEmit", "--strict", "dashboard/check/types.check.ts"], { cwd: ROOT });
  expect([r.exitCode, r.stdout.toString() + r.stderr.toString()]).toEqual([0, ""]);
}, 120000);
