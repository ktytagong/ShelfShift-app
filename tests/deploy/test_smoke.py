"""deploy/smoke.py against DB-free fake servers (httpx.MockTransport), so it runs in the deploy gate (06.1 D-47, D-53,
D-62; HQ handoff item 7).

`Product` answers like build/product's app (as read on 2026-10-17) for every route a step uses: sign-in by account
(HQ only with the HQ value, a store code only with the store value; the credential before the Idempotency-Key, and a
key required when a cookie is present), a paged board, the evaluate route, the policy settings (HQ_ONLY 403 before
the version check, out_of_bounds after it), the demo routes (403 forbidden with demo mode off) and the removed routes
(404 not_found). Its wrong-passcode counter carries attempts_left, as build/deploy's D-24 rule does after the merge.
Keyword switches turn one behaviour into a broken deploy's. Both credentials are synthetic; no test reaches a
network, a database or a real service.
"""
from __future__ import annotations

import json
import re

import httpx
import pytest

from deploy import smoke

STORE = "fake-store-code-9"  # synthetic store passcode
HQ = "fake-hq-passcode-42"  # synthetic HQ passcode (at least 12 characters, not STORE)
SOURCE = "S101"
CHECKED = [s for s in smoke.STEPS if s not in ("chat ping", "passcode counter")]
COUNTED = "ok (the forged X-Forwarded-For was counted with this client's own guesses)"
BOUNDS = {"waste_price": {"min": 0, "max": 100, "unit": "USD/kg"}, "min_sellable_days": {"min": 1, "max": 7},
          "shortlist_step": {"allowed": [0, 5, 10]}}
DETAIL = {"rec_version": "v-active", "result": {"recommended": "T1", "options": [{"key": "T1", "run_code": "R1"}]},
          "engine_input": {"runs": [{"run_code": "R1", "fixed_cost": 10.0}]}}


def err(status: int, code: str, **extra) -> httpx.Response:
    return httpx.Response(status, json={"error": code, "message": "x", **extra})


class Product:
    """A fake of build/product's app; `sent` records every request."""

    def __init__(self, *, hq_takes_store=False, store_writes=False, hq_writes=False, demo_open=False,
                 removed_routes_back=False, tag=smoke.STORY_TAG, unevaluated=False, pages=3, echo=False,
                 set_cookie="ss_session={sid}; HttpOnly; Secure; Path=/; SameSite=lax", hq_role="hq",
                 store_branch=None, left=10, apart=False, no_attempts_left=False, **override):
        self.hq_takes_store, self.store_writes, self.hq_writes = hq_takes_store, store_writes, hq_writes
        self.demo_open, self.removed_routes_back, self.echo = demo_open, removed_routes_back, echo
        self.set_cookie, self.hq_role, self.store_branch = set_cookie, hq_role, store_branch
        self.unevaluated = unevaluated
        self.left = {"peer": left}  # wrong guesses left per counted address
        self.apart, self.no_attempts_left = apart, no_attempts_left  # a broken D-24: forged entries counted apart
        self.override = {tuple(k.split(" ", 1)): v for k, v in override.items()}
        self.version = 7
        self.values = {"waste_price": 0.5, "min_sellable_days": 2, "shortlist_step": 5}
        self.sessions: dict[str, dict] = {}
        story = {"case_id": "c-story", "key": "SUS-19", "tag": tag, "source": SOURCE,
                 "rec_version": None if unevaluated else "v-active"}
        filler = [{"case_id": f"c{i}", "key": f"SUS-{100 + i}", "tag": None, "source": "S9", "rec_version": "v"}
                  for i in range(2 * pages)]
        self.cards = filler + [story]  # the story on the last page
        self.page_size = 2
        self.evaluated = False
        self.sent: list[httpx.Request] = []

    # --- helpers ---
    def _session(self, request):
        cookie = request.headers.get("cookie", "")
        m = re.search(r"ss_session=([^;]+)", cookie)
        return self.sessions.get(m.group(1)) if m else None

    def _signed_in(self, account, role, branch):
        sid = f"s{len(self.sessions) + 1}"
        self.sessions[sid] = {"account": account, "role": role, "branch": branch}
        return httpx.Response(200, json={"ok": True, "reset_ms": 5, "account": account, "role": role,
                                         "branch": branch},
                              headers={"set-cookie": self.set_cookie.format(sid=sid)})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        method, path = request.method, request.url.raw_path.decode().split("?", 1)[0]
        if (method, path) in self.override:
            return self.override[(method, path)](request)
        query = dict(request.url.params)
        s = self._session(request)
        keyed = bool(request.headers.get("Idempotency-Key", "").strip())
        if path == "/api/healthz":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/readyz":
            return httpx.Response(200, json={"ok": True, "db": "ok"})
        if path == "/":
            return httpx.Response(200, text='<div id="root"></div>', headers={"Cache-Control": "no-cache"})
        if path == smoke.GUARD_PATH:
            return err(404, "not_found")
        if path == "/api/session" and method == "POST":
            body = json.loads(request.content or b"{}")
            if self.echo:
                return httpx.Response(400, json={"error": f"echo {request.content.decode()}"})
            account, given = body.get("account"), body.get("passcode")
            if account == "HQ":
                ok = given == HQ or (self.hq_takes_store and given == STORE)
            else:
                ok = given == STORE
            if not ok:
                forged = request.headers.get("x-forwarded-for")
                who = forged if (self.apart and forged) else "peer"
                self.left.setdefault(who, 10)  # a new address starts with a fresh counter
                if self.left[who] == 0:
                    return err(429, "cap_reached", kind="passcode")
                self.left[who] -= 1
                if self.no_attempts_left:
                    return err(401, "bad_passcode")
                return err(401, "bad_passcode", attempts_left=self.left[who])
            if s is not None and not keyed:
                return err(400, "idempotency_key")
            if account == "HQ":
                return self._signed_in("HQ", self.hq_role, None)
            return self._signed_in(account, "manager", self.store_branch or account)
        # every route below needs a session; removed routes are 404 before any dependency runs
        if method == "POST" and (path == "/api/demo/role" or re.fullmatch(r"/api/cases/[^/]+/wait", path)):
            if self.removed_routes_back and s is not None:
                return httpx.Response(200, json={"ok": True})
            return err(404, "not_found")
        if s is None:
            return err(401, "locked")
        if path == "/api/session" and method == "GET":
            return httpx.Response(200, json={**s, "version": self.version, "label": "Scenario date: x"})
        if path == "/api/cases" and method == "GET":
            page = int(query.get("page", "1"))
            start = (page - 1) * self.page_size
            cards = [dict(c) for c in self.cards[start:start + self.page_size]]
            if self.evaluated:
                for c in cards:
                    if c["case_id"] == "c-story":
                        c["rec_version"] = "v-active"
            return httpx.Response(200, json={"day": 3, "version": self.version, "cases": cards, "page": page,
                                             "page_size": self.page_size, "total": len(self.cards)})
        if path == "/api/cases/c-story/evaluate" and method == "POST":
            if not keyed:
                return err(400, "idempotency_key")
            self.evaluated = True
            return httpx.Response(200, json=DETAIL)
        if path == "/api/cases/c-story":
            return httpx.Response(200, json=DETAIL)
        if path == "/api/cases/c-story/simulate":
            return httpx.Response(200, json={"rec_version": "v-whatif"}) if keyed else err(400, "idempotency_key")
        if path == "/api/cases/c-story/export":
            return httpx.Response(200, json={"engine_input": DETAIL["engine_input"]})
        if path == "/api/settings" and method == "GET":
            return httpx.Response(200, json={"version": self.version, "values": dict(self.values),
                                             "bounds": BOUNDS})
        if path == "/api/settings" and method == "PATCH":
            if not keyed:
                return err(400, "idempotency_key")
            body = json.loads(request.content)
            if s["role"] != "hq" and not self.store_writes:
                return err(403, "forbidden")
            if body.get("expected_version") != self.version:
                return err(409, "stale_version", version=self.version)
            writes = self.hq_writes if s["role"] == "hq" else self.store_writes
            if writes:
                self.values.update(body["changes"])
                self.version += 1
                return httpx.Response(200, json={"version": self.version, "values": self.values, "bounds": BOUNDS})
            return err(400, "out_of_bounds")
        if path in ("/api/demo/reset", "/api/demo/advance-day") and method == "POST":
            if not self.demo_open:
                return err(403, "forbidden")
            return httpx.Response(200, json={"ok": True})
        return err(404, "not_found")


def client_for(handler, base="https://smoke.example.invalid") -> httpx.Client:
    return httpx.Client(base_url=base, transport=httpx.MockTransport(handler))


def run(handler, base="https://smoke.example.invalid", sleeps=None) -> dict[str, str]:
    with client_for(handler, base) as c:
        return dict(smoke.check(c, STORE, HQ, sleep=(sleeps.append if sleeps is not None else lambda s: None)))


def posts(server: Product, path: str) -> list[dict]:
    return [json.loads(r.content or b"{}") for r in server.sent if r.method == "POST" and r.url.path == path]


# --- the order and every ok line ----------------------------------------------------------------------------------

def test_the_steps_are_the_product_checks_in_order():
    assert smoke.STEPS == ("healthz", "readyz", "spa page", "spa guard", "locked gate", "HQ sign-in", "story card",
                           "HQ policy edit", "store sign-in", "store policy edit refused", "simulate", "export",
                           "demo endpoints refused", "HQ refuses the store passcode", "passcode counter",
                           "chat ping")
    assert smoke.NEEDS == {"spa guard": "spa page", "story card": "HQ sign-in", "HQ policy edit": "HQ sign-in",
                           "store sign-in": "story card", "store policy edit refused": "store sign-in",
                           "simulate": "story card", "export": "story card",
                           "demo endpoints refused": "HQ sign-in", "passcode counter": "HQ sign-in"}


def test_every_checked_step_is_ok_against_the_product_fake():
    server = Product()
    got = run(server)
    assert list(got) == list(smoke.STEPS)
    assert {s: got[s] for s in CHECKED} == {s: "ok" for s in CHECKED}
    assert got["passcode counter"] == COUNTED and got["chat ping"] == smoke.CHAT_NOT_RUN
    sign_ins = posts(server, "/api/session")
    assert sign_ins[0] == {"account": "HQ", "passcode": HQ}
    assert sign_ins[1] == {"account": SOURCE, "passcode": STORE}  # the store is the story's source
    assert {"account": "HQ", "passcode": STORE} in sign_ins


def test_every_request_with_the_cookie_that_writes_carries_a_fresh_key():
    server = Product()
    run(server)
    writes = [r for r in server.sent if r.method in ("POST", "PATCH") and "ss_session=" in r.headers.get("cookie", "")]
    keys = [r.headers.get("Idempotency-Key") for r in writes]
    assert writes and all(k and re.fullmatch(r"[0-9a-f]{32}", k) for k in keys)
    assert len(set(keys)) == len(keys)


def test_the_story_is_found_by_tag_across_pages_and_an_unevaluated_one_is_evaluated():
    server = Product(unevaluated=True)
    got = run(server)
    assert got["story card"] == "ok" and got["simulate"] == "ok"
    pages = [r.url.params.get("page") for r in server.sent if r.url.path == "/api/cases" and "page" in r.url.params]
    assert pages == ["1", "2", "3", "4"]  # the story is on the last page; the search stops there
    assert len(posts(server, "/api/cases/c-story/evaluate")) == 1


def test_an_evaluated_story_is_not_evaluated_again():
    server = Product()
    run(server)
    assert posts(server, "/api/cases/c-story/evaluate") == []


def test_a_public_sets_reviewer_case_is_found_by_its_verify_or_transfer_tag():
    """build/deploy's public route (05.2 D-38, ASSUMPTION R1): the first card tagged one of STORY_TAGS is the story."""
    assert smoke.STORY_TAGS == ("verify", "transfer", "milk-story") and smoke.STORY_TAG in smoke.STORY_TAGS
    for tag in ("verify", "transfer"):
        got = run(Product(tag=tag))
        for step in ("story card", "store sign-in", "simulate", "export"):
            assert got[step] == "ok", (tag, step)


def test_a_board_without_a_reviewer_tag_names_every_tag_looked_for():
    got = run(Product(tag="markdown"))
    assert got["story card"] == "FAIL no card tagged verify, transfer or milk-story on the board (tags seen: markdown)"


def test_an_empty_board_fails_naming_what_is_missing():
    empty = lambda r: httpx.Response(200, json={"cases": [], "page": 1, "page_size": 2, "total": 0})  # noqa: E731
    got = run(Product(**{"GET /api/cases": empty}))
    assert got["story card"].startswith("FAIL") and "no cases" in got["story card"]


def test_the_hq_policy_proof_writes_nothing():
    server = Product()
    got = run(server)
    assert got["HQ policy edit"] == "ok"
    patches = [json.loads(r.content) for r in server.sent if r.method == "PATCH"]
    assert patches[0] == {"expected_version": 7, "changes": {"waste_price": 101}}  # one above the first max
    assert server.version == 7


def test_at_most_five_wrong_guesses_per_run():
    server = Product()
    run(server)
    wrong = [b for b in posts(server, "/api/session") if b["passcode"] not in (HQ, STORE) or b == {
        "account": "HQ", "passcode": STORE}]
    assert 1 <= len(wrong) <= 5
    for b in wrong:
        assert b["account"] == "HQ"  # every wrong guess goes to the HQ account
    guesses = [b["passcode"] for b in wrong if b["passcode"] != STORE]
    assert guesses and HQ not in guesses


# --- what a broken deploy looks like -------------------------------------------------------------------------------

def test_a_store_that_can_edit_policy_fails_its_step():
    got = run(Product(store_writes=True))
    assert got["store policy edit refused"].startswith("FAIL")
    assert "200" in got["store policy edit refused"]


def test_an_hq_policy_proof_that_writes_fails():
    got = run(Product(hq_writes=True))
    assert got["HQ policy edit"].startswith("FAIL") and "out_of_bounds" in got["HQ policy edit"]


def test_a_demo_route_that_answers_200_fails_naming_it():
    got = run(Product(demo_open=True))
    assert got["demo endpoints refused"].startswith("FAIL")
    assert "/api/demo/reset" in got["demo endpoints refused"] and "200" in got["demo endpoints refused"]
    back = run(Product(removed_routes_back=True))
    assert back["demo endpoints refused"].startswith("FAIL") and "/api/demo/role" in back["demo endpoints refused"]


def test_a_missing_story_tag_names_the_tags_seen_and_skips_what_needs_it():
    got = run(Product(tag="other-story"))
    assert got["story card"].startswith("FAIL") and smoke.STORY_TAG in got["story card"]
    assert "other-story" in got["story card"]
    for step in ("store sign-in", "simulate", "export"):
        assert got[step] == "skipped (needs story card)", step
    assert got["store policy edit refused"] == "skipped (needs story card)"
    assert got["HQ policy edit"] == "ok" and got["demo endpoints refused"] == "ok"


def test_a_failed_hq_sign_in_skips_every_step_that_needs_it():
    got = run(Product(hq_role="manager"))
    assert got["HQ sign-in"].startswith("FAIL") and "hq" in got["HQ sign-in"]
    for step in ("story card", "HQ policy edit", "store sign-in", "store policy edit refused", "simulate", "export",
                 "demo endpoints refused", "passcode counter"):
        assert got[step] == "skipped (needs HQ sign-in)", step
    assert got["HQ refuses the store passcode"] == "ok"  # it needs no session


def test_a_store_sign_in_with_another_branch_fails():
    got = run(Product(store_branch="S999"))
    assert got["store sign-in"].startswith("FAIL") and SOURCE in got["store sign-in"]
    assert got["store policy edit refused"] == "skipped (needs store sign-in)"


@pytest.mark.parametrize("cookie, missing", [("ss_session={sid}; HttpOnly; Path=/", "Secure"),
                                             ("ss_session={sid}; Secure; Path=/", "HttpOnly")])
def test_on_https_the_hq_cookie_needs_secure_and_httponly(cookie, missing):
    got = run(Product(set_cookie=cookie))
    assert got["HQ sign-in"].startswith("FAIL") and missing in got["HQ sign-in"]


def test_on_http_secure_is_not_required():
    got = run(Product(set_cookie="ss_session={sid}; HttpOnly; Path=/"), base="http://smoke.example.invalid")
    assert got["HQ sign-in"] == "ok"


def test_hq_refuses_the_store_passcode_is_ok_only_on_401_bad_passcode():
    got = run(Product(hq_takes_store=True))
    assert got["HQ refuses the store passcode"].startswith("FAIL ")
    assert "store passcode" in got["HQ refuses the store passcode"]


def test_neither_value_appears_in_any_status_even_when_echoed():
    got = run(Product(echo=True))
    text = json.dumps(got)
    assert STORE not in text and HQ not in text
    assert "echo" in got["HQ refuses the store passcode"]  # the echo came back, hidden


# --- the unchanged steps' rules (moved here from tests/api/test_smoke.py) ------------------------------------------

def test_readyz_must_report_the_current_contract():
    for body, status in (({"ok": True, "postgres": "ok"}, 200), ({"ok": False, "db": "unreachable"}, 503),
                         ({"ok": True, "db": "ok", "postgres": "ok"}, 200)):
        got = run(Product(**{"GET /api/readyz": lambda r, b=body, s=status: httpx.Response(s, json=b)}))
        assert got["readyz"].startswith("FAIL"), body
        assert got["HQ sign-in"] == "ok"  # readyz failing hides nothing after it


def test_readyz_accepts_the_judge_ready_body_with_its_dependency_keys():
    # Phase 11's readyz adds its dependency states and Phase 9 the model id; ok and db still decide, and an unknown key
    # (an older contract's "postgres") still fails
    body = {"ok": True, "db": "ok", "vertex_ai": "disabled", "cloud_storage": "disabled", "data_bucket": "not_checked",
            "uploads_bucket": "disabled", "model_id": "", "checked_at": "2026-10-18T06:00:00Z"}
    got = run(Product(**{"GET /api/readyz": lambda r: httpx.Response(200, json=body)}))
    assert got["readyz"] == "ok"


def test_the_spa_guard_refuses_served_source():
    source = lambda r: httpx.Response(200, text="def load_settings(path):\n    ...\n")  # noqa: E731
    assert run(Product(**{f"GET {smoke.GUARD_PATH}": source}))["spa guard"].startswith("FAIL")
    for answer in (lambda r: httpx.Response(400, text="Bad Request"),
                   lambda r: httpx.Response(200, text='<div id="root"></div>')):
        assert run(Product(**{f"GET {smoke.GUARD_PATH}": answer}))["spa guard"] == "ok"
    other = lambda r: httpx.Response(200, text="<p>something else</p>")  # noqa: E731
    assert run(Product(**{f"GET {smoke.GUARD_PATH}": other}))["spa guard"].startswith("FAIL")


def test_the_spa_page_needs_the_root_element_and_no_cache():
    for answer in (lambda r: httpx.Response(200, text='<div id="root"></div>'),
                   lambda r: httpx.Response(200, text="<p>hello</p>", headers={"Cache-Control": "no-cache"}),
                   lambda r: err(404, "not_found")):
        got = run(Product(**{"GET /": answer}))
        assert got["spa page"].startswith("FAIL") and got["spa guard"] == "skipped (needs spa page)"


def test_an_unreachable_server_is_a_fail_on_every_step_never_a_traceback():
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)
    got = run(down)
    for step in ("healthz", "readyz", "spa page", "locked gate", "HQ sign-in", "HQ refuses the store passcode"):
        assert got[step].startswith("FAIL ConnectError"), (step, got[step])
    assert got["spa guard"] == "skipped (needs spa page)" and got["export"] == "skipped (needs HQ sign-in)"


def test_a_non_json_answer_is_a_fail_not_a_traceback():
    got = run(Product(**{"GET /api/healthz": lambda r: httpx.Response(200, text="<html>")}))
    assert got["healthz"].startswith("FAIL")


def test_a_sign_in_without_its_cookie_fails():
    got = run(Product(set_cookie="other=1; Path=/"))
    assert got["HQ sign-in"].startswith("FAIL") and "ss_session" in got["HQ sign-in"]


def test_the_counter_step_waits_when_it_leaves_the_address_blocked():
    """3 left: the HQ refusal spends one, the counter's pair the last two, so it waits 61 s before returning."""
    sleeps = []
    got = run(Product(left=3), sleeps=sleeps)
    assert got["passcode counter"] == COUNTED + smoke.WAITED and sleeps == [smoke.BLOCK_WAIT_S]


def test_the_counter_step_reports_an_address_that_is_already_blocked():
    got = run(Product(left=1))
    assert got["passcode counter"] == ("FAIL blocked by earlier wrong guesses from this address; wait a minute and "
                                       "rerun")


def test_the_counter_step_fails_when_the_forged_entry_is_counted_apart():
    server = Product(apart=True)
    got = run(server)
    assert got["passcode counter"] == "FAIL the forged X-Forwarded-For was counted as another address"
    forged = [r for r in server.sent if "x-forwarded-for" in r.headers]
    assert len(forged) == 2  # the pair was retried once
    assert all(re.fullmatch(r"203\.0\.113\.\d{1,3}", r.headers["x-forwarded-for"]) for r in forged)


def test_a_server_without_attempts_left_fails_the_counter_step_plainly():
    got = run(Product(no_attempts_left=True))
    assert got["passcode counter"] == "FAIL a wrong guess's 401 carries no attempts_left"


ADDRESS = re.compile(r"\d{1,3}(\.\d{1,3}){3}|[0-9a-f]{0,4}(:[0-9a-f]{0,4}){2,}")


def test_no_status_names_an_address():
    for server in (Product(), Product(apart=True), Product(left=3)):
        for step, status in run(server).items():
            assert "203.0.113." not in status and not ADDRESS.search(status), (step, status)


# --- the credentials and the CLI ---------------------------------------------------------------------------------

@pytest.mark.parametrize("hq", ["", "   ", None])
def test_a_blank_hq_passcode_is_a_value_error_before_any_request(hq):
    server = Product()
    with client_for(server) as c, pytest.raises(ValueError, match="SMOKE_HQ_PASSCODE"):
        smoke.check(c, STORE, hq)
    assert server.sent == []


@pytest.mark.parametrize("env, word", [({"SMOKE_PASSCODE": STORE}, "SMOKE_HQ_PASSCODE"),
                                       ({"SMOKE_PASSCODE": STORE, "SMOKE_HQ_PASSCODE": "  "}, "SMOKE_HQ_PASSCODE"),
                                       ({"SMOKE_HQ_PASSCODE": HQ}, "SMOKE_PASSCODE")])
def test_main_exits_2_naming_the_missing_variable_before_any_request(env, word, monkeypatch, capsys):
    for key in ("SMOKE_PASSCODE", "SMOKE_HQ_PASSCODE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(smoke.sys, "stdin", None)

    def no_connect(url):
        raise AssertionError("a request was about to be sent")

    monkeypatch.setattr(smoke, "connect", no_connect)
    assert smoke.main(["https://smoke.example.invalid"]) == 2
    out = capsys.readouterr()
    assert word in out.err and STORE not in out.out + out.err and HQ not in out.out + out.err


def test_main_prints_one_line_per_step_and_exits_0_or_1(monkeypatch, capsys):
    monkeypatch.setenv("SMOKE_PASSCODE", STORE)
    monkeypatch.setenv("SMOKE_HQ_PASSCODE", HQ)
    monkeypatch.setattr(smoke, "connect", lambda url: client_for(Product()))
    monkeypatch.setattr(smoke.time, "sleep", lambda s: None)
    assert smoke.main(["https://smoke.example.invalid"]) == 0
    out = capsys.readouterr()
    assert [line.split(": ", 1)[0] for line in out.out.splitlines()] == list(smoke.STEPS)
    monkeypatch.setattr(smoke, "connect", lambda url: client_for(Product(store_writes=True)))
    assert smoke.main(["https://smoke.example.invalid"]) == 1
    out2 = capsys.readouterr()
    for text in (out.out + out.err, out2.out + out2.err):
        assert STORE not in text and HQ not in text


def test_main_takes_no_passcode_argument():
    with pytest.raises(SystemExit) as stop:
        smoke.main(["https://smoke.example.invalid", "--passcode", STORE])
    assert stop.value.code == 2
