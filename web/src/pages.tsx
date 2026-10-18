// About this prototype (05-UI-SPEC "About this prototype: #/about (public)"), readable without the passcode. The text is
// 05-CONTEXT D-15's honest minimum, rewritten for the public dataset (05.2 D-03, D-04): what ShelfShift is, what is
// public, assumed and simulated, the dataset's CC BY 4.0 attribution, the three kinds of data, the modifications and
// the limits (copy.ts ABOUT_*, word for word the paragraphs of the repository's DATASET.md), the supported options,
// what is not yet proven and the labels. Release details are read from /api/about; conditional claims follow the
// reviewed release manifest.
import { useCallback, useContext, useEffect, useRef, useState, type ComponentType, type ReactNode } from "react";
import { ABOUT_KINDS, ABOUT_LIMITS, ABOUT_MODIFICATIONS, ABOUT_SOURCE, datasetMark, textLinks } from "./copy.ts";
import { aiEvalNotice, benchmarkText, DIFFERENT_VERSION } from "./aboutview.ts";
import { DatasetMark, SessionCtx } from "./shell.tsx";
import { SHIPPED } from "./release.ts";
import { PausedBanner, PausedCtx } from "./paused.tsx";
import { ApiError, NETWORK } from "./api.ts";
import { getAbout, type AboutPayload } from "./contract.ts";
import {
  AI_NOT_PUBLISHED, BENCHMARK_SCENARIOS, NOT_AVAILABLE, WEEKCHECK_TEXT, checkRows, versionRows, realNowLines, notInReleaseLines, benchmarkInRelease,
  TRAINING_INPUTS_TEXT, SALES_REPLAY_TEXT, ON_THIS_PAGE, notYetProven, labelRows,
} from "./aboutview.ts";
import { BACK_TO_TODAY, ErrorLine, Icon, Kv, Loading, Pill, Section, Table, reducedMotion } from "./ui.tsx";

// The DATA-04 sentence, verbatim, as the public dataset changed it (05.2 D-03, D-04, D-15): the sales are public, the
// stock is simulated and the assumptions are labelled.
export const DATA_04 = "Public sales from FreshRetailNet-50K, with simulated stock and labelled assumptions";

// The stockout-day fill is floored at the day's own units on the public set (05.2 D-14), so that line is gone.
// (proposed, 06.1-19's wording for 06.1-30) the forecast line no longer says "demo" (D-03).
export const FORECAST_DOES_NOT_LEARN = "The forecast does not learn from what happens in the app.";
export const NOT_YET_PROVEN = [
  "There is no real retailer data.",
  "The markdown response and the cost of a trip that ships nothing are assumptions.",
  "Execution is simulated.",
  FORECAST_DOES_NOT_LEARN,
  "Stock, lots and deliveries are simulated from the public sales.",
  // Phase 7's stated limitations, word for word (07-UI-SPEC surface 7; 07-CONTEXT D-11, D-12, D-22; 07-22)
  "Fact checks test each unresolved fact on its own: a plan that survives each fact alone but fails when two are true together is not detected, so ShelfShift claims no complete feasibility protection.",
  "A lot with mixed expiry dates is held for people to separate; the app doesn't split it.",
  "Staff must state when they checked a count or a date; that stops silent misdating but can't prove a typed time accurate.",
];

// A paragraph whose web addresses are links (the dataset and the license, CC BY 4.0 §3(a)(1)(A)(v), (C)).
function Linked({ text }: { text: string }) {
  return (
    <p className="text-body text-tx break-words">
      {textLinks(text).map((part, i) => part.href === null ? part.text : (
        <a key={i} href={part.href} className="text-accent underline" target="_blank" rel="noreferrer noopener">{part.text}</a>
      ))}
    </p>
  );
}

// (proposed, 06.1-19's wording for 06.1-30) the precomputed forecast's label no longer says "demo" (D-03).
export const PRECOMPUTED_FORECAST_TEXT = "Demand forecasts computed in advance, each with its run and cutoff.";
// The 05 label texts with D-61's term (Case → Stock decision): the term is swapped, the sentences are unchanged.
export const ENGINE_LABEL_TEXT = "A figure the deterministic engine computed for this version of the stock decision.";
export const TEMPLATE_LABEL_TEXT = "The stock decision summary: a fixed template filled in with the engine's figures.";
export const VERSION_LINE_TEXT = // the "Real now" paragraph's last sentence, with D-61's term
  "Each stock decision's version line names the forecast run behind it and its provider.";

// The footer's way back (06.1-UI-SPEC "About"): "Back to Today" signed in, "Sign in" signed out; both go to "#/".
export const SIGN_IN_LINK = "Sign in";

// 06.1 adds two short sections after the 05.2-20 article (06.1-UI-SPEC "About"; D-05, D-58, D-63). Both sentences are
// (proposed): which clock is which, and that the operating model is this prototype's proposal, not a retailer's.
export const TWO_CLOCKS = "Two clocks";
export const TWO_CLOCKS_TEXT = // (proposed)
  "Recorded is when a person acted, in Bangkok time. Scenario date is the operational date of the public sales " +
  "scenario. It does not move with the calendar.";
export const OPERATING_MODEL = "Operating model";
export const OPERATING_MODEL_TEXT = // (proposed)
  "The roles, routines and nine steps are a proposed operating model for this prototype, not a retailer's practice.";

// Read once on entry/refocus and on an explicit retry. A late response never replaces a newer read.
function useReleaseDetails(unavailable: boolean) {
  const [about, setAbout] = useState<AboutPayload | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const generation = useRef(0);
  const reload = useCallback(async () => {
    const ticket = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const payload = await getAbout();
      if (ticket === generation.current) setAbout(payload);
    } catch (e) {
      if (ticket === generation.current) setError(e instanceof ApiError ? e : new ApiError(0, "network", NETWORK));
    } finally {
      if (ticket === generation.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    if (!unavailable) void reload();
    const refocus = () => { if (!unavailable && document.visibilityState === "visible") void reload(); };
    document.addEventListener("visibilitychange", refocus);
    return () => { ++generation.current; document.removeEventListener("visibilitychange", refocus); };
  }, [reload, unavailable]);
  return { about, error, loading, reload };
}

const evalModules = import.meta.glob("./evalsummary.tsx", { eager: true });
const EvalSummary = (Object.values(evalModules)[0] as {EvalSummary?: ComponentType} | undefined)?.EvalSummary;

export function About() {
  const signedIn = useContext(SessionCtx) !== null;
  const unavailable = useContext(PausedCtx)?.paused != null;
  const { about, error, loading, reload } = useReleaseDetails(unavailable);
  const sectionId = new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("s");
  useEffect(() => {
    if (!sectionId) return;
    const target = document.getElementById(sectionId);
    const heading = target?.matches("h2,h3") ? target : target?.querySelector("h2,h3");
    if (heading instanceof HTMLElement) {
      heading.tabIndex = -1;
      heading.focus({preventScroll:true});
      heading.scrollIntoView({behavior:reducedMotion() ? "auto" : "smooth", block:"start"});
    }
  }, [sectionId]);
  const current = error !== null || loading || unavailable ? null : about;
  const releaseBody = (children: ReactNode) => unavailable
    ? <p className="text-body text-tx2">Not available right now.</p>
    : loading ? <Loading text="Loading release details…" />
    : error !== null ? <ErrorLine error={error} action={{ label: "Try again", onClick: () => void reload() }} />
    : children;
  return (
    <article className="mx-auto max-w-3xl p-8">
      <PausedBanner />
      <h1 className="text-display font-semibold">About this prototype</h1>
      <div className="mt-4"><p className="text-body text-tx">
            ShelfShift is a prototype for branch managers and HQ planners. For each lot of short-life stock that one branch
            will not sell before it expires, it answers: should the lot move to another branch, how many units, to which
            branch and by when? Is that better than keeping it or marking it down? And are the stock facts behind the
            answer real? Every number comes from a deterministic engine, and people approve every action.
          </p></div>
      <nav aria-label="On this page" className="mt-6 flex flex-wrap items-center gap-x-3 gap-y-2 text-body">
        {ON_THIS_PAGE.map(([id, title]) => <a key={id} href={`#/about?s=${id}`} className="text-accent underline">{title}</a>)}
      </nav>
      <div className="mt-6 flex min-w-0 flex-col gap-4">
        <div data-about-section="versions">
          <Section title="Versions" headingId="versions">
            {releaseBody(about === null ? null : <div className="[&_dl]:grid [&_dl]:gap-4 md:[&_dl]:grid-cols-2">
              <Kv stacked items={versionRows(about).map(({ key, label, value, note }) => [label,
                <div key={key} className="min-w-0">
                  <span data-testid={`about-version-${key}`} className="break-all whitespace-pre-line font-mono text-body text-tx">{value}</span>
                  {note && <span className="block text-body text-tx2">{note}</span>}
                </div>])} />
            </div>)}
          </Section>
        </div>
        <div data-about-section="checks">
          <Section title="Checks and evaluations" headingId="checks">
            {releaseBody(about === null ? null : <>
              <Table head={["Check", "What it measures", "Status", "Details"]}
                rows={checkRows(about).map((row) => [row.label, row.measures,
                  <div><span data-testid={`check-status-${row.key}`}><Pill tone={row.tone} icon={row.icon}>{row.status}</Pill></span>{row.runLine && <p className="mt-1 break-all font-mono text-label text-tx2">{row.runLine}</p>}</div>,
                  <a href={`#/about?s=${row.details}`} className="text-body text-accent underline">Details</a>])} />
              <div id="benchmark" className="mt-6">
                <h3 className="text-body font-semibold text-tx">Benchmark evaluation</h3>
                <p className="mt-2 text-body text-tx">{about === null ? NOT_AVAILABLE : benchmarkText(about)}</p>
                <p className="mt-2 text-body text-tx2">{about?.benchmark ? "It compares:" : "When published, it compares:"}</p>
                <ul className="list-disc pl-6 text-body text-tx">{BENCHMARK_SCENARIOS.map((name) => <li key={name} className="mt-1">{name}</li>)}</ul>
              </div>
            </>)}
          </Section>
        </div>
        <div data-about-section="ai-evaluation">
          <Section title="AI evaluation" headingId="ai-evaluation">
            {releaseBody(<>
              {aiEvalNotice(current) && <p role="status" className="mb-4 break-words text-body text-warn">{aiEvalNotice(current)}</p>}
              {current?.ai_eval && !current.ai_eval.version_match && <div className="mb-4"><Pill tone="warn" icon="alert">{DIFFERENT_VERSION}</Pill></div>}
              {EvalSummary && current?.ai_eval_status === "published" ? <EvalSummary /> : <div className="flex flex-col items-center gap-4 py-12 text-center">
              <span className="flex h-11 w-11 items-center justify-center rounded-full bg-sunken text-tx2"><Icon name="chart" /></span>
              <h3 className="text-title font-semibold text-tx">{AI_NOT_PUBLISHED}</h3>
            </div>}
            </>)}
          </Section>
        </div>
        <div data-about-section="weekcheck">
          <Section title="Synthetic 5-seed engineering check" headingId="weekcheck">
            {releaseBody(about === null ? null : <Pill tone={checkRows(about)[2].tone} icon={checkRows(about)[2].icon}>{checkRows(about)[2].status}</Pill>)}
            {WEEKCHECK_TEXT.map((text) => <p key={text} className="mt-4 break-words text-body text-tx">{text}</p>)}
          </Section>
        </div>
        <div data-about-section="real-now"><Section title="Real now" headingId="real-now">
          <ul className="list-disc pl-6 text-body text-tx">{realNowLines(about, error !== null || unavailable || loading).map(line => <li key={line} className="mt-1">{line}</li>)}</ul>
        </Section></div>
        <div data-about-section="simulated"><Section title="Simulated" headingId="simulated">
          <p className="text-body text-tx">{DATA_04}.</p>
          <p className="mt-2 text-body text-tx">{ABOUT_KINDS}</p>
          <div className="mt-4 grid min-w-0 gap-4 md:grid-cols-2">
            <div className="min-w-0 rounded-xl border border-line p-4">
              <h3 className="text-body font-semibold">Forecast training inputs</h3>
              <div className="my-2 flex flex-wrap gap-2">{["Recorded", "Converted", "Imputed"].map(name => <Pill key={name} tone="gray">{name}</Pill>)}</div>
              <p className="break-words text-body">{TRAINING_INPUTS_TEXT}</p>
            </div>
            {benchmarkInRelease(current) && <div className="min-w-0 rounded-xl border border-line p-4">
              <h3 className="text-body font-semibold">Benchmark sales replay</h3>
              <p className="mt-2 break-words text-body">{SALES_REPLAY_TEXT}</p>
              <p className="mt-2 break-words text-body">It is a sales replay, not true demand, with the same price response in every scenario so promotions are not counted twice.</p>
            </div>}
          </div>
        </Section></div>
        <Section title="Supported options">
          <p className="text-body text-tx">
            Keep, markdown 20% or 30%, and transfer to one destination. Split transfers and replenishment are not searched.
          </p>
        </Section>
        <div data-about-section="not-yet-proven"><Section title="Not yet proven" headingId="not-yet-proven">
          {notYetProven(current, false).map(group => <div key={group.title} className="mb-4">
            <h3 className="text-body font-semibold">{group.title}</h3>
            <ul className="list-disc pl-6 text-body text-tx">{group.lines.map(line => <li key={line} className="mt-1 break-words">{line}</li>)}</ul>
          </div>)}
          {notInReleaseLines().length > 0 && <><h3 className="text-body font-semibold">Not part of this release</h3>
            <ul className="list-disc pl-6 text-body text-tx">{notInReleaseLines().map(line => <li key={line} className="mt-1 break-words">{line}</li>)}</ul></>}
        </Section></div>
        <div data-about-section="labels"><Section title="Labels" headingId="labels">
          <dl className="flex min-w-0 flex-col gap-4">{labelRows(SHIPPED.map(surface => surface.id), current).map(row =>
            <div key={row.name} className="flex min-w-0 flex-wrap items-baseline gap-2">
              <dt>{row.tone === null ? <span className="text-body text-tx2">{row.name}</span> : <Pill tone={row.tone} icon={row.icon}>{row.name}</Pill>}</dt>
              <dd className="m-0 min-w-0 flex-1 break-words text-body text-tx">{row.meaning}</dd>
            </div>)}</dl>
        </Section></div>
        <div data-about-section="dataset"><Section title="The dataset" headingId="dataset">
          <h3 className="mb-2 text-body font-semibold">The data</h3><Linked text={ABOUT_SOURCE} />
          <h3 className="mb-2 mt-4 text-body font-semibold">Modifications</h3><Linked text={ABOUT_MODIFICATIONS} />
          <h3 className="mb-2 mt-4 text-body font-semibold">Limits</h3><Linked text={ABOUT_LIMITS} />
          <h3 className="mb-2 mt-4 text-body font-semibold">{TWO_CLOCKS}</h3><p className="text-body text-tx">{TWO_CLOCKS_TEXT}</p>
          <h3 className="mb-2 mt-4 text-body font-semibold">{OPERATING_MODEL}</h3><p className="text-body text-tx">{OPERATING_MODEL_TEXT}</p>
        </Section></div>
      </div>
      <footer className="mt-6 flex flex-wrap items-center justify-between gap-2">
        <a href="#/" className="text-body text-accent underline">{signedIn ? BACK_TO_TODAY : SIGN_IN_LINK}</a>
        {!signedIn && about?.label
          ? <span data-testid="dataset-mark" className="text-label text-tx2">{datasetMark({ label: about.label, data_as_of: about.data_as_of })}</span>
          : <DatasetMark />}
        {about?.contact && <a href={`mailto:${about.contact}`} className="w-full break-all text-body text-accent underline">{about.contact}</a>}
      </footer>
    </article>
  );
}
