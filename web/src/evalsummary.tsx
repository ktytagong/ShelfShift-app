// Public, read-only evaluation. Server sentences are rendered as React text.
import { useEffect, useState } from "react";
import { getEvalSummary, type EvalCell, type EvalGroup, type EvalSummaryPayload } from "./api.ts";
import { EVAL_COPY as C } from "./copy.ts";
import { cellText, evalDate, failuresByGroup, groupLine, noticeFor, usageLine } from "./evalview.ts";
import { ErrorLine, Icon, Pill, Table, TRY_AGAIN } from "./ui.tsx";

function RunCell({ cell }: { cell: EvalCell }) {
  const view = cellText(cell);
  const tone = view.result === C.pass ? "good" : view.result === C.fail ? "warn" : "gray";
  return <div className="num flex flex-col items-start gap-2">
    <span>{view.text}</span>
    {cell.result !== "INVALID" && view.result !== C.na && <Pill tone={tone} icon={tone === "gray" ? "clock" : tone === "good" ? "check" : "alert"}>{view.result}</Pill>}
  </div>;
}

function EvalGrid({ group }: { group: EvalGroup }) {
  return <div className="max-w-full overflow-x-auto" tabIndex={0} role="region" aria-label={`${group.name} results scroll area`}>
    <table aria-label={`${group.name} results`} className="w-full min-w-[440px] table-fixed border-collapse text-body">
      <thead className="bg-side"><tr>
        <th scope="col" className="w-56 min-w-56 border-b border-line px-4 py-2 text-left text-label font-semibold text-tx3">{C.scenario}</th>
        {[1, 2, 3].map((run) => <th key={run} scope="col" className="min-w-18 border-b border-line px-2 py-2 text-left text-label font-semibold text-tx3">Run {run}</th>)}
      </tr></thead>
      <tbody>{group.scenarios.map((scenario) => <tr key={scenario.scenario}>
        <th scope="row" className="w-56 min-w-56 border-b border-line px-4 py-2 text-left font-normal [overflow-wrap:anywhere]">
          {scenario.title}
          {scenario.runs.length > 0 && new Set(scenario.runs.map((r) => r.a)).size === 1 &&
            <p className="text-label text-tx2">{scenario.runs[0].a} applicable items</p>}
        </th>
        {scenario.runs.map((cell) => {
          const view = cellText(cell);
          const label = cell.result === "INVALID" || cell.a === 0 ? view.text :
            `${cell.p} of ${cell.a} applicable items passed, ${cell.f} failed · ${view.result}`;
          return <td key={cell.run} aria-label={["Run", `${cell.run}:`, label].join(" ")}
            className="min-w-18 border-b border-line px-2 py-2 align-top [overflow-wrap:anywhere]"><RunCell cell={cell} /></td>;
        })}
      </tr>)}</tbody>
    </table>
  </div>;
}

function Failures({ group }: { group: EvalGroup }) {
  const unfinished = group.scenarios.flatMap((s) => s.runs
    .filter((r) => r.result === "NOT_SCORED")
    .map((r) => ({ scenario: s, cell: r })));
  return <div className="mt-2 flex flex-col gap-2">
    {group.failures.length === 0 ? <p className="text-body text-tx2">{C.noFailures}</p> :
      <ul className="flex flex-col gap-2" aria-label={`${group.name} failed items`}>
        {failuresByGroup(group.failures).map((f) => <li key={`${f.run}-${f.scenario}-${f.item_id}`} className="min-w-0 break-words [overflow-wrap:anywhere]">
          <p className="text-label font-semibold">Run {f.run} · {group.scenarios.find((s) => s.scenario === f.scenario)?.title ?? f.scenario} · {f.item}</p>
          <p className="whitespace-pre-wrap text-body">{f.note}</p>
          {f.context && <p className="text-label text-tx2">{f.context}</p>}
        </li>)}
      </ul>}
    {unfinished.length > 0 && <div><h4 className="text-label font-semibold">{C.notScoredYet}</h4>
      <ul>{unfinished.map(({ scenario, cell }) => <li key={`${scenario.scenario}-${cell.run}`} className="break-words text-body">
        Run {cell.run} · {scenario.title} · {C.notScored} · {cell.p} of {cell.a}
      </li>)}</ul>
    </div>}
  </div>;
}

export function EvalSummary() {
  const [data, setData] = useState<EvalSummaryPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    getEvalSummary().then((value) => { if (live) setData(value); },
      (reason: Error) => { if (live) setError(reason.message); });
    return () => { live = false; };
  }, [attempt]);
  if (error) return <ErrorLine error={error} action={{ label: TRY_AGAIN, onClick: () => {
    setError(null); setData(null); setAttempt((n) => n + 1);
  } }} />;
  if (!data) return <p role="status" className="text-body text-tx2">{C.loading}</p>;
  const report = data.published;
  const notice = noticeFor(data);
  if (!report) return <div className="flex flex-col items-center gap-4 py-12 text-center">
    <span className="flex h-11 w-11 items-center justify-center rounded-full bg-sunken"><Icon name="chart" /></span>
    <h3 className="text-title font-semibold">{C.empty}</h3><p className="text-body text-tx2">{C.emptyBody}</p>
    {notice && <p className="break-words text-body [overflow-wrap:anywhere]">{notice}</p>}
  </div>;
  const safetyPassed = report.safety.length > 0 && report.safety.every((row) => row.result === "PASS");
  return <div className="flex min-w-0 flex-col gap-4 text-body">
    {notice && <p role="note" className="rounded-md bg-warn-soft p-4 text-body [overflow-wrap:anywhere]">{notice}</p>}
    <div className="flex flex-wrap items-center gap-2">
      <p className="text-label font-semibold text-tx2">{C.label}</p>
      {data.version_mismatch && <span className="min-w-0 max-w-full [&>span]:h-auto [&>span]:whitespace-normal"><Pill tone="warn" icon="alert">{C.mismatch}</Pill></span>}
    </div>
    <p className="flex flex-wrap gap-1 text-label text-tx2 [overflow-wrap:anywhere]">
      <span>Gemini {report.model_id}</span><span>· build <span className="font-mono">{report.build.slice(0, 12)}</span></span>
      <span>· references <span className="font-mono">{report.fingerprint.slice(0, 12)}</span></span>
      <span>· run {evalDate(report.date)}</span><span>· reviewed by a person {report.reviewed_at ? evalDate(report.reviewed_at) : C.notScored}</span>
    </p>
    <p className="text-body text-tx2">{C.scope}</p>
    <section className="min-w-0"><h3 className="text-title font-semibold">{C.safety}</h3>
      <Pill tone={safetyPassed ? "good" : "bad"} icon={safetyPassed ? "check" : "lock"}>{safetyPassed ? C.safetyPass : C.safetyFail}</Pill>
      <Table head={[C.check, C.modelAttempted, C.shownExecuted, C.result]} rows={report.safety.map((row) => [
        row.label, `${row.attempted} ${row.gate === "unsourced" ? "written" : "attempted"}`,
        row.shown !== undefined ? `${row.shown} shown` : row.executed !== undefined ? `${row.executed} executed` : `${row.exposed} exposed`,
        <div><Pill tone={row.result === "PASS" ? "good" : "warn"}>{row.result === "PASS" ? C.pass : C.fail}</Pill>
          {row.note && <p className="text-label text-tx2">{row.note}</p>}</div>,
      ])} />
      {report.groups.filter((g) => g.name === "Safety").map((group) => <Failures key={group.name} group={group} />)}
    </section>
    {report.groups.filter((g) => g.name !== "Safety").map((group) => <section key={group.name} className="min-w-0">
      <h3 className="text-title font-semibold">{group.name}</h3>
      <EvalGrid group={group} />
      <p className="mt-2 text-label text-tx2">{groupLine(group)}</p>
      <Failures group={group} />
    </section>)}
    <p className="text-label text-tx2">{usageLine(report.usage)}</p>
  </div>;
}
