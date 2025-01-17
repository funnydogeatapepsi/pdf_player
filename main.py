# This Python file uses the following encoding: utf-8
import sys
from pathlib import Path
import pickle
import os
import logging
import platform

if platform.system() == "Windows":
    APPDATA_LOCAL = Path(os.getenv("LOCALAPPDATA")).joinpath("pdf_player")
else:
    APPDATA_LOCAL = Path(os.getenv("XDG_DATA_HOME", "~/.local/share")).expanduser().joinpath("pdf_player")

NUMBA_CACHE_DIR = APPDATA_LOCAL / "numba"
NUMBA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
os.environ["NUMBA_CACHE_DIR"] = str(NUMBA_CACHE_DIR)

from PySide6.QtWidgets import (QApplication, QMainWindow, QSlider, QFileDialog, QMessageBox, QLabel)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtCore import QUrl, Qt, QPointF, Signal, QSettings
from PySide6.QtGui import QIcon

from windows.mainwindow import Ui_MainWindow
from widgets.audio_player import AudioPlayer, Song
from widgets.graphics import GraphicsView, AudioMarkerType
from widgets.pdf import PdfView

DEBUG = True

LOG_LEVEL = logging.INFO
log = logging.getLogger()
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(logging.Formatter('%(levelname)s - %(name)s:%(funcName)s - %(message)s'))
log.setLevel(LOG_LEVEL)
log.addHandler(stream_handler)

DATA_DIR = Path(os.getenv('LOCALAPPDATA')) / ".pdf_player"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class Project:
    """
    This class supports the save/load functionality -- it is saved using pickle and can be opened later for importing
    """
    def __init__(self, song_path, page_marker_times, practice_marker_times, pdf_path):
        self.song_path = song_path
        self.page_marker_times = page_marker_times
        self.practice_marker_times = practice_marker_times
        self.pdf_path = pdf_path


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
        self._lmb_click = False
        value = self._map_value(ev)
        self.setValue(value)
        self.end_value_signal.emit(min(value, self.MAX_VALUE))

    def setValue(self, value):
        super().setValue(value)
        self.valueChanged.emit(self.value())

# class SettingsDialog(QSettings):
#     FILE_LOC = str(DATA_DIR / "settings.ini")
#     # todo probably need to set this per project. Have it be provided by the project.
#
#     def __init__(self, parent=None):
#         super().__init__(self.FILE_LOC, QSettings.Format.IniFormat, parent)


class MainWindow(QMainWindow):
    _save_path = None
    _open_path = None
    _pdf_path = None

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

        # Set up volume bar:
        self.volume_bar = GenericSlider(parent=self)
        # set up speed bar:
        self.playbackspeed_bar = GenericSlider(parent=self, tick_rate=5)

        self.file_dialog = QFileDialog(self)

        self._connect_tool_bar()
        self._connect_menu()
        self._connect_audio_player()
        self._connect_graphics()

        self.show()
        self.raise_()

        if DEBUG:
            # TODO; Add a recent files thing, and option to last saved file on load.
            self._open_path = Path("E:\\developer\\repos\\pdf_player\\test_resources\\save\\Air_Chrysalis_Animals_as_Leaders.pkl")
            self._load_markers(self._open_path)


    def _warning(self, title, text, accept=QMessageBox.StandardButton.Ok, cancel=QMessageBox.StandardButton.Cancel):
        reply = QMessageBox.warning(self, title, text, accept, cancel)
        log.info(f"{reply=}")
        accepted = reply == accept
        return accepted

    def _connect_graphics(self):
        self.m_ui.actionAdd_Practice_Marker.connect(lambda: self.graphics_scene._add_marker_at_scrubber(None, add_practice=True))
        self.m_ui.actionAdd_Page_Marker.connect(lambda: self.graphics_scene._add_marker_at_scrubber(None, add_practice=False))

    def _connect_tool_bar(self):
        volume_label = QLabel('100', self)
        self.volume_bar.valueChanged.connect(lambda x: volume_label.setText(str(x)))
        self.m_ui.toolBar.addSeparator()
        self.m_ui.toolBar.addWidget(volume_label)
        self.m_ui.toolBar.addSeparator()
        self.m_ui.toolBar.addWidget(self.volume_bar)
        self.m_ui.toolBar.orientationChanged.connect(self.volume_bar.setOrientation)

        playbackspeed_label = QLabel('100%', self)
        self.playbackspeed_bar.valueChanged.connect(lambda x: playbackspeed_label.setText(str(x) + '%'))
        self.m_ui.toolBar.addSeparator()
        self.m_ui.toolBar.addWidget(playbackspeed_label)
        self.m_ui.toolBar.addSeparator()
        self.m_ui.toolBar.addWidget(self.playbackspeed_bar)
        self.m_ui.toolBar.orientationChanged.connect(self.playbackspeed_bar.setOrientation)

    def _connect_audio_player(self):
        # Connect audio controls
        self.volume_bar.valueChanged.connect(lambda x:
                                             self.audio_player.audio_output.setVolume(x/self.volume_bar.MAX_VALUE))
        self.playbackspeed_bar.end_value_signal.connect(lambda x: self.audio_player.setPlaybackRate(x/self.playbackspeed_bar.MAX_VALUE))

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
            self.graphics_scene.clear_next_marker(marker_type=AudioMarkerType.PRACTICE)

        def _clear_previous_practice_marker():
            self.graphics_scene.clear_previous_marker(marker_type=AudioMarkerType.PRACTICE)

        def _clear_next_page_marker():
            self.graphics_scene.clear_next_marker(marker_type=AudioMarkerType.PAGE)

        def _clear_previous_page_marker():
            self.graphics_scene.clear_previous_marker(marker_type=AudioMarkerType.PAGE)

        self.m_ui.actionPlay.triggered.connect(toggle_audio)
        self.audio_player.playbackStateChanged.connect(set_icon)
        self.m_ui.actionNext_Practice_Marker.triggered.connect(self.graphics_scene.next_practice_marker)
        self.m_ui.actionPrevious_Practice_Marker.triggered.connect(self.graphics_scene.previous_practice_marker)
        self.m_ui.actionNext_Page.triggered.connect(self.graphics_scene.next_page)
        self.m_ui.actionPrevious_Page.triggered.connect(self.graphics_scene.previous_page)
        self.m_ui.actionDelete_All_Markers.triggered.connect(self.graphics_scene.clear_markers)
        self.m_ui.actionDelete_Next_Practice_Marker.triggered.connect(_clear_next_practice_marker)
        self.m_ui.actionDelete_Previous_Practice_Marker.triggered.connect(_clear_previous_practice_marker)
        self.m_ui.actionDelete_Next_Page_Marker.triggered.connect(_clear_previous_page_marker)
        self.m_ui.actionDelete_Previous_Page_Marker.triggered.connect(_clear_next_page_marker)

    def _connect_menu(self):
        def _toggle_fullscreen(toggled):
            if toggled:
                self.showFullScreen()
            else:
                self.showNormal()
        self.m_ui.actionFullscreen.toggled.connect(_toggle_fullscreen)

        def _import_audio():
            file_url = self.file_dialog.getOpenFileUrl(self, caption="Import Audio File",
                                                       filter="Audio (*.m4a *.mp3 *.wav *.FLAC)")[0]
            filepath = Path(file_url.path()[1:])
            if (file_url is None) or (file_url == '') or (not filepath.exists()):
                log.debug("No import path specified.")
                return

            self.audio_player.current_song = Song(file_url=file_url)
            self.graphics_scene.clear_markers()

        def _import_pdf():
            file_url = self.file_dialog.getOpenFileUrl(self, caption="Import PDF File", filter="PDF (*.pdf)")[0]
            filepath = Path(file_url.path()[1:])
            if (file_url is None) or (file_url == '') or (not filepath.exists()):
                log.debug("No import path specified.")
                return
            self.pdf_viewer.load(str(filepath))
            self._pdf_path = str(filepath)

        self.m_ui.actionImportAudio.triggered.connect(_import_audio)
        self.m_ui.actionImportPDF.triggered.connect(_import_pdf)

        def _save_markers(save_as=False):
            if self.audio_player.current_song.is_empty:
                log.info("No song currently loaded... Audio is required in order to save.")
                # todo; maybe in the future we could create a version that works on its own time base.
                #       (normalize to [0, 1]) users would set the duration and then they can add markers to
                #       automate page turns.
                return

            if save_as or (self._save_path is None):
                filesave = self.file_dialog.getSaveFileUrl(self, caption="Save Project", filter="pkl (*.pkl)")[0]
                self._save_path = filesave.path()[1:]

            if (self._save_path is None) or (self._save_path == ''):
                log.info("No output path specified.")
                return

            pdf_path = self._pdf_path
            song_path = self.audio_player.current_song.file_path
            page_marker_times = [self.graphics_scene.scrubber_to_time(*marker.scrubber_coords)/self.graphics_scene.song_duration
                                 for marker in self.graphics_scene.page_markers]
            practice_marker_times = [self.graphics_scene.scrubber_to_time(*marker.scrubber_coords)/self.graphics_scene.song_duration
                                  for marker in self.graphics_scene.practice_markers]
            project = Project(song_path=song_path,
                              page_marker_times=page_marker_times,
                              practice_marker_times=practice_marker_times,
                              pdf_path=pdf_path)
            with open(self._save_path, 'wb') as f:
                pickle.dump(project, f)

            self.setWindowTitle(f"{self.WINDOW_TITLE} - {self._save_path}")
            log.info(f"Saved Project as {self._save_path}.")

        def _new_project():
            if self.graphics_scene.markers_exist:
                title, text, = "Create new project?", "This will delete any existing markers and create a new project."
                accepted = self._warning(title, text)
                if not accepted:
                    return

            self.setWindowTitle(self.WINDOW_TITLE)

            self.graphics_scene.clear_markers()
            self.m_ui.actionImportAudio.trigger()
            self.m_ui.actionImportPDF.trigger()
            self.m_ui.actionSave_As.trigger()

        def _options():
            pass

        self.m_ui.actionSave.triggered.connect(lambda x: _save_markers(save_as=False))
        self.m_ui.actionSave_As.triggered.connect(lambda x: _save_markers(save_as=True))
        self.m_ui.actionOpen.triggered.connect(self._load_markers)
        self.m_ui.actionNew_Project.triggered.connect(_new_project)
        self.m_ui.actionProject_Options.triggered.connect(_options)

    def _load_markers(self, load_path=Path('')):
        if isinstance(load_path, bool) or load_path is None:
            fileload = self.file_dialog.getOpenFileUrl(self, caption="Open Project", filter="pkl (*.pkl)")[0]
            load_path = Path(fileload.path()[1:])

        if load_path.stem == "" or (not load_path.exists()):
            log.info("No input path specified.")
            return

        if (load_path != self._open_path) and self.graphics_scene.markers_exist:
            title = "Open different project?"
            text = "This will delete any existing markers and open a different project."
            accepted = self._warning(title, text)
            if not accepted:
                return

        log.info(f"Opening Project {load_path}.")
        self._open_path = load_path
        self._save_path = load_path
        self.graphics_scene.clear_markers()
        with open(load_path, "rb") as f:
            project = pickle.load(f)

        self.setWindowTitle(f"{self.WINDOW_TITLE} - {self._save_path}")

        song_path = project.song_path
        page_marker_times = project.page_marker_times
        practice_marker_times = project.practice_marker_times
        pdf_path = project.pdf_path

        if pdf_path is not None:
            self.pdf_viewer.load(pdf_path)
            self._pdf_path = pdf_path

        if page_marker_times is not None:
            self.graphics_scene.set_markers(page_marker_times, practice_marker_times)

        if song_path is not None:
            self.audio_player.current_song = Song(file_url=QUrl().fromLocalFile(song_path))

    def keyPressEvent(self, event):
        self.graphics_scene.keyPressEvent(event)

if __name__ == "__main__":
    app = QApplication(sys.argv)

    main_window = MainWindow()
    main_window.show()

    sys.exit(app.exec())
