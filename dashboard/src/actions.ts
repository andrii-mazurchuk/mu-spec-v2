// The page's person-only actions (S-219): their names and the relative route each is sent to.
// Nothing here sends, classifies or gates a press: the action tracker (tracker.ts) alone hands an
// action to the request part, classifyAction (outcome.ts) alone classifies its answer, and each
// control's availability comes from the tracker's control for its (project, action) key.
// Rolling back a Ship has no name and no route here.

export type ActionName =
  | "ratify"
  | "reject"
  | "cut_units"
  | "set_repo"
  | "set_labels"
  | "emit_tickets"
  | "set_delegation";

/** As a set, the unit's waiting actions (S-136 WAITING_ACTIONS). */
export const ACTION_NAMES: readonly ActionName[] = [
  "ratify", "reject", "cut_units", "set_repo", "set_labels", "emit_tickets", "set_delegation",
];

/** Each action's method and path as the unit declares it (S-319 ACTIONS), relative, the project filled in. */
export const ACTION_ROUTES: { [A in ActionName]: { method: "POST"; path: (project: string) => string } } = {
  ratify: { method: "POST", path: (project) => `projects/${project}/slicing/proposal/ratify` },
  reject: { method: "POST", path: (project) => `projects/${project}/slicing/proposal/reject` },
  cut_units: { method: "POST", path: (project) => `projects/${project}/units/cut` },
  set_repo: { method: "POST", path: (project) => `projects/${project}/repo` },
  set_labels: { method: "POST", path: (project) => `projects/${project}/labels` },
  emit_tickets: { method: "POST", path: (project) => `projects/${project}/emit` },
  set_delegation: { method: "POST", path: (project) => `projects/${project}/delegation` },
};
