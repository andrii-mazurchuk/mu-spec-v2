// The pure scheduler (S-239): one state machine decides when every dashboard read is sent.
// step(state, event) returns the reads to send, the reads to abort and the next wake time; timers,
// visibility listeners and the action port only deliver events to it, so a simulated clock can
// drive it. Times are the page's monotonic milliseconds (performance.now()), never the wall clock.
//
// The background rhythm (lastAskAt, failures, outstanding) and the Ship progress track sit side by
// side: no progress ask, progress outcome, Ship press or end-of-run refresh reads or writes the
// rhythm's fields, so every background ask goes at the same instant as with no run.

export const ASK_INTERVAL_DEFAULT_MS = 10000; // ask_interval
export const ASK_INTERVAL_MIN_MS = 10000;
export const BACKOFF_CEILING_MS = 60000; // backoff_ceiling
export const SHIP_PROGRESS_INTERVAL_MS = 1000; // ship_progress_interval (B-87)
export const NODE_TIMEOUT_MS = 5000; // node_timeout (B-41): a progress ask with no outcome by then is aborted

export type Kind = "ask" | "refresh" | "head" | "rest" | "after_action" | "progress" | "end_refresh";
/** Progress sends carry "whole" and the wiring ignores it. */
export type PartWanted = "head" | "rest" | "whole";
export type OutcomeClass = "parts" | "unchanged" | "preparing" | "failure";
export type ProgressClass = "running" | "finishing" | "ended" | "failure";

export type Event =
  | { type: "time"; now: number }
  | { type: "hidden"; now: number }
  | { type: "visible"; now: number }
  /** A screen-read outcome; run: the run id its run part names when the answer is for the project now chosen, else null. */
  | { type: "outcome"; now: number; seq: number; cls: OutcomeClass; applied: boolean; complete: boolean; run: string | null }
  /** run: the id a running or finishing answer names. */
  | { type: "progress"; now: number; seq: number; cls: ProgressClass; run: string | null }
  | { type: "switch"; now: number; projectChanged: boolean }
  | { type: "refresh"; now: number }
  | { type: "action_confirmed"; now: number }
  /** The page sent its own Ship press for the shown project. */
  | { type: "ship_sent"; now: number }
  /** That press ended done, failed or not confirmed. */
  | { type: "ship_settled"; now: number };

/** run: the tracked run id, progress sends only; null otherwise. */
export type Send = { seq: number; epoch: number; kind: Kind; part: PartWanted; run: string | null };

export type Track = {
  readonly run: string | null;
  readonly pressAwaited: boolean;
  readonly outstanding: { seq: number; deadline: number } | null;
  readonly dueAt: number | null;
};

export type State = {
  readonly interval: number;
  readonly visible: boolean;
  readonly seq: number;
  readonly epoch: number;
  readonly lastAskAt: number | null;
  readonly failures: number;
  readonly outstanding: { seq: number; deadline: number } | null;
  readonly inflight: ReadonlyMap<number, { kind: Kind; epoch: number }>;
  readonly track: Track | null;
  readonly seenEnded: ReadonlySet<string>;
};

export type Step = { state: State; sends: Send[]; aborts: number[]; wakeAt: number | null };

/** The background wait after `failures` consecutive failed asks. */
export function waitInForce(interval: number, failures: number): number {
  if (failures === 0) return interval;
  return Math.max(interval, Math.min(BACKOFF_CEILING_MS, interval * 2 ** failures));
}

export function init(interval: number = ASK_INTERVAL_DEFAULT_MS): State {
  if (!(interval >= ASK_INTERVAL_MIN_MS)) {
    throw new RangeError(`ask interval ${interval} is below ${ASK_INTERVAL_MIN_MS} ms`);
  }
  return {
    interval,
    visible: false,
    seq: 0,
    epoch: 0,
    lastAskAt: null,
    failures: 0,
    outstanding: null,
    inflight: new Map(),
    track: null,
    seenEnded: new Set(),
  };
}

export function step(s: State, e: Event): Step {
  let visible = s.visible;
  let seq = s.seq;
  let epoch = s.epoch;
  let lastAskAt = s.lastAskAt;
  let failures = s.failures;
  let outstanding = s.outstanding;
  let track = s.track;
  const inflight = new Map(s.inflight);
  const seenEnded = new Set(s.seenEnded);
  const sends: Send[] = [];
  const aborts: number[] = [];

  const send = (kind: Kind, part: PartWanted, run: string | null = null): number => {
    seq += 1;
    sends.push({ seq, epoch, kind, part, run });
    inflight.set(seq, { kind, epoch });
    return seq;
  };

  // 1. the event
  switch (e.type) {
    case "hidden":
      visible = false;
      break;
    case "visible":
      visible = true;
      if (track && !track.outstanding) track = { ...track, dueAt: e.now };
      break;
    case "switch":
      epoch += 1;
      if (e.projectChanged && track) {
        if (track.outstanding) {
          aborts.push(track.outstanding.seq);
          inflight.delete(track.outstanding.seq);
        }
        track = null;
      }
      if (visible) send("head", "head");
      break;
    case "refresh":
      if (visible) send("refresh", "whole");
      break;
    case "action_confirmed":
      if (visible) send("after_action", "whole");
      break;
    case "outcome": {
      const entry = inflight.get(e.seq);
      if (!entry) break;
      inflight.delete(e.seq);
      if (outstanding && outstanding.seq === e.seq) {
        outstanding = null;
        failures = e.cls === "failure" ? failures + 1 : 0;
      }
      if (
        entry.kind === "head" &&
        e.cls === "parts" &&
        e.applied &&
        !e.complete &&
        entry.epoch === epoch &&
        visible
      ) {
        send("rest", "rest");
      }
      if (e.run !== null && !seenEnded.has(e.run)) {
        if (!track) track = { run: e.run, pressAwaited: false, outstanding: null, dueAt: e.now };
        else if (track.run === null) track = { ...track, run: e.run };
      }
      break;
    }
    case "progress": {
      if (!inflight.has(e.seq)) break;
      inflight.delete(e.seq);
      if (!track || !track.outstanding || track.outstanding.seq !== e.seq) break;
      const next = e.now + SHIP_PROGRESS_INTERVAL_MS;
      if (e.cls === "running" || e.cls === "finishing") {
        track = { ...track, outstanding: null, run: e.run ?? track.run, dueAt: next };
      } else if (e.cls === "failure") {
        track = { ...track, outstanding: null, dueAt: next };
      } else if (!track.pressAwaited) {
        if (track.run !== null) seenEnded.add(track.run);
        track = null;
        if (visible) send("end_refresh", "whole");
      } else {
        if (track.run !== null) seenEnded.add(track.run);
        track = { ...track, outstanding: null, run: null, dueAt: next };
      }
      break;
    }
    case "ship_sent":
      track = track
        ? { ...track, pressAwaited: true }
        : { run: null, pressAwaited: true, outstanding: null, dueAt: e.now + SHIP_PROGRESS_INTERVAL_MS };
      break;
    case "ship_settled":
      if (track) track = { ...track, pressAwaited: false };
      break;
    case "time":
      break;
  }

  // 2. abort what has passed its deadline
  if (outstanding && e.now >= outstanding.deadline) {
    aborts.push(outstanding.seq);
    inflight.delete(outstanding.seq);
    outstanding = null;
    failures += 1;
  }
  if (track && track.outstanding && e.now >= track.outstanding.deadline) {
    aborts.push(track.outstanding.seq);
    inflight.delete(track.outstanding.seq);
    track = { ...track, outstanding: null, dueAt: e.now + SHIP_PROGRESS_INTERVAL_MS };
  }

  // 3. the due background ask
  let due: number | null = null;
  if (visible && !outstanding) {
    due = lastAskAt === null ? e.now : lastAskAt + waitInForce(s.interval, failures);
    if (e.now >= due) {
      const wait = waitInForce(s.interval, failures);
      const askSeq = send("ask", "whole");
      lastAskAt = e.now;
      outstanding = { seq: askSeq, deadline: e.now + wait };
    }
  }

  // 4. the due progress ask
  if (visible && track && !track.outstanding && track.dueAt !== null && e.now >= track.dueAt) {
    const progressSeq = send("progress", "whole", track.run);
    track = { ...track, outstanding: { seq: progressSeq, deadline: e.now + NODE_TIMEOUT_MS }, dueAt: null };
  }

  // 5. the next wake time
  const background = outstanding ? outstanding.deadline : visible ? due : null;
  const progress = track
    ? track.outstanding
      ? track.outstanding.deadline
      : visible
        ? track.dueAt
        : null
    : null;
  const wakeAt = background === null ? progress : progress === null ? background : Math.min(background, progress);

  return {
    state: { interval: s.interval, visible, seq, epoch, lastAskAt, failures, outstanding, inflight, track, seenEnded },
    sends,
    aborts,
    wakeAt,
  };
}
