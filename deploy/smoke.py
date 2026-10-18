"""Smoke-check a running ShelfShift URL against the product app's contract (06-CONTEXT D-18; OPS-07; 06.1 D-47, D-53,
D-62; HQ handoff item 7).

    python -m deploy.smoke URL

It checks, in this order, and prints one line per step, `step: ok` (the passcode counter adds a note in brackets),
`step: FAIL <reason>` or `step: skipped (needs <earlier step>)`, so a failing step never hides the steps after it that
do not depend on it (NEEDS). It exits 0 only when every checked step is ok, else 1; a missing or blank credential exits
2 before any request is sent.

    healthz           `{"ok": true}`
    readyz            exactly `{"ok": true, "db": "ok"}`
    spa page          index.html with the root element, revalidated with `Cache-Control: no-cache`
    spa guard         an encoded `..` path is refused or answered with index.html, never with source
    locked gate       a private route without a session is 401 locked
    HQ sign-in        POST /api/session {"account": "HQ", "passcode": <HQ value>} answers 200 with role "hq"; on an
                      https URL its `ss_session` cookie carries Secure and HttpOnly (FLAG-04-11a)
    story card        as HQ, GET /api/cases page by page (bounded by `total`, at most MAX_PAGES pages) until the
                      first card in server order tagged one of STORY_TAGS (the reviewer case: a verify or transfer
                      case on a public set, the milk story SUS-19 on the synthetic one); an unevaluated one
                      (rec_version null) is evaluated first
    HQ policy edit    the write-free proof (POLICY_PROOF): GET /api/settings, then PATCH /api/settings with that
                      version and the first field that has a max set one above it must answer 400 out_of_bounds, and
                      a second GET shows the same version and values
    store sign-in     POST /api/session {"account": <the story's source>, "passcode": <store value>} answers 200 with
                      role "manager" and that branch
    store policy edit refused   the same PATCH as the store answers 403 forbidden, and the version is unchanged
    simulate          a what-if on the story (a doubled transport quote gives another version and leaves the active
                      one unchanged)
    export            the story's bundle holds the stored engine input
    demo endpoints refused      with demo mode off, POST /api/demo/reset and /api/demo/advance-day answer 403
                      forbidden and the removed POST /api/demo/role and /api/cases/{id}/wait answer 404 not_found
                      (DEMO_REFUSED); any other answer names the route and the answer
    HQ refuses the store passcode   the store value on the HQ account answers 401 bad_passcode: the two credentials
                      never open each other's account
    passcode counter  D-24, below
    chat ping         `not run (no chat route until Phase 9)`, never ok (D-18)

Why the HQ policy proof writes nothing (T-p4t-05): an accepted policy write re-evaluates every live case of the
sandbox on the shared engine worker, during judging too, while build/product's API tests already prove the real write
and its re-evaluation. A value above the bound reaches the write path (HQ passes the role check and the version check,
which come first) and is refused there, so nothing is written. The coordinator kept this default on 2026-10-17.

Which writes land where (ASSUMPTION A12 (06.1-HANDOFF)): each run clears its cookie first, so the HQ sign-in opens the
smoke's own sandbox (one of the daily sandbox cap), and the store sign-in keeps that workspace under a rotated session.
The evaluate and the what-if write only there; the refused calls (policy edits, demo routes) write nothing; judges'
sandboxes are never touched. Every request that carries the cookie and writes sends a fresh Idempotency-Key.

The passcode counter step (D-24) proves that a visitor cannot dodge the wrong-passcode counter by writing
X-Forwarded-For. It sends one wrong guess to the HQ account plainly and one with a forged X-Forwarded-For (a random
203.0.113.x documentation address, RFC 5737) and is ok when the forged guess was counted with the plain one: its
attempts_left is one lower, or it is a 429 when none were left. A fixed window can turn over between the two guesses,
so a pair that does not match is sent once more, and only a second mismatch reports the forged entry counted as another
address. When the step leaves this address with no attempts it waits 61 s before returning (a block lasts until the
clock minute ends, at most 60 s). The step never prints an address or a hash of one; on Cloud Run it tests the
rightmost entry Google's front end appends, locally the TCP peer. Each wrong guess differs from both credentials.
Wrong guesses per run: one for "HQ refuses the store passcode" plus the counter's pair and at most one retry pair, so
at most 5, under the merged app's 10 per clock minute (ASSUMPTION A8 (06.1-HANDOFF)).

Both credentials come from the environment, SMOKE_PASSCODE (the store passcode) and SMOKE_HQ_PASSCODE, or from hidden
prompts ("Store passcode: ", "HQ passcode: ") when stdin is a terminal; neither is ever an argument, printed or part of
a status, even when a server echoes it back. A missing or blank one exits 2 naming its variable before any request.
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
import time
import uuid

import httpx

STEPS = ("healthz", "readyz", "spa page", "spa guard", "locked gate", "HQ sign-in", "story card", "HQ policy edit",
         "store sign-in", "store policy edit refused", "simulate", "export", "demo endpoints refused",
         "HQ refuses the store passcode", "passcode counter", "chat ping")
NEEDS = {"spa guard": "spa page", "story card": "HQ sign-in", "HQ policy edit": "HQ sign-in",
         "store sign-in": "story card", "store policy edit refused": "store sign-in", "simulate": "story card",
         "export": "story card", "demo endpoints refused": "HQ sign-in", "passcode counter": "HQ sign-in"}
CHAT_NOT_RUN = "not run (no chat route until Phase 9)"
# ASSUMPTION R1 (05.2-HANDOFF): the reviewer case's tag. A public set's start seat is the source of its first verify
# case, else of its first transfer case (reference/public.json catalogue.start_kind, 05.2 D-38); the synthetic set's
# story is SUS-19, tagged api/cases.py STORY_TAG (STORY_TAG below). The first card in server order with one of these
# tags is taken.
STORY_TAGS = ("verify", "transfer", "milk-story")
COOKIE = "ss_session"
READY = {"ok": True, "db": "ok"}
# The keys readyz may add to READY: Phase 11's dependency states and Phase 9's model id (11-CONTEXT D-01, D-19). ok and
# db decide; a key outside this set (an older contract's "postgres") fails.
READY_EXTRA = frozenset({"vertex_ai", "cloud_storage", "data_bucket", "uploads_bucket", "model_id", "checked_at"})


def is_ready(body) -> bool:
    return (isinstance(body, dict) and all(body.get(k) == v for k, v in READY.items())
            and set(body) - set(READY) <= READY_EXTRA)
# Sent with its encoded slashes intact: a server that decodes them before routing would walk out of web/dist.
GUARD_PATH = "/..%2f..%2fapi/config.py"
SOURCE_MARK = "def load_settings"  # a line of api/config.py: seeing it means source was served
TIMEOUT_S = 60  # a what-if runs the engine; the case budget is 3 s, the rest is headroom for a cold instance
COUNTED = "ok (the forged X-Forwarded-For was counted with this client's own guesses)"
WAITED = "; waited 61 s for this address's block to end"
# ASSUMPTION A8 (06.1-HANDOFF): both account kinds spend one per-address cap on POST /api/session; after the merge it
# is build/deploy's D-24 rule, 10 per clock minute, so a block lasts at most 60 s (06-REVIEWS R2-4, D-R1)
BLOCK_WAIT_S = 61
HQ_ACCOUNT = "HQ"  # ASSUMPTION A1 (06.1-HANDOFF): the HQ account's code on POST /api/session {"account", "passcode"}
HQ_ROLE, STORE_ROLE = "hq", "manager"
STORY_TAG = "milk-story"  # ASSUMPTION A5 (06.1-HANDOFF): the synthetic release data's story tag (one of STORY_TAGS)
MAX_PAGES = 50  # the story search's hard cap, whatever `total` says
# ASSUMPTION A3 (06.1-HANDOFF): PATCH /api/settings {expected_version, changes}; a store is 403 forbidden before any
# read; HQ with the current version and a value above a field's max is 400 out_of_bounds and writes nothing; GET
# /api/settings answers {version, values, bounds}
POLICY_PROOF = {"hq": "400 out_of_bounds", "store": "403 forbidden"}
# ASSUMPTION A4 (06.1-HANDOFF): with DEMO_MODE off, reset and advance-day are 403 forbidden; the seat switch and /wait
# were removed, so they are 404 not_found
DEMO_REFUSED = (("/api/demo/reset", "403 forbidden"), ("/api/demo/advance-day", "403 forbidden"),
                ("/api/demo/role", "404 not_found"), ("/api/cases/{case_id}/wait", "404 not_found"))


class Fail(Exception):
    """A step's failure, with the reason printed after FAIL."""


def _key() -> dict[str, str]:
    """A fresh Idempotency-Key for a request that carries the cookie and writes."""
    return {"Idempotency-Key": uuid.uuid4().hex}


def _answer(r: httpx.Response) -> str:
    """The status code and, for the error envelope, its error code ("401 bad_passcode")."""
    try:
        body = r.json()
    except ValueError:
        return str(r.status_code)
    if isinstance(body, dict) and isinstance(body.get("error"), str):
        return f"{r.status_code} {body['error']}"
    return str(r.status_code)


def _json(r: httpx.Response, status: int = 200):
    """The JSON body of an answer that must have `status`; anything else is a Fail naming what came back."""
    if r.status_code != status:
        raise Fail(_answer(r))
    try:
        return r.json()
    except ValueError:
        raise Fail(f"{r.status_code} with a body that is not JSON") from None


def _cookie_flags(r: httpx.Response) -> set[str] | None:
    """The attribute names (lower case) on the ss_session Set-Cookie, or None when the answer sets no such cookie."""
    for header in r.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        if name.strip() == COOKIE:
            return {part.split("=", 1)[0].strip().lower() for part in rest.split(";")[1:]}
    return None


def _healthz(c, ctx):
    if _json(c.get("/api/healthz")) != {"ok": True}:
        raise Fail("the body is not {\"ok\": true}")


def _readyz(c, ctx):
    r = c.get("/api/readyz")
    if r.status_code != 200:
        raise Fail(_answer(r))
    if not is_ready(_json(r)):
        raise Fail('the body is not {"ok": true, "db": "ok"} with only the known dependency keys')


def _spa_page(c, ctx):
    r = c.get("/")
    if r.status_code != 200:
        raise Fail(_answer(r))
    if 'id="root"' not in r.text:
        raise Fail("the page has no root element")
    if "no-cache" not in r.headers.get("cache-control", ""):
        raise Fail("index.html is not served with Cache-Control: no-cache")
    ctx["index"] = r.text


def _spa_guard(c, ctx):
    r = c.get(GUARD_PATH)
    if SOURCE_MARK in r.text:
        raise Fail("a path outside the web build served source")
    if r.status_code in (400, 404):
        return
    if r.status_code == 200 and r.text == ctx["index"]:
        return
    raise Fail(f"{r.status_code} with a body that is not index.html")


def _locked_gate(c, ctx):
    r = c.get("/api/cases")
    if _answer(r) != "401 locked":
        raise Fail(f"{_answer(r)} without a session, not 401 locked")


def _sign_in(c, account: str, passcode: str) -> tuple[dict, httpx.Response]:
    """POST /api/session by account (ASSUMPTION A1 (06.1-HANDOFF)): the body and the answer; a key is sent whenever
    a cookie is held. Fail unless 200 ok with the session cookie set (Secure and HttpOnly on https)."""
    headers = _key() if c.cookies.get(COOKIE) else {}
    r = c.post("/api/session", json={"account": account, "passcode": passcode}, headers=headers)
    body = _json(r)
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise Fail("the answer is not ok")
    flags = _cookie_flags(r)
    if flags is None:
        raise Fail(f"no {COOKIE} cookie was set")
    if c.base_url.scheme == "https":
        lacking = [name for name in ("Secure", "HttpOnly") if name.lower() not in flags]
        if lacking:
            raise Fail(f"the {COOKIE} cookie lacks {' and '.join(lacking)} on https")
    return body, r


def _hq_sign_in(c, ctx):
    body, _ = _sign_in(c, HQ_ACCOUNT, ctx["hq"])
    if body.get("role") != HQ_ROLE:
        raise Fail(f"the {HQ_ACCOUNT} account signed in with role {body.get('role')!r}, not {HQ_ROLE!r}")


def story_card(c) -> dict:
    """The first card in server order tagged one of STORY_TAGS, paging GET /api/cases from page 1 until a page is
    empty, `page_size` or `total` is not an int (the synthetic set's one page), the pages cover `total`, or MAX_PAGES;
    Fail naming the tags seen when there is none."""
    seen: list[str] = []
    page = 1
    while page <= MAX_PAGES:
        board = _json(c.get("/api/cases", params={"page": page}))
        cards = board.get("cases") if isinstance(board, dict) else None
        if not isinstance(cards, list):
            raise Fail("the board answer holds no cases list")
        if page == 1 and not cards:
            raise Fail("the board has no cases")
        for card in cards:
            if isinstance(card, dict) and card.get("tag") in STORY_TAGS:
                return card
            if isinstance(card, dict) and card.get("tag") and card["tag"] not in seen:
                seen.append(card["tag"])
        size, total = board.get("page_size"), board.get("total")
        if not cards or not isinstance(size, int) or not isinstance(total, int) or page * size >= total:
            break
        page += 1
    tags = ", ".join(seen[:10]) if seen else "none"
    raise Fail(f"no card tagged {', '.join(STORY_TAGS[:-1])} or {STORY_TAGS[-1]} on the board (tags seen: {tags})")


def _story_card(c, ctx):
    card = story_card(c)
    if not card.get("case_id") or not card.get("source"):
        raise Fail(f"the {card.get('tag')} card has no case_id or source")
    ctx["case_id"], ctx["source"] = card["case_id"], card["source"]
    # ASSUMPTION A6 (06.1-HANDOFF): an unevaluated public-set flag (rec_version null) is evaluated before the what-if
    if card.get("rec_version") is None:
        d = _json(c.post(f"/api/cases/{card['case_id']}/evaluate", json={}, headers=_key()))
        if not isinstance(d, dict) or not d.get("rec_version"):
            raise Fail(f"evaluating the {card.get('tag')} card returned no version")


def _settings(c) -> dict:
    s = _json(c.get("/api/settings"))
    if not isinstance(s, dict) or not isinstance(s.get("version"), int) or not isinstance(s.get("values"), dict) \
            or not isinstance(s.get("bounds"), dict):
        raise Fail("GET /api/settings is not {version, values, bounds}")
    return s


def _above_max(settings: dict) -> dict:
    """One field set one above its bound's max: the first field whose bound has a max."""
    for field, bound in settings["bounds"].items():
        bound = bound if isinstance(bound, dict) else {}
        b = bound["value"] if isinstance(bound.get("value"), dict) else bound  # the file's shape or the flat one
        if isinstance(b.get("max"), (int, float)) and not isinstance(b.get("max"), bool):
            return {field: b["max"] + 1}
    raise Fail("no policy field has a max to step over")


def _policy_patch(c, settings: dict) -> httpx.Response:
    return c.patch("/api/settings", json={"expected_version": settings["version"],
                                          "changes": _above_max(settings)}, headers=_key())


def _hq_policy_edit(c, ctx):
    before = _settings(c)
    r = _policy_patch(c, before)
    if _answer(r) != POLICY_PROOF["hq"]:
        raise Fail(f"an out-of-bounds HQ policy edit answered {_answer(r)}, not {POLICY_PROOF['hq']}")
    after = _settings(c)
    if (after["version"], after["values"]) != (before["version"], before["values"]):
        raise Fail("the refused HQ policy edit changed the settings")


def _store_sign_in(c, ctx):
    body, _ = _sign_in(c, ctx["source"], ctx["store"])
    if body.get("role") != STORE_ROLE or body.get("branch") != ctx["source"]:
        raise Fail(f"the store {ctx['source']} signed in as role {body.get('role')!r}, branch {body.get('branch')!r}")


def _store_policy_edit(c, ctx):
    before = _settings(c)
    r = _policy_patch(c, before)
    if _answer(r) != POLICY_PROOF["store"]:
        raise Fail(f"a store's policy edit answered {_answer(r)}, not {POLICY_PROOF['store']}")
    if _settings(c)["version"] != before["version"]:
        raise Fail("the refused store policy edit changed the version")


def _detail(c, ctx) -> dict:
    d = _json(c.get(f"/api/cases/{ctx['case_id']}"))
    if not isinstance(d, dict):
        raise Fail("the case detail is not an object")
    return d


# ASSUMPTION A7 (06.1-HANDOFF): the detail keeps rec_version, result.recommended, result.options[].run_code and
# engine_input.runs[].fixed_cost; the export holds engine_input
def _simulate(c, ctx):
    d = _detail(c, ctx)
    active = d.get("rec_version")
    options = (d.get("result") or {}).get("options", [])
    best = next((o for o in options if o.get("key") == (d.get("result") or {}).get("recommended")), {})
    runs = (d.get("engine_input") or {}).get("runs") or []
    trip = next((run for run in runs if best.get("run_code") and run.get("run_code") == best["run_code"]
                 and run.get("dest") in (None, best.get("dest"))), None)
    trip = trip or (runs[0] if runs else None)
    if trip is None:
        raise Fail("the case's engine input has no transport run to quote")
    quote = [{"kind": "transport_quote", "run_code": trip["run_code"], "fixed_cost": trip["fixed_cost"] * 2}]
    w = _json(c.post(f"/api/cases/{ctx['case_id']}/simulate", json={"overrides": quote}, headers=_key()))
    if not isinstance(w, dict) or not w.get("rec_version"):
        raise Fail("the what-if returned no version")
    if w["rec_version"] == active:
        raise Fail("the what-if returned the active version")
    if _detail(c, ctx).get("rec_version") != active:
        raise Fail("the what-if changed the active version")


def _export(c, ctx):
    d = _detail(c, ctx)
    bundle = _json(c.get(f"/api/cases/{ctx['case_id']}/export"))
    if not isinstance(bundle, dict) or "engine_input" not in bundle:
        raise Fail("the export holds no engine input")
    if bundle["engine_input"] != d.get("engine_input"):
        raise Fail("the export's engine input is not the stored one")


def _demo_refused(c, ctx):
    session = c.get("/api/session")
    version = session.json().get("version", 0) if session.status_code == 200 else 0
    bodies = {"/api/demo/advance-day": {"expected_version": version}}
    wrong = []
    for route, want in DEMO_REFUSED:
        path = route.format(case_id=ctx.get("case_id") or "c-smoke")
        got = _answer(c.post(path, json=bodies.get(route, {}), headers=_key()))
        if got != want:
            wrong.append(f"{route} answered {got}, not {want}")
    if wrong:
        raise Fail("; ".join(wrong))


# ASSUMPTION A2 (06.1-HANDOFF): the store passcode on the HQ account is 401 bad_passcode and spends one wrong attempt
def _hq_refuses_store(c, ctx):
    headers = _key() if c.cookies.get(COOKIE) else {}
    r = c.post("/api/session", json={"account": HQ_ACCOUNT, "passcode": ctx["store"]}, headers=headers)
    if _answer(r) != "401 bad_passcode":
        raise Fail(f"the store passcode on the {HQ_ACCOUNT} account answered {_answer(r)}, not 401 bad_passcode")


def _wrong_guess(c, ctx, forged: str | None = None) -> httpx.Response:
    """A wrong guess on the HQ account, different from both credentials."""
    known = {ctx["store"].strip(), ctx["hq"].strip()}
    guess = secrets.token_hex(8)
    while guess in known:
        guess = secrets.token_hex(8)
    headers = {"X-Forwarded-For": forged} if forged else {}
    if c.cookies.get(COOKIE):
        headers.update(_key())
    return c.post("/api/session", json={"account": HQ_ACCOUNT, "passcode": guess}, headers=headers)


def _attempts_left(r: httpx.Response) -> int | None:
    """attempts_left of a 401 bad_passcode, else None."""
    if _answer(r) != "401 bad_passcode":
        return None
    left = r.json().get("attempts_left")
    return left if isinstance(left, int) and not isinstance(left, bool) else None


def _is_block(r: httpx.Response) -> bool:
    try:
        return r.status_code == 429 and r.json().get("error") == "cap_reached" and r.json().get("kind") == "passcode"
    except ValueError:
        return False


def _counted_pair(c, ctx, retry: bool = False) -> tuple[bool, bool]:
    """One plain wrong guess, then one with a forged X-Forwarded-For: (the forged one was counted with the plain one,
    no attempts are left afterwards). On the retry a plain guess that is blocked means the first pair's forged guess
    did not use up this address's last attempt, so it was counted apart."""
    plain = _wrong_guess(c, ctx)
    if _is_block(plain):
        if retry:
            return False, True
        raise Fail("blocked by earlier wrong guesses from this address; wait a minute and rerun")
    a = _attempts_left(plain)
    if a is None:
        if _answer(plain) == "401 bad_passcode":
            raise Fail("a wrong guess's 401 carries no attempts_left")
        raise Fail(f"a wrong guess answered {_answer(plain)}, not 401 bad_passcode")
    forged = _wrong_guess(c, ctx, f"203.0.113.{secrets.randbelow(254) + 1}")
    if a == 0:
        return _is_block(forged), True
    left = _attempts_left(forged)
    return left == a - 1, left == 0


def _passcode_counter(c, ctx):
    together, none_left = _counted_pair(c, ctx)
    if not together:  # the window may have turned over between the two guesses: one retry settles it
        together, none_left = _counted_pair(c, ctx, retry=True)
    if not together:
        raise Fail("the forged X-Forwarded-For was counted as another address")
    if none_left:
        ctx["sleep"](BLOCK_WAIT_S)
        return COUNTED + WAITED
    return COUNTED


CHECKS = {"healthz": _healthz, "readyz": _readyz, "spa page": _spa_page, "spa guard": _spa_guard,
          "locked gate": _locked_gate, "HQ sign-in": _hq_sign_in, "story card": _story_card,
          "HQ policy edit": _hq_policy_edit, "store sign-in": _store_sign_in,
          "store policy edit refused": _store_policy_edit, "simulate": _simulate, "export": _export,
          "demo endpoints refused": _demo_refused, "HQ refuses the store passcode": _hq_refuses_store,
          "passcode counter": _passcode_counter}


def _hidden(text: str, passcode: str, hq_passcode: str = "") -> str:
    """The text with both credentials replaced, so no status can carry one (a server may echo a request back)."""
    for value, label in ((passcode, "[passcode]"), (hq_passcode, "[HQ passcode]")):
        for form in {value, (value or "").strip()}:
            if form:
                text = text.replace(form, label)
    return text


def check(client, passcode: str, hq_passcode: str, *, sleep=time.sleep) -> list[tuple[str, str]]:
    """Run every step against `client` (an httpx.Client with a base_url, or FastAPI's TestClient) and return one
    (step, status) pair per entry of STEPS, in order. A blank store or HQ passcode is a ValueError before any request.
    `sleep` is the passcode counter's wait (tests inject it)."""
    if not isinstance(passcode, str) or not passcode.strip():
        raise ValueError("the passcode is empty: set SMOKE_PASSCODE")
    if not isinstance(hq_passcode, str) or not hq_passcode.strip():
        raise ValueError("the HQ passcode is empty: set SMOKE_HQ_PASSCODE")
    client.cookies.clear()  # every run starts without a session, so the HQ sign-in opens a fresh sandbox
    ctx: dict = {"sleep": sleep, "store": passcode, "hq": hq_passcode}
    roots: dict[str, str] = {}  # step -> the earlier step whose failure it waits on (itself when it failed)
    out = []
    for step in STEPS:
        if step == "chat ping":
            out.append((step, CHAT_NOT_RUN))
            continue
        need = NEEDS.get(step)
        if need is not None and need in roots:
            roots[step] = roots[need]
            out.append((step, f"skipped (needs {roots[need]})"))
            continue
        try:
            status = CHECKS[step](client, ctx) or "ok"
        except Fail as exc:
            status = f"FAIL {exc}"
        except httpx.HTTPError as exc:
            status = f"FAIL {type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - a malformed answer is a FAIL, never a traceback
            status = f"FAIL {type(exc).__name__} reading the answer"
        if status != "ok" and not status.startswith("ok "):
            roots[step] = step
        out.append((step, _hidden(status, passcode, hq_passcode)))
    return out


def connect(url: str) -> httpx.Client:
    return httpx.Client(base_url=url, timeout=TIMEOUT_S, follow_redirects=False)


def _credential(variable: str, prompt: str) -> str | None:
    """The credential from the environment variable, else a hidden prompt on a terminal, else None."""
    if variable in os.environ:
        return os.environ[variable]
    if sys.stdin is not None and sys.stdin.isatty():
        return getpass.getpass(prompt)
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m deploy.smoke", description=__doc__.splitlines()[0])
    ap.add_argument("url", help="the base URL, e.g. http://127.0.0.1:8080 or the service URL")
    args = ap.parse_args(argv)
    if not args.url.startswith(("http://", "https://")):
        print("smoke: the URL must start with http:// or https://", file=sys.stderr)
        return 2
    found = {}
    for variable, prompt, name in (("SMOKE_PASSCODE", "Store passcode: ", "passcode"),
                                   ("SMOKE_HQ_PASSCODE", "HQ passcode: ", "HQ passcode")):
        value = _credential(variable, prompt)
        if value is None:
            print(f"smoke: set {variable}, or run on a terminal to be asked for the {name}", file=sys.stderr)
            return 2
        if not value.strip():
            print(f"smoke: the {name} is empty: set {variable}", file=sys.stderr)
            return 2
        found[variable] = value
    client = connect(args.url.rstrip("/"))
    try:
        out = check(client, found["SMOKE_PASSCODE"], found["SMOKE_HQ_PASSCODE"])
    finally:
        client.close()
    for step, status in out:
        print(f"{step}: {status}", flush=True)
    return 1 if any(status.startswith("FAIL") for _, status in out) else 0


if __name__ == "__main__":
    sys.exit(main())
