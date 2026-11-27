// The page's request records and apply rules (S-203): T-156, that the screen part changes only for
// the current epoch and a newer send, applies the open fallback and no_project, fills a head in only
// at its own token and marks a failed ask over a shown screen; and T-157, that the list part applies
// by its own sequence, the chosen project's run part is kept and returned, an ended run is never drawn
// again, and progress answers apply by project and sequence, a failure keeping the values not current.
//
// The module under test is imported inside each test, so a module not yet written fails only the
// tests that need it.

import { expect, test } from "bun:test";

/** A 200 answer carrying `body`, as the network module gives it. */
const ans = (body: unknown) => ({ kind: "answer", status: 200, body });
const H = { fields: {}, items: [] };

test("screen part gating", async () => {
  const { opened, switched, record, input, apply } = await import("../src/apply.ts");

  // 1. page open sends the remembered project as a fallback
  const page = opened({ project: "gone", view: "spine", filter: null });
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

/** A RunPart with that id and percent. */
const rp = (id: string, percent: number) => ({
  id, step: "creating", percent, total: 4, processed: 1, created: 1, skipped: 0, failed: 0,
  time_left: 9, started_at: 100,
});

test("list, run and progress parts", async () => {
  const { opened, switched, record, apply, recordProgress, progressInput, applyProgress } =
    await import("../src/apply.ts");

  // Arrange: P shown for p at token t, parts whole, list {["p"], L0, 1}
  const open = opened({ project: "p", view: "ship", filter: null });
  const s1 = record(open, { seq: 1, epoch: 0, kind: "ask", part: "whole" });
  const P = apply(open, s1, ans({
    status: "parts", project: "p", token: "t", complete: true, head: H, rest: H,
    list_token: "L0", names: ["p"], run: null,
  })).page;
  expect(P.screen).toMatchObject({ status: "shown", token: "t", parts: "whole" });
  expect(P.list).toEqual({ names: ["p"], token: "L0", seq: 1 });
  const ask = (seq: number) => record(P, { seq, epoch: P.epoch, kind: "ask", part: "whole" });

  // 1. the list applies by its own send sequence: an older answer arriving later is not drawn
  let page = apply(P, ask(11), ans({ status: "unchanged", token: "t", list_token: "Lb", names: ["p", "q"], run: null })).page;
  page = apply(page, ask(10), ans({ status: "unchanged", token: "t", list_token: "La", names: ["p"], run: null })).page;
  expect(page.list).toEqual({ names: ["p", "q"], token: "Lb", seq: 11 });

  // 2. the chosen project's run part is kept and returned; a list_token without names confirms only
  let r = apply(page, ask(12), ans({ status: "unchanged", token: "t", list_token: "Lb", run: rp("n:2", 25) }));
  expect(r.run).toBe("n:2");
  page = r.page;
  expect(page.run?.id).toBe("n:2");
  expect(page.run).toEqual(rp("n:2", 25));
  expect(page.runSeq).toBe(12);
  expect(page.list).toEqual({ names: ["p", "q"], token: "Lb", seq: 11 });

  // 3. a send recorded while the choice was q: its run is neither kept nor returned
  const qPage = switched(page, { project: "q", view: "ship", filter: null }, page.epoch + 1);
  const s13 = record(qPage, { seq: 13, epoch: qPage.epoch, kind: "ask", part: "whole" });
  r = apply(page, s13, ans({ status: "unchanged", token: "t", list_token: "Lb", run: rp("n:7", 5) }));
  expect(r.run).toBeNull();
  page = r.page;
  expect(page.run?.id).toBe("n:2");

  // 4. progress answers apply by their own sequence, not arrival order
  const sp20 = recordProgress(page, { seq: 20, run: "n:2" });
  const sp21 = recordProgress(page, { seq: 21, run: "n:2" });
  expect(progressInput(sp20)).toEqual({ project: "p", run: "n:2" });
  const p21 = applyProgress(page, sp21, ans({ state: "running", run: rp("n:2", 50), elapsed: 7 }));
  const p20 = applyProgress(p21.page, sp20, ans({ state: "running", run: rp("n:2", 40), elapsed: 6 }));
  page = p20.page;
  expect(page.progress?.run?.percent).toBe(50);
  expect(page.progress).toEqual({ seq: 21, state: "running", run: rp("n:2", 50), elapsed: 7, current: true });
  expect(page.progressSeq).toBe(21);
  expect([p21.cls, p21.run]).toEqual(["running", "n:2"]);
  expect([p20.cls, p20.run]).toEqual(["running", "n:2"]);

  // 5. a failed progress ask keeps the values, not current
  let pr = applyProgress(page, recordProgress(page, { seq: 22, run: "n:2" }), { kind: "answer", status: 503, body: {} });
  expect(pr.cls).toBe("failure");
  page = pr.page;
  expect(page.progress?.run?.percent).toBe(50);
  expect(page.progress?.elapsed).toBe(7);
  expect(page.progress?.current).toBe(false);

  // 6. finishing with no run keeps the last run and elapsed, current again
  pr = applyProgress(page, recordProgress(page, { seq: 23, run: "n:2" }), ans({ state: "finishing", run: null }));
  expect(pr.cls).toBe("finishing");
  page = pr.page;
  expect(page.progress?.state).toBe("finishing");
  expect(page.progress?.run?.percent).toBe(50);
  expect(page.progress?.elapsed).toBe(7);
  expect(page.progress?.current).toBe(true);

  // 7. ended: the run is remembered as ended and cleared
  pr = applyProgress(page, recordProgress(page, { seq: 24, run: "n:2" }), ans({ state: "ended" }));
  expect(pr.cls).toBe("ended");
  expect(pr.run).toBeNull();
  page = pr.page;
  expect(page.endedRuns).toContain("n:2");
  expect(page.progress).toBeNull();
  expect(page.run).toBeNull();

  // 8. a later screen answer naming the ended run never draws it, but still returns its id
  r = apply(page, ask(25), ans({ status: "unchanged", token: "t", list_token: "Lb", run: rp("n:2", 100) }));
  expect(r.page.run).toBeNull();
  expect(r.run).toBe("n:2");
  page = r.page;

  // 9. a progress answer for another project changes nothing and still returns its class
  const spq = recordProgress(qPage, { seq: 26, run: "n:7" });
  pr = applyProgress(page, spq, ans({ state: "running", run: rp("n:7", 5), elapsed: 1 }));
  expect(pr.page).toEqual(page);
  expect(pr.cls).toBe("running");
});
