// Reviewed release surfaces (11-CONTEXT D-09, D-10; 11-UI-SPEC "Navigation freeze").
// Move a surface only with the freeze gate's independent required-surface copy and a green integration check.
export type Surface = { id: string; name: string; path: string | null };
export const SHIPPED: Surface[] = [
  {id:"today", name:"Today", path:"/"},
  {id:"board", name:"Decision board", path:"/board"},
  {id:"decision", name:"Stock decision", path:"/cases/:key"},
  {id:"schedule", name:"Schedule", path:"/schedule"},
  {id:"tasks", name:"Tasks", path:"/tasks"},
  {id:"transfers", name:"Branch transfers", path:"/transfers"},
  {id:"stock", name:"Stock & lots", path:"/stock"},
  {id:"analytics", name:"Analytics", path:"/analytics"},
  {id:"forecasts", name:"Forecasts", path:"/forecasts"},
  {id:"reports", name:"Reports", path:"/reports"},
  {id:"policies", name:"Policies", path:"/policies"},
  {id:"about", name:"About", path:"/about"},
  // Phase 7 ships with the full version (07-19..07-22 on build/deploy-screens): its page and its checks (no page).
  {id:"events", name:"Business events", path:"/events"},
  {id:"verification", name:"Count and expiry checks with lot holds and what-ifs", path:null},
  {id:"assistant", name:"Gemini decision briefs and Ask", path:null}, // the full version ships Phase 9 (deploy session, 2026-10-18)
  {id:"reading", name:"Gemini field-report reading", path:null}, // Phase 10 ships in release C (deploy session, 2026-10-18)
];
export const NOT_IN_RELEASE: Surface[] = [
  {id:"orders", name:"Branch orders", path:null},
  {id:"task-detail", name:"Task detail", path:"/tasks/:id"},
];
export const isShipped = (id: string): boolean => SHIPPED.some(surface => surface.id === id);
