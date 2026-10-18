"""The smoke against the real app on the tiny public template (05.2 D-38 tags; quick 261017-v6z): the story card step
finds a verify- or transfer-tagged case on the board HQ sees (every store), and every checked step is ok.

Its own module: `public_template` is module-scoped and leaves the public template ACTIVE until the module ends.
"""
from deploy import smoke
from tests.api.conftest import HQ, PASSCODE
# web_dist and demo_mode_off are fixtures (demo_mode_off is autouse: the product smoke runs with demo mode off, as the
# deployed service does, 06.1 D-62)
from tests.api.test_smoke import CHAT, COUNTED, demo_mode_off, no_sleep, visitor, web_dist  # noqa: F401


def test_every_checked_step_is_ok_against_the_public_app(public_template, web_dist):
    out = smoke.check(visitor("public-ok"), PASSCODE, HQ, sleep=no_sleep)
    assert [step for step, _ in out] == list(smoke.STEPS)
    assert smoke.STEPS[-1] == "chat ping" and out[-1] == CHAT
    assert dict(out)["passcode counter"] == COUNTED
    bad = [(step, status) for step, status in out[:-2] if status != "ok"]
    assert not bad, bad
