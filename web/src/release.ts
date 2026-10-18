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
];
export const NOT_IN_RELEASE: Surface[] = [
  {id:"orders", name:"Branch orders", path:null},
  {id:"events", name:"Business events", path:"/events"},
  {id:"task-detail", name:"Task detail", path:"/tasks/:id"},
  {id:"assistant", name:"Gemini decision briefs and Ask", path:null},
  {id:"reading", name:"Gemini field-report reading", path:null},
  {id:"verification", name:"Count and expiry checks with lot holds and what-ifs", path:null},
];
export const isShipped = (id: string): boolean => SHIPPED.some(surface => surface.id === id);
