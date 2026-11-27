// The classification of an action's answer (S-217): T-180, that classifyAction confirms only a
// success holding every confirmation key and refuses only a text of the action's own refusal forms,
// given exactly, and T-181, that every other answer is unknown by its kind and ACTION_DEADLINE_MS
// sits a fixed margin past NODE_TIMEOUT_MS.
//
// The modules under test are imported inside each test, so a module not yet written fails only the
// tests that need it. The tables are read from the modules, never copied.

import { expect, test } from "bun:test";

type RefusalForm = { name: string; status: number; field: "error" | "reason"; pattern: string };
type NetResult =
  | { kind: "answer"; status: number; body: unknown }
  | { kind: "no_answer"; reason: "aborted" | "network" };

const FIELD = "[\\s\\S]*?";
const FOREIGN = "zz-not-a-catalogue-text-zz";

function answer(status: number, body: unknown): NetResult {
  return { kind: "answer", status, body };
}

/** A text the form's pattern matches: the literal parts unescaped, each field filled with 'Q7'. */
function textOf(m: RefusalForm): string {
  let s = m.pattern;
  if (s.startsWith("^")) s = s.slice(1);
  if (s.endsWith("$")) s = s.slice(0, -1);
  s = s.split(FIELD).join("Q7");
  return s.replace(/\\([\\^$.*+?()[\]{}|/])/g, "$1");
}

/** Whether some member of `forms` with this status and field matches `text`. */
function anyMatches(forms: readonly RefusalForm[], status: number, field: string, text: string): boolean {
  return forms.some((n) => n.status === status && n.field === field && new RegExp(n.pattern).test(text));
}

test("confirms and refuses only in the unit's form", async () => {
  const { classifyAction, CONFIRM_KEYS, REFUSAL_FORMS } = await import("../src/outcome.ts");
  const { ACTION_NAMES } = await import("../src/actions.ts");
  const confirmKeys = CONFIRM_KEYS as Record<string, readonly string[]>;
  const refusalForms = REFUSAL_FORMS as Record<string, readonly RefusalForm[]>;
  const classify = classifyAction as (a: string, r: NetResult) => unknown;
  const names: readonly string[] = ACTION_NAMES;

  const ok = (a: string): Record<string, unknown> => {
    const body: Record<string, unknown> = {};
    for (const k of confirmKeys[a]) body[k] = 1;
    body.zz_extra = 2;
    return body;
  };

  let foreignChecked = 0;
  for (const a of names) {
    const forms = refusalForms[a];
    // preconditions: every member's text is one its own pattern matches
    for (const m of forms) expect([a, m.name, new RegExp(m.pattern).test(textOf(m))]).toEqual([a, m.name, true]);

    // 1, 2. a success holding every key is confirmed with its body, extra keys kept
    expect([a, classify(a, answer(200, ok(a)))]).toEqual([a, { kind: "confirmed", body: ok(a) }]);
    expect([a, classify(a, answer(201, ok(a)))]).toEqual([a, { kind: "confirmed", body: ok(a) }]);

    // 3. a success missing any one key is foreign
    for (const k of confirmKeys[a]) {
      const body = ok(a);
      delete body[k];
      expect([a, k, classify(a, answer(200, body))]).toEqual([a, k, { kind: "unknown", why: "foreign" }]);
    }

    // 4. a success whose body is not a JSON object is foreign
    expect([a, classify(a, answer(200, null))]).toEqual([a, { kind: "unknown", why: "foreign" }]);
    expect([a, classify(a, answer(200, [ok(a)]))]).toEqual([a, { kind: "unknown", why: "foreign" }]);

    for (const m of forms) {
      const text = textOf(m);

      // 5. the form's own text under its own field is refused, the text given exactly
      const refused = classify(a, answer(m.status, { [m.field]: text }));
      expect([a, m.name, refused]).toEqual([a, m.name, { kind: "refused", status: m.status, message: text }]);
      expect((refused as { message: string }).message === text).toBe(true);

      // 6. the same text under the other field is foreign, unless a form of that field takes it
      const other = m.field === "reason" ? "error" : "reason";
      if (!anyMatches(forms, m.status, other, text)) {
        expect([a, m.name, other, classify(a, answer(m.status, { [other]: text }))]).toEqual([
          a, m.name, other, { kind: "unknown", why: "foreign" },
        ]);
      }

      // 7. a refusal status carrying a text no form of it takes is foreign
      if (!anyMatches(forms, m.status, m.field, FOREIGN)) {
        foreignChecked++;
        expect([a, m.name, classify(a, answer(m.status, { [m.field]: FOREIGN }))]).toEqual([
          a, m.name, { kind: "unknown", why: "foreign" },
        ]);
      }
    }
  }
  expect(foreignChecked).toBeGreaterThanOrEqual(1);
});

test("every other answer is unknown, by its kind", async () => {
  const { classifyAction, REFUSAL_FORMS, ACTION_MARGIN_MS, ACTION_DEADLINE_MS } = await import("../src/outcome.ts");
  const { ACTION_NAMES } = await import("../src/actions.ts");
  const { NODE_TIMEOUT_MS } = await import("../src/scheduler.ts");
  const refusalForms = REFUSAL_FORMS as Record<string, readonly RefusalForm[]>;
  const classify = classifyAction as (a: string, r: NetResult) => unknown;
  const names: readonly string[] = ACTION_NAMES;

  // preconditions: no form uses 418, and no 502 or 504 form takes the node's own error texts
  for (const a of names) {
    const forms = refusalForms[a];
    expect([a, forms.filter((m) => m.status === 418).map((m) => m.name)]).toEqual([a, []]);
    for (const status of [502, 504]) {
      for (const text of ["upstream timed out", "bad gateway"]) {
        for (const field of ["error", "reason"]) {
          expect([a, status, field, text, anyMatches(forms, status, field, text)]).toEqual([
            a, status, field, text, false,
          ]);
        }
      }
    }
  }

  for (const a of names) {
    expect([a, classify(a, { kind: "no_answer", reason: "network" })]).toEqual([a, { kind: "unknown", why: "dropped" }]);
    expect([a, classify(a, { kind: "no_answer", reason: "aborted" })]).toEqual([a, { kind: "unknown", why: "deadline" }]);
    expect([a, classify(a, answer(504, { error: "upstream timed out" }))]).toEqual([
      a, { kind: "unknown", why: "gave_up" },
    ]);
    expect([a, classify(a, answer(502, { error: "bad gateway" }))]).toEqual([a, { kind: "unknown", why: "gave_up" }]);
    expect([a, classify(a, answer(504, null))]).toEqual([a, { kind: "unknown", why: "gave_up" }]);
    expect([a, classify(a, answer(418, { error: "teapot" }))]).toEqual([a, { kind: "unknown", why: "foreign" }]);
  }

  expect(ACTION_MARGIN_MS).toBe(2000);
  expect(ACTION_DEADLINE_MS).toBe(NODE_TIMEOUT_MS + ACTION_MARGIN_MS);
  expect(ACTION_DEADLINE_MS).toBe(7000);
  expect(ACTION_DEADLINE_MS).toBeGreaterThan(NODE_TIMEOUT_MS);
});
