// About in the page registry (06.1-16): the 05.2-20 article, readable while signed out (`public`).
import { About } from "../pages.tsx";
import type { PageDef } from "../pagereg.ts";

export const page: PageDef = {
  path: "/about",
  title: "About",
  nav: { label: "About", icon: "info", section: "Admin", order: 10 },
  public: true,
  Component: () => <About />,
};
