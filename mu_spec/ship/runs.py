"""The in-memory record of each project's last Ship or rollback run.

One project has at most one run of any kind in flight. Run ids are random, never derived from the
project, the time or an earlier id. A project's generation moves only when a run ends. The run
marker is raised at each admission, progress report and end, inside the registry's lock, so its
observer is told the project after the run state has changed. Nothing here reads or writes the
store: a restart leaves no run, no busy marker and no end point.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass

from mu_spec.outside import Clock

RUN_ID_BYTES = 6        # secrets.token_hex(RUN_ID_BYTES): 12 lowercase hex characters, e.g. '9f2c4e81a07b'
NONCE_BYTES = 8         # secrets.token_hex(NONCE_BYTES): 16 lowercase hex characters, the marker's nonce
KINDS = ("person_ship", "delegated_ship", "rollback")
PHASES = ("running", "done", "failed")
SHIP_STEPS = ("withdrawing", "editing", "creating", "wiring", "sweeping")
ROLLBACK_STEPS = ("closing",)
VIEW_FIELDS = ("run_id", "phase", "step", "total", "processed", "created", "skipped", "failed",
               "wired", "unwired", "edited", "withdrawn", "touched", "started_at", "finished_at",
               "error", "result")
COUNTER_FIELDS = ("total", "processed", "created", "skipped", "failed", "wired", "unwired",
                  "edited", "withdrawn", "touched")   # total starts None, the rest 0
LATE_FIELDS = ("to_wire", "adopted", "swept", "closed")  # present in the view only once the run has reported them
STAGES = ("issues", "order")   # the stage a Ship's progress is shown for: creating issues, then declaring order
ORDER_FROM_STEP = "wiring"     # the SHIP_STEPS member from which, with to_wire above 0, the stage is 'order'
PROGRESS_FIELDS = ("admitted", "stage", "stage_done", "stage_total", "percent")   # in the progress view only

_ORDER_STEPS = SHIP_STEPS[SHIP_STEPS.index(ORDER_FROM_STEP):]


class Busy(Exception):
    """A run is already in flight for the project."""

    def __init__(self, run_id: str) -> None:
        super().__init__(run_id)
        self.run_id = run_id


class Stale(Exception):
    """The project's generation moved since the caller read it."""


@dataclass
class Run:
    run_id: str
    project: str
    kind: str                       # one of KINDS
    inputs: object                  # what admission read (ShipInputs or RollbackInputs of the plan entry)
    counters: dict                  # COUNTER_FIELDS, then LATE_FIELDS as reported, in first-report order
    admitted: int = 0               # the counter marker.raise_ returned at this run's admission
    phase: str = "running"
    step: str | None = None
    reached_order_step: bool = False   # set True by report once step has been ORDER_FROM_STEP or later
    started_at: float = 0.0         # clock.now().timestamp() at admission
    finished_at: float | None = None
    error: str | None = None
    result: dict | None = None
    commit_generation: int | None = None   # set by note_commit, inside the outcome commit's writer turn
    ended: bool = False


EndPoint = tuple[int, str, int]     # (commit generation, marker nonce, marker counter after the end raise)


class RunMarker:
    """Per-project counters raised by the registry, plus the learned login, with one observer."""

    def __init__(self) -> None:
        self.nonce = secrets.token_hex(NONCE_BYTES)
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._login: str | None = None
        self._observer: Callable[[str | None], None] | None = None

    def raise_(self, project: str) -> tuple[str, int]:
        with self._lock:
            counter = self._counters.get(project, 0) + 1
            self._counters[project] = counter
            observer = self._observer
        if observer is not None:
            observer(project)
        return self.nonce, counter

    def learn(self, login: str | None) -> None:
        with self._lock:
            changed = login != self._login
            self._login = login
            observer = self._observer
        if changed and observer is not None:
            observer(None)

    def value(self, project: str) -> tuple[str, int, str | None]:
        with self._lock:
            return self.nonce, self._counters.get(project, 0), self._login

    def observe(self, changed: Callable[[str | None], None]) -> None:
        with self._lock:
            if self._observer is not None:
                raise RuntimeError("the run marker already has an observer")
            self._observer = changed


def stage_progress(run: Run) -> tuple[str | None, int | None, int | None, int | None]:
    """(stage, stage_done, stage_total, percent) of a run, the same whatever its phase."""
    if run.kind == "rollback":
        return None, None, None, None
    counters = run.counters
    to_wire = counters.get("to_wire")
    if run.reached_order_step and _positive_int(to_wire):
        stage, total = "order", to_wire
        done = min(counters["wired"], to_wire)
    else:
        stage, done, total = "issues", counters["processed"], counters["total"]
    percent = (100 * done) // total if _positive_int(total) else 0
    return stage, done, total, percent


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class RunRegistry:
    """Each project's last run, its generation and its end point, under one lock."""

    def __init__(self, clock: Clock) -> None:
        self.marker = RunMarker()
        self._clock = clock
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}
        self._generations: dict[str, int] = {}
        self._end_points: dict[str, EndPoint] = {}
        self._source: Callable[[str], int | None] | None = None

    def set_generation_source(self, fn: Callable[[str], int | None]) -> None:
        with self._lock:
            if self._source is not None:
                raise RuntimeError("the generation source is already set")
            self._source = fn

    def generation(self, project: str) -> int:
        with self._lock:
            return self._generations.get(project, 0)

    def admit(self, project: str, kind: str, inputs: object, seen_generation: int) -> Run:
        with self._lock:
            last = self._runs.get(project)
            if last is not None and not last.ended:
                raise Busy(last.run_id)
            if self._generations.get(project, 0) != seen_generation:
                raise Stale()
            counters: dict = {name: 0 for name in COUNTER_FIELDS}
            counters["total"] = None
            run = Run(run_id=secrets.token_hex(RUN_ID_BYTES), project=project, kind=kind,
                      inputs=inputs, counters=counters,
                      started_at=self._clock.now().timestamp())
            self._runs[project] = run
            _, run.admitted = self.marker.raise_(project)
            return run

    def in_flight(self, project: str) -> Run | None:
        with self._lock:
            run = self._runs.get(project)
            return run if run is not None and not run.ended else None

    def report(self, run: Run, step: str | None = None, **counters: int | None) -> None:
        with self._lock:
            if step is not None:
                run.step = step
                if step in _ORDER_STEPS:
                    run.reached_order_step = True
            for name, value in counters.items():
                run.counters[name] = value
            self.marker.raise_(run.project)

    def finish(self, run: Run, phase: str, result: dict | None, error: str | None) -> None:
        with self._lock:
            run.phase = phase
            run.result = result
            run.error = error
            run.finished_at = self._clock.now().timestamp()
            if phase == "done" and result is not None and "counts" in result:
                for name, value in result["counts"].items():
                    if name in COUNTER_FIELDS or name in LATE_FIELDS:
                        run.counters[name] = value

    def note_commit(self, run: Run) -> None:
        with self._lock:
            if self._source is not None:
                run.commit_generation = self._source(run.project)

    def end(self, run: Run) -> None:
        with self._lock:
            if run.ended:
                return
            run.ended = True
            project = run.project
            self._generations[project] = self._generations.get(project, 0) + 1
            nonce, counter = self.marker.raise_(project)
            generation = run.commit_generation
            if generation is None:
                generation = self._source(project) if self._source is not None else None
            self._end_points[project] = (generation or 0, nonce, counter)

    def view(self, project: str) -> dict | None:
        with self._lock:
            return self._view(project)

    def marked_view(self, project: str) -> tuple[tuple[str, int, str | None], dict | None]:
        with self._lock:
            return self.marker.value(project), self._view(project)

    def progress_view(self, project: str) -> tuple[dict | None, EndPoint | None]:
        with self._lock:
            end_point = self._end_points.get(project)
            run = self._runs.get(project)
            if run is None:
                return None, end_point
            stage = dict(zip(PROGRESS_FIELDS[1:], stage_progress(run), strict=True))
            pv: dict = {}
            for name, value in self._view(project).items():  # type: ignore[union-attr]
                pv[name] = value
                if name == "run_id":
                    pv["admitted"] = run.admitted
                elif name == COUNTER_FIELDS[-1]:
                    pv.update(stage)
            return pv, end_point

    def _view(self, project: str) -> dict | None:
        run = self._runs.get(project)
        if run is None:
            return None
        view: dict = {"running": not run.ended}
        for name in VIEW_FIELDS:
            view[name] = run.counters[name] if name in COUNTER_FIELDS else getattr(run, name)
        for name, value in run.counters.items():
            if name in LATE_FIELDS:
                view[name] = value
        return view
