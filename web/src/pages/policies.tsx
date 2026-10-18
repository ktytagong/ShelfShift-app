// Policies in the page registry (06.1-16, 06.1-19; D-53): HQ edits the workspace's policies, a store manager reads
// them (settings.tsx). The label is the UI-SPEC's open question 7 default. "#/settings" redirects to "#/policies"
// (main.tsx).
import type { PageDef } from "../pagereg.ts";
import { POLICIES, PolicySettings } from "../settings.tsx";

export const page: PageDef = {
  path: "/policies",
  title: POLICIES,
  nav: { label: POLICIES, icon: "sliders", section: "Admin", order: 9 },
  Component: () => <PolicySettings />,
};
