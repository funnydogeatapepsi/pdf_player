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
                               QDialog, QDialogButtonBox, QFormLayout, QSpinBox, QComboBox, QDoubleSpinBox)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtCore import QUrl, Qt, QPointF, Signal, QSettings
from PySide6.QtGui import QIcon, QAction, QCloseEvent, QKeySequence

from windows.mainwindow import Ui_MainWindow
from widgets.audio_player import AudioPlayer, Song, STRETCH_METHODS, DEFAULT_STRETCH_METHOD
from widgets.graphics import GraphicsView, AudioMarker
from widgets.pdf import PdfView
from audio_effects.metronome import TempoChange, bar_beat_at

DEBUG = True

LOG_LEVEL = logging.INFO
log = logging.getLogger()
stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(logging.Formatter('%(levelname)s - %(name)s:%(funcName)s - %(message)s'))
log.setLevel(LOG_LEVEL)
log.addHandler(stream_handler)

RECENT_PROJECTS_JSON = DATA_DIR / 'recents.json'
MAX_RECENT_PROJECTS = 15

# Application-wide (not per project) settings, stored in DATA_DIR/settings.ini
SETTINGS_INI = DATA_DIR / 'settings.ini'
SETTING_STRETCH_METHOD = "playback/stretch_method"
SETTING_CLICK_GAIN = "metronome/click_gain"


class Project:
    """
    A saved project: audio + pdf paths, marker positions (normalised to [0, 1] of the song), page offset.

    Saved as JSON (*.json) since v0.2; older *.pkl (pickle) files are still readable.
    """
    FORMAT_VERSION = 2      # 2: added tempo_markers
    JSON_SUFFIX = ".json"

    def __init__(self, song_path, page_marker_times, practice_marker_times, pdf_path, page_offset=0,
                 tempo_markers=None):
        self.song_path = song_path
        self.page_marker_times = page_marker_times
        self.practice_marker_times = practice_marker_times
        self.pdf_path = pdf_path
        # Number of leading PDF pages (cover, table of contents, ...) before the page that marker 1 turns *from*.
        self.page_offset = page_offset
        # Metronome tempo changes: list of {"time_s", "bpm", "beats_per_bar"} in original song seconds.
        self.tempo_markers = list(tempo_markers or [])

    def to_dict(self) -> dict:
        return {
            "format_version": self.FORMAT_VERSION,
            "song_path": None if self.song_path is None else str(self.song_path),
            "pdf_path": None if self.pdf_path is None else str(self.pdf_path),
            "page_offset": int(self.page_offset),
            "page_marker_times": [float(t) for t in (self.page_marker_times or [])],
            "practice_marker_times": [float(t) for t in (self.practice_marker_times or [])],
            "tempo_markers": [TempoChange.from_dict(c).to_dict() for c in self.tempo_markers],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        version = data.get("format_version", 0)
        if version > cls.FORMAT_VERSION:
            log.warning(f"Project file is version {version}, newer than this app ({cls.FORMAT_VERSION}); "
                        f"unknown fields will be ignored.")
        return cls(song_path=data.get("song_path"),
                   page_marker_times=data.get("page_marker_times", []),
                   practice_marker_times=data.get("practice_marker_times", []),
                   pdf_path=data.get("pdf_path"),
                   page_offset=data.get("page_offset", 0),
                   tempo_markers=data.get("tempo_markers", []))

    def save(self, path: Path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=4)

    @classmethod
    def load(cls, path: Path) -> "Project":
        path = Path(path)
        if path.suffix.lower() == ".pkl":
            with open(path, "rb") as f:
                legacy = pickle.load(f)
            return cls(song_path=legacy.song_path,
                       page_marker_times=legacy.page_marker_times,
                       practice_marker_times=legacy.practice_marker_times,
                       pdf_path=legacy.pdf_path,
                       page_offset=getattr(legacy, "page_offset", 0))    # older pickles predate the offset
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


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


class TempoMarkerDialog(QDialog):
    """Edit one tempo marker: tempo (bpm) and beats per bar."""
    def __init__(self, parent, bpm: float, beats_per_bar: int):
        super().__init__(parent)
        self.setWindowTitle("Tempo Marker")
        self.bpm_spinbox = QDoubleSpinBox(self)
        self.bpm_spinbox.setRange(20.0, 400.0)
        self.bpm_spinbox.setDecimals(2)
        self.bpm_spinbox.setSingleStep(1.0)
        self.bpm_spinbox.setValue(bpm)
        self.beats_spinbox = QSpinBox(self)
        self.beats_spinbox.setRange(1, 32)
        self.beats_spinbox.setValue(beats_per_bar)

        layout = QFormLayout(self)
        layout.addRow("Tempo (bpm):", self.bpm_spinbox)
        layout.addRow("Beats per bar:", self.beats_spinbox)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.bpm_spinbox.selectAll()

    @property
    def bpm(self) -> float:
        return self.bpm_spinbox.value()

    @property
    def beats_per_bar(self) -> int:
        return self.beats_spinbox.value()


class ProjectOptionsDialog(QDialog):
    """
    Project settings (PDF page offset) and playback settings (time stretch method, metronome level).
    """
    def __init__(self, parent, page_offset: int, n_pages: int, stretch_method: str, click_gain: float):
        super().__init__(parent)
        self.setWindowTitle("Project Options")

        self.page_offset_spinbox = QSpinBox(self)
        self.page_offset_spinbox.setRange(0, max(0, n_pages - 1))
        self.page_offset_spinbox.setValue(page_offset)
        self.page_offset_spinbox.setToolTip("Number of leading PDF pages (cover, contents, ...) to skip.\n"
                                            "The first page shown is this page; page marker 1 turns to the next one.\n"
                                            "Tip: Ctrl + scroll wheel over the PDF adjusts this as well.")

        self.stretch_method_combo = QComboBox(self)
        for key, label in STRETCH_METHODS.items():
            self.stretch_method_combo.addItem(label, userData=key)
        self.stretch_method_combo.setCurrentIndex(max(0, self.stretch_method_combo.findData(stretch_method)))
        self.stretch_method_combo.setToolTip("Algorithm used to slow down / speed up the audio.\n"
                                             "Applies to all projects; changing it re-processes the current song "
                                             "if a playback speed other than 100% is active.")

        self.click_gain_spinbox = QSpinBox(self)
        self.click_gain_spinbox.setRange(0, 100)
        self.click_gain_spinbox.setSuffix(" %")
        self.click_gain_spinbox.setValue(int(round(click_gain * 100)))
        self.click_gain_spinbox.setToolTip("Metronome click level. Ctrl+M toggles the click, M toggles the tempo editor.")

        layout = QFormLayout(self)
        layout.addRow(f"PDF page offset (0 - {max(0, n_pages - 1)}):", self.page_offset_spinbox)
        layout.addRow("Slow-down method:", self.stretch_method_combo)
        layout.addRow("Metronome volume:", self.click_gain_spinbox)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    @property
    def page_offset(self) -> int:
        return self.page_offset_spinbox.value()

    @property
    def stretch_method(self) -> str:
        return self.stretch_method_combo.currentData()

    @property
    def click_gain(self) -> float:
        return self.click_gain_spinbox.value() / 100.0


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

        # Application settings:
        self.settings = QSettings(str(SETTINGS_INI), QSettings.Format.IniFormat, self)

        # Create Audio Player:
        self.audio_player = AudioPlayer(parent=self)
        stretch_method = str(self.settings.value(SETTING_STRETCH_METHOD, DEFAULT_STRETCH_METHOD))
        if stretch_method not in STRETCH_METHODS:
            stretch_method = DEFAULT_STRETCH_METHOD
        self.audio_player.setStretchMethod(stretch_method)
        self.audio_player.setClickGain(float(self.settings.value(SETTING_CLICK_GAIN, 0.5)))

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
        self._connect_metronome()

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

    def _connect_metronome(self):
        scene = self.graphics_scene

        self.actionMetronome_Mode = QAction("Metronome Edit Mode [M]", self)
        self.actionMetronome_Mode.setCheckable(True)
        self.actionMetronome_Mode.setShortcut(QKeySequence("M"))
        self.actionMetronome_Mode.setToolTip("Show only tempo markers and the beat grid. Shift+click adds a tempo "
                                             "marker, right-click edits it, Ctrl/Shift+right-click removes it.")
        self.actionMetronome_Mode.toggled.connect(scene.set_metronome_mode)
        scene.metronome_mode_signal.connect(self.actionMetronome_Mode.setChecked)

        self.actionMetronome_Click = QAction("Metronome Click [Ctrl+M]", self)
        self.actionMetronome_Click.setCheckable(True)
        self.actionMetronome_Click.setShortcut(QKeySequence("Ctrl+M"))
        self.actionMetronome_Click.setToolTip("Play a click on every beat of the tempo markers.")
        self.actionMetronome_Click.toggled.connect(self.audio_player.setClickEnabled)

        self.m_ui.menuOption.addSeparator()
        self.m_ui.menuOption.addAction(self.actionMetronome_Mode)
        self.m_ui.menuOption.addAction(self.actionMetronome_Click)

        scene.tempo_changed_signal.connect(lambda: self.audio_player.setTempoMap(scene.tempo_map))
        scene.tempo_marker_edit_signal.connect(self._edit_tempo_marker)

        # Bar : beat readout
        self.bar_beat_label = QLabel("Bar  -  :  -", self)
        self.m_ui.toolBar.addSeparator()
        self.m_ui.toolBar.addWidget(self.bar_beat_label)
        self.audio_player.positionChanged.connect(self._update_bar_beat)
        scene.tempo_changed_signal.connect(lambda: self._update_bar_beat(self.audio_player.position()))

    def _edit_tempo_marker(self, marker):
        dialog = TempoMarkerDialog(self, bpm=marker.bpm, beats_per_bar=marker.beats_per_bar)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.graphics_scene.set_tempo_marker(marker, dialog.bpm, dialog.beats_per_bar)

    def _update_bar_beat(self, position_ms):
        scene = self.graphics_scene
        if not scene.tempo_markers or scene.audio_metadata is None:
            self.bar_beat_label.setText("Bar  -  :  -")
            return
        # position is in (possibly stretched) stream time; the tempo map is in original song seconds
        original_s = position_ms / 1000.0 * self.audio_player.loaded_rate
        bar_beat = bar_beat_at(scene.tempo_map, original_s)
        if bar_beat is None:
            self.bar_beat_label.setText("Bar  -  :  -")
        else:
            self.bar_beat_label.setText(f"Bar {bar_beat[0]:3d} : {bar_beat[1]}")

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

        def _stretch_busy(busy):
            if busy:
                self.statusBar().showMessage(f"Processing audio at {self.playbackspeed_bar.value()}% ...")
            else:
                self.statusBar().clearMessage()

        self.m_ui.actionPlay.triggered.connect(toggle_audio)
        self.audio_player.playbackStateChanged.connect(set_icon)
        self.audio_player.stretch_busy_signal.connect(_stretch_busy)
        self.audio_player.stretch_failed_signal.connect(
            lambda msg: self._critical("Time stretch failed", msg, accept=QMessageBox.StandardButton.Ok,
                                       cancel=QMessageBox.StandardButton.NoButton))
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
                                      n_pages=self.graphics_scene.n_pages,
                                      stretch_method=self.audio_player.stretch_method,
                                      click_gain=self.audio_player.click_gain)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._set_page_offset(dialog.page_offset)
            if abs(dialog.click_gain - self.audio_player.click_gain) > 1e-6:
                self.settings.setValue(SETTING_CLICK_GAIN, dialog.click_gain)
                self.settings.sync()
                self.audio_player.setClickGain(dialog.click_gain)
            if dialog.stretch_method != self.audio_player.stretch_method:
                self.settings.setValue(SETTING_STRETCH_METHOD, dialog.stretch_method)
                self.settings.sync()
                self.audio_player.setStretchMethod(dialog.stretch_method)

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
            filesave = self.file_dialog.getSaveFileUrl(self, caption="Save Project",
                                                       filter="Project (*.json)")[0]
            save_path = self._url_to_path(filesave)

        if save_path is None:
            log.info("No output path specified.")
            return False
        if save_path.suffix.lower() != Project.JSON_SUFFIX:
            # Projects are saved as JSON now; a project opened from an old .pkl is saved next to it as .json.
            save_path = save_path.with_suffix(Project.JSON_SUFFIX)

        pdf_path = self._pdf_path
        song_path = self.audio_player.current_song.file_path
        page_marker_times = [marker.norm_time for marker in self.graphics_scene.page_markers]
        practice_marker_times = [marker.norm_time for marker in self.graphics_scene.practice_markers]
        project = Project(song_path=song_path,
                          page_marker_times=page_marker_times,
                          practice_marker_times=practice_marker_times,
                          pdf_path=pdf_path,
                          page_offset=self.graphics_scene.page_offset,
                          tempo_markers=[change.to_dict() for change in self.graphics_scene.tempo_map])
        project.save(save_path)

        self._save_path = save_path
        if self._open_path != save_path:
            self._open_path = save_path
            self._add_recent_project(save_path)
        self.set_dirty(False)
        log.info(f"Saved Project as {save_path}.")
        return True

    def _load_markers(self, load_path: Path | None = None):
        if load_path is None:
            fileload = self.file_dialog.getOpenFileUrl(self, caption="Open Project",
                                                       filter="Project (*.json *.pkl)")[0]
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
            project = Project.load(load_path)
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
            self.graphics_scene.set_markers(page_marker_times, practice_marker_times, project.tempo_markers)

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

    def keyReleaseEvent(self, event):
        # Mirror keyPressEvent: the scene must see the release of [A] or markers stay unlocked.
        self.graphics_scene.keyReleaseEvent(event)
        if not event.isAccepted():
            super().keyReleaseEvent(event)

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
