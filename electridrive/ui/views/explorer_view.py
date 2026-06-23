from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QDesktopServices, QStandardItem, QStandardItemModel
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from electridrive.ui import icons
from electridrive.ui.theme import Palette
from electridrive.ui.widgets import EmptyState, IconButton, format_relative_time, format_size

ROLE_FILE = Qt.UserRole + 1
ROLE_SORT = Qt.UserRole + 2

SPECIAL_TITLES = {
    "recent": ("Recent", "clock", "Nothing recent yet"),
    "starred": ("Starred", "star", "No starred files"),
    "trash": ("Trash", "trash", "Trash is empty"),
    "search": ("Search", "search", "No matches"),
}


class DropTreeView(QTreeView):
    files_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            super().dropEvent(event)


class ExplorerView(QWidget):
    """Browse Drive, upload (button + drag&drop) and download. `mode` selects the
    source: 'my_drive' is navigable; 'recent'/'starred'/'trash' are flat lists."""

    status = Signal(str)
    open_transfers = Signal()

    def __init__(self, session, palette: Palette, mode: str = "my_drive"):
        super().__init__()
        self._session = session
        self._p = palette
        self.mode = mode
        self.query = ""
        self._stack: list[tuple[str, str]] = [("root", "My Drive")]
        self._load_token = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        # Toolbar
        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.breadcrumb = QHBoxLayout()
        self.breadcrumb.setSpacing(4)
        crumb_wrap = QWidget()
        crumb_wrap.setLayout(self.breadcrumb)
        bar.addWidget(crumb_wrap, 1)

        self.upload_btn = QPushButton("  Upload")
        self.upload_btn.setProperty("accent", True)
        self.upload_btn.setIcon(icons.icon("upload", palette.accent_text, 18))
        self.upload_btn.setCursor(Qt.PointingHandCursor)
        self.upload_btn.clicked.connect(self._upload_menu)

        self.add_btn = QPushButton("  Add from Drive")
        self.add_btn.setProperty("ghost", True)
        self.add_btn.setIcon(icons.icon("drive", palette.text, 18))
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setToolTip("Pick one or more existing files to grant access. "
                                "(Free plan: selecting a folder only lets ElectriDrive upload "
                                "into it — full existing-folder access needs Pro.)")
        self.add_btn.clicked.connect(self._add_from_drive)

        self.newfolder_btn = IconButton("plus", palette, "New folder")
        self.newfolder_btn.clicked.connect(self._new_folder)
        self.download_btn = IconButton("download", palette, "Download selected")
        self.download_btn.clicked.connect(self._download_selected)
        self.refresh_btn = IconButton("refresh", palette, "Refresh")
        self.refresh_btn.clicked.connect(self.load)

        for w in (self.download_btn, self.newfolder_btn, self.refresh_btn,
                  self.add_btn, self.upload_btn):
            bar.addWidget(w)
        root.addLayout(bar)

        # Stack: table or empty state
        self.stack = QStackedWidget()
        self.tree = DropTreeView()
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setIconSize(QSize(20, 20))
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.doubleClicked.connect(self._on_double_click)
        self.tree.files_dropped.connect(self._upload_paths)
        self.model = QStandardItemModel(0, 3, self)
        self.model.setHorizontalHeaderLabels(["Name", "Size", "Modified"])
        self.tree.setModel(self.model)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)

        if mode == "my_drive":
            empty_icon, empty_title, empty_sub = (
                "folder", "Nothing here yet",
                "Upload files, or “Add from Drive” to grant access to existing files & folders.")
        else:
            empty_title, empty_icon, empty_sub2 = SPECIAL_TITLES[mode]
            empty_sub = empty_sub2
        self.empty = EmptyState(empty_icon, empty_title, empty_sub, palette)
        self.stack.addWidget(self.tree)
        self.stack.addWidget(self.empty)
        root.addWidget(self.stack, 1)

        if mode != "my_drive":
            self.upload_btn.hide()
            self.add_btn.hide()
            self.newfolder_btn.hide()

    # ------------------------------------------------------------- navigation
    @property
    def current_id(self) -> str:
        return self._stack[-1][0]

    def _rebuild_breadcrumb(self):
        while self.breadcrumb.count():
            item = self.breadcrumb.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if self.mode != "my_drive":
            title, icon_name, _ = SPECIAL_TITLES[self.mode]
            if self.mode == "search" and self.query:
                title = f'Search: "{self.query}"'
            lbl = QLabel(title)
            lbl.setObjectName("CrumbActive")
            self.breadcrumb.addWidget(lbl)
            self.breadcrumb.addStretch(1)
            return
        for i, (fid, name) in enumerate(self._stack):
            is_last = i == len(self._stack) - 1
            btn = QPushButton(name)
            btn.setProperty("ghost", True)
            btn.setFlat(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setObjectName("CrumbActive" if is_last else "Crumb")
            btn.setStyleSheet("border: none; background: transparent; padding: 4px 6px;"
                              + ("font-weight: 700;" if is_last else f"color: {self._p.muted};"))
            btn.clicked.connect(lambda _=False, idx=i: self._go_to_crumb(idx))
            self.breadcrumb.addWidget(btn)
            if not is_last:
                sep = QLabel()
                sep.setPixmap(icons.pixmap("chevron", self._p.muted, 12))
                self.breadcrumb.addWidget(sep)
        self.breadcrumb.addStretch(1)

    def _go_to_crumb(self, idx: int):
        self._stack = self._stack[: idx + 1]
        self.load()

    def _on_double_click(self, index):
        remote = self.model.itemFromIndex(self.model.index(index.row(), 0)).data(ROLE_FILE)
        if remote and remote.is_folder and self.mode == "my_drive":
            self._stack.append((remote.id, remote.name))
            self.load()

    # ----------------------------------------------------------------- loading
    def load(self):
        self._load_token += 1
        token = self._load_token
        self._rebuild_breadcrumb()
        self.status.emit("Loading…")

        if self.mode == "my_drive":
            parent = self.current_id
            self._session.submit(
                lambda: self._session.client.list_folder(parent).files,
                lambda files: self._populate(files, token),
                self._on_error,
            )
        else:
            fetchers = {
                "recent": lambda: self._session.client.list_files(limit=100),
                "starred": lambda: self._session.client.list_starred(),
                "trash": lambda: self._session.client.list_trash(),
                "search": lambda: self._session.client.search(self.query) if self.query else [],
            }
            self._session.submit(fetchers[self.mode],
                                 lambda files: self._populate(files, token),
                                 self._on_error)

    def _populate(self, files, token):
        if token != self._load_token:
            return  # a newer navigation superseded this result
        self.model.removeRows(0, self.model.rowCount())
        if self.mode == "my_drive":
            self._session.db.cache_remote_children(self.current_id, files)
        for f in files:
            icon_name = "folder" if f.is_folder else "file"
            name_item = QStandardItem(icons.icon(icon_name, self._p.accent if f.is_folder
                                                 else self._p.muted, 20), f.name)
            name_item.setData(f, ROLE_FILE)
            size_item = QStandardItem("—" if f.is_folder else format_size(f.size))
            size_item.setData(f.size or 0, ROLE_SORT)
            mod_item = QStandardItem(format_relative_time(f.modified_time))
            for it in (name_item, size_item, mod_item):
                it.setEditable(False)
            self.model.appendRow([name_item, size_item, mod_item])
        empty = self.model.rowCount() == 0
        self.stack.setCurrentWidget(self.empty if empty else self.tree)
        title = "My Drive" if self.mode == "my_drive" else SPECIAL_TITLES[self.mode][0]
        self.status.emit(f"{self.model.rowCount()} item(s) · {title}")

    def _on_error(self, message: str):
        self.status.emit(f"Error: {message}")
        QMessageBox.warning(self, "Could not load", message)

    # ----------------------------------------------------------------- actions
    def _selected_files(self):
        rows = {idx.row() for idx in self.tree.selectionModel().selectedIndexes()}
        out = []
        for r in sorted(rows):
            remote = self.model.item(r, 0).data(ROLE_FILE)
            if remote:
                out.append(remote)
        return out

    def _upload_menu(self):
        menu = QMenu(self)
        menu.addAction(icons.icon("file", self._p.text, 16), "Upload files…", self._upload_files)
        menu.addAction(icons.icon("folder", self._p.text, 16), "Upload folder…", self._upload_folder)
        menu.exec(self.upload_btn.mapToGlobal(self.upload_btn.rect().bottomLeft()))

    def _upload_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose files to upload", str(Path.home()))
        self._upload_paths(paths)

    def _upload_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Choose folder to upload", str(Path.home()))
        if path:
            self._upload_paths([path])

    def _upload_paths(self, paths):
        if not paths or not self._session.transfers:
            return
        if self.mode != "my_drive":
            QMessageBox.information(self, "Upload", "Switch to My Drive to upload here.")
            return
        for p in paths:
            self._session.transfers.enqueue_upload(Path(p), self.current_id)
        self.status.emit(f"Queued upload of {len(paths)} item(s)")
        self.open_transfers.emit()

    def _add_from_drive(self):
        if not self._session.client:
            return
        self.add_btn.setEnabled(False)
        self.status.emit("Opening Drive Picker in your browser…")

        def done(result):
            self.add_btn.setEnabled(True)
            self.status.emit(f"Added {len(result.file_ids)} item(s) from Drive")
            self.load()

        def err(msg):
            self.add_btn.setEnabled(True)
            self.status.emit(f"Picker: {msg}")
            QMessageBox.warning(self, "Add from Drive failed", msg)

        self._session.pick_from_drive(on_done=done, on_error=err)

    def _download_selected(self):
        files = self._selected_files()
        if not files:
            QMessageBox.information(self, "Download", "Select one or more items first.")
            return
        dest = QFileDialog.getExistingDirectory(self, "Download to…", str(Path.home() / "Downloads"))
        if not dest:
            return
        for f in files:
            self._session.transfers.enqueue_download(f, Path(dest))
        self.status.emit(f"Queued download of {len(files)} item(s)")
        self.open_transfers.emit()

    def _new_folder(self):
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if ok and name.strip():
            self._session.submit(
                lambda: self._session.client.create_folder(name.strip(), self.current_id),
                lambda _id: self.load(),
                self._on_error,
            )

    def _context_menu(self, pos):
        files = self._selected_files()
        if not files:
            return
        menu = QMenu(self)
        menu.addAction(icons.icon("download", self._p.text, 16), "Download", self._download_selected)
        if len(files) == 1 and files[0].web_view_link:
            menu.addAction(icons.icon("link", self._p.text, 16), "Open in browser",
                           lambda: QDesktopServices.openUrl(QUrl(files[0].web_view_link)))
        menu.addSeparator()
        menu.addAction(icons.icon("trash", self._p.danger, 16), "Move to Trash", self._trash_selected)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _trash_selected(self):
        files = self._selected_files()
        if not files:
            return
        names = ", ".join(f.name for f in files[:3]) + ("…" if len(files) > 3 else "")
        if QMessageBox.question(self, "Move to Trash",
                                f"Move {len(files)} item(s) to Drive Trash?\n{names}\n\n"
                                "They stay recoverable in Drive Trash.") != QMessageBox.Yes:
            return
        ids = [f.id for f in files]

        def do():
            for fid in ids:
                self._session.client.trash(fid)
        self._session.submit(do, lambda _: self.load(), self._on_error)
