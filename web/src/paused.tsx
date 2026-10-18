// Outage notice: 11-CONTEXT D-03, D-04; 11-UI-SPEC "Paused screen". No polling or raw error details.
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { NETWORK, type Paused } from "./api.ts";
import { DatasetMark } from "./shell.tsx";
import { Banner, Button, Elapsed, Icon } from "./ui.tsx";

export const UNAVAILABLE_HEADING = "ShelfShift is temporarily unavailable.";
export const TRY_LATER = "Please try again in a moment.";
export const STILL_UNAVAILABLE = "Still unavailable. Try again in a few minutes.";
export const CHECKING = "Checking";
export const ABOUT_LINK = "About ShelfShift and the data";
export type Retry = () => Promise<"ok" | "still" | "network">;
export const PausedCtx = createContext<{ paused: Paused | null; retry: Retry } | null>(null);

export function contactSentence(contact: string) {
  return contact === "" ? null : <> If the problem continues, contact <a href={`mailto:${contact}`}
    className="break-all text-accent underline">{contact}</a>.</>;
}

function RetryAction({ retry, primary = false, children }: { retry: Retry; primary?: boolean; children?: ReactNode }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const button = useRef<HTMLButtonElement>(null);
  const check = async () => {
    if (busy) return;
    setBusy(true);
    setMessage("");
    let recovered = false;
    try {
      const answer = await retry();
      recovered = answer === "ok";
      setMessage(answer === "still" ? STILL_UNAVAILABLE : answer === "network" ? NETWORK : "");
    } catch {
      setMessage(NETWORK);
    } finally {
      setBusy(false);
      if (!recovered) requestAnimationFrame(() => button.current?.focus());
    }
  };
  return <div className="flex w-full flex-wrap items-center gap-2">
    <Button ref={button} kind={primary ? "primary" : "plain"} disabled={busy} aria-busy={busy || undefined}
      onClick={() => void check()}>{busy ? <Elapsed label={CHECKING} /> : "Try again"}</Button>
    {children}
    <p aria-live="polite" className="w-full text-body text-tx2">{message}</p>
  </div>;
}

export function PausedNotice({ contact, retry }: { contact: string; retry: Retry }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => heading.current?.focus(), []);
  return <main className="flex min-h-full items-center justify-center overflow-auto bg-page px-4 py-8 md:items-start md:pt-16">
    <section aria-labelledby="unavailable-title"
      className="flex w-full min-w-0 max-w-[480px] flex-col gap-4 rounded-2xl bg-white p-6 shadow-lg md:p-8">
      <div className="flex items-center gap-2">
        <img src="/logo.png" alt="" width={40} height={40} className="h-10 w-10 shrink-0" />
        <span className="text-title font-semibold text-tx">ShelfShift</span>
      </div>
      <div className="flex h-11 w-11 items-center justify-center rounded-full bg-warn-soft text-warn"><Icon name="pause" /></div>
      <h1 ref={heading} tabIndex={-1} id="unavailable-title" className="text-display font-semibold text-tx">{UNAVAILABLE_HEADING}</h1>
      <p className="text-body text-tx">{TRY_LATER}{contactSentence(contact)}</p>
      <RetryAction retry={retry} primary>
        <a href="#/about" className="text-body text-accent underline">{ABOUT_LINK}</a>
      </RetryAction>
      <footer><DatasetMark /></footer>
    </section>
  </main>;
}

export function PausedBanner() {
  const value = useContext(PausedCtx);
  if (value === null || value.paused === null) return null;
  return <div className="mb-4">
    <Banner tone="warn" icon="pause" title={UNAVAILABLE_HEADING}
      meta={<>{TRY_LATER}{contactSentence(value.paused.contact)}</>}>
      <RetryAction retry={value.retry} />
    </Banner>
  </div>;
}
