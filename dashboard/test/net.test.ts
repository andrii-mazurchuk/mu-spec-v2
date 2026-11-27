// The page's one caller of the network (S-253): T-153, that request refuses a path that is not
// relative before any fetch, sends today's fetch options, answers a non-JSON body as null, and turns
// a fetch rejection into no_answer aborted or network without throwing.
//
// The module under test is imported inside each test, so a module not yet written fails only the
// tests that need it.

import { expect, test } from "bun:test";

type Call = { path: string; init: RequestInit };

/** Replaces globalThis.fetch with a stub that records each call and answers through `answer`. */
function stubFetch(answer: (path: string, init: RequestInit) => Promise<Response>): Call[] {
  const calls: Call[] = [];
  globalThis.fetch = ((path: string, init: RequestInit) => {
    calls.push({ path, init });
    return answer(path, init);
  }) as unknown as typeof fetch;
  return calls;
}

test("request and isRelative", async () => {
  const { isRelative, request, NotRelative, SCREEN_PATH, PROGRESS_PATH } = await import("../src/net.ts");
  const realFetch = globalThis.fetch;
  try {
    // 1. relativity
    for (const p of ["dash/screen", "dash/progress", SCREEN_PATH, PROGRESS_PATH, "project/p/ratify"]) {
      expect([p, isRelative(p)]).toEqual([p, true]);
    }
    for (const p of ["", "/dash/screen", "//x/y", "https://x", "\\x", "a:b"]) {
      expect([p, isRelative(p)]).toEqual([p, false]);
    }
    expect(isRelative("a/b:c")).toBe(true);

    // 2. a path that is not relative throws synchronously, before any fetch
    let calls = stubFetch(async () => new Response('{"a":1}', { status: 200 }));
    let thrown: unknown;
    try {
      request("POST", "/dash/screen", {});
    } catch (e) {
      thrown = e;
    }
    expect(thrown).toBeInstanceOf(NotRelative);
    expect(thrown).toBeInstanceOf(Error);
    expect((thrown as InstanceType<typeof NotRelative>).path).toBe("/dash/screen");
    expect(calls).toEqual([]);

    // 3. a JSON answer, and today's fetch options for a request with a body
    calls = stubFetch(async () => new Response('{"a":1}', { status: 200 }));
    expect(await request("POST", "dash/screen", { x: 1 })).toEqual({ kind: "answer", status: 200, body: { a: 1 } });
    expect(calls.length).toBe(1);
    expect(calls[0].path).toBe("dash/screen");
    const init = calls[0].init;
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "content-type": "application/json" });
    expect(init.body).toBe('{"x":1}');
    expect(init.credentials).toBe("same-origin");
    expect(init.cache).toBe("no-store");
    expect(init.redirect).toBe("error");

    // 4. no body: no content-type and no body
    calls = stubFetch(async () => new Response('{"a":1}', { status: 200 }));
    await request("GET", "dash/screen", undefined);
    expect(calls.length).toBe(1);
    expect(calls[0].init.method).toBe("GET");
    expect(calls[0].init.headers).toEqual({});
    expect(calls[0].init.body).toBeUndefined();

    // 5. the node giving up is an answer, its non-JSON body null
    stubFetch(async () => new Response("Gateway Timeout", { status: 504 }));
    expect(await request("GET", "dash/screen", undefined)).toEqual({ kind: "answer", status: 504, body: null });

    // 6. a rejection is no answer, never a throw
    stubFetch(async () => {
      throw new TypeError("Failed to fetch");
    });
    expect(await request("GET", "dash/screen", undefined)).toEqual({ kind: "no_answer", reason: "network" });

    const controller = new AbortController();
    controller.abort();
    calls = stubFetch(async () => {
      throw new DOMException("The operation was aborted.", "AbortError");
    });
    expect(await request("GET", "dash/screen", undefined, controller.signal)).toEqual({
      kind: "no_answer",
      reason: "aborted",
    });
    expect(calls.length).toBe(1);
    expect(calls[0].init.signal).toBe(controller.signal);
  } finally {
    globalThis.fetch = realFetch;
  }
});
