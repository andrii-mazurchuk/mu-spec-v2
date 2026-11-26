// dashboard/src/scheduler.ts (S-239): the pure scheduler that decides every dashboard read.
// Each test is one scripted run of step, one event per line, asserting what that event sends,
// aborts and wakes for. The module is imported inside each test, so a missing scheduler fails
// the tests one by one instead of failing this file at collection.

import { expect, test } from "bun:test";

type Send = { seq: number; epoch: number; kind: string; part: string; run: string | null };
type Step = { state: any; sends: Send[]; aborts: number[]; wakeAt: number | null };

const load = () => import("../src/scheduler");

/** [kind#seq part run] for each send, in order. */
const show = (sends: Send[]) => sends.map((x) => `${x.kind}#${x.seq} ${x.part} ${x.run}`);

/** A scripted run: deliver(e) steps the kept state and records every ask's instant. */
async function script(interval?: number) {
  const { init, step } = await load();
  const run = {
    state: interval === undefined ? init() : init(interval),
    askAt: [] as number[],
    progressAt: [] as number[],
    deliver(e: any): Step {
      const before = run.state.outstanding as { seq: number } | null;
      const r = step(run.state, e) as Step;
      const asks = r.sends.filter((x) => x.kind === "ask");
      // never two asks outstanding: an ask goes out only once the previous one is answered or aborted
      expect(asks.length).toBeLessThanOrEqual(1);
      if (asks.length === 1 && before !== null) expect(r.aborts).toContain(before.seq);
      for (const _ of asks) run.askAt.push(e.now);
      for (const x of r.sends) if (x.kind === "progress") run.progressAt.push(e.now);
      run.state = r.state;
      return r;
    },
  };
  return run;
}

const ok = { applied: true, complete: true, run: null };

test("rhythm, back-off, hide and switch", async () => {
  const { waitInForce, init } = await load();

  // 1. the wait in force, and the interval floor
  expect([0, 1, 2, 3, 4].map((f) => waitInForce(10000, f))).toEqual([10000, 20000, 40000, 60000, 60000]);
  expect([0, 1, 2, 3, 4].map((f) => waitInForce(90000, f))).toEqual([90000, 90000, 90000, 90000, 90000]);
  expect(() => init(9999)).toThrow(RangeError);
  expect(() => init(10000)).not.toThrow();

  const p = await script();
  let r: Step;

  // 2. the first visible moment is page open and ask 1
  r = p.deliver({ type: "visible", now: 0 });
  expect(show(r.sends)).toEqual(["ask#1 whole null"]);
  expect(r.aborts).toEqual([]);
  expect(r.wakeAt).toBe(10000);
  r = p.deliver({ type: "outcome", now: 50, seq: 1, cls: "unchanged", ...ok });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  expect(r.wakeAt).toBe(10000);

  // 3. a failure backs off from the failed ask's send, not from its outcome
  r = p.deliver({ type: "time", now: 10000 });
  expect(show(r.sends)).toEqual(["ask#2 whole null"]);
  expect(r.aborts).toEqual([]);
  r = p.deliver({ type: "outcome", now: 14000, seq: 2, cls: "failure", ...ok });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  expect(r.wakeAt).toBe(30000);

  // 4. an ask with no outcome by its wait is aborted, and counts as a failure
  r = p.deliver({ type: "time", now: 30000 });
  expect(show(r.sends)).toEqual(["ask#3 whole null"]);
  expect(r.aborts).toEqual([]);
  expect(r.wakeAt).toBe(50000);
  r = p.deliver({ type: "time", now: 50000 });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([3]);
  expect(r.wakeAt).toBe(70000);

  // 5. a success resets the back-off
  r = p.deliver({ type: "time", now: 70000 });
  expect(show(r.sends)).toEqual(["ask#4 whole null"]);
  expect(r.aborts).toEqual([]);
  r = p.deliver({ type: "outcome", now: 70100, seq: 4, cls: "parts", ...ok });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  expect(r.wakeAt).toBe(80000);

  // 6. a refresh goes at once and never moves the ask
  r = p.deliver({ type: "refresh", now: 72000 });
  expect(show(r.sends)).toEqual(["refresh#5 whole null"]);
  expect(r.aborts).toEqual([]);
  expect(r.wakeAt).toBe(80000);
  r = p.deliver({ type: "time", now: 80000 });
  expect(show(r.sends)).toEqual(["ask#6 whole null"]);
  expect(r.aborts).toEqual([]);
  r = p.deliver({ type: "outcome", now: 80050, seq: 6, cls: "unchanged", ...ok });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);

  // 7. a hidden page sends nothing; on return the overdue ask goes at once
  r = p.deliver({ type: "hidden", now: 81000 });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  r = p.deliver({ type: "time", now: 90000 });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  r = p.deliver({ type: "visible", now: 95000 });
  expect(show(r.sends)).toEqual(["ask#7 whole null"]);
  expect(r.aborts).toEqual([]);
  r = p.deliver({ type: "outcome", now: 95050, seq: 7, cls: "unchanged", ...ok });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);

  // 8. on return before the wait has passed, the ask waits for it
  r = p.deliver({ type: "hidden", now: 96000 });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  r = p.deliver({ type: "visible", now: 99000 });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  expect(r.wakeAt).toBe(105000);

  // 9. a switch sends the head, and an incomplete head of the current epoch one rest
  r = p.deliver({ type: "switch", now: 100000, projectChanged: false });
  expect(show(r.sends)).toEqual(["head#8 head null"]);
  expect(r.sends[0].epoch).toBe(1);
  expect(r.aborts).toEqual([]);
  r = p.deliver({ type: "outcome", now: 100100, seq: 8, cls: "parts", applied: true, complete: false, run: null });
  expect(show(r.sends)).toEqual(["rest#9 rest null"]);
  expect(r.sends[0].epoch).toBe(1);
  expect(r.aborts).toEqual([]);

  // 10. a head answer from a superseded switch brings no rest
  r = p.deliver({ type: "switch", now: 101000, projectChanged: false });
  expect(show(r.sends)).toEqual(["head#10 head null"]);
  expect(r.sends[0].epoch).toBe(2);
  expect(r.aborts).toEqual([]);
  r = p.deliver({ type: "switch", now: 102000, projectChanged: false });
  expect(show(r.sends)).toEqual(["head#11 head null"]);
  expect(r.sends[0].epoch).toBe(3);
  expect(r.aborts).toEqual([]);
  r = p.deliver({ type: "outcome", now: 102100, seq: 10, cls: "parts", applied: true, complete: false, run: null });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);

  // 11. switches never moved the ask
  r = p.deliver({ type: "time", now: 105000 });
  expect(show(r.sends)).toEqual(["ask#12 whole null"]);
  expect(r.aborts).toEqual([]);

  expect(p.askAt).toEqual([0, 10000, 30000, 70000, 80000, 95000, 105000]);
});

test("ship progress track", async () => {
  // Arrange A: the page's own Ship press, through to one end-of-run refresh
  const a = await script();
  let r: Step;

  // 1. the background rhythm before the press
  r = a.deliver({ type: "visible", now: 0 });
  expect(show(r.sends)).toEqual(["ask#1 whole null"]);
  expect(r.aborts).toEqual([]);
  r = a.deliver({ type: "outcome", now: 0, seq: 1, cls: "unchanged", ...ok });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  r = a.deliver({ type: "time", now: 10000 });
  expect(show(r.sends)).toEqual(["ask#2 whole null"]);
  expect(r.aborts).toEqual([]);
  r = a.deliver({ type: "outcome", now: 10050, seq: 2, cls: "unchanged", ...ok });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);

  // 2. the press itself sends nothing; progress is due one second later
  r = a.deliver({ type: "ship_sent", now: 15000 });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  expect(r.wakeAt).toBe(16000);

  // 3. 'ended' while the press is awaited keeps the track on
  r = a.deliver({ type: "time", now: 16000 });
  expect(show(r.sends)).toEqual(["progress#3 whole null"]);
  expect(r.aborts).toEqual([]);
  r = a.deliver({ type: "progress", now: 16050, seq: 3, cls: "ended", run: null });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  expect(r.wakeAt).toBe(17050);

  // 4. the next progress ask follows the answer by one second, and a running answer names the run
  r = a.deliver({ type: "time", now: 17050 });
  expect(show(r.sends)).toEqual(["progress#4 whole null"]);
  expect(r.aborts).toEqual([]);
  r = a.deliver({ type: "progress", now: 17150, seq: 4, cls: "running", run: "n:1" });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);

  // 5. the progress ask carries the tracked run; the press settling stops nothing
  r = a.deliver({ type: "time", now: 18150 });
  expect(show(r.sends)).toEqual(["progress#5 whole n:1"]);
  expect(r.aborts).toEqual([]);
  r = a.deliver({ type: "ship_settled", now: 18200 });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  r = a.deliver({ type: "progress", now: 18250, seq: 5, cls: "running", run: "n:1" });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  expect(r.wakeAt).toBe(19250);

  // 6. 'ended' with no press awaited ends the track with one end-of-run refresh
  r = a.deliver({ type: "time", now: 19250 });
  expect(show(r.sends)).toEqual(["progress#6 whole n:1"]);
  expect(r.aborts).toEqual([]);
  r = a.deliver({ type: "progress", now: 19350, seq: 6, cls: "ended", run: null });
  expect(show(r.sends)).toEqual(["end_refresh#7 whole null"]);
  expect(r.aborts).toEqual([]);
  expect(r.state.track).toBeNull();
  const progressSentBy6 = a.progressAt.length;

  // 7. the background ask is where it would be with no run; a late run part naming the ended run is ignored
  r = a.deliver({ type: "time", now: 20000 });
  expect(show(r.sends)).toEqual(["ask#8 whole null"]);
  expect(r.aborts).toEqual([]);
  r = a.deliver({ type: "outcome", now: 20050, seq: 8, cls: "unchanged", applied: true, complete: true, run: "n:1" });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  expect(r.state.track).toBeNull();

  // 8. the end-of-run refresh's answer changes nothing, and the rhythm holds
  r = a.deliver({ type: "outcome", now: 20100, seq: 7, cls: "parts", ...ok });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  r = a.deliver({ type: "time", now: 30000 });
  expect(show(r.sends)).toEqual(["ask#9 whole null"]);
  expect(r.aborts).toEqual([]);
  expect(a.progressAt.length).toBe(progressSentBy6);
  expect(a.askAt).toEqual([0, 10000, 20000, 30000]);

  // Arrange B: a run discovered from a run part, timed out, hidden, and switched away from
  const b = await script();

  // 9. an outcome naming a run while the track is off asks for its progress at that instant
  r = b.deliver({ type: "visible", now: 0 });
  expect(show(r.sends)).toEqual(["ask#1 whole null"]);
  expect(r.aborts).toEqual([]);
  r = b.deliver({ type: "outcome", now: 100, seq: 1, cls: "unchanged", applied: true, complete: true, run: "n:2" });
  expect(show(r.sends)).toEqual(["progress#2 whole n:2"]);
  expect(r.aborts).toEqual([]);
  expect(r.wakeAt).toBe(5100);

  // 10. a progress ask with no answer by node_timeout is aborted; the next is due a second later
  r = b.deliver({ type: "time", now: 5100 });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([2]);
  expect(r.wakeAt).toBe(6100);
  r = b.deliver({ type: "time", now: 6100 });
  expect(show(r.sends)).toEqual(["progress#3 whole n:2"]);
  expect(r.aborts).toEqual([]);

  // 11. a hidden page sends no progress ask and wakes for nothing
  r = b.deliver({ type: "hidden", now: 6200 });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  r = b.deliver({ type: "progress", now: 6300, seq: 3, cls: "running", run: "n:2" });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  r = b.deliver({ type: "time", now: 7300 });
  expect(show(r.sends)).toEqual([]);
  expect(r.aborts).toEqual([]);
  expect(r.wakeAt).toBeNull();

  // 12. on return the tracked run's progress ask goes at once; the background ask is not yet due
  r = b.deliver({ type: "visible", now: 9300 });
  expect(show(r.sends)).toEqual(["progress#4 whole n:2"]);
  expect(r.aborts).toEqual([]);
  const after12 = b.state;

  // 13, branch 1: a project switch aborts the outstanding progress ask and drops the track
  r = b.deliver({ type: "switch", now: 9400, projectChanged: true });
  expect(show(r.sends)).toEqual(["head#5 head null"]);
  expect(r.aborts).toEqual([4]);
  expect(r.state.track).toBeNull();
  const progressSentBy13 = b.progressAt.length;
  r = b.deliver({ type: "time", now: 10000 });
  expect(show(r.sends)).toEqual(["ask#6 whole null"]);
  expect(r.aborts).toEqual([]);
  expect(b.progressAt.length).toBe(progressSentBy13);

  // 13, branch 2: a switch within the project keeps the track and its outstanding ask
  b.state = after12;
  r = b.deliver({ type: "switch", now: 9400, projectChanged: false });
  expect(show(r.sends)).toEqual(["head#5 head null"]);
  expect(r.aborts).toEqual([]);
  expect(r.state.track).not.toBeNull();
  expect(r.state.track.outstanding?.seq).toBe(4);
});
