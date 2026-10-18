// Policies (06.1-UI-SPEC "Policies"; D-53, D-03, D-61; contract Settings and "Product mode (06.1)"): the workspace's
// policy values, read with GET /api/settings and written with PATCH /api/settings. Two fields, the waste price and min
// sellable days; their min, max, unit and helper text come only from the response's bounds (shortlist_step is not
// shown), so no retailer value is typed here.
//
// Only HQ changes policies (D-53). The server is the control (06.1-06: a manager's PATCH is 403 "Only HQ can change
// policies."); the screen only reflects it: HQ gets the form, a store manager gets the same values as text with the
// HQ-only line, and never an input or a disabled form that looks editable. A save sends both fields with
// expected_version and one Idempotency-Key per press (kept for Try again), shows Elapsed while the server recomputes
// every open stock decision, then the saved line, and refreshes the session (the run version moved on).
//
// The typed text is kept per field outside the loaded values, so a reload (a stale save, the tab becoming visible
// again, a new run version) never clears what HQ typed. Nothing is resent on its own.
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, NETWORK, get, patch, type Settings, type SettingsSaved } from "./api.ts";
import {
  SETTINGS_FIELDS, boundsHelp, dayCount, dayWord, errorAction, settingsChanges, type SettingsDraft, type SettingsField,
} from "./copy.ts";
import { money, units } from "./format.ts";
import { useAction, useRefetchOnVisible } from "./hooks.ts";
import { PageHead, useSession } from "./shell.tsx";
import { Button, Elapsed, ErrorLine, Icon, LoadError, Loading, Section, StaleBanner, TRY_AGAIN } from "./ui.tsx";

// The page's own words. "(proposed)" marks wording the user has not confirmed; one constant each.
export const POLICIES = "Policies"; // UI-SPEC open question 7 default (the nav label and h1)
export const POLICIES_TEXT = {
  loading: "Loading policies…", // (proposed) UI-SPEC "Interaction states"
  failed: "Policies didn't load", // (proposed) the per-page error title
  // UI-SPEC "Policies": the 05 note with the D-61 term
  note: "Changes every open stock decision in your workspace; an approval in progress on a changed plan is cancelled.",
  // (proposed) a store manager's read-only line; its first sentence is the server's own refusal (api/settings.py)
  hqOnly: "Only HQ can change policies. These values apply to every branch in your workspace.",
  // (proposed) replaces the Phase 5 stale_version line, whose wording D-03 retired
  stale: "These values changed since you loaded this page. They were reloaded; your edits are kept in the fields.",
  recomputing: "Recomputing open stock decisions", // UI-SPEC: Save policy's Elapsed label
  save: "Save policy",
  // (proposed) the 05 saved line with the D-61 term, singular for exactly one
  saved: (n: number) =>
    n === 1 ? "Saved. 1 open stock decision was recomputed." : `Saved. ${units(n)} open stock decisions were recomputed.`,
} as const;

const LABEL: Record<SettingsField, string> = { waste_price: "Waste price", min_sellable_days: "Min sellable days" };
// The native step: cents for the waste price, whole days for min sellable days (UI-SPEC).
const STEP: Record<SettingsField, string> = { waste_price: "0.01", min_sellable_days: "1" };

type SaveBody = { expected_version: number; changes: Record<SettingsField, number> };

// The suffix beside an editable field: the bounds unit, except Min sellable days, which reads "day" or "days" by the
// field's current number (the typed text when it parses, else the loaded value) through copy.ts dayWord (G-05-12 (e)).
function unitFor(f: SettingsField, typed: string | undefined, loaded: number, unit: string): string {
  if (f !== "min_sellable_days") return unit;
  const n = typed === undefined || typed.trim() === "" ? NaN : Number(typed);
  return dayWord(Number.isFinite(n) ? n : loaded);
}

// A value as text: the waste price as money in the session currency with the per-unit part of the bounds unit
// (money, then "/kg" from the unit "{currency}/kg"), min sellable days as "1 day" / "2 days".
function valueText(f: SettingsField, v: number, unit: string, currency: string): string {
  if (f === "min_sellable_days") return dayCount(v);
  const slash = unit.indexOf("/");
  return `${money(v, currency)}${slash < 0 ? "" : unit.slice(slash)}`;
}

// The value's place between its bounds: an 8px sunken track with a navy-700 fill (UI-SPEC "Three-second scan":
// the two values with their bounds). The numbers are in the helper beside it, so the bar itself is hidden from
// assistive tech. A value that does not parse draws an empty track.
function BoundBar({ value, min, max }: { value: number; min: number; max: number }) {
  const share = Number.isFinite(value) && max > min ? Math.min(1, Math.max(0, (value - min) / (max - min))) : 0;
  return (
    <span aria-hidden="true" data-testid="bound-bar" className="block h-2 w-full max-w-60 overflow-hidden rounded-full bg-sunken">
      <span className="block h-full rounded-full bg-navy-700" style={{ width: `${Math.round(share * 100)}%` }} />
    </span>
  );
}

// A store manager's view: each value as text with its bar and bounds, and the HQ-only line. No input, no button.
function ReadOnly({ settings, currency }: { settings: Settings; currency: string }) {
  return (
    <Section>
      <p className="m-0 flex items-start gap-2 text-body text-tx2">
        <Icon name="lock" className="mt-0.5 shrink-0" />
        <span>{POLICIES_TEXT.hqOnly}</span>
      </p>
      <dl className="m-0 mt-4 grid gap-4 sm:grid-cols-2">
        {SETTINGS_FIELDS.map((f) => {
          const b = settings.bounds[f];
          return (
            <div key={f} data-testid={`policy-${f}`} className="flex min-w-0 flex-col gap-1">
              <dt className="text-label text-tx3">{LABEL[f]}</dt>
              <dd className="num m-0 text-display font-semibold tracking-[-0.02em] text-tx">
                {valueText(f, settings.values[f], b.unit, currency)}
              </dd>
              <dd className="m-0"><BoundBar value={settings.values[f]} min={b.min} max={b.max} /></dd>
              <dd data-testid={`help-${f}`} className="m-0 text-label text-tx3">
                {boundsHelp(f, settings.bounds)}
              </dd>
            </div>
          );
        })}
      </dl>
    </Section>
  );
}

export function PolicySettings() {
  const { session, refresh } = useSession();
  const hq = session.role === "hq";
  const [settings, setSettings] = useState<Settings | null>(null);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [draft, setDraft] = useState<SettingsDraft>({});
  const [saved, setSaved] = useState<number | null>(null);
  const latest = useRef(0);

  // A response that arrives after a newer load started is dropped; a failed reload keeps the last values on screen.
  const load = useCallback(async () => {
    const n = ++latest.current;
    try {
      const s = await get<Settings>("/api/settings");
      if (n !== latest.current) return;
      setSettings(s);
      setLoadError(null);
    } catch (e) {
      if (n !== latest.current) return;
      setLoadError(e instanceof ApiError ? e : new ApiError(0, "network", NETWORK));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, session.version]);
  useRefetchOnVisible(() => void load());

  // A 409 stale_version reloads the values, the bounds and the version; the typed text stays in the inputs and
  // nothing is resent. The session refreshes too, so every page reads the workspace as it is now.
  const save = useAction(async (body: SaveBody, key: string) => {
    try {
      const out = await patch<SettingsSaved>("/api/settings", body, key);
      setSettings({ version: out.version, values: out.values, bounds: out.bounds });
      setDraft({});
      setSaved(out.reevaluated.length);
      await refresh().catch(() => {});
      return out;
    } catch (e) {
      if (e instanceof ApiError && e.code === "stale_version") {
        void load();
        void refresh().catch(() => {});
      }
      throw e;
    }
  });

  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (settings === null || save.busy) return;
    setSaved(null);
    void save.run({ expected_version: settings.version, changes: settingsChanges(settings.values, draft) });
  };

  const error = save.error;
  // out_of_bounds names its field: the message goes under that input. A stale save shows the page's own line (never
  // the server's wording); any other refusal (a 403 included) shows the server's message under Save policy.
  const badField = error !== null && error.code === "out_of_bounds" && typeof error.extra.field === "string"
    && (SETTINGS_FIELDS as readonly string[]).includes(error.extra.field) ? error.extra.field as SettingsField : null;
  const formError = error === null || badField !== null ? null
    : error.code === "stale_version" ? POLICIES_TEXT.stale : error;
  const formAction = error !== null && badField === null && errorAction(error.code) === "retry"
    ? { label: TRY_AGAIN, onClick: () => void save.retry() } : undefined;

  let body;
  if (settings === null && loadError !== null) {
    body = <LoadError title={POLICIES_TEXT.failed} error={loadError} onRetry={() => void load()} />;
  } else if (settings === null) {
    body = <Loading text={POLICIES_TEXT.loading} />;
  } else if (!hq) {
    body = <ReadOnly settings={settings} currency={session.currency} />;
  } else {
    body = (
      <Section>
        <p className="m-0 flex items-start gap-2 text-body text-tx2">
          <Icon name="info" className="mt-0.5 shrink-0" />
          <span>{POLICIES_TEXT.note}</span>
        </p>
        <form onSubmit={submit} className="mt-4 flex flex-col gap-4">
          <fieldset disabled={save.busy} className="m-0 grid gap-4 border-0 p-0 sm:grid-cols-2">
            {SETTINGS_FIELDS.map((f) => {
              const b = settings.bounds[f];
              const id = `setting-${f}`;
              const bad = badField === f;
              const typed = draft[f];
              const shown = typed === undefined || typed.trim() === "" ? settings.values[f] : Number(typed);
              return (
                <div key={f} data-testid={`policy-${f}`} className="flex min-w-0 flex-col gap-1">
                  <label htmlFor={id} className="text-body font-semibold">{LABEL[f]}</label>
                  <span className="flex items-center gap-2">
                    <input id={id} type="number" required min={b.min} max={b.max} step={STEP[f]}
                      inputMode={f === "waste_price" ? "decimal" : "numeric"}
                      value={typed ?? String(settings.values[f])}
                      onChange={(e) => setDraft((d) => ({ ...d, [f]: e.target.value }))}
                      aria-invalid={bad || undefined}
                      aria-describedby={bad ? `${id}-help ${id}-error` : `${id}-help`}
                      className={`num h-8 w-40 rounded-md border bg-white px-2 text-body max-md:h-11 ${bad ? "border-bad" : "border-line-2"}`} />
                    <span data-testid={`unit-${f}`} className="text-body text-tx2">{unitFor(f, typed, settings.values[f], b.unit)}</span>
                  </span>
                  <BoundBar value={shown} min={b.min} max={b.max} />
                  <p id={`${id}-help`} data-testid={`help-${f}`} className="m-0 text-label text-tx3">{boundsHelp(f, settings.bounds)}</p>
                  {bad && <ErrorLine id={`${id}-error`} error={error} />}
                </div>
              );
            })}
          </fieldset>
          <div className="flex flex-wrap items-center gap-2">
            <Button kind="primary" type="submit" disabled={save.busy} aria-busy={save.busy || undefined}
              className="max-md:h-11">{POLICIES_TEXT.save}</Button>
            {save.busy && <span className="text-label text-tx2"><Elapsed label={POLICIES_TEXT.recomputing} /></span>}
          </div>
          <ErrorLine error={formError} action={formAction} />
          {saved !== null && !save.busy && error === null && (
            <p role="status" className="num m-0 rounded-md bg-good-soft px-2 py-1 text-body text-good">{POLICIES_TEXT.saved(saved)}</p>
          )}
        </form>
      </Section>
    );
  }

  return (
    <div className="min-w-0">
      <PageHead title={POLICIES} />
      <div className="flex min-w-0 flex-col gap-4 p-4 md:p-6">
        {settings !== null && loadError !== null && <StaleBanner kind="refresh_failed" onAct={() => void load()} />}
        {body}
      </div>
    </div>
  );
}
