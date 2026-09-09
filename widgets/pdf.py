import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QWheelEvent
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView

log = logging.getLogger(__name__)


class PdfView(QPdfView):
    # Emitted with +1 / -1 when the user Ctrl+scrolls over the PDF to nudge the project's page offset.
    page_offset_step_signal = Signal(int)

    def __init__(self, parent, layout):
        super().__init__(parent=parent)

        self.setPageMode(QPdfView.PageMode.SinglePage)
        self.setZoomMode(QPdfView.ZoomMode.FitInView)
        self.pdf_document = QPdfDocument(self)
        self.setDocument(self.pdf_document)

        layout.addWidget(self)

    def load(self, filepath: str):
        self.pdf_document.load(str(filepath))

    def wheelEvent(self, event: QWheelEvent):
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() // 120       # one notch == 120 (1/8 degree units)
            if steps != 0:
                # Scrolling down moves further into the book (offset +1); up moves back.
                self.page_offset_step_signal.emit(-1 if steps > 0 else 1)
            event.accept()
            return
        super().wheelEvent(event)
