"""Case Ask file drops open the existing evidence sheet; only explicit analysis may spend an AI request."""
import urllib.parse

import pytest
from playwright.sync_api import expect

from tests.web.test_assistant import assistant_api_command, real_assistant  # noqa: F401 -- explicit local fixtures
from tests.web.test_compare import owned_at_compare
from tests.web.conftest import switch_seat

pytestmark = pytest.mark.web
TITLE = "Report an observation for this case."


def open_ask(visitor, real_assistant):
    real_assistant.configure()
    page = visitor.page
    detail = owned_at_compare(page)
    expect(page.get_by_role("region", name="AI brief", exact=True).get_by_test_id("checked-brief")).to_be_visible()
    ask = page.get_by_role("region", name="Ask about this stock decision", exact=True)
    expect(ask).to_be_visible()
    return page, detail, ask


def drag_files(page, target, files, event="drop"):
    transfer = page.evaluate_handle("""files => {
      const dt = new DataTransfer();
      for (const file of files) dt.items.add(new File([new Uint8Array(file.size ?? 16)], file.name,
        {type: file.type}));
      return dt;
    }""", files)
    try:
        target.dispatch_event(event, {"dataTransfer": transfer})
    finally:
        transfer.dispose()


def writes_after_setup(page):
    writes = []
    page.on("request", lambda request: writes.append(urllib.parse.urlsplit(request.url).path)
            if request.method not in ("GET", "HEAD") and "/api/" in request.url else None)
    return writes


def test_drop_opens_the_case_sheet(visitor, real_assistant):
    page, detail, ask = open_ask(visitor, real_assistant)
    question = ask.get_by_role("textbox", name="Your question", exact=True)
    question.fill("why so many?")
    calls_before = len(real_assistant.calls())
    writes = writes_after_setup(page)
    drag_files(page, ask.locator("form"), [{"name": "shelf.jpg", "type": "image/jpeg"}])
    dialog = page.get_by_role("dialog", name=TITLE, exact=True)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("list", name="Report steps").locator('[aria-current="step"]')).to_have_text("Add evidence")
    expect(dialog.get_by_role("button", name="Remove file shelf.jpg", exact=True)).to_be_visible()
    expect(dialog.get_by_label("Store", exact=True)).to_have_value(detail["source"])
    expect(dialog.get_by_label("Product", exact=True)).to_have_value(detail["sku"])
    expect(dialog.get_by_label("Lot", exact=True)).to_have_value(detail["lot_id"])
    expect(dialog.get_by_text("Typed by you", exact=True)).to_have_count(3)
    expect(dialog.get_by_label("Message (optional)")).to_have_value("")
    expect(question).to_have_value("why so many?")
    assert writes == []
    assert len(real_assistant.calls()) == calls_before


@pytest.mark.parametrize("files,message", [
    ([{"name": "notes.txt", "type": "text/plain"}], "Only PNG, JPEG or PDF files can be attached."),
    ([{"name": "large.jpg", "type": "image/jpeg", "size": 6 * 1024 * 1024}], "This file is larger than 5 MB. Choose a smaller file."),
    ([{"name": "a.png", "type": "image/png"}, {"name": "b.pdf", "type": "application/pdf"}], "Drop one file at a time."),
], ids=["type", "size", "count"])
def test_refused_drop_leaves_sheet_closed(visitor, real_assistant, files, message):
    page, _, ask = open_ask(visitor, real_assistant)
    writes = writes_after_setup(page)
    calls_before = len(real_assistant.calls())
    drag_files(page, ask.locator("form"), files)
    expect(ask.get_by_role("alert")).to_contain_text(message)
    expect(page.get_by_role("dialog", name=TITLE, exact=True)).to_have_count(0)
    assert writes == []
    assert len(real_assistant.calls()) == calls_before


@pytest.mark.parametrize("bound", [32, None, 0, -1], ids=["configured", "missing", "zero", "negative"])
def test_drop_uses_session_upload_bound_and_fails_closed(visitor, real_assistant, bound):
    page = visitor.page

    def session_bound(route):
        response = route.fetch()
        data = response.json()
        if bound is None:
            data.pop("upload_max_bytes", None)
        else:
            data["upload_max_bytes"] = bound
        route.fulfill(response=response, json=data)

    page.route("**/api/session", session_bound)
    page.reload()  # case navigation changes only the hash; refresh the session before testing its policy
    page, _, ask = open_ask(visitor, real_assistant)
    writes = writes_after_setup(page)
    drag_files(page, ask.locator("form"), [{"name": "shelf.png", "type": "image/png", "size": 33}])
    expect(ask.get_by_role("alert")).to_contain_text("larger than 5 MB" if bound == 32 else "File uploads are unavailable")
    expect(page.get_by_role("dialog", name=TITLE, exact=True)).to_have_count(0)
    expect(ask.get_by_role("button", name="Report with a file", exact=True)).to_be_enabled()
    assert writes == []


def test_keyboard_entry_and_question_copy_are_explicit(visitor, real_assistant):
    page, _, ask = open_ask(visitor, real_assistant)
    question = "Why so many?\n" + "Keep these observations unchanged. " * 20
    field = ask.get_by_role("textbox", name="Your question", exact=True)
    field.fill(question)
    entry = ask.get_by_role("button", name="Report with a file", exact=True)
    choosers = []
    page.on("filechooser", lambda chooser: choosers.append(chooser))
    writes = writes_after_setup(page)
    entry.focus()
    entry.press("Enter")
    dialog = page.get_by_role("dialog", name=TITLE, exact=True)
    expect(dialog.get_by_role("button", name="Add file", exact=True)).to_be_focused()
    assert choosers == []
    message = dialog.get_by_label("Message (optional)")
    expect(message).to_have_value("")
    dialog.get_by_role("button", name="Copy my question into the message", exact=True).click()
    expect(message).to_have_value(question)
    expect(field).to_have_value(question)
    message.fill("A separate observation")
    expect(field).to_have_value(question)
    with page.expect_file_chooser() as choosing:
        dialog.get_by_role("button", name="Add file", exact=True).click()
    choosing.value.set_files({"name": "report.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n%%EOF"})
    expect(dialog.get_by_role("button", name="Remove file report.pdf", exact=True)).to_be_visible()
    assert writes == []


def test_drag_outline_escape_text_drop_and_narrow_target(visitor, real_assistant):
    page, _, ask = open_ask(visitor, real_assistant)
    page.set_viewport_size({"width": 390, "height": 844})
    field = ask.get_by_role("textbox", name="Your question", exact=True)
    field.fill("Keep this unsent question")
    form = ask.locator("form")
    file = [{"name": "shelf.png", "type": "image/png"}]
    drag_files(page, form, file, "dragenter")
    overlay = ask.get_by_text("Drop to report an observation", exact=True)
    expect(overlay).to_be_visible()
    outline_box, form_box = overlay.bounding_box(), form.bounding_box()
    assert outline_box and form_box
    for axis in ("x", "y", "width", "height"):
        assert abs(outline_box[axis] - form_box[axis]) <= 2
    field.press("Escape")
    expect(overlay).to_have_count(0)
    drag_files(page, form, file, "dragenter")
    drag_files(page, form, file, "dragleave")
    expect(overlay).to_have_count(0)
    expect(field).to_have_value("Keep this unsent question")
    intercepted = field.evaluate("""element => {
      const dt = new DataTransfer(); dt.setData('text/plain', 'ordinary text');
      const event = new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: dt});
      element.dispatchEvent(event); return event.defaultPrevented;
    }""")
    assert not intercepted
    expect(page.get_by_role("dialog", name=TITLE, exact=True)).to_have_count(0)
    button = ask.get_by_role("button", name="Report with a file", exact=True)
    assert button.bounding_box()["height"] >= 44
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_drop_does_not_interrupt_a_running_ask(visitor, real_assistant):
    page, _, ask = open_ask(visitor, real_assistant)
    question = ask.get_by_role("textbox", name="Your question", exact=True)
    question.fill("What stock needs checking?")
    real_assistant.configure(block=True)
    try:
        ask.get_by_role("button", name="Ask question", exact=True).click()
        expect(ask.get_by_role("list", name="Question tool progress")).to_contain_text("Stock decision details")
        expect(ask.get_by_role("button", name="Ask question", exact=True)).to_have_attribute("aria-busy", "true")
        writes = writes_after_setup(page)
        drag_files(page, ask.locator("form"), [{"name": "shelf.jpg", "type": "image/jpeg"}])
        dialog = page.get_by_role("dialog", name=TITLE, exact=True)
        expect(dialog).to_be_visible()
        expect(question).to_have_value("What stock needs checking?")
        assert writes == []
        real_assistant.release()
        expect(ask.get_by_test_id("answer-group")).to_have_count(1)
        expect(ask).to_contain_text("Answer ready")
    finally:
        real_assistant.release()


def test_hq_drop_keeps_transport_permissions(visitor, real_assistant):
    page, detail, _ = open_ask(visitor, real_assistant)
    switch_seat(page, None)
    page.goto(f"/#/cases/{detail['case_id']}?step=compare")
    ask = page.get_by_role("region", name="Ask about this stock decision", exact=True)
    ask.get_by_role("button", name="Report with a file", exact=True).click()
    dialog = page.get_by_role("dialog", name=TITLE, exact=True)
    dialog.get_by_role("button", name="Next · enter details", exact=True).click()
    expect(dialog.get_by_role("radio", name="Transport", exact=True)).to_be_checked()
    expect(dialog.get_by_role("radio")).to_have_count(1)
