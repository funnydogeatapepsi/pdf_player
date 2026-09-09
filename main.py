# This Python file uses the following encoding: utf-8
import sys
from pathlib import Path
import pickle
import os
import logging
import platform
import json

# Application data directory (recent projects, numba cache, ...):
if platform.system() == "Windows":
    DATA_DIR = Path("~/AppData/Roaming/.pdf_player").expanduser()
else:
    DATA_DIR = Path(os.getenv("XDG_DATA_HOME", "~/.local/share")).expanduser() / "pdf_player"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# numba must know its cache directory before audio_effects.time_stretch is imported:
NUMBA_CACHE_DIR = DATA_DIR / "numba"
NUMBA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
os.environ["NUMBA_CACHE_DIR"] = str(NUMBA_CACHE_DIR)

from PySide6.QtWidgets import (QApplication, QMainWindow, QSlider, QFileDialog, QMessageBox, QLabel, QMenu,
                               QDialog, QDialogButtonBox, QFormLayout, QSpinBox)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtCore import QUrl, Qt, QPointF, Signal
from PySide6.QtGui import QIcon, QAction, QCloseEvent

from windows.mainwindow import Ui_MainWindow
from widgets.audio_player import AudioPlayer, Song
from widgets.graphics import GraphicsView, AudioMarker
from widgets.pdf import PdfView

DEBUG = True

LOG_LEVEL = logging.INFO
log = logging.getLogger()
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(logging.Formatter('%(levelname)s - %(name)s:%(funcName)s - %(message)s'))
log.setLevel(LOG_LEVEL)
log.addHandler(stream_handler)

RECENT_PROJECTS_JSON = DATA_DIR / 'recents.json'
MAX_RECENT_PROJECTS = 15


class Project:
    """
    This class supports the save/load functionality -- it is saved using pickle and can be opened later for importing
    """
    def __init__(self, song_path, page_marker_times, practice_marker_times, pdf_path, page_offset=0):
        self.song_path = song_path
        self.page_marker_times = page_marker_times
        self.practice_marker_times = practice_marker_times
        self.pdf_path = pdf_path
        # Number of leading PDF pages (cover, table of contents, ...) before the page that marker 1 turns *from*.
        self.page_offset = page_offset


class GenericSlider(QSlider):
    TICK_RATE = 1
    MAX_VALUE = 100

    _lmb_click = False
    end_value_signal = Signal(float)

    def __init__(self, orientation=Qt.Orientation.Horizontal, parent=None, max_value=None, tick_rate=None):
        super().__init__(orientation, parent)

        if max_value is not None:
            self.MAX_VALUE = max_value
        self.setMaximum(self.MAX_VALUE)
        self.setMinimum(0)
        self.setValue(self.maximum())

        if tick_rate is not None:
            self.TICK_RATE = tick_rate
        self.setTickInterval(self.TICK_RATE)

    def _map_value(self, ev, tick_enabled=True):
        if self.orientation() == Qt.Orientation.Horizontal:
            value = self.MAX_VALUE * ev.position().x() / self.width()
        else:
            value = self.MAX_VALUE * (1 - ev.position().y() / self.height())
        if tick_enabled:
            value = round(value / self.TICK_RATE) * self.TICK_RATE
        return value

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self._lmb_click = True
            ev.accept()
            value = self._map_value(ev)
            self.setValue(value)

    def mouseMoveEvent(self, ev):
        if self._lmb_click:
            value = self._map_value(ev)
            self.setValue(value)

    def mouseReleaseEvent(self, ev):
        if not self._lmb_click:
            return
        self._lmb_click = False
        value = self._map_value(ev)
        self.setValue(value)
        self.end_value_signal.emit(max(0, min(value, self.MAX_VALUE)))


class ProjectOptionsDialog(QDialog):
    """
    Project settings. Currently: PDF page offset.
    """
    def __init__(self, parent, page_offset: int, n_pages: int):
        super().__init__(parent)
        self.setWindowTitle("Project Options")

        self.page_offset_spinbox = QSpinBox(self)
        self.page_offset_spinbox.setRange(0, max(0, n_pages - 1))
        self.page_offset_spinbox.setValue(page_offset)
        self.page_offset_spinbox.setToolTip("Number of leading PDF pages (cover, contents, ...) to skip.\n"
                                            "The first page shown is this page; page marker 1 turns to the next one.\n"
                                            "Tip: Ctrl + scroll wheel over the PDF adjusts this as well.")

        layout = QFormLayout(self)
        layout.addRow(f"PDF page offset (0 - {max(0, n_pages - 1)}):", self.page_offset_spinbox)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    @property
    def page_offset(self) -> int:
        return self.page_offset_spinbox.value()


class MainWindow(QMainWindow):
    _save_path: Path | None = None
    _open_path: Path | None = None
    _pdf_path = None
    _dirty = False

    _hide_toolbar_items = False
    _recent_projects: list[Path] = []

    def __init__(self, parent=None):
        super(MainWindow, self).__init__(parent)

        self.m_ui = Ui_MainWindow()
        self.m_ui.setupUi(self)
        self.WINDOW_TITLE = self.windowTitle()

        # Create Audio Player:
        self.audio_player = AudioPlayer(parent=self)

        # Create Graphics:
        self.graphics_view = GraphicsView(parent=self.m_ui.audio_tab, layout=self.m_ui.horizontalLayout,
                                          audio_player=self.audio_player)
        self.graphics_scene = self.graphics_view.graphics_scene

        # Create PDF:
        self.pdf_viewer = PdfView(self, layout=self.m_ui.gridLayout_2)
        self.graphics_scene.page_changed_signal.connect(lambda x:
                                                        self.pdf_viewer.pageNavigator().jump(x, QPointF(0, 0)))
        self.pdf_viewer.pdf_document.pageCountChanged.connect(self.graphics_scene.set_n_pages)
        self.pdf_viewer.page_offset_step_signal.connect(
            lambda step: self._set_page_offset(self.graphics_scene.page_offset + step))

        # Set up volume bar:
        self.volume_bar = GenericSlider(parent=self)
        # set up speed bar:
        self.playbackspeed_bar = GenericSlider(parent=self, tick_rate=5, max_value=200)
        self.playbackspeed_bar.setValue(100)

        self.file_dialog = QFileDialog(self)
        self.msg_box = QMessageBox(self)

        self._connect_tool_bar()
        self._connect_menu()
        self._connect_audio_player()
        self._connect_graphics()

        self.hide_toolbar_items(True)
        self._load_recent_projects()
        self._rebuild_recent_projects_menu()

        self.show()
        self.raise_()

        if len(self._recent_projects) > 0:
            self._load_markers(self._recent_projects[0])

    # ------------------------------------------------------------------ dialogs
    def _warning(self, title, text, accept=QMessageBox.StandardButton.Ok, cancel=QMessageBox.StandardButton.Cancel):
        # QMessageBox.warning(parent, title, text, buttons, defaultButton): show both buttons, default to cancel.
        reply = self.msg_box.warning(self, title, text, accept | cancel, cancel)
        log.info(f"{reply=}")
        accepted = reply == accept
        return accepted

    def _critical(self, title, text, accept=QMessageBox.StandardButton.Ok, cancel=QMessageBox.StandardButton.Cancel):
        reply = self.msg_box.critical(self, title, text, accept | cancel, cancel)
        log.info(f"{reply=}")
        accepted = reply == accept
        return accepted

    # ------------------------------------------------------------------ unsaved changes
    def set_dirty(self, dirty: bool = True):
        self._dirty = dirty
        self._update_window_title()

    def _update_window_title(self):
        title = self.WINDOW_TITLE
        if self._save_path is not None:
            title = f"{title} - {self._save_path}"
        if self._dirty:
            title = f"{title} *"
        self.setWindowTitle(title)

    def _confirm_discard_changes(self, title="Unsaved changes") -> bool:
        """
        If the project has unsaved changes, ask the user to save / discard / cancel.

        :return: True if it is OK to continue (changes saved or discarded), False if the user cancelled.
        """
        if not self._dirty:
            return True

        buttons = (QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
                   QMessageBox.StandardButton.Cancel)
        reply = self.msg_box.warning(self, title, "The current project has unsaved changes. Save them?",
                                     buttons, QMessageBox.StandardButton.Save)
        log.info(f"{reply=}")
        if reply == QMessageBox.StandardButton.Save:
            return self._save_markers(save_as=False)
        return reply == QMessageBox.StandardButton.Discard

    def closeEvent(self, event: QCloseEvent):
        if self._confirm_discard_changes(title="Quit without saving?"):
            event.accept()
        else:
            event.ignore()

    # ------------------------------------------------------------------ connections
    def _connect_graphics(self):
        self.m_ui.actionAdd_Practice_Marker.triggered.connect(lambda: self.graphics_scene._add_marker_at_scrubber(None, add_practice=True))
        self.m_ui.actionAdd_Page_Marker.triggered.connect(lambda: self.graphics_scene._add_marker_at_scrubber(None, add_practice=False))
        self.graphics_scene.markers_changed_signal.connect(self.set_dirty)

    def _connect_tool_bar(self):
        volume_text_label = QLabel('Volume:\t', self)
        volume_label = QLabel('100', self)
        self.volume_bar.valueChanged.connect(lambda x: volume_label.setText(str(x)))
        self.m_ui.toolBar.addSeparator()
        self.m_ui.toolBar.addWidget(volume_text_label)
        self.m_ui.toolBar.addWidget(volume_label)
        self.m_ui.toolBar.addSeparator()
        self.m_ui.toolBar.addWidget(self.volume_bar)
        self.m_ui.toolBar.orientationChanged.connect(self.volume_bar.setOrientation)

        playbackspeed_text_label = QLabel('Playback Speed:\t', self)
        playbackspeed_label = QLabel('100%', self)
        self.playbackspeed_bar.valueChanged.connect(lambda x: playbackspeed_label.setText(f"{x:3d}%"))
        self.m_ui.toolBar.addSeparator()
        self.m_ui.toolBar.addWidget(playbackspeed_text_label)
        self.m_ui.toolBar.addWidget(playbackspeed_label)
        self.m_ui.toolBar.addSeparator()
        self.m_ui.toolBar.addWidget(self.playbackspeed_bar)
        self.m_ui.toolBar.orientationChanged.connect(self.playbackspeed_bar.setOrientation)

    def _connect_audio_player(self):
        # Connect audio controls
        self.volume_bar.valueChanged.connect(lambda x: self.audio_player.setVolume(x/self.volume_bar.MAX_VALUE))
        self.playbackspeed_bar.end_value_signal.connect(lambda x: self.audio_player.setPlaybackRate(x/100))

        def toggle_audio():
            if self.audio_player.isPlaying():
                self.audio_player.pause()
            else:
                self.audio_player.play()

        def set_icon():
            if self.audio_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.m_ui.actionPlay.setIcon(QIcon(QIcon.fromTheme(u"media-playback-pause")))
            else:
                self.m_ui.actionPlay.setIcon(QIcon(QIcon.fromTheme(u"media-playback-start")))

        def _clear_next_practice_marker():
            self.graphics_scene.clear_next_marker(marker_type=AudioMarker.TYPE.PRACTICE)

        def _clear_previous_practice_marker():
            self.graphics_scene.clear_previous_marker(marker_type=AudioMarker.TYPE.PRACTICE)

        def _clear_next_page_marker():
            self.graphics_scene.clear_next_marker(marker_type=AudioMarker.TYPE.PAGE)

        def _clear_previous_page_marker():
            self.graphics_scene.clear_previous_marker(marker_type=AudioMarker.TYPE.PAGE)

        self.m_ui.actionPlay.triggered.connect(toggle_audio)
        self.audio_player.playbackStateChanged.connect(set_icon)
        self.m_ui.actionNext_Practice_Marker.triggered.connect(self.graphics_scene.next_practice_marker)
        self.m_ui.actionPrevious_Practice_Marker.triggered.connect(self.graphics_scene.previous_practice_marker)
        self.m_ui.actionNext_Page.triggered.connect(self.graphics_scene.next_page)
        self.m_ui.actionPrevious_Page.triggered.connect(self.graphics_scene.previous_page)
        self.m_ui.actionDelete_All_Markers.triggered.connect(self.graphics_scene.clear_markers)
        self.m_ui.actionDelete_Next_Practice_Marker.triggered.connect(_clear_next_practice_marker)
        self.m_ui.actionDelete_Previous_Practice_Marker.triggered.connect(_clear_previous_practice_marker)
        self.m_ui.actionDelete_Next_Page_Marker.triggered.connect(_clear_next_page_marker)
        self.m_ui.actionDelete_Previous_Page_Marker.triggered.connect(_clear_previous_page_marker)

    def _connect_menu(self):
        def _toggle_fullscreen(toggled):
            if toggled:
                self.showFullScreen()
            else:
                self.showNormal()
        self.m_ui.actionFullscreen.toggled.connect(_toggle_fullscreen)

        def _new_project():
            if not self._confirm_discard_changes(title="Create new project?"):
                return

            self._save_path = None
            self._open_path = None
            self._pdf_path = None
            self.graphics_scene.clear_markers()
            self.graphics_scene.page_offset = 0
            self.set_dirty(False)

            # Each step can be cancelled; stop at the first cancellation.
            if not self._import_audio():
                return
            if not self._import_pdf():
                return
            self._save_markers(save_as=True)

        self.m_ui.actionImportAudio.triggered.connect(lambda checked: self._import_audio())
        self.m_ui.actionImportPDF.triggered.connect(lambda checked: self._import_pdf())
        self.m_ui.actionSave.triggered.connect(lambda checked: self._save_markers(save_as=False))
        self.m_ui.actionSave_As.triggered.connect(lambda checked: self._save_markers(save_as=True))
        self.m_ui.actionOpen.triggered.connect(lambda checked: self._load_markers(None))
        self.m_ui.actionNew_Project.triggered.connect(_new_project)
        self.m_ui.actionProject_Options.triggered.connect(self._options)

    # ------------------------------------------------------------------ project options
    def _options(self):
        dialog = ProjectOptionsDialog(self, page_offset=self.graphics_scene.page_offset,
                                      n_pages=self.graphics_scene.n_pages)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._set_page_offset(dialog.page_offset)

    def _set_page_offset(self, page_offset: int):
        old_offset = self.graphics_scene.page_offset
        self.graphics_scene.page_offset = page_offset
        if self.graphics_scene.page_offset != old_offset:
            self.set_dirty(True)

    # ------------------------------------------------------------------ import / save / load
    @staticmethod
    def _url_to_path(file_url: QUrl) -> Path | None:
        """Convert a QFileDialog url into a local Path, or None if the dialog was cancelled."""
        if file_url is None or file_url.isEmpty():
            return None
        local_path = file_url.toLocalFile()
        if local_path == "":
            return None
        return Path(local_path)

    def _import_audio(self, clear_markers=True) -> bool:
        file_url = self.file_dialog.getOpenFileUrl(self, caption="Import Audio - Select a audio file to continue",
                                                   filter="Audio (*.m4a *.mp3 *.wav *.flac *.FLAC)")[0]
        filepath = self._url_to_path(file_url)
        if filepath is None or not filepath.is_file():
            log.debug("No import path specified.")
            return False

        if clear_markers:
            self.graphics_scene.clear_markers()
        self.playbackspeed_bar.setValue(100)    # a freshly imported song plays at its native rate
        self.audio_player.current_song = Song(file_url=file_url)
        self.set_dirty(True)
        return True

    def _import_pdf(self) -> bool:
        file_url = self.file_dialog.getOpenFileUrl(self, caption="Import PDF -  select a PDF to continue", filter="PDF (*.pdf)")[0]
        filepath = self._url_to_path(file_url)
        if filepath is None or not filepath.is_file():
            log.debug("No import path specified.")
            return False
        self.pdf_viewer.load(str(filepath))
        self._pdf_path = str(filepath)
        self.set_dirty(True)
        return True

    def _save_markers(self, save_as=False) -> bool:
        if self.audio_player.current_song.is_empty:
            log.info("No song currently loaded... Audio is required in order to save.")
            # todo; maybe in the future we could create a version that works on its own time base.
            #       (normalize to [0, 1]) users would set the duration and then they can add markers to
            #       automate page turns.
            self._warning("Cannot save", "Import audio before saving the project.",
                          accept=QMessageBox.StandardButton.Ok, cancel=QMessageBox.StandardButton.NoButton)
            return False

        save_path = self._save_path
        if save_as or (save_path is None):
            filesave = self.file_dialog.getSaveFileUrl(self, caption="Save Project", filter="pkl (*.pkl)")[0]
            save_path = self._url_to_path(filesave)

        if save_path is None:
            log.info("No output path specified.")
            return False

        pdf_path = self._pdf_path
        song_path = self.audio_player.current_song.file_path
        page_marker_times = [self.graphics_scene.scrubber_to_time(*marker.scrubber_coords)/self.graphics_scene.song_duration
                             for marker in self.graphics_scene.page_markers]
        practice_marker_times = [self.graphics_scene.scrubber_to_time(*marker.scrubber_coords)/self.graphics_scene.song_duration
                              for marker in self.graphics_scene.practice_markers]
        project = Project(song_path=song_path,
                          page_marker_times=page_marker_times,
                          practice_marker_times=practice_marker_times,
                          pdf_path=pdf_path,
                          page_offset=self.graphics_scene.page_offset)
        with open(save_path, 'wb') as f:
            pickle.dump(project, f)

        self._save_path = save_path
        if self._open_path != save_path:
            self._open_path = save_path
            self._add_recent_project(save_path)
        self.set_dirty(False)
        log.info(f"Saved Project as {save_path}.")
        return True

    def _load_markers(self, load_path: Path | None = None):
        if load_path is None:
            fileload = self.file_dialog.getOpenFileUrl(self, caption="Open Project", filter="pkl (*.pkl)")[0]
            load_path = self._url_to_path(fileload)

        if load_path is None:
            log.info("No input path specified.")
            return
        load_path = Path(load_path)

        if not load_path.is_file():
            log.info(f"Project file not found: {load_path}")
            self._critical("File not found.", f"The project file {load_path} was not found.",
                           accept=QMessageBox.StandardButton.Ok, cancel=QMessageBox.StandardButton.NoButton)
            self._remove_recent_project(load_path)
            return

        if not self._confirm_discard_changes(title="Open different project?"):
            return

        log.info(f"Opening Project {load_path}.")
        try:
            with open(load_path, "rb") as f:
                project = pickle.load(f)
        except Exception as err:
            log.exception(f"Failed to open project {load_path}")
            self._critical("Failed to open project.", f"Could not read {load_path}:\n{err}",
                           accept=QMessageBox.StandardButton.Ok, cancel=QMessageBox.StandardButton.NoButton)
            return

        self._open_path = load_path
        self._save_path = load_path
        self._add_recent_project(load_path)
        self.graphics_scene.clear_markers()

        song_path = project.song_path
        page_marker_times = project.page_marker_times
        practice_marker_times = project.practice_marker_times
        pdf_path = project.pdf_path
        page_offset = getattr(project, "page_offset", 0)    # older project files predate the page offset

        imported_new_file = False
        if pdf_path is not None:
            if Path(pdf_path).exists():
                self.pdf_viewer.load(pdf_path)
                self._pdf_path = pdf_path
            else:
                title = "File not found."
                text = f"The pdf file {pdf_path} was not found. Would you like to import a different file?"
                accepted = self._critical(title, text)
                if accepted:
                    imported_new_file = self._import_pdf()
        self.graphics_scene.page_offset = page_offset

        if page_marker_times is not None:
            self.graphics_scene.set_markers(page_marker_times, practice_marker_times)

        if song_path is not None:
            if Path(song_path).exists():
                self.playbackspeed_bar.setValue(100)
                self.audio_player.current_song = Song(file_url=QUrl.fromLocalFile(song_path))
            else:
                title = "File not found."
                text = f"The audio file {song_path} was not found. Would you like to import a different file?"
                accepted = self._critical(title, text)
                if accepted:
                    imported_new_file = self._import_audio(clear_markers=False) or imported_new_file

        self.set_dirty(imported_new_file)
        if imported_new_file:
            self._save_markers(save_as=False)

    # ------------------------------------------------------------------ events
    def keyPressEvent(self, event):
        self.graphics_scene.keyPressEvent(event)
        if not event.isAccepted():
            super().keyPressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)

        if event.button() == Qt.MouseButton.RightButton and event.modifiers() == Qt.KeyboardModifier.NoModifier:
            context_menu = QMenu()
            hide_action = context_menu.addAction("Hide Toolbar Items")
            hide_action.setCheckable(True)
            hide_action.setChecked(self._hide_toolbar_items)
            hide_action.toggled.connect(self.hide_toolbar_items)
            context_menu.exec(event.globalPos())

    def hide_toolbar_items(self, toggled):
        # TODO; should add in same positions every time toggled. make an actions dict or something?
        # TODO; is there a way to hide but keep enabled???
        # hide_actions = [self.m_ui.actionAdd_Page_Marker, self.m_ui.actionAdd_Practice_Marker,
        #                 self.m_ui.actionNext_Page, self.m_ui.actionNext_Practice_Marker,
        #                 self.m_ui.actionPrevious_Page, self.m_ui.actionPrevious_Practice_Marker]
        hide_actions = []
        self._hide_toolbar_items = toggled
        if toggled:
            for action in hide_actions:
                self.m_ui.toolBar.removeAction(action)
        else:
            for action in hide_actions:
                self.m_ui.toolBar.addAction(action)

    # ------------------------------------------------------------------ recent projects
    def _load_recent_projects(self):
        self._recent_projects = []
        if not RECENT_PROJECTS_JSON.exists():
            return
        try:
            with open(RECENT_PROJECTS_JSON, "r") as file:
                data = json.load(file)
            paths = data.get('recent_projects', [])
        except (OSError, ValueError) as err:
            log.warning(f"Could not read {RECENT_PROJECTS_JSON}: {err}")
            return

        for path_str in paths:
            path = Path(path_str)
            if path not in self._recent_projects:
                self._recent_projects.append(path)

    def _save_recent_projects(self):
        try:
            with open(RECENT_PROJECTS_JSON, 'w') as json_file:
                json.dump({'recent_projects': [str(path) for path in self._recent_projects]}, json_file, indent=4)
        except OSError as err:
            log.warning(f"Could not write {RECENT_PROJECTS_JSON}: {err}")

    def _add_recent_project(self, project_path: Path):
        project_path = Path(project_path).resolve()
        if project_path in self._recent_projects:
            self._recent_projects.remove(project_path)
        self._recent_projects.insert(0, project_path)
        del self._recent_projects[MAX_RECENT_PROJECTS:]
        self._save_recent_projects()
        self._rebuild_recent_projects_menu()

    def _remove_recent_project(self, project_path: Path):
        project_path = Path(project_path).resolve()
        if project_path in self._recent_projects:
            self._recent_projects.remove(project_path)
            self._save_recent_projects()
            self._rebuild_recent_projects_menu()

    def _rebuild_recent_projects_menu(self):
        menu = self.m_ui.menuRecent_Projects
        menu.clear()
        for project_path in self._recent_projects:
            recent_action = QAction(project_path.name, self)
            recent_action.setToolTip(str(project_path))
            recent_action.triggered.connect(lambda checked=False, x=project_path: self._load_markers(x))
            menu.addAction(recent_action)
        menu.setEnabled(len(self._recent_projects) > 0)


if __name__ == "__main__":
    app = QApplication(sys.argv)

    main_window = MainWindow()
    main_window.show()

    sys.exit(app.exec())
