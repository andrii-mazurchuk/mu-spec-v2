// The page's request records and apply rules (S-462): T-444, that the screen part changes only for
// the current epoch and a newer send, applies the open fallback and no_project, fills a head in only
// at its own token and marks a failed ask over a shown screen; T-443, that the list part applies by
// its own sequence, the chosen project's run part is kept and returned and sets the elapsed origin at
// first sight, an ended run is never drawn again, and progress answers apply by project and sequence
// with elapsed counted on the page's clock, a failure keeping the values not current; and T-446, that
// the Ship samples keep the last SAMPLE_MAX points of one stage and the elapsed origin lives from the
// press or first sight until a switch, an unconfirmed press or an end.
//
// The module under test is imported inside each test, so a module not yet written fails only the
// tests that need it.

import { expect, test } from "bun:test";

/** A 200 answer carrying `body`, as the network module gives it. */
const ans = (body: unknown) => ({ kind: "answer", status: 200, body });
const H = { fields: {}, items: [] };

test("screen part gating", async () => {
  const { opened, switched, record, input, apply } = await import("../src/apply.ts");

  // 1. page open sends the remembered project as a fallback; no origin, progress or ended run yet
  const page = opened({ project: "gone", view: "spine", filter: null });
  expect(page.origin).toBeNull();
  expect(page.progress).toBeNull();
  expect(page.endedRuns).toEqual([]);
  const s1 = record(page, { seq: 1, epoch: 0, kind: "ask", part: "whole" });
  expect(input(s1)).toEqual({
    project: "gone", fallback: true, view: "spine", filter: null, part: "whole", held: null, list_token: null,
  });

  // 2. the fallback answer names the project actually shown
  const a = apply(page, s1, ans({
    status: "parts", project: "p", token: "t1", complete: true, head: H, rest: H,
    list_token: "L1", names: ["p", "q"], run: null,
  }));
  expect(a.page.choice.project).toBe("p");
  expect(a.page.fallback).toBe(false);
  expect(a.page.screen).toMatchObject({ status: "shown", token: "t1", parts: "whole" });
  expect(a.page.list).toEqual({ names: ["p", "q"], token: "L1", seq: 1 });
  expect(a.cls).toBe("parts");
  expect(a.applied).toBe(true);
  expect(a.run).toBeNull();

  // 3. switch to q, then back to p before q's answer arrives
  const pq = switched(a.page, { project: "q", view: "spine", filter: null }, 1);
  const s2 = record(pq, { seq: 2, epoch: 1, kind: "head", part: "head" });
  expect(s2.held).toBeNull();
  expect(s2.fallback).toBe(false);
  const pp = switched(pq, { project: "p", view: "spine", filter: null }, 2);
  const s3 = record(pp, { seq: 3, epoch: 2, kind: "head", part: "head" });

  // 4. q's late answer has a newer seq than shownSeq but an abandoned epoch: rejected
  const b = apply(pp, s2, ans({ status: "parts", token: "tq", complete: true, head: H, rest: H, list_token: "L1" }));
  expect(b.applied).toBe(false);
  expect(b.page.screen.status).toBe("loading");
  expect(b.page.choice.project).toBe("p");

  // 5. the current head draws a head-only screen
  const c = apply(b.page, s3, ans({ status: "parts", token: "t3", complete: false, head: H, list_token: "L1" }));
  expect(c.applied).toBe(true);
  expect(c.page.screen).toMatchObject({ status: "shown", token: "t3", parts: "head" });
  expect(c.page.shownSeq).toBe(3);
  expect(c.complete).toBe(false);

  // 6. an ask holds the shown token and parts; unchanged at that token applies and keeps the head
  const s4 = record(c.page, { seq: 4, epoch: 2, kind: "ask", part: "whole" });
  expect(s4.held).toEqual({ token: "t3", parts: "head" });
  const d = apply(c.page, s4, ans({ status: "unchanged", token: "t3", list_token: "L1" }));
  expect(d.applied).toBe(true);
  expect(d.cls).toBe("unchanged");
  expect(d.page.screen).toMatchObject({ status: "shown", token: "t3", parts: "head" });

  // 7. a rest at another token never joins the shown head; one at its token completes it
  const s5 = record(d.page, { seq: 5, epoch: 2, kind: "rest", part: "rest" });
  const e5 = apply(d.page, s5, ans({ status: "parts", token: "t9", complete: true, rest: H, list_token: "L1" }));
  expect(e5.applied).toBe(false);
  expect(e5.page.screen).toMatchObject({ status: "shown", token: "t3", parts: "head" });
  const s6 = record(e5.page, { seq: 6, epoch: 2, kind: "rest", part: "rest" });
  const e6 = apply(e5.page, s6, ans({ status: "parts", token: "t3", complete: true, rest: H, list_token: "L1" }));
  expect(e6.applied).toBe(true);
  expect(e6.page.screen).toMatchObject({ status: "shown", token: "t3", parts: "whole" });
  expect(e6.page.shownSeq).toBe(6);

  // 8. a failed ask over a shown screen marks it and keeps it
  const s7 = record(e6.page, { seq: 7, epoch: 2, kind: "ask", part: "whole" });
  const f = apply(e6.page, s7, { kind: "answer", status: 503, body: { reason: "down" } });
  expect(f.cls).toBe("failure");
  expect(f.page.couldNotUpdate).toBe(true);
  expect(f.page.screen).toMatchObject({ status: "shown", token: "t3", parts: "whole" });

  // 9. an empty store answers no_project: an empty screen, never failed
  const empty = opened({ project: null, view: "spine", filter: null });
  const s = record(empty, { seq: 1, epoch: 0, kind: "ask", part: "whole" });
  const g = apply(empty, s, ans({ status: "no_project", token: null, list_token: "L0", names: [] }));
  expect(g.page.screen).toEqual({ status: "empty" });
  expect(g.cls).toBe("parts");
  expect(g.complete).toBe(true);
  expect(g.page.choice.project).toBeNull();
  expect(g.page.fallback).toBe(true);
  expect(g.page.list?.names).toEqual([]);

  // 10. a switch with project null keeps the fallback, and its sends carry it
  const sw = switched(empty, { project: null, view: "order", filter: null }, 1);
  expect(sw.fallback).toBe(true);
  const sh = record(sw, { seq: 2, epoch: 1, kind: "head", part: "head" });
  expect(input(sh)).toMatchObject({ project: null, fallback: true });

  // 11. an aborted request changes nothing
  const s8 = record(f.page, { seq: 8, epoch: 2, kind: "ask", part: "whole" });
  const h = apply(f.page, s8, { kind: "no_answer", reason: "aborted" });
  expect(h.cls).toBeNull();
  expect(h.page).toEqual(f.page);
});

test("list, run and progress parts", async () => {
  const { opened, record, apply, recordProgress, progressInput, applyProgress } = await import("../src/apply.ts");
  type Page = ReturnType<typeof opened>;

  /** A RunPart with that id and percent, `done` issues of four created; it carries no time left. */
  const rp = (id: string, percent: number, done: number) => ({
    id, phase: "running", step: "creating", stage: "issues", stage_done: done, stage_total: 4,
    percent, total: 4, processed: done, created: done, skipped: 0, failed: 0, started_at: 100,
  });
  const ask = (page: Page, seq: number) => record(page, { seq, epoch: page.epoch, kind: "ask", part: "whole" });
  const asQ = (page: Page): Page => ({ ...page, choice: { project: "q", view: "ship", filter: null } });
  const pages: Page[] = [];

  // Arrange: P shown for p at token t, parts whole, list {["p"], L0, 1}, no origin
  const open = opened({ project: "p", view: "ship", filter: null });
  const P = apply(open, ask(open, 1), ans({
    status: "parts", project: "p", token: "t", complete: true, head: H, rest: H,
    list_token: "L0", names: ["p"], run: null,
  })).page;
  expect(P.screen).toMatchObject({ status: "shown", token: "t", parts: "whole" });
  expect(P.list).toEqual({ names: ["p"], token: "L0", seq: 1 });
  expect(P.origin).toBeNull();
  pages.push(P);

  // 1. the list applies by its own send sequence: an older answer arriving later is not drawn
  const a10 = ask(P, 10);
  let page = apply(P, ask(P, 11), ans({ status: "unchanged", token: "t", list_token: "Lb", names: ["p", "q"], run: null })).page;
  page = apply(page, a10, ans({ status: "unchanged", token: "t", list_token: "La", names: ["p"], run: null })).page;
  expect(page.list).toEqual({ names: ["p", "q"], token: "Lb", seq: 11 });
  pages.push(page);

  // 2. the chosen project's run part is kept and returned and sets the origin at first sight;
  //    a list_token without names confirms only
  let r = apply(page, ask(page, 12), ans({ status: "unchanged", token: "t", list_token: "Lb", run: rp("n:2", 25, 1) }), 1000);
  expect(r.run).toBe("n:2");
  page = r.page;
  expect(page.run?.id).toBe("n:2");
  expect(page.runSeq).toBe(12);
  expect(page.origin).toEqual({ run: "n:2", at: 1000 });
  expect(page.list).toEqual({ names: ["p", "q"], token: "Lb", seq: 11 });
  pages.push(page);

  // 3. a send recorded while the choice was q: its run is neither kept, returned nor sighted
  const s13 = record(asQ(page), { seq: 13, epoch: page.epoch, kind: "ask", part: "whole" });
  r = apply(page, s13, ans({ status: "unchanged", token: "t", list_token: "Lb", run: rp("n:7", 5, 0) }), 1500);
  expect(r.run).toBeNull();
  page = r.page;
  expect(page.run?.id).toBe("n:2");
  expect(page.origin).toEqual({ run: "n:2", at: 1000 });
  pages.push(page);

  // 4. progress answers apply by their own sequence, not arrival order; elapsed is counted on the
  //    page's clock from the first sight at 1000, never the answer's own elapsed
  const sp20 = recordProgress(page, { seq: 20, run: "n:2" });
  const sp21 = recordProgress(page, { seq: 21, run: "n:2" });
  expect(progressInput(sp20)).toEqual({ project: "p", run: "n:2" });
  const p21 = applyProgress(page, sp21, ans({ state: "running", run: rp("n:2", 50, 2), elapsed: 900 }), 3000);
  const p20 = applyProgress(p21.page, sp20, ans({ state: "running", run: rp("n:2", 40, 1), elapsed: 800 }), 3500);
  page = p20.page;
  expect(page.progress?.run?.percent).toBe(50);
  expect(page.progressSeq).toBe(21);
  expect(page.progress?.elapsed).toBe(2);
  expect(page.progress?.samples.points).toEqual([{ at: 3000, done: 2 }]);
  expect([p21.cls, p21.run]).toEqual(["running", "n:2"]);
  expect([p20.cls, p20.run]).toEqual(["running", "n:2"]);
  pages.push(p21.page, page);

  // 5. a failed progress ask keeps the values, elapsed and samples included, not current
  const samples = page.progress?.samples;
  let pr = applyProgress(page, recordProgress(page, { seq: 22, run: "n:2" }), { kind: "answer", status: 503, body: {} }, 4000);
  expect(pr.cls).toBe("failure");
  page = pr.page;
  expect(page.progress?.run?.percent).toBe(50);
  expect(page.progress?.elapsed).toBe(2);
  expect(page.progress?.samples).toEqual(samples);
  expect(page.progress?.current).toBe(false);
  pages.push(page);

  // 6. finishing with no run keeps the last run, counts elapsed again, current again
  pr = applyProgress(page, recordProgress(page, { seq: 23, run: "n:2" }), ans({ state: "finishing", run: null }), 6000);
  expect(pr.cls).toBe("finishing");
  page = pr.page;
  expect(page.progress?.state).toBe("finishing");
  expect(page.progress?.run?.percent).toBe(50);
  expect(page.progress?.elapsed).toBe(5);
  expect(page.progress?.current).toBe(true);
  pages.push(page);

  // 7. ended: the run is remembered as ended, cleared, and its origin dropped
  pr = applyProgress(page, recordProgress(page, { seq: 24, run: "n:2" }), ans({ state: "ended" }), 7000);
  expect(pr.cls).toBe("ended");
  expect(pr.run).toBeNull();
  page = pr.page;
  expect(page.endedRuns).toContain("n:2");
  expect(page.progress).toBeNull();
  expect(page.run).toBeNull();
  expect(page.origin).toBeNull();
  pages.push(page);

  // 8. a later screen answer naming the ended run never draws or sights it, but still returns its id
  r = apply(page, ask(page, 25), ans({ status: "unchanged", token: "t", list_token: "Lb", run: rp("n:2", 100, 4) }), 8000);
  expect(r.page.run).toBeNull();
  expect(r.page.origin).toBeNull();
  expect(r.run).toBe("n:2");
  page = r.page;
  pages.push(page);

  // 9. a progress answer for another project changes nothing and still returns its class
  const spq = recordProgress(asQ(page), { seq: 26, run: "n:7" });
  pr = applyProgress(page, spq, ans({ state: "running", run: rp("n:7", 5, 0), elapsed: 1 }), 9000);
  expect(pr.page).toEqual(page);
  expect(pr.cls).toBe("running");
  pages.push(pr.page);

  // 10. no answer here carried a time left, and no page holds one
  for (const p of pages) expect(JSON.stringify(p)).not.toContain("time_left");
});

test("samples and elapsed origin", async () => {
  const { SAMPLE_MAX, sampled, pressed, unpressed, opened, switched, record, apply, recordProgress, applyProgress } =
    await import("../src/apply.ts");
  type Page = ReturnType<typeof opened>;

  /** A RunPart of run `id` in `stage`, `done` of twenty through it. */
  const rp = (id: string, stage: string, done: number | null) => ({
    id, phase: "running", step: "creating", stage, stage_done: done, stage_total: 20,
    percent: 0, total: 20, processed: 0, created: 0, skipped: 0, failed: 0, started_at: 100,
  });
  const prog = (page: Page, seq: number, run: string | null, body: unknown, now: number) =>
    applyProgress(page, recordProgress(page, { seq, run }), ans(body), now).page;
  const ask = (page: Page, seq: number) => record(page, { seq, epoch: page.epoch, kind: "ask", part: "whole" });
  const fresh = () => opened({ project: "p", view: "ship", filter: null });
  /** A screen answer for the chosen project naming `run`. */
  const screen = (run: unknown) => ans({ status: "preparing", token: null, list_token: "L", run });

  // A. Samples
  expect(SAMPLE_MAX).toBe(10);

  // 1. twelve answers of one stage keep the last ten, oldest first
  let page = fresh();
  let seq = 0;
  for (let i = 0; i <= 11; i++) {
    seq += 1;
    page = prog(page, seq, "n:1", { state: "running", run: rp("n:1", "issues", i), elapsed: 5 }, 1000 * i);
  }
  let points = page.progress?.samples.points ?? [];
  expect(points.length).toBe(10);
  expect(points[0]).toEqual({ at: 2000, done: 2 });
  expect(points[9]).toEqual({ at: 11000, done: 11 });
  expect(page.progress?.samples.run).toBe("n:1");
  expect(page.progress?.samples.stage).toBe("issues");

  // 2. a new stage starts the samples over
  seq += 1;
  page = prog(page, seq, "n:1", { state: "running", run: rp("n:1", "order", 0), elapsed: 5 }, 12000);
  expect(page.progress?.samples.points).toEqual([{ at: 12000, done: 0 }]);
  expect(page.progress?.samples.stage).toBe("order");

  // 3. a stage_done of null adds no point
  seq += 1;
  page = prog(page, seq, "n:1", { state: "running", run: rp("n:1", "order", null), elapsed: 5 }, 13000);
  points = page.progress?.samples.points ?? [];
  expect(points.length).toBe(1);

  // 4. sampled starts from nothing, and starts over for another run
  expect(sampled(null, rp("n:9", "issues", 3), 50).points).toEqual([{ at: 50, done: 3 }]);
  const n1 = page.progress?.samples ?? null;
  expect(n1?.run).toBe("n:1");
  const n9 = sampled(n1, rp("n:9", "order", 1), 60);
  expect(n9).toEqual({ run: "n:9", stage: "order", points: [{ at: 60, done: 1 }] });

  // B. Own press
  // 5. elapsed counts from the press, never from the answer's own elapsed
  let b = pressed(fresh(), 1000);
  expect(b.origin).toEqual({ run: null, at: 1000 });
  b = prog(b, 1, "n:1", { state: "running", run: rp("n:1", "issues", 1), elapsed: 900 }, 3000);
  expect(b.progress?.elapsed).toBe(2);
  b = prog(b, 2, "n:1", { state: "running", run: rp("n:1", "issues", 2), elapsed: 900 }, 5500);
  expect(b.progress?.elapsed).toBe(4.5);
  expect(b.origin).toEqual({ run: "n:1", at: 1000 });

  // 6. unpressed drops only a press that never learned its run
  expect(unpressed(pressed(fresh(), 1000)).origin).toBeNull();
  expect(unpressed(b).origin).toEqual({ run: "n:1", at: 1000 });

  // C. Found run
  // 7. a screen answer naming a run with no origin held is its first sight
  let c = fresh();
  seq = 1;
  c = apply(c, ask(c, seq), screen(rp("n:3", "issues", 1)), 4000).page;
  expect(c.origin).toEqual({ run: "n:3", at: 4000 });
  seq += 1;
  c = prog(c, seq, "n:3", { state: "running", run: rp("n:3", "issues", 2), elapsed: 300 }, 10000);
  expect(c.progress?.elapsed).toBe(6);

  // 8. a switch away drops the origin; back on p the run is sighted afresh
  c = switched(c, { project: "q", view: "ship", filter: null }, 1);
  expect(c.origin).toBeNull();
  expect(c.run).toBeNull();
  expect(c.progress).toBeNull();
  c = switched(c, { project: "p", view: "ship", filter: null }, 2);
  seq += 1;
  c = apply(c, ask(c, seq), screen(rp("n:3", "issues", 3)), 20000).page;
  expect(c.origin).toEqual({ run: "n:3", at: 20000 });
  seq += 1;
  c = prog(c, seq, "n:3", { state: "running", run: rp("n:3", "issues", 4), elapsed: 300 }, 21000);
  expect(c.progress?.elapsed).toBe(1);

  // 9. another run seen with no end of the held one gets its own origin and its own samples
  expect(c.origin?.run).toBe("n:3");
  seq += 1;
  c = apply(c, ask(c, seq), screen(rp("n:5", "issues", 0)), 23000).page;
  expect(c.origin).toEqual({ run: "n:5", at: 23000 });
  seq += 1;
  c = prog(c, seq, "n:5", { state: "running", run: rp("n:5", "issues", 1), elapsed: 300 }, 24000);
  expect(c.progress?.elapsed).toBe(1);
  expect(c.progress?.samples.points).toEqual([{ at: 24000, done: 1 }]);

  // 10. an end drops the origin; the ended run is never sighted again; a new run is
  seq += 1;
  c = prog(c, seq, "n:5", { state: "ended" }, 25000);
  expect(c.origin).toBeNull();
  expect(c.endedRuns).toContain("n:5");
  seq += 1;
  c = apply(c, ask(c, seq), screen(rp("n:5", "issues", 1)), 26000).page;
  expect(c.origin).toBeNull();
  seq += 1;
  c = apply(c, ask(c, seq), screen(rp("n:4", "issues", 0)), 30000).page;
  expect(c.origin).toEqual({ run: "n:4", at: 30000 });
  seq += 1;
  c = prog(c, seq, "n:4", { state: "running", run: rp("n:4", "issues", 1), elapsed: 300 }, 32000);
  expect(c.progress?.elapsed).toBe(2);

  // 11. an end for a send with no run keeps a press still awaiting its run
  const e = prog(pressed(fresh(), 1000), 1, null, { state: "ended" }, 2000);
  expect(e.origin).toEqual({ run: null, at: 1000 });
});
