"""Dialog to review the wiki changes proposed by agents."""

import difflib

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from app.services import wiki_service


class WikiProposalsDialog(QDialog):
    def __init__(self, current_user=None, parent=None):
        super().__init__(parent)
        self.current_user = current_user or {"name": "Unknown"}
        self.setWindowTitle(self.tr("Wiki Proposals"))
        self.setMinimumSize(1000, 560)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)
        intro = QLabel(
            self.tr(
                "Changes to the wiki proposed by the AI agents. The wiki changes only when you "
                "approve; a page that changed since the proposal can't be approved."
            )
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        splitter = QSplitter()
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._show)
        splitter.addWidget(self.list)
        right = QWidget()
        lay = QVBoxLayout(right)
        lay.setContentsMargins(0, 0, 0, 0)
        self.reason = QLabel()
        self.reason.setWordWrap(True)
        lay.addWidget(self.reason)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        self.diff.setFont(font)
        lay.addWidget(self.diff, 1)
        splitter.addWidget(right)
        splitter.setSizes([300, 700])
        root.addWidget(splitter, 1)

        buttons = QHBoxLayout()
        self.show_all = QCheckBox(self.tr("Show decided ones too"))
        self.show_all.toggled.connect(self.refresh)
        buttons.addWidget(self.show_all)
        buttons.addStretch(1)
        self.approve_btn = QPushButton(self.tr("Approve"))
        self.approve_btn.setObjectName("primaryButton")
        self.approve_btn.clicked.connect(lambda: self._decide(True))
        self.reject_btn = QPushButton(self.tr("Reject"))
        self.reject_btn.clicked.connect(lambda: self._decide(False))
        close_btn = QPushButton(self.tr("Close"))
        close_btn.clicked.connect(self.accept)
        for b in (self.approve_btn, self.reject_btn, close_btn):
            buttons.addWidget(b)
        root.addLayout(buttons)

    def refresh(self):
        selected = self._current()
        self.list.clear()
        status = None if self.show_all.isChecked() else wiki_service.PENDING
        for proposal in wiki_service.list_proposals(status):
            new = "new page" if proposal["base_version"] is None else "update"
            item = QListWidgetItem(
                f"#{proposal['id']} {proposal['page']} ({new}) · {proposal['status']}\n"
                f"{proposal['proposed_by']}, {proposal['created_at'][:16].replace('T', ' ')}"
            )
            item.setData(256, proposal["id"])
            self.list.addItem(item)
            if selected and proposal["id"] == selected["id"]:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)
        if not self.list.count():
            self._show(None, None)

    def _current(self):
        item = self.list.currentItem()
        return wiki_service.get_proposal(item.data(256)) if item else None

    def _show(self, item, _previous=None):
        proposal = wiki_service.get_proposal(item.data(256)) if item else None
        pending = bool(proposal) and proposal["status"] == wiki_service.PENDING
        self.approve_btn.setEnabled(pending)
        self.reject_btn.setEnabled(pending)
        if not proposal:
            self.reason.setText(self.tr("No proposals waiting."))
            self.diff.setPlainText("")
            return
        decided = (
            self.tr("\n{0} by {1}: {2}").format(
                proposal["status"], proposal["decided_by"], proposal["comment"] or ""
            )
            if proposal["decided_by"]
            else ""
        )
        self.reason.setText(self.tr("Reason: {0}").format(proposal["reason"]) + decided)
        self.diff.setPlainText(self.diff_text(proposal))

    @staticmethod
    def diff_text(proposal) -> str:
        """Returns the proposal's diff against the current page."""
        path = wiki_service.page_path(proposal["page"])
        current = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        proposed = proposal["content"].splitlines()
        if not current:
            return "\n".join(f"+ {line}" for line in proposed)
        return (
            "\n".join(
                difflib.unified_diff(current, proposed, "current", "proposed", lineterm="", n=3)
            )
            or "(no change)"
        )

    def _decide(self, approve):
        proposal = self._current()
        if not proposal:
            return
        comment = ""
        if not approve:
            comment, ok = QInputDialog.getText(
                self, self.tr("Reject Proposal"), self.tr("Why (optional, the agent sees it):")
            )
            if not ok:
                return
        try:
            wiki_service.decide(
                proposal["id"], approve, self.current_user.get("name") or "Unknown", comment
            )
        except wiki_service.WikiError as exc:
            QMessageBox.warning(self, self.tr("Wiki Proposals"), str(exc))
        self.refresh()
