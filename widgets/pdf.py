import logging

from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView

log = logging.getLogger(__name__)


class PdfView(QPdfView):
    def __init__(self, parent, layout):
        super().__init__(parent=parent)

        self.setPageMode(QPdfView.PageMode.SinglePage)
        self.setZoomMode(QPdfView.ZoomMode.FitInView)
        self.pdf_document = QPdfDocument(self)
        self.setDocument(self.pdf_document)

        layout.addWidget(self)

    def load(self, filepath: str):
        self.pdf_document.load(str(filepath))
