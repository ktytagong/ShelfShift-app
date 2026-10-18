// The sign-in card (06.1-UI-SPEC "Sign-in"; sketch 001 E; D-02, D-47, D-64). It replaces the Phase 5 passcode gate:
// a person signs in as a store manager (store code + the store passcode) or as the HQ planner (HQ's own passcode).
// Rules kept here:
// - The credential lives only in this component's state (and the pending attempt's memory): never in storage, the URL
//   or a log (T-06.1-49). The card never lists accounts, pre-fills a field or hints at a credential (D-02, D-60).
// - Each press is one keyed write through useAction: a fresh Idempotency-Key per action, the same key and body on
//   Try again (D-44). Only the retry group gets Try again; a cap never does (UI-SPEC "Interaction states").
// - Server sentences are shown word for word: a wrong credential under the passcode, an unknown store under Store
//   code; focus goes back to the field that failed.
import { useEffect, useRef, useState, type FormEvent } from "react";
import { retryable } from "./copy.ts";
import { signIn } from "./contract.ts";
import { useAction } from "./hooks.ts";
import { DatasetMark } from "./shell.tsx";
import { Button, ErrorLine, TRY_AGAIN } from "./ui.tsx";

// --- copy (06.1-UI-SPEC "Sign-in", "Copywriting Contract"). "(proposed)" marks wording the user has not confirmed:
// one constant each, so a change is one line. The others are the UI-SPEC's own words. -------------------------------
export const SIGN_IN_INTRO = // (proposed) replaces the Phase 5 gate text
  "ShelfShift helps store managers and HQ planners decide what to do with short-life stock a store won't sell before " +
  "it expires: move it, mark it down or keep it. It checks the stock facts first and leaves every approval to people.";
export const STORE_CODE_HELP = "The code of the store you manage."; // (proposed)
export const OPENING_WORKSPACE = "Opening your workspace…"; // (proposed) replaces the Phase 5 gate's preparing line (D-03)
export const SIGNED_OUT_LINE = "You signed out. Your workspace is kept in this browser."; // (proposed)
export const SIGN_IN_AS = "Sign in as";
export const STORE_MANAGER = "Store manager";
export const HQ_PLANNER = "HQ planner";
export const STORE_CODE = "Store code";
export const PASSCODE = "Passcode";
export const HQ_PASSCODE = "HQ passcode";
export const SIGN_IN = "Sign in";
export const SIGNING_IN = "Signing in…";
export const ABOUT_LINK = "About";

// The HQ planner's account (04-API-CONTRACT "Product mode (06.1)", POST /api/session).
const HQ_ACCOUNT = "HQ";

type Kind = "store" | "hq";
type SignInBody = { account: string; passcode: string };

// `onSignedIn` refreshes the app's session (GET /api/session), which then renders the shell. `signedOut` shows the
// after-sign-out line. `workspaceKept` is true when this browser already has a workspace (it was signed in earlier in
// this page, or just signed out): the busy label is then "Signing in…"; otherwise the sign-in creates the workspace
// and the label reads "Opening your workspace…".
export function SignIn({ onSignedIn, signedOut = false, workspaceKept = false }: {
  onSignedIn: () => Promise<void>;
  signedOut?: boolean;
  workspaceKept?: boolean;
}) {
  const [kind, setKind] = useState<Kind>("store");
  const [store, setStore] = useState("");
  const [passcode, setPasscode] = useState("");
  const storeRef = useRef<HTMLInputElement>(null);
  const passRef = useRef<HTMLInputElement>(null);

  const action = useAction(async (body: SignInBody, key: string) => {
    const out = await signIn(body.account, body.passcode, key);
    await onSignedIn();
    return out;
  });
  const { busy, error } = action;

  // Where the error goes: an unknown store under Store code; everything else under the passcode field.
  const storeError = error !== null && error.code === "unknown_account" ? error : null;
  const passError = error !== null && error.code !== "unknown_account" ? error : null;

  // Focus stays on the field that failed (UI-SPEC "Sign-in" Errors).
  useEffect(() => {
    if (error === null) return;
    (error.code === "unknown_account" ? storeRef : passRef).current?.focus();
  }, [error]);

  const choose = (next: Kind) => {
    if (busy || next === kind) return;
    setKind(next);
    setPasscode(""); // a credential typed for one account kind never carries over to the other
    action.clear();
  };

  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (busy) return;
    const account = kind === "hq" ? HQ_ACCOUNT : store.trim().toUpperCase();
    void action.run({ account, passcode });
  };

  // Try again only for the retry group (busy, being prepared, unreachable): the same key and the same body.
  const tryAgain = passError !== null && retryable(passError.code)
    ? { label: TRY_AGAIN, onClick: () => void action.retry() }
    : undefined;

  const field = "mt-1 h-11 w-full rounded-md border border-line-2 bg-white px-2 text-body text-tx read-only:bg-side "
    + "aria-[invalid=true]:border-bad md:h-8";
  const passLabel = kind === "hq" ? HQ_PASSCODE : PASSCODE;

  return (
    <main className="flex min-h-full items-center justify-center overflow-auto px-4 py-8 md:items-start md:pt-16"
      style={{ background: "radial-gradient(900px 500px at 50% 0, var(--color-navy-800) 0, var(--color-navy-950) 60%)" }}>
      <form onSubmit={submit} aria-labelledby="signin-title"
        className="flex w-full max-w-[400px] flex-col gap-4 rounded-2xl bg-white p-6 shadow-lg md:p-8">
        <div className="flex items-center gap-2">
          <img src="/logo.png" alt="" width={40} height={40} className="h-10 w-10 shrink-0" />
          <h1 id="signin-title" className="text-title font-semibold text-tx">ShelfShift</h1>
        </div>
        <p className="text-body text-tx2">{SIGN_IN_INTRO}</p>
        {signedOut && (
          <p role="status" className="rounded-md bg-info-soft px-2 py-1 text-body text-tx">{SIGNED_OUT_LINE}</p>
        )}

        <fieldset role="radiogroup" aria-labelledby="signin-as" className="min-w-0">
          <legend id="signin-as" className="text-body font-semibold text-tx">{SIGN_IN_AS}</legend>
          <div className="mt-1 flex gap-1 rounded-lg bg-sunken p-1">
            {([["store", STORE_MANAGER], ["hq", HQ_PLANNER]] as const).map(([value, label]) => (
              <label key={value}
                className="flex h-11 min-w-0 flex-1 cursor-pointer items-center justify-center gap-2 rounded-md px-2 text-body text-tx2 has-[:checked]:bg-white has-[:checked]:font-semibold has-[:checked]:text-tx has-[:checked]:shadow-sm md:h-8">
                <input type="radio" name="signin-kind" value={value} checked={kind === value} disabled={busy}
                  onChange={() => choose(value)} className="accent-navy-800" />
                <span>{label}</span>
              </label>
            ))}
          </div>
        </fieldset>

        {kind === "store" && (
          <div>
            <label htmlFor="signin-store" className="block text-body font-semibold text-tx">{STORE_CODE}</label>
            <input ref={storeRef} id="signin-store" type="text" required autoFocus autoComplete="username"
              autoCapitalize="characters" spellCheck={false} value={store} readOnly={busy}
              onChange={(e) => setStore(e.target.value)}
              aria-invalid={storeError !== null || undefined}
              aria-describedby={storeError !== null ? "signin-store-help signin-store-error" : "signin-store-help"}
              className={field} />
            <p id="signin-store-help" className="mt-1 text-label text-tx3">{STORE_CODE_HELP}</p>
            <ErrorLine id="signin-store-error" error={storeError} className="mt-1" />
          </div>
        )}

        <div>
          <label htmlFor="signin-passcode" className="block text-body font-semibold text-tx">{passLabel}</label>
          <input ref={passRef} id="signin-passcode" key={kind} type="password" required autoFocus={kind === "hq"}
            autoComplete="current-password" value={passcode} readOnly={busy}
            onChange={(e) => setPasscode(e.target.value)}
            aria-invalid={passError?.code === "bad_passcode" || undefined}
            aria-describedby={passError !== null ? "signin-error" : undefined}
            className={field} />
          <ErrorLine id="signin-error" error={passError} action={tryAgain} className="mt-1" />
        </div>

        <Button kind="primary" type="submit" busy={busy} className="h-11 w-full md:h-10">
          {busy ? (workspaceKept ? SIGNING_IN : OPENING_WORKSPACE) : SIGN_IN}
        </Button>

        <div className="flex flex-wrap items-center justify-between gap-2">
          <DatasetMark />
          <a href="#/about" className="text-label text-navy-800 underline">{ABOUT_LINK}</a>
        </div>
      </form>
    </main>
  );
}
