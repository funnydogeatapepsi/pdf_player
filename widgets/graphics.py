import logging
from time import time
from typing import Union
from enum import Enum

from PySide6.QtWidgets import (QGraphicsScene, QGraphicsLineItem, QGraphicsRectItem, QGraphicsView,
                               QGraphicsSceneMouseEvent, QMessageBox, QGraphicsPathItem, QGraphicsPixmapItem, QGraphicsItemGroup)
from PySide6.QtCore import Qt, QPointF, Signal, QTimer
from time import monotonic
from PySide6.QtGui import QPen, QBrush, QColor, QPainterPath, QPixmap, QPainter, QKeyEvent, QTransform

import numpy as np

from .audio_player import AudioPlayer
from .audio_graphics import AudioMarker, Scrubber, GraphicsType
from audio_effects.metronome import TempoChange, beat_times

log = logging.getLogger(__name__)



class GraphicsView(QGraphicsView):
    """
    Shows the timeline scene fitted to the viewport width. When the scene is zoomed (more, shorter lines) the
    view scrolls vertically and follows the scrubber. Ctrl + wheel zooms.
    """
    FOLLOW_MARGIN_PX = 60

    def __init__(self, parent, layout, audio_player: AudioPlayer):
        super().__init__(parent=parent)
        layout.addWidget(self)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        # Repaint the whole viewport on every update: the scene is small and this avoids the trails that partial
        # updates leave behind when the view scrolls under the scrubber/markers.
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)

        self.graphics_scene = GraphicsScene(parent=self, audio_player=audio_player)
        self.setScene(self.graphics_scene)
        self.graphics_scene.init_graphics()
        self.graphics_scene.zoom_changed_signal.connect(self._on_zoom_changed)
        self.graphics_scene.scrubber_moved_signal.connect(self._follow_scrubber)

    def fit_width(self):
        """Scale so the scene's full width fills the viewport; height then follows (scrolls when zoomed)."""
        scene_width = self.graphics_scene.sceneRect().width()
        if scene_width <= 0:
            return
        scale = self.viewport().width() / scene_width
        self.setTransform(QTransform.fromScale(scale, scale))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_width()

    def wheelEvent(self, event):
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() // 120
            if steps > 0:
                self.graphics_scene.zoom_in()
            elif steps < 0:
                self.graphics_scene.zoom_out()
            event.accept()
            return
        super().wheelEvent(event)

    def _on_zoom_changed(self, zoom):
        self.fit_width()
        self.centerOn(self.graphics_scene.scrubber)

    def _follow_scrubber(self):
        if self.graphics_scene.zoom > 1:
            self.ensureVisible(self.graphics_scene.scrubber, 0, self.FOLLOW_MARGIN_PX)


class GraphicsScene(QGraphicsScene):
    x_off = 10
    BASE_N_LINES = 11               # lines for the whole song at zoom 1
    ZOOM_LEVELS = (1, 2, 4, 8, 16)  # n_lines = BASE_N_LINES * zoom
    line_width = 70
    max_x = 1000
    max_y = 1000
    line_length = max_x-2*x_off
    line_pitch = max_y / (BASE_N_LINES + 1)     # vertical distance between lines (constant across zoom levels)

    scrubber: Scrubber
    scrubber_time_ms = 0

    scrubber_color = QColor(0, 255, 0, 125)
    practice_marker_color = Qt.GlobalColor.red
    line_color = Qt.GlobalColor.transparent
    background_color = Qt.GlobalColor.gray

    audio_metadata = None
    _unlock_markers = False

    _active_practice_marker_index = 0
    _n_practice_markers = 0

    _active_page_marker_index = 0
    _n_page_markers = 0
    _n_pages = 0
    _page_offset = 0        # Number of leading PDF pages to skip (e.g. cover / table of contents)
    _loading_markers = False

    TIME_TOL_LOW_MS = 30
    TIME_TOL_HIGH_MS = 500
    START_MARKER = AudioMarker(norm_time=0.0, line_width=line_width)
    END_MARKER = AudioMarker(norm_time=0.9999, line_width=line_width)

    _audio_graphics = None

    page_changed_signal = Signal(int)       # Emits the PDF page index (page offset already applied)
    markers_changed_signal = Signal()       # Emitted whenever the user adds/removes/moves markers
    zoom_changed_signal = Signal(int)       # Emits the new zoom level after the lines were rebuilt
    scrubber_moved_signal = Signal()
    tempo_changed_signal = Signal()         # Tempo markers added / removed / moved / edited
    tempo_marker_edit_signal = Signal(object)   # User double-clicked a tempo marker (edit its bpm / beats)
    metronome_mode_signal = Signal(bool)

    def __init__(self, parent, audio_player: AudioPlayer):
        super().__init__(0, 0, self.max_x, self.max_y, parent)
        self.setBackgroundBrush(QBrush(self.background_color))

        self._zoom = 1
        self.n_lines = self.BASE_N_LINES
        self.audio_lines: list[QGraphicsRectItem] = []
        self._practice_markers: list[AudioMarker] = []
        self._page_markers: list[AudioMarker] = []
        self._tempo_markers: list[AudioMarker] = []
        self._load_practice_marker_times = []
        self._load_page_marker_times = []
        self._load_tempo_markers: list[dict] = []
        self._metronome_mode = False
        self._beat_grid_items: list[QGraphicsLineItem] = []

        self._audio_player = audio_player
        self._audio_player.positionChanged.connect(self.set_scrubber_time)
        self._audio_player.audio_ready_signal.connect(self.set_audio)

        # positionChanged only arrives every ~50-100 ms; between updates the scrubber is advanced by a timer so
        # it moves smoothly (matters when zoomed in, where one update is tens of pixels).
        self._last_position_update = (0, monotonic())     # (time_ms reported by the player, wall clock)
        self._scrubber_timer = QTimer(self)
        self._scrubber_timer.setInterval(16)
        self._scrubber_timer.timeout.connect(self._animate_scrubber)
        self._audio_player.playbackStateChanged.connect(self._on_playback_state_changed)

    def _emit_markers_changed(self):
        if not self._loading_markers:
            self.markers_changed_signal.emit()

    def init_graphics(self):
        """
        Initialize the graphics. This will set up the scrub lines, scrubber, and markers

        :return:
        """
        self._build_lines()

        self.scrubber = Scrubber(self.line_width)

        scrubber_brush = QBrush(self.scrubber_color, Qt.BrushStyle.SolidPattern)
        scrubber_pen = QPen(Qt.GlobalColor.black, 1, Qt.PenStyle.SolidLine)
        self.scrubber.setPen(scrubber_pen)
        self.scrubber.setBrush(scrubber_brush)
        self.scrubber.setZValue(2)

        self.addItem(self.scrubber)
        self.reset_scrubber_position()

    def _build_lines(self):
        """(Re)create the audio line rectangles for the current zoom level and resize the scene to fit them."""
        for line in self.audio_lines:
            self.removeItem(line)       # removes the waveform pixmaps (children) as well
        self.audio_lines = []

        x0 = self.x_off
        x1 = self.line_length
        line_pen = QPen(Qt.GlobalColor.black, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        line_brush = QBrush(self.line_color)
        for k_line in range(self.n_lines):
            y = k_line * self.line_pitch + self.line_width
            audio_line = QGraphicsRectItem(x0, y - self.line_width, x1 - x0, self.line_width)
            audio_line.setPen(line_pen)
            audio_line.setBrush(line_brush)
            audio_line.setZValue(0)
            self.addItem(audio_line)
            self.audio_lines.append(audio_line)

        self.setSceneRect(0, 0, self.max_x, self.line_pitch * (self.n_lines + 1))

    # ------------------------------------------------------------------ zoom
    @property
    def zoom(self) -> int:
        return self._zoom

    def set_zoom(self, zoom: int):
        """
        Set the zoom level: the song is laid out over BASE_N_LINES * zoom lines, so each line covers a shorter
        stretch of time and the pixel resolution of the timeline goes up accordingly.
        """
        zoom = min(self.ZOOM_LEVELS, key=lambda z: abs(z - zoom))
        if zoom == self._zoom:
            return
        self._zoom = zoom
        self.n_lines = self.BASE_N_LINES * zoom
        log.info(f"Timeline zoom {zoom}x ({self.n_lines} lines)")

        self._build_lines()
        if self.audio_metadata is not None:
            self.create_audio_graphics()
        for marker in self._practice_markers + self._page_markers + self._tempo_markers:
            marker.update_position()
        self.rebuild_beat_grid()
        self.set_scrubber_time(self.scrubber_time_ms or 0)
        self.zoom_changed_signal.emit(zoom)

    def zoom_in(self):
        idx = self.ZOOM_LEVELS.index(self._zoom)
        self.set_zoom(self.ZOOM_LEVELS[min(idx + 1, len(self.ZOOM_LEVELS) - 1)])

    def zoom_out(self):
        idx = self.ZOOM_LEVELS.index(self._zoom)
        self.set_zoom(self.ZOOM_LEVELS[max(idx - 1, 0)])

    @property
    def song_duration(self):
        if self.audio_metadata is None:
            song_duration = 1
        else:
            song_duration = self.audio_metadata.duration * 1000
        return song_duration

    @property
    def practice_markers(self):
        return self._practice_markers

    @practice_markers.setter
    def practice_markers(self, practice_marker_list):
        self._practice_markers = self._sort_markers(practice_marker_list)
        self._n_practice_markers = len(practice_marker_list)

    @property
    def n_practice_markers(self):
        return self._n_practice_markers

    @property
    def active_practice_marker_index(self):
        return self._active_practice_marker_index

    @active_practice_marker_index.setter
    def active_practice_marker_index(self, marker_index):
        if marker_index != self._active_practice_marker_index:
            log.info(f"Active marker changed to {marker_index}")
            self._active_practice_marker_index = marker_index

    @property
    def page_markers(self):
        return self._page_markers

    @page_markers.setter
    def page_markers(self, page_marker_list):
        self._page_markers = self._sort_markers(page_marker_list)
        self._n_page_markers = len(page_marker_list)
        self.reset_marker_numbers()

    @property
    def n_pages(self):
        return self._n_pages

    def set_n_pages(self, val):
        self._n_pages = val
        self.page_offset = self._page_offset    # re-clamp offset to the new page count

    @property
    def page_offset(self):
        return self._page_offset

    @page_offset.setter
    def page_offset(self, offset):
        """
        Set the number of leading PDF pages to skip. Page marker k (1-based) maps to PDF page index k + offset,
        and the page shown before the first marker is index `offset`.
        """
        offset = int(max(0, offset))
        if self.n_pages > 0:
            offset = min(offset, self.n_pages - 1)
        if offset != self._page_offset:
            self._page_offset = offset
            log.info(f"Page offset set to {offset}")
        # Always re-emit so the pdf view follows the (possibly re-clamped) offset:
        self.page_changed_signal.emit(self.marker_index_to_page(self._active_page_marker_index))

    @property
    def n_usable_pages(self):
        """Number of PDF pages available for page markers once the offset is applied."""
        return max(0, self.n_pages - self._page_offset)

    def marker_index_to_page(self, marker_index):
        """Convert an active page-marker index (0 = before the first marker) to a PDF page index."""
        page = marker_index + self._page_offset
        if self.n_pages > 0:
            page = min(page, self.n_pages - 1)
        return max(0, page)

    @property
    def markers_exist(self):
        return (self.n_practice_markers > 0) or (self.n_page_markers > 0) or (len(self._tempo_markers) > 0)

    # ------------------------------------------------------------------ tempo / metronome
    @property
    def tempo_markers(self) -> list:
        return self._tempo_markers

    @property
    def tempo_map(self) -> list:
        """TempoChange list (original song time, seconds) derived from the tempo markers."""
        return [TempoChange(self.norm_to_time(m.norm_time) / 1000.0, m.bpm, m.beats_per_bar)
                for m in self._tempo_markers]

    @property
    def metronome_mode(self) -> bool:
        return self._metronome_mode

    def set_metronome_mode(self, enabled: bool):
        """
        Metronome edit mode: only tempo markers and the beat grid are shown (page/practice markers are hidden),
        and Shift+click places tempo markers instead of practice markers.
        """
        enabled = bool(enabled)
        if enabled == self._metronome_mode:
            return
        self._metronome_mode = enabled
        for marker in self._practice_markers + self._page_markers:
            marker.setVisible(not enabled)
        for marker in self._tempo_markers:
            marker.setVisible(enabled)
        for item in self._beat_grid_items:
            item.setVisible(enabled)
        log.info(f"Metronome edit mode {'on' if enabled else 'off'}")
        self.metronome_mode_signal.emit(enabled)

    def insert_tempo_marker(self, norm_time: float, bpm: float = None, beats_per_bar: int = None) -> AudioMarker:
        """Add a tempo change. Defaults to the tempo/meter in effect just before it (or 120 bpm, 4 beats)."""
        previous = [m for m in self._tempo_markers if m.norm_time <= norm_time]
        if bpm is None:
            bpm = previous[-1].bpm if previous else 120.0
        if beats_per_bar is None:
            beats_per_bar = previous[-1].beats_per_bar if previous else 4
        marker = AudioMarker(norm_time, self.line_width, marker_type=AudioMarker.TYPE.TEMPO)
        self._tempo_markers.append(marker)
        self._tempo_markers = self._sort_markers(self._tempo_markers)
        self.addItem(marker)
        marker.update_position()
        for item in marker.items(parent_first=True):
            item.setZValue(1)
        marker.set_tempo(bpm, beats_per_bar)
        marker.setVisible(self._metronome_mode)
        self._on_tempo_changed()
        return marker

    def set_tempo_marker(self, marker: AudioMarker, bpm: float, beats_per_bar: int):
        marker.set_tempo(bpm, beats_per_bar)
        self._on_tempo_changed()

    def _on_tempo_changed(self):
        self.rebuild_beat_grid()
        self._emit_markers_changed()
        if not self._loading_markers:
            self.tempo_changed_signal.emit()

    def rebuild_beat_grid(self):
        """Redraw the beat ticks (tall on downbeats) implied by the tempo markers, for the current zoom level."""
        for item in self._beat_grid_items:
            self.removeItem(item)
        self._beat_grid_items = []
        if self.audio_metadata is None or not self._tempo_markers:
            return

        times_s, downbeat, _, _ = beat_times(self.tempo_map, self.song_duration / 1000.0)
        beat_pen = QPen(QColor(0, 110, 0, 160), 1)
        bar_pen = QPen(QColor(0, 110, 0, 230), 2)
        for t_s, is_down in zip(times_s, downbeat):
            line_index, frac = self.norm_to_scrubber(self.time_to_norm(t_s * 1000.0))
            rect = self.audio_lines[line_index].rect()
            x = rect.left() + rect.width() * frac
            height = rect.height() if is_down else rect.height() * 0.35
            tick = QGraphicsLineItem(x, rect.bottom() - height, x, rect.bottom())
            tick.setPen(bar_pen if is_down else beat_pen)
            tick.setZValue(0.5)
            tick.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            tick.setVisible(self._metronome_mode)
            self.addItem(tick)
            self._beat_grid_items.append(tick)

    @property
    def n_page_markers(self):
        return self._n_page_markers

    @property
    def active_page_marker_index(self):
        return self._active_page_marker_index

    @active_page_marker_index.setter
    def active_page_marker_index(self, page_index):
        """
        NOTE - This does not tell you which marker to go to.
        :param page_index:
        :return:
        """
        page_index = max(0, min(self._n_page_markers, page_index))
        if page_index != self._active_page_marker_index:
            log.info(f"Active page changed to {page_index}")
            self._active_page_marker_index = page_index
            self.page_changed_signal.emit(self.marker_index_to_page(page_index))

    def update_active_marker_indexes(self):
        self.active_practice_marker_index = min(
            max(0, self._calc_marker_index(marker_type=AudioMarker.TYPE.PRACTICE, next_marker=True)),
            self._n_practice_markers)
        self.active_page_marker_index = max(0, self._calc_marker_index(marker_type=AudioMarker.TYPE.PAGE, next_marker=True)-1)

    @staticmethod
    def _sort_markers(marker_list):
        """
        Sort the marker list

        :param marker_list:
        :return:
        """
        if len(marker_list) > 1:
            marker_list = sorted(marker_list, key=lambda marker: marker.norm_time)
        return marker_list

    def get_next_page_marker(self):
        if self._n_page_markers == 0:
            return self.END_MARKER

        self.active_page_marker_index = min(self._calc_marker_index(marker_type=AudioMarker.TYPE.PAGE, next_marker=True),
                                            self._n_page_markers - 1)
        marker = self.page_markers[self.active_page_marker_index - 1]
        return marker

    def get_previous_page_marker(self):
        if self._n_page_markers == 0:
            return self.START_MARKER

        marker_index = self._calc_marker_index(marker_type=AudioMarker.TYPE.PAGE, next_marker=False)
        log.info(f"{marker_index=}")
        if marker_index > 0:
            # self.active_page = marker_index-1
            marker = self.page_markers[marker_index-1]
        else: # marker_index == 0:
            marker = self.START_MARKER
        # else:
        #     raise IndexError(f"marker_index out of range: {marker_index=}")
        return marker

    def next_page(self):
        log.info("Next Page")
        marker = self.get_next_page_marker()
        time_ms = self.norm_to_time(marker.norm_time) + self.TIME_TOL_LOW_MS
        self._audio_player.setPosition(time_ms)

    def previous_page(self):
        log.info("Previous Page")
        marker = self.get_previous_page_marker()

        if self._audio_player.isPlaying():
            time_tol = self.TIME_TOL_HIGH_MS
        else:
            time_tol = self.TIME_TOL_LOW_MS

        time_ms = self.norm_to_time(marker.norm_time) - time_tol
        self._audio_player.setPosition(time_ms)

    def get_next_practice_marker(self):
        if self.n_practice_markers == 0:
            return self.END_MARKER

        marker_index = self._calc_marker_index(marker_type=AudioMarker.TYPE.PRACTICE, next_marker=True)
        if marker_index < self._n_practice_markers:
            marker = self.practice_markers[marker_index]
        else:
            marker = self.END_MARKER
        return marker

    def get_previous_practice_marker(self):
        if self.n_practice_markers == 0:
            return self.START_MARKER

        marker_index = self._calc_marker_index(marker_type=AudioMarker.TYPE.PRACTICE, next_marker=False)
        if marker_index >= 0:
            marker = self.practice_markers[marker_index]
        else:
            marker = self.START_MARKER
        return marker

    def next_practice_marker(self):
        marker = self.get_next_practice_marker()
        time_ms = self.norm_to_time(marker.norm_time) + self.TIME_TOL_LOW_MS
        self._audio_player.setPosition(time_ms)

    def previous_practice_marker(self):
        marker = self.get_previous_practice_marker()
        if self._audio_player.isPlaying():
            time_tol = self.TIME_TOL_HIGH_MS
        else:
            time_tol = self.TIME_TOL_LOW_MS
        time_ms = self.norm_to_time(marker.norm_time) - time_tol
        self._audio_player.setPosition(time_ms)

    def _calc_marker_index(self, marker_type=AudioMarker.TYPE.PRACTICE, next_marker=False):
        """
        Determine marker index at current timestamp / scrubber location

        :param marker_type:
        :param next_marker:
        :return:
        """
        if marker_type == AudioMarker.TYPE.PRACTICE:
            markers = self.practice_markers
        else:
            markers = self.page_markers

        if next_marker and marker_type == AudioMarker.TYPE.PRACTICE:
            marker_index = self._n_practice_markers  # default value is max
        elif next_marker and marker_type == AudioMarker.TYPE.PAGE:
            marker_index = self._n_page_markers - 1  # default value is max
        else:
            marker_index = -1   # default value is min

        # Find active marker (all positions normalised to [0, 1] of the song):
        if len(markers) > 0:
            scrubber_coord = self.time_to_norm(self._audio_player.position())

            coord_list = [0.0]
            coord_list.extend([marker.norm_time for marker in markers])
            if marker_type == AudioMarker.TYPE.PRACTICE:
                coord_list.append(1.0)
            marker_coords = np.array(coord_list)
            marker_inds = np.arange(marker_coords.shape[0])
            if marker_type == AudioMarker.TYPE.PRACTICE:
                marker_inds -= 1

            err_tol = 1e-3 / self.BASE_N_LINES       # "on the marker" tolerance, independent of zoom
            marker_error = scrubber_coord - marker_coords
            argmin_error = np.argmin(marker_error ** 2)     # Find nearest marker
            marker_candidate = marker_inds[argmin_error]
            if (np.abs(marker_error[argmin_error]) < err_tol) and next_marker:
                # if the scrubber is on the marker and next is requested, give candidate
                marker_index = marker_candidate
            elif (marker_error[argmin_error] < 0) or (np.abs(marker_error[argmin_error]) < err_tol):
                # if the scrubber is to the left of the marker, return left most marker
                marker_index = marker_candidate - 1
            else:
                marker_index = marker_candidate

        if next_marker:
            marker_index = max(marker_index + 1, 0)
        log.debug(f"marker_index for {marker_type=}, {next_marker=}:\t{marker_index=}")
        return marker_index

    # ------------------------------------------------------------------ coordinates
    # Source of truth for any position is normalised song time in [0, 1]. (line_index, scrubber_index) and scene
    # positions are derived from it for the current zoom level.
    def time_to_norm(self, time_ms) -> float:
        return max(0.0, min(1.0, time_ms / self.song_duration))

    def norm_to_time(self, norm) -> int:
        return int(norm * self.song_duration)

    def norm_to_scrubber(self, norm):
        norm = max(0.0, min(1.0, norm))
        line_index = max(0, min(int(norm * self.n_lines), self.n_lines - 1))
        scrubber_index = max(min(norm * self.n_lines - line_index, 1), 0)
        return line_index, scrubber_index

    def scrubber_to_norm(self, line_index, scrubber_index) -> float:
        return max(0.0, min(1.0, (scrubber_index + line_index) / self.n_lines))

    def scrubber_to_time(self, line_index, scrubber_index) -> int:
        return self.norm_to_time(self.scrubber_to_norm(line_index, scrubber_index))

    def time_to_scrubber(self, time_ms):
        return self.norm_to_scrubber(self.time_to_norm(time_ms))

    def scrubber_to_pos(self, line_index, scrubber_index):
        line = self.audio_lines[line_index]
        line: QGraphicsRectItem

        pos_y = line.rect().center().y()
        pos_x = line.rect().x() + line.rect().width() * scrubber_index
        return QPointF(pos_x, pos_y)

    def norm_to_pos(self, norm) -> QPointF:
        return self.scrubber_to_pos(*self.norm_to_scrubber(norm))

    def pos_to_scrubber(self, scene_pos: QPointF):
        line_index = self.n_lines - 1
        for k in range(self.n_lines):
            rect = self.audio_lines[k].rect()
            if scene_pos.y() <= rect.bottom():      # first line whose bottom is below the point (above line 0 -> 0)
                line_index = k
                break
        rect = self.audio_lines[line_index].rect()
        scrubber_index = max(0.0, min(1.0, (scene_pos.x() - rect.left()) / rect.width()))
        log.debug(f"{scene_pos} -> {line_index=}, {scrubber_index=}")
        return line_index, scrubber_index

    def pos_to_norm(self, scene_pos: QPointF) -> float:
        return self.scrubber_to_norm(*self.pos_to_scrubber(scene_pos))

    def pos_to_time(self, scene_pos: QPointF):
        return self.scrubber_to_time(*self.pos_to_scrubber(scene_pos))

    def time_to_pos(self, time_ms) -> QPointF:
        return self.scrubber_to_pos(*self.time_to_scrubber(time_ms))

    def reset_scrubber_position(self):
        log.debug("Resetting scrubber position.")
        if self._audio_player.hasAudio():
            if self._audio_player.isPlaying():
                self._audio_player.stop()
            self._audio_player.setPosition(0)
        self.set_scrubber_position(0, 0)

    def set_scrubber_position(self, line_index, scrubber_index):
        log.debug(f"Setting scrubber position to: {line_index=}, {scrubber_index=}")
        self.scrubber_time_ms = self.scrubber_to_time(line_index, scrubber_index)
        self.scrubber.setPos(self.scrubber_to_pos(line_index, scrubber_index))
        self.scrubber_moved_signal.emit()

    def set_scrubber_time(self, time_ms):
        self.scrubber_time_ms = time_ms
        self._last_position_update = (time_ms, monotonic())
        line_index, scrubber_index = self.time_to_scrubber(time_ms)
        self.set_scrubber_position(line_index, scrubber_index)
        self.update_active_marker_indexes()

    def _on_playback_state_changed(self, state):
        if self._audio_player.isPlaying():
            self._last_position_update = (self._audio_player.position(), monotonic())
            self._scrubber_timer.start()
        else:
            self._scrubber_timer.stop()

    def _animate_scrubber(self):
        """Move the scrubber graphics to the extrapolated playback position (no marker/page logic here)."""
        if not self._audio_player.isPlaying():
            return
        time_ms, t0 = self._last_position_update
        estimated_ms = min(time_ms + (monotonic() - t0) * 1000, self.song_duration)
        self.scrubber.setPos(self.norm_to_pos(self.time_to_norm(estimated_ms)))
        self.scrubber_moved_signal.emit()

    def set_audio(self, audio_is_ready):
        if not audio_is_ready:
            return

        self.reset_scrubber_position()

        self.audio_metadata = self._audio_player.current_song.metadata
        if self.scrubber_time_ms is not None:
            log.debug(f"{self.scrubber_time_ms=}, {self._audio_player.duration()=}")
            self._audio_player.setPosition(self.scrubber_time_ms)

        self.create_audio_graphics()
        self.load_markers()
        self.rebuild_beat_grid()
        log.info(f"Audio set w/ metadata {self.audio_metadata=}")

    def create_audio_graphics(self):
        """
        Create audio waveform image for the scrubber lines.

        :return:
        """
        if self._audio_player.current_song is None:
            return

        if self._audio_graphics is not None:
            self.removeItem(self._audio_graphics)
            self._audio_graphics = None

        t_start = time()
        raw_audio = self._audio_player.current_song.raw_audio
        # Take bins at size .2% of 1 second (finer when zoomed in, so the waveform keeps pixel-level detail):
        ind_r = int(self._audio_player.current_song.sample_rate * 0.002 / self._zoom)
        ind_rate = max(10, ind_r)   # At least 10 samples per bin
        log.info(f'{ind_r=}, {ind_rate=}')
        # Downsample by that amount. Take mean to convert to mono.
        raw_audio = raw_audio[::ind_rate].mean(1)
        n_samples = raw_audio.shape[0]

        # Time base for decimated audio:
        t_raw_audio = np.linspace(0, self.song_duration, n_samples)

        # Normalize and convert to scrubber coords:
        norm_val = t_raw_audio / self.song_duration
        line_index = (norm_val * self.n_lines).astype(int)
        scrubber_index = norm_val * self.n_lines - line_index

        # Now we average the signal to decrease the amount of data for the QPainterPath:
        resolution = 10
        for k in range(self.n_lines):
            # Find points on the given line:
            inds_line = np.nonzero(k == line_index)[0]
            if inds_line.shape[0] == 0:
                continue

            # Get the line graphics objects:
            line = self.audio_lines[k]
            line: QGraphicsLineItem

            # Determine image size to match line object:
            image_rect = line.boundingRect()
            image_width = int(image_rect.width())
            image_height = int(image_rect.height())

            # Calculate the scrubber inds along the line and normalize the audio to unit range
            scrubber_inds = scrubber_index[inds_line]
            normalized_audio = raw_audio[inds_line] / (np.abs(raw_audio).max() + 1e-15)   # safe for silent audio

            # Bin the samples at `resolution` sub-pixel bins per image pixel and average each bin (vectorised):
            n_bins = image_width * resolution
            bin_ids = np.clip((scrubber_inds * n_bins).astype(int), 0, n_bins - 1)
            counts = np.bincount(bin_ids, minlength=n_bins)
            sums = np.bincount(bin_ids, weights=normalized_audio, minlength=n_bins)
            filled = counts > 0
            pixel_audio = sums[filled] / counts[filled]
            pixel_audio = pixel_audio / (np.abs(pixel_audio).max() + 1e-15)
            pixel_inds = np.nonzero(filled)[0] / max(n_bins - 1, 1)

            # Lets pack this into an image. First convert to image coordinates:
            pos_y = (pixel_audio + 1) / 2 * image_height
            pos_x = image_width * pixel_inds
            pos = np.nan_to_num(np.column_stack([pos_x, pos_y]))
            if pos.shape[0] == 0:
                continue

            # Make pixmap and paint the audio image onto the pixmap using QPainter:
            qpixmap = QPixmap(image_width, image_height)
            qpixmap.fill(Qt.GlobalColor.white)
            qpainter = QPainter(qpixmap)
            painter_path = QPainterPath()
            painter_path.moveTo(pos[0, 0], pos[0, 1])
            for x, y in pos[1:]:
                painter_path.lineTo(x, y)
            qpainter.setPen(QPen(Qt.GlobalColor.darkGray, 1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap))
            qpainter.drawPath(painter_path)
            qpainter.setPen(QPen(Qt.GlobalColor.black, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap))
            qpainter.drawLine(QPointF(0, image_height/2), QPointF(image_width, image_height/2))
            qpainter.end()

            # Place Audio on Line:
            audio_line_item = QGraphicsPixmapItem(qpixmap, line)
            audio_line_item.setPos(line.sceneBoundingRect().topLeft())
            audio_line_item.setFlags(QGraphicsPathItem.GraphicsItemFlag.ItemStacksBehindParent)

        log.info(f"Time to generate {self.n_lines} audio images: {time() - t_start} seconds")

    def insert_marker(self, line_index, scrubber_index, marker_type=AudioMarker.TYPE.PRACTICE):
        """Insert a marker at (line_index, scrubber_index) of the current zoom level."""
        self.insert_marker_at_norm(self.scrubber_to_norm(line_index, scrubber_index), marker_type)

    def insert_marker_at_norm(self, norm_time: float, marker_type=AudioMarker.TYPE.PRACTICE):
        """Insert a marker at a normalised song time in [0, 1]."""
        marker = AudioMarker(norm_time, self.line_width, marker_type=marker_type)
        log.debug(f"Adding marker at {norm_time=:.4f}, {marker_type=}")

        if marker_type == AudioMarker.TYPE.PRACTICE:
            self._practice_markers.append(marker)
            self.practice_markers = self._practice_markers
        else:
            # One page marker per page turn; the first `page_offset` pages and the first shown page need no marker.
            max_page_markers = max(0, self.n_usable_pages - 1)
            if self._n_page_markers < max_page_markers:
                self._page_markers.append(marker)
                self.page_markers = self._page_markers
            else:
                QMessageBox.information(self.parent(), "Insert Page Marker Failed",
                                        f"Failed to insert page marker because there are already "
                                        f"{self._n_page_markers} of {max_page_markers} possible "
                                        f"({self.n_pages} pages, page offset {self._page_offset}).")
                return

        self.addItem(marker)    # adding the group adds its children as well
        marker.update_position()
        for item in marker.items(parent_first=True):
            item.setZValue(1)
        marker.setVisible(not self._metronome_mode)
        self._emit_markers_changed()

    def remove_marker(self, marker: AudioMarker):
        """
        Remove provided marker from lists
        :param marker:
        :return:
        """
        marker_ind = self._get_marker_index(marker)
        if marker_ind is None:
            # e.g. START_MARKER / END_MARKER sentinels, which are not real markers.
            log.debug("Marker not in scene; nothing to remove.")
            return

        self.removeItem(marker)  # removing the group removes its children as well

        if marker.marker_type == AudioMarker.TYPE.PRACTICE:
            log.debug(f"Removing practice marker {marker_ind=}")
            self._practice_markers.pop(marker_ind)
            self._n_practice_markers = len(self._practice_markers)
        elif marker.marker_type == AudioMarker.TYPE.TEMPO:
            log.debug(f"Removing tempo marker {marker_ind=}")
            self._tempo_markers.pop(marker_ind)
            self._on_tempo_changed()
            return
        else:
            log.debug(f"Removing page marker {marker_ind=}")
            self._page_markers.pop(marker_ind)
            self._n_page_markers = len(self._page_markers)

        self.reset_marker_numbers()
        self._emit_markers_changed()

    def reset_marker_numbers(self):
        for k_marker, marker in enumerate(self.page_markers):
            marker.page_index = k_marker+1

    def _get_marker_index(self, marker: AudioMarker):
        if marker.marker_type == AudioMarker.TYPE.PRACTICE:
            target_list = self.practice_markers
        elif marker.marker_type == AudioMarker.TYPE.TEMPO:
            target_list = self._tempo_markers
        else:
            target_list = self.page_markers
        marker_ind = 0
        for m in target_list:
            if marker == m:
                return marker_ind
            marker_ind += 1

    def set_markers(self, page_marker_times, practice_marker_times, tempo_markers=None):
        """
        Queue markers to be created once the audio is loaded (their positions depend on the song duration).

        :param tempo_markers: list of dicts {"time_s", "bpm", "beats_per_bar"} in original song seconds
        """
        self._load_practice_marker_times = practice_marker_times
        self._load_page_marker_times = page_marker_times
        self._load_tempo_markers = list(tempo_markers or [])

    def load_markers(self):
        """
        Load marker positions from file

        :return:
        """
        log.info(f'Load Markers: {self._load_practice_marker_times=}, {self._load_page_marker_times=}, '
                 f'{self.audio_metadata.duration}')

        self._loading_markers = True
        try:
            self._load_markers()
        finally:
            self._loading_markers = False
        self.rebuild_beat_grid()
        self.tempo_changed_signal.emit()       # let the player pick up the loaded tempo map

    def _load_markers(self):
        for norm_time in self._load_practice_marker_times:
            self.insert_marker_at_norm(norm_time, marker_type=AudioMarker.TYPE.PRACTICE)
        self._load_practice_marker_times = []

        for norm_time in self._load_page_marker_times:
            self.insert_marker_at_norm(norm_time, marker_type=AudioMarker.TYPE.PAGE)
        self._load_page_marker_times = []

        for change in self._load_tempo_markers:
            self.insert_tempo_marker(self.time_to_norm(float(change["time_s"]) * 1000.0),
                                     bpm=float(change["bpm"]), beats_per_bar=int(change["beats_per_bar"]))
        self._load_tempo_markers = []

    def clear_next_marker(self, marker_type=AudioMarker.TYPE.PRACTICE):
        if marker_type == AudioMarker.TYPE.PRACTICE:
            marker = self.get_next_practice_marker()
        else:
            marker = self.get_next_page_marker()
        self.remove_marker(marker)

    def clear_previous_marker(self, marker_type=AudioMarker.TYPE.PRACTICE):
        if marker_type == AudioMarker.TYPE.PRACTICE:
            marker = self.get_previous_practice_marker()
        else:
            marker = self.get_previous_page_marker()
        self.remove_marker(marker)

    def clear_practice_markers(self):
        for marker in self.practice_markers:
            self.removeItem(marker)
        self._practice_markers = []
        self._n_practice_markers = 0

    def clear_page_markers(self):
        for marker in self.page_markers:
            self.removeItem(marker)
        self._page_markers = []
        self._n_page_markers = 0

    def clear_tempo_markers(self):
        for marker in self._tempo_markers:
            self.removeItem(marker)
        self._tempo_markers = []
        self.rebuild_beat_grid()

    def clear_markers(self):
        self.scrubber_time_ms = None
        had_markers = self.markers_exist
        self.clear_practice_markers()
        self.clear_page_markers()
        self.clear_tempo_markers()
        self._load_practice_marker_times = []
        self._load_page_marker_times = []
        self._load_tempo_markers = []
        self._active_page_marker_index = 0
        self._active_practice_marker_index = 0
        if had_markers:
            self._emit_markers_changed()

    def _item_under(self, scene_pos: QPointF):
        """
        (item, GraphicsType name) under a scene position. Visible markers win over anything stacked on top of
        them (the scrubber has a whole-row hit area and a higher z-value, so plain itemAt() would hide markers).
        """
        items = self.items(scene_pos, Qt.ItemSelectionMode.IntersectsItemShape, Qt.SortOrder.DescendingOrder,
                           self.parent().transform())
        for item in items:
            if isinstance(item, AudioMarker) and item.isVisible():
                return item, GraphicsType.MARKER.name
        for item in items:
            item_type = GraphicsType.get_item_type(item)
            if item_type is not None:
                return item, item_type
        return None, None

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent):
        """
        We want to interact with the scene.

        Shift + Left Click = insert audio marker
        Ctrl + Left Click = insert practice marker
        Ctrl/Shift + Right Click = remove marker

        Left Click on line = Move Scrubber to position

        Letter A + Left Click allows movement of markers
        
        :param event:
        :return:
        """
        clicked_item, item_type = self._item_under(event.scenePos())
        is_marker = item_type == GraphicsType.MARKER.name
        is_audio_line = item_type == GraphicsType.LINE.name
        is_scrubber = item_type == GraphicsType.SCRUBBER.name
        log.info(f"mousePressEvent: {item_type=}, {clicked_item=}")

        # If marker and we are in marker adjust mode, then send the event to the marker.
        if is_marker and self._unlock_markers:
            clicked_item.mousePressEvent(event)
            return

        # Plain right-click on a tempo marker (metronome edit mode) opens its editor.
        if (is_marker and clicked_item.marker_type == AudioMarker.TYPE.TEMPO and self._metronome_mode
                and event.button() == Qt.MouseButton.RightButton
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            self.tempo_marker_edit_signal.emit(clicked_item)
            event.accept()
            return

        shift_left_click = (event.modifiers() == Qt.KeyboardModifier.ShiftModifier and
                            event.button() == Qt.MouseButton.LeftButton)
        ctrl_left_click = (event.modifiers() == Qt.KeyboardModifier.ControlModifier and
                           event.button() == Qt.MouseButton.LeftButton)

        # If not marker, scrubber, or line, add a marker at the scrubber if left-click + shift/ctrl
        if not (is_marker or is_audio_line or is_scrubber):
            time_ms = self._audio_player.position()
            line_index, scrubber_index = self.time_to_scrubber(time_ms)
            if shift_left_click and self._metronome_mode:
                self.insert_tempo_marker(self.scrubber_to_norm(line_index, scrubber_index))
            elif shift_left_click:
                # insert audio marker at line index and scrubber index
                self.insert_marker(line_index, scrubber_index, marker_type=AudioMarker.TYPE.PRACTICE)
            elif ctrl_left_click and not self._metronome_mode:
                self.insert_marker(line_index, scrubber_index, marker_type=AudioMarker.TYPE.PAGE)
            return

        # If line and shift+click, add Practice Marker
        # If line and ctrl+click, add Page Marker
        # If marker and shift+click OR ctrl+click, delete marker
        right_click = ((event.modifiers() == Qt.KeyboardModifier.ControlModifier or
                       event.modifiers() == Qt.KeyboardModifier.ShiftModifier) and
                       event.button() == Qt.MouseButton.RightButton)
        line_index, scrubber_index = self.pos_to_scrubber(event.scenePos())
        if (is_audio_line or is_scrubber or is_marker) and shift_left_click:
            if self._metronome_mode:
                self.insert_tempo_marker(self.scrubber_to_norm(line_index, scrubber_index))
            else:
                self.insert_marker(line_index, scrubber_index, marker_type=AudioMarker.TYPE.PRACTICE)
            return
        elif (is_audio_line or is_scrubber or is_marker) and ctrl_left_click:
            if not self._metronome_mode:
                self.insert_marker(line_index, scrubber_index, marker_type=AudioMarker.TYPE.PAGE)
            return
        elif is_marker and right_click:
            self.remove_marker(clicked_item)
            return

        # If not in marker adjust mode and left click, place scrubber at mouse
        left_click = (event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier)
        if left_click and (not self._unlock_markers) and (is_scrubber or is_marker or is_audio_line):
            self._set_audio_player_scrubber_coords(line_index, scrubber_index)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent):
        clicked_item, item_type = self._item_under(event.scenePos())
        log.debug(f"mouseMoveEvent: {item_type=}, {clicked_item=}")

        if self._unlock_markers and item_type == GraphicsType.MARKER.name:
            clicked_item.norm_time = self.pos_to_norm(event.scenePos())
            return

        left_click = (Qt.MouseButton.LeftButton in event.buttons() and event.modifiers() == Qt.KeyboardModifier.NoModifier)
        if left_click and item_type in [GraphicsType.LINE.name, GraphicsType.SCRUBBER.name]:
            # Determine line and scrubber index:
            self._set_audio_player_scrubber_coords(*self.pos_to_scrubber(event.scenePos()))

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent):
        clicked_item, item_type = self._item_under(event.scenePos())
        log.info(f"mouseReleaseEvent: {item_type=}, {clicked_item=}")

        if self._unlock_markers and item_type == GraphicsType.MARKER.name:
            clicked_item.norm_time = self.pos_to_norm(event.scenePos())
            # A moved marker may have changed order; re-sort (and renumber page markers).
            if clicked_item.marker_type == AudioMarker.TYPE.PRACTICE:
                self.practice_markers = self._practice_markers
            elif clicked_item.marker_type == AudioMarker.TYPE.TEMPO:
                self._tempo_markers = self._sort_markers(self._tempo_markers)
                self._on_tempo_changed()
                return
            else:
                self.page_markers = self._page_markers
            self._emit_markers_changed()
            return

        left_click = (event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier)
        if left_click and item_type in [GraphicsType.LINE.name, GraphicsType.SCRUBBER.name]:
            # Determine line and scrubber index:
            self._set_audio_player_scrubber_coords(*self.pos_to_scrubber(event.scenePos()))

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent):
        """Double-click on a tempo marker (in metronome edit mode) opens its editor; anything else acts as a click."""
        clicked_item, item_type = self._item_under(event.scenePos())
        if (item_type == GraphicsType.MARKER.name and clicked_item.marker_type == AudioMarker.TYPE.TEMPO
                and self._metronome_mode and event.button() == Qt.MouseButton.LeftButton):
            self.tempo_marker_edit_signal.emit(clicked_item)
            event.accept()
            return
        # Qt sends press, release, double-click, release: treat the double-click like the press so a fast
        # double-click on the timeline still just seeks.
        self.mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key.Key_A:
            self._unlock_markers = True
            # todo; When F is held, unlock the markers and ignore scrubber placements.
            #       Mouse Events while F is held will go to markers so that they can be adjusted on the timeline.

        self._add_marker_at_scrubber(event)
        event.accept()

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_A:
            self._unlock_markers = False

    def _set_audio_player_scrubber_coords(self, line_index, scrubber_index):
        time_ms = self.scrubber_to_time(line_index, scrubber_index)
        self._audio_player.setPosition(int(time_ms))

    def _add_marker_at_scrubber(self, event: Union[QGraphicsSceneMouseEvent, QKeyEvent, None], add_practice=None):
        # Else add at scrubber if left-click + shift/ctrl
        time_ms = self._audio_player.position()
        line_index, scrubber_index = self.time_to_scrubber(time_ms)
        if isinstance(event, QGraphicsSceneMouseEvent):
            add_practice_marker = (event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.ShiftModifier)
            add_page_marker = (event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.ControlModifier)
        elif isinstance(event, QKeyEvent):
            add_practice_marker = event.key() == Qt.Key.Key_Space and event.modifiers() == Qt.KeyboardModifier.ShiftModifier
            add_page_marker = event.key() == Qt.Key.Key_Space and event.modifiers() == Qt.KeyboardModifier.ControlModifier
        elif event is None:
            add_practice_marker = add_practice
            add_page_marker = not add_practice
        else:
            raise TypeError(f"{type(event)} not supported.")

        if add_practice_marker and self._metronome_mode:
            self.insert_tempo_marker(self.scrubber_to_norm(line_index, scrubber_index))
        elif add_practice_marker:
            # insert practice marker at line index and scrubber index
            self.insert_marker(line_index, scrubber_index, marker_type=AudioMarker.TYPE.PRACTICE)
        elif add_page_marker and not self._metronome_mode:
            self.insert_marker(line_index, scrubber_index, marker_type=AudioMarker.TYPE.PAGE)

