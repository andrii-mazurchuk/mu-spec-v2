// The page's one drawing function (S-254): T-216's first step, that shownRun takes the Ship run from
// the latest progress answer when one holds a run, else from the screen read's run part with no
// elapsed time and current, and is null when the page holds neither. The drawing itself is judged
// in headless Chromium by tests/dash/test_views_draw.py and tests/dash/test_views_press.py.
//
// The module under test is imported inside each test, so a module not yet written fails only the
// tests that need it.

import { expect, test } from "bun:test";

/** A RunPart with that id, step and percent. */
const rp = (id: string, step: string, percent: number) => ({
  id, step, percent, total: 4, processed: 1, created: 1, skipped: 0, failed: 0,
  time_left: 9, started_at: 100,
});

test("shownRun", async () => {
  const { opened } = await import("../src/apply.ts");
  const { shownRun } = await import("../src/views.ts");

  const base = opened({ project: "p", view: "spine", filter: null });
  const run1 = rp("n:1", "creating", 40);
  const prog = { seq: 3, state: "running", run: rp("n:1", "creating", 60), elapsed: 12, current: true };

  // the screen read's run part alone: no elapsed time, current
  expect(shownRun({ ...base, run: run1 })).toEqual({ run: run1, elapsed: null, current: true });

  // a progress answer holding the run wins over the run part
  const withProgress = shownRun({ ...base, run: run1, progress: prog });
  expect(withProgress?.run.percent).toBe(60);
  expect(withProgress?.elapsed).toBe(12);
  expect(withProgress?.current).toBe(true);

  // a progress answer kept after a failed ask is not current
  expect(shownRun({ ...base, run: run1, progress: { ...prog, current: false } })?.current).toBe(false);

  // no run part and no progress: nothing to show
  expect(shownRun(base)).toBeNull();
});
