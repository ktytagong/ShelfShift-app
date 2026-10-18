// The page registry (06.1 D-09; 06.1-16's contract for every page plan): each page is one module under ./pages/ that
// exports `page: PageDef`. The router (main.tsx) and the nav (shell.tsx) read only this registry, so a page plan adds
// its module and nothing else, and a page module that exists is a page that works: there is never a dead or disabled
// link (D-09). Transport has no module and so no link (D-66); the Orders slot stays empty until Phase 05.1.
import type { ComponentType } from "react";
import type { Route } from "./shell.tsx";
import type { IconName } from "./ui.tsx";

export type NavSection = "" | "Movement" | "Stock" | "Admin";
export type CountKey = "today" | "board" | "tasks";

export type PageDef = {
  path?: string; // an exact path ("/board")
  match?: RegExp; // or a pattern ("/cases/{key}"); a page with neither is never routed
  title: string;
  nav?: { label: string; icon: IconName; section: NavSection; order: number; countKey?: CountKey };
  public?: boolean; // readable while signed out (About)
  Component: ComponentType<{ route: Route }>;
};

// The sections in nav order (UI-SPEC "Shell" nav table).
export const SECTIONS: readonly NavSection[] = ["", "Movement", "Stock", "Admin"];

const modules = import.meta.glob<{ page: PageDef }>("./pages/*.tsx", { eager: true });

// Every page, in a fixed order (the module path), whatever order the bundler lists them in.
export const PAGES: readonly PageDef[] = Object.keys(modules).sort().map((k) => modules[k].page);

// The page a route shows: an exact path first, then a pattern; null when no page claims it ("Page not found.").
export function pageFor(route: Pick<Route, "path">): PageDef | null {
  return PAGES.find((p) => p.path === route.path)
    ?? PAGES.find((p) => p.match !== undefined && p.match.test(route.path))
    ?? null;
}

export type NavItem = {
  href: string;
  path: string;
  label: string;
  icon: IconName;
  count: number; // 0 when the session gives none; a count of 0 is never drawn
  countKey?: CountKey;
};
export type NavGroup = { section: NavSection; items: NavItem[] };

// The nav: every page with `nav` and a path, grouped by section in SECTIONS order, then by `order`. Counts come from
// the session's nav_counts (06.1-13; optional until that API lands, and absent means no count).
export function navItems(session: { nav_counts?: Partial<Record<CountKey, number>> }): NavGroup[] {
  const counts = session.nav_counts ?? {};
  return SECTIONS.map((section) => ({
    section,
    items: PAGES.flatMap((p) => (p.nav !== undefined && p.path !== undefined && p.nav.section === section ? [p] : []))
      .sort((a, b) => (a.nav?.order ?? 0) - (b.nav?.order ?? 0))
      .map((p) => {
        const nav = p.nav!;
        const n = nav.countKey === undefined ? 0 : counts[nav.countKey] ?? 0;
        return { href: `#${p.path}`, path: p.path!, label: nav.label, icon: nav.icon, count: n, countKey: nav.countKey };
      }),
  })).filter((g) => g.items.length > 0);
}
