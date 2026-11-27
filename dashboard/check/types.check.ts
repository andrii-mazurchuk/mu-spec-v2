// Compiled, never run, by the action wiring test (T-212) with `bunx tsc --noEmit --strict`: the
// wiring conforms to the action port (S-235), and the tracker's control to its control view.

import type { ControlView, WireActions } from "../src/actionport";
import { wireActions } from "../src/actionwire";
import { control, initTracker } from "../src/tracker";

const w: WireActions = wireActions;
const v: ControlView = control(initTracker(), "p", "set_repo");
