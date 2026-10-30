"""The Validation ▸ Show validation report dialog, on the committed live report."""

from transitory.validation.gui import ReportDialog
from transitory.validation.report import DOCS_DIR, REPORT_NAME


def find_block(doc, text: str):
    block = doc.begin()
    while block.isValid():
        if block.text() == text:
            return block
        block = block.next()
    raise AssertionError(f"no {text!r} block in the report")


def test_sections_after_the_figures_are_laid_out_and_scroll_into_view(qtbot):
    dialog = ReportDialog(DOCS_DIR / REPORT_NAME)
    qtbot.addWidget(dialog)
    dialog.resize(1100, 900)
    dialog.show()
    doc = dialog.browser.document()
    layout = doc.documentLayout()
    credits = find_block(doc, "Data credits")
    references = find_block(doc, "References for published values")
    # fit_images runs after the first resize; the layout of a long document is lazy.
    qtbot.waitUntil(lambda: dialog._image_width > 0 and layout.blockBoundingRect(credits).height() > 0,
                    timeout=5000)
    assert layout.blockBoundingRect(references).height() > 0
    block = doc.begin()
    while block.isValid():
        assert block.layout().lineCount() > 0, f"block {block.blockNumber()} {block.text()[:40]!r} has no lines"
        block = block.next()

    bar = dialog.browser.verticalScrollBar()
    viewport = dialog.browser.viewport()
    # The scroll range catches up with the fitted figures' heights on a later event.
    qtbot.waitUntil(lambda: bar.maximum() + viewport.height() == doc.size().height(), timeout=5000)
    bar.setValue(bar.maximum())
    rect = layout.blockBoundingRect(credits)
    top = rect.top() - bar.value()
    assert 0 <= top and top + rect.height() <= viewport.height()
