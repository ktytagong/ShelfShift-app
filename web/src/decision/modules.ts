import type { StepKey } from "../steps.ts";
import type { StepModule } from "./types.ts";

// The nine step modules by key, found through import.meta.glob (decision/types.ts). The stock decision page and the
// Decision board's guided moves (06.1-CONTEXT D-69) use the same panels, forms and permission checks from here.
const modules = import.meta.glob<{ step: StepModule }>("./steps/*.tsx", { eager: true });
export const STEP_MODULES: Partial<Record<StepKey, StepModule>> = Object.fromEntries(
  Object.keys(modules).sort().map((k) => [modules[k].step.key, modules[k].step]),
);
