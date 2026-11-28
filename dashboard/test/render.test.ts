// render.ts's side mark and text-only path (S-509): T-488, that darkSide holds only for a top-level
// page that prefers dark, markSide adds DARK_MARK to the root exactly then, and el sets every string
// as a text node and a slice by its data attribute and today's slice variable.
//
// render.ts is imported inside the test, after a fake document is in place, so a module not yet
// written fails only this test.

import { expect, test } from "bun:test";

type FakeElement = {
  tagName: string;
  dataset: Record<string, string>;
  style: { props: Record<string, string>; setProperty(p: string, v: string): void };
  kids: unknown[];
  listeners: [string, () => void][];
  className?: string;
  append(...c: unknown[]): void;
  appendChild(c: unknown): unknown;
  replaceChildren(...c: unknown[]): void;
  addEventListener(t: string, f: () => void): void;
};

/** A fake document: elements record their children, styles and listeners; text nodes are marked. */
const fakeDocument = {
  createElement(tag: string): FakeElement {
    return {
      tagName: tag,
      dataset: {},
      style: {
        props: {},
        setProperty(p: string, v: string) {
          this.props[p] = v;
        },
      },
      kids: [],
      listeners: [],
      append(...c: unknown[]) {
        this.kids.push(...c);
      },
      appendChild(c: unknown) {
        this.kids.push(c);
        return c;
      },
      replaceChildren(...c: unknown[]) {
        this.kids = [...c];
      },
      addEventListener(t: string, f: () => void) {
        this.listeners.push([t, f]);
      },
    };
  },
  createTextNode(s: string) {
    return { text: s, isText: true };
  },
};

type FakeWindow = {
  matchMedia(q: string): { matches: boolean };
  asked: string[];
  document: { documentElement: { classes: string[]; classList: { add(c: string): void } } };
  self?: unknown;
  top?: unknown;
};

/** A fake window, top-level when top, whose colour-scheme query answers matches. */
function win(top: boolean, matches: boolean): FakeWindow {
  const w: FakeWindow = {
    matchMedia(q: string) {
      w.asked.push(q);
      return { matches };
    },
    asked: [],
    document: {
      documentElement: {
        classes: [],
        classList: {
          add(c: string) {
            w.document.documentElement.classes.push(c);
          },
        },
      },
    },
  };
  w.self = w;
  w.top = top ? w : {};
  return w;
}

test("side mark and el", async () => {
  const g = globalThis as unknown as { document: unknown };
  const saved = g.document;
  g.document = fakeDocument;
  try {
    const r = await import("../src/render.ts");
    const asWindow = (w: FakeWindow) => w as unknown as Window;

    // 1. dark only for a top-level page that prefers dark, asked by today's query
    const windows = [win(true, true), win(false, true), win(true, false)];
    expect(windows.map((w) => r.darkSide(asWindow(w)))).toEqual([true, false, false]);
    for (const w of windows) {
      for (const q of w.asked) expect(q).toBe("(prefers-color-scheme: dark)");
    }

    // 2. markSide marks the root with DARK_MARK exactly when darkSide holds
    expect(r.DARK_MARK).toBe("dark");
    const a = win(true, true);
    r.markSide(asWindow(a));
    expect(a.document.documentElement.classes).toEqual([r.DARK_MARK]);
    const b = win(false, true);
    r.markSide(asWindow(b));
    expect(b.document.documentElement.classes).toEqual([]);
    const c = win(true, false);
    r.markSide(asWindow(c));
    expect(c.document.documentElement.classes).toEqual([]);

    // 3. each side's selector, the dark one keyed on DARK_MARK
    expect(r.SIDE_CONDITION).toEqual({ light: ":root", dark: ".dark" });
    expect(r.SIDE_CONDITION.dark).toBe("." + r.DARK_MARK);

    // 4. a slice by its data attribute and today's slice variable, index 0 being --slice-1
    expect(r.SLICE_DATA).toBe("slice");
    expect(r.SLICE_PROPERTIES.length).toBeGreaterThan(0);
    const e = r.el("span", { slice: 3 }) as unknown as FakeElement;
    expect(e.dataset[r.SLICE_DATA]).toBe("3");
    for (const p of r.SLICE_PROPERTIES) expect([p, e.style.props[p]]).toEqual([p, "var(--slice-4)"]);
    const f = r.el("span", { slice: null }) as unknown as FakeElement;
    expect(f.dataset.slice).toBe("none");
    for (const p of r.SLICE_PROPERTIES) expect([p, f.style.props[p]]).toEqual([p, "var(--slice-none)"]);
    const z = r.el("span", { slice: 0 }) as unknown as FakeElement;
    expect(z.style.props[r.SLICE_PROPERTIES[0]]).toBe("var(--slice-1)");

    // 5. every string child a text node, null skipped
    const g5 = r.el(
      "div",
      { cls: "x" },
      "<b>x</b>",
      null,
      r.el("em", {}),
    ) as unknown as FakeElement;
    expect(g5.className).toBe("x");
    expect(g5.kids.length).toBe(2);
    expect(g5.kids[0]).toEqual({ text: "<b>x</b>", isText: true });
    expect((g5.kids[1] as FakeElement).tagName).toBe("em");
    for (const k of g5.kids) expect(typeof k).not.toBe("string");

    // 6. mount replaces the children by the same conversion
    r.mount(g5 as unknown as Element, "y");
    expect(g5.kids).toEqual([{ text: "y", isText: true }]);

    // 7. onClick becomes one click listener that calls the handler
    let called = 0;
    const h = () => {
      called += 1;
    };
    const btn = r.el("button", { onClick: h }) as unknown as FakeElement;
    expect(btn.listeners.length).toBe(1);
    expect(btn.listeners[0][0]).toBe("click");
    btn.listeners[0][1]();
    expect(called).toBe(1);
  } finally {
    g.document = saved;
  }
});
