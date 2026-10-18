// Step 9 · Archive (06.1-UI-SPEC "Step panel" row 9; 06.1-CONTEXT D-39, D-50, D-58): the decision's closing line, its
// full history and "Export stock decision".
// Rules kept here:
// - The history is the API's activity list in its order (oldest first by recorded time); the filter keeps the API's
//   own groups (bars.historyFilter, All · Decisions · Data). Nothing here sorts or re-maps an entry.
// - Each entry reads through vocab.historyLine: "{Event} by {who}", "Recorded … {City} time · Scenario date: …", and
//   the details in words (D-39, D-58, D-63). In the details a 12-character hash shows in mono, and other ids (codes
//   with digits and separators, or any very long token) may break anywhere, so a long detail wraps in full at 360px.
// - The closing line is the server's record: "Closed · {the closing note}" for a closed decision.
// - Export saves GET /api/cases/{id}/export byte for byte (shared.useExport: a read, no Idempotency-Key); it sits in
//   the panel's header (the step module's HeaderAction), and a failure shows the API message under the button.
// Strings marked (proposed) are Claude's wording, one constant each, so a change is one line.
import { useState, type ReactNode } from "react";
import type { HistoryFilter } from "../../bars.ts";
import { retryable } from "../../copy.ts";
import { useSession } from "../../shell.tsx";
import { Button, ErrorLine, HISTORY_FILTERS, HistoryList, Loading, TRY_AGAIN } from "../../ui.tsx";
import { historyLine, type HistoryEntry } from "../../vocab.ts";
import { HISTORY_EMPTY, LOADING_HISTORY, useExport } from "../shared.ts";
import type { StepModule, StepProps } from "../types.ts";

export const HISTORY_TITLE = "Full history"; // (proposed)
export const EXPORT_LABEL = "Export stock decision";
export const CLOSED_LINE = (reason: string | null) => (reason === null || reason.trim() === "" ? "Closed" : `Closed · ${reason}`);

// --- the details: hashes in mono, ids that may break anywhere ----------------------------------------------------------

const SHORT_HASH = /^[0-9a-f]{12}$/; // vocab.historyLine cuts every 64-character hash to 12 (D-39)
const ID_LIKE = /\d.*[-#:_/]|[-#:_/].*\d/; // a code with digits and separators: "SUS-19#1:T", "L-0042"
const LONG = 24; // a token this long breaks anywhere rather than widen the page

function detailParts(text: string): { text: string; kind: "hash" | "id" | "text" }[] {
  return text.split(/(\s+)/).filter((t) => t !== "").map((t) => {
    const bare = t.replace(/[.,;:)]+$/, "");
    if (SHORT_HASH.test(bare) && /\d/.test(bare)) return { text: t, kind: "hash" as const };
    if (!/^\s+$/.test(t) && (ID_LIKE.test(bare) || t.length >= LONG)) return { text: t, kind: "id" as const };
    return { text: t, kind: "text" as const };
  });
}

function Detail({ text }: { text: string }): ReactNode {
  return detailParts(text).map((p, i) => (
    p.kind === "hash" ? <span key={i} className="font-mono">{p.text}</span>
      : p.kind === "id" ? <span key={i} className="break-all">{p.text}</span>
      : p.text
  ));
}

function Panel({ d, activity }: StepProps) {
  const { session } = useSession();
  const [filter, setFilter] = useState<HistoryFilter>(() => {
    const requested = new URLSearchParams(window.location.hash.split("?")[1]).get("history");
    return HISTORY_FILTERS.find(value => value === requested) ?? "All";
  });
  const { entries, error, reload } = activity;
  const line = d.state === "CLOSED" ? CLOSED_LINE(d.reason) : null;
  const lines = entries === null ? null : entries.map((e) => {
    const h = historyLine(e as unknown as HistoryEntry, session.time_zone);
    const id = JSON.stringify([e.at, e.type, e.payload.draft_id ?? e.payload.request_id ?? e.payload]);
    return { id, title: h.title, meta: h.meta, detailText: h.detail,
      detail: h.detail === "" ? undefined : <Detail text={h.detail} />, group: h.group };
  });
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {line !== null && <p data-testid="closing-line" className="m-0 min-w-0 text-body break-words text-tx">{line}</p>}
      <section data-testid="full-history" aria-labelledby="full-history-title" className="flex min-w-0 flex-col gap-2">
        <h3 id="full-history-title" className="m-0 text-label font-semibold tracking-[.06em] text-tx3 uppercase">{HISTORY_TITLE}</h3>
        {error !== null && (
          <ErrorLine error={error} action={retryable(error.code) || error.status === 0 ? { label: TRY_AGAIN, onClick: reload } : undefined} />
        )}
        {lines === null
          ? error === null && <Loading text={LOADING_HISTORY} />
          : <HistoryList entries={lines} filter={filter} onFilter={setFilter} emptyText={HISTORY_EMPTY(filter)} />}
      </section>
    </div>
  );
}

// Export stock decision in the panel's header, with the API's message under it when the export fails.
function HeaderAction({ d }: StepProps) {
  const exported = useExport(d);
  return (
    <div className="flex min-w-0 flex-col items-end gap-1">
      <Button kind="ghost" icon="download" onClick={() => void exported.run()}>{EXPORT_LABEL}</Button>
      <ErrorLine error={exported.error} />
    </div>
  );
}

export const step: StepModule = { key: "ARCHIVE", Panel, HeaderAction };
