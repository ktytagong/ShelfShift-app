// The step module contract of the stock decision page (06.1-18; 06.1-26, -27 and -28 rely on it). Each step's panel is
// one module, web/src/decision/steps/<name>.tsx, exporting `step: StepModule`; pages/decision.tsx finds the modules
// through import.meta.glob by their `key`, so a later plan adds a step by adding its module, never by editing the page.
// - `Panel` draws the step's panel body under the stepper when that step is viewed (?step=, or the current step).
// - `Primary` (optional) draws the summary's action slot while this step is the decision's current step (the server's
//   `step`) and the signed-in account is among the people the server says act next (next_action.who; HQ also at Take
//   ownership, D-67). It renders nothing when its own rule says this account has nothing to press.
// - `HeaderAction` (optional) draws an action at the right of the panel's header, beside its status chip, while the
//   step is viewed (Archive's Export stock decision).
// The current step is always the server's (D-24): no module derives it, and `go` only changes which panel is viewed.
import type { ComponentType } from "react";
import type { CaseDetail61, Session61 } from "../contract.ts";
import type { ActivityLoad } from "../hooks.ts";
import type { StepKey } from "../steps.ts";

export type StepProps = {
  d: CaseDetail61; // the latest detail the page loaded
  session: Session61; // the signed-in account's session
  reload: () => void; // refetches the detail and the history (after a write, or Reload the stock decision)
  go: (step: StepKey) => void; // shows another step's panel (?step=); never moves the decision
  activity: ActivityLoad; // the decision's history, oldest first by recorded time
};

export type StepModule = {
  key: StepKey;
  Panel: ComponentType<StepProps>;
  Primary?: ComponentType<StepProps>;
  HeaderAction?: ComponentType<StepProps>;
};
