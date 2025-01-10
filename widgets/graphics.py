import logging
from time import time
from enum import Enum

from PySide6.QtWidgets import (QGraphicsScene, QGraphicsLineItem, QGraphicsRectItem, QGraphicsView,
                               QGraphicsSceneMouseEvent, QMessageBox, QGraphicsPathItem, QGraphicsPixmapItem)
from PySide6.QtCore import Qt, QPointF, Signal
from PySide6.QtGui import QPen, QBrush, QColor, QPainterPath, QPixmap, QPainter

import numpy as np

from .audio_player import AudioPlayer

log = logging.getLogger(__name__)


class AudioMarkerType(Enum):
    PRACTICE = 0
    PAGE = 1


class AudioMarker(QGraphicsRectItem):
    audio_color = Qt.GlobalColor.red
    page_color = Qt.GlobalColor.blue

    pen = QPen(Qt.GlobalColor.black, 2, Qt.PenStyle.SolidLine)

    def __init__(self, line_index, scrubber_index,  line_width=10, marker_type=AudioMarkerType.PRACTICE):
        super().__init__(0, 0, max(3, int(line_width / 3)), int(line_width * 1.5))
        self.setPen(self.pen)

        if marker_type == AudioMarkerType.PRACTICE:
            color = self.audio_color
        else:
            color = self.page_color
        brush = QBrush(color, Qt.BrushStyle.SolidPattern)

        self.setBrush(brush)
        self._scrubber_coords = (line_index, scrubber_index)
        self.marker_type = marker_type

    @property
    def scrubber_coords(self):
        return self._scrubber_coords


class GraphicsView(QGraphicsView):
    def __init__(self, parent, layout, audio_player: AudioPlayer):
        super().__init__(parent=parent)
        layout.addWidget(self)

        self.graphics_scene = GraphicsScene(parent=self, audio_player=audio_player)
        self.setScene(self.graphics_scene)
        self.graphics_scene.init_graphics()

    def resizeEvent(self, event):
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        super().resizeEvent(event)


class GraphicsScene(QGraphicsScene):
    x_off = 10
    n_lines = 9
    line_width = 35
    max_x = 1000
    max_y = 600
    line_length = max_x-2*x_off

    scrubber: QGraphicsRectItem
    scrubber_time_ms = 0

    scrubber_color = QColor(0, 255, 0, 125)
    practice_marker_color = Qt.GlobalColor.red
    line_color = QColor(255, 255, 255, 125)
    background_color = Qt.GlobalColor.gray

    audio_lines = []
    audio_metadata = None

    _load_practice_marker_times = []
    _practice_markers = []
    _active_practice_marker_index = 0
    _n_practice_markers = 0

    _load_page_marker_times = []
    _page_markers = []
    _active_page_marker_index = 0
    _n_page_markers = 0
    _n_pages = 0

    TIME_TOL_MS = 30
    DEFAULT_MARKER = AudioMarker(line_index=0, scrubber_index=0, line_width=line_width)

    _audio_graphics = None

    page_changed_signal = Signal(int)

    def __init__(self, parent, audio_player: AudioPlayer):
        super().__init__(0, 0, self.max_x, self.max_y, parent)
        background_brush = QBrush(self.background_color)
        self.setBackgroundBrush(background_brush)

        self._audio_player = audio_player
        self._audio_player.positionChanged.connect(self.set_scrubber_time)
        self._audio_player.audio_ready_signal.connect(self.set_audio)

    def init_graphics(self):
        x0 = self.x_off
        x1 = self.line_length

        dy = self.max_y/(self.n_lines + 1)
        y = np.arange(1, self.n_lines+1)*dy - self.line_width

        line_pen = QPen(self.line_color, self.line_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap)
        for k_line in range(self.n_lines):
            audio_line = QGraphicsLineItem(x0, y[k_line], x1, y[k_line])
            audio_line.setPen(line_pen)

            self.addItem(audio_line)
            self.audio_lines.append(audio_line)

        self.scrubber = QGraphicsRectItem(0, 0, 5, int(self.line_width * 1.5 - 2))
        scrubber_brush = QBrush(self.scrubber_color, Qt.BrushStyle.SolidPattern)
        scrubber_pen = QPen(Qt.GlobalColor.black, 1, Qt.PenStyle.SolidLine)
        self.scrubber.setPen(scrubber_pen)
        self.scrubber.setBrush(scrubber_brush)
        self.scrubber.setZValue(10)

        self.addItem(self.scrubber)
        self.reset_scrubber_position()

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
        self._n_page_markers = len(page_marker_list)+1

    @property
    def n_pages(self):
        return self._n_pages

    def set_n_pages(self, val):
        self._n_pages = val

    @property
    def markers_exist(self):
        return (self.n_practice_markers > 0) or (self.n_pages > 0)

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
        page_index = min(self.n_pages, page_index)
        if page_index != self._active_page_marker_index:
            log.info(f"Active page changed to {page_index}")
            self.page_changed_signal.emit(page_index)
            self._active_page_marker_index = page_index

    def update_active_marker_indexes(self):
        self.active_practice_marker_index = min(
            max(0, self._calc_marker_index(marker_type=AudioMarkerType.PRACTICE, next_marker=True)),
            self._n_practice_markers)
        self.active_page_marker_index = max(0, self._calc_marker_index(marker_type=AudioMarkerType.PAGE, next_marker=True)-1)

    @staticmethod
    def _sort_markers(marker_list):
        if len(marker_list) > 1:
            marker_coords = [np.sum(marker.scrubber_coords) for marker in marker_list]
            marker_list = [marker_list[k] for k in np.argsort(marker_coords)]
        return marker_list

    def get_next_page_marker(self):
        if self._n_page_markers == 0:
            return self.DEFAULT_MARKER

        self.active_page_marker_index = min(self._calc_marker_index(marker_type=AudioMarkerType.PAGE, next_marker=True),
                                            self._n_page_markers - 1)
        marker = self.page_markers[self.active_page_marker_index - 1]
        return marker

    def get_previous_page_marker(self):
        if self._n_page_markers == 0:
            return self.DEFAULT_MARKER

        marker_index = self._calc_marker_index(marker_type=AudioMarkerType.PAGE, next_marker=False)
        log.info(f"{marker_index=}")
        if marker_index > 0:
            # self.active_page = marker_index-1
            marker = self.page_markers[marker_index-1]
        else: # marker_index == 0:
            marker = AudioMarker(line_index=0, scrubber_index=0,
                                 line_width=self.line_width, marker_type=AudioMarkerType.PAGE)
        # else:
        #     raise IndexError(f"marker_index out of range: {marker_index=}")
        return marker

    def next_page(self):
        log.info("Next Page")
        marker = self.get_next_page_marker()
        time_ms = self.scrubber_to_time(*marker.scrubber_coords) + self.TIME_TOL_MS
        self._audio_player.setPosition(time_ms)

    def previous_page(self):
        log.info("Previous Page")
        marker = self.get_previous_page_marker()

        if self._audio_player.isPlaying():
            time_tol = 500
        else:
            time_tol = self.TIME_TOL_MS

        time_ms = self.scrubber_to_time(*marker.scrubber_coords) - time_tol
        self._audio_player.setPosition(time_ms)

    def get_next_practice_marker(self):
        if self.n_practice_markers == 0:
            return self.DEFAULT_MARKER

        marker_index = self._calc_marker_index(marker_type=AudioMarkerType.PRACTICE, next_marker=True)
        if marker_index < self._n_practice_markers:
            marker = self.practice_markers[marker_index]
        else:
            marker = AudioMarker(line_index=self.n_lines, scrubber_index=1,
                                 line_width=self.line_width, marker_type=AudioMarkerType.PRACTICE)
        return marker

    def get_previous_practice_marker(self):
        if self.n_practice_markers == 0:
            return self.DEFAULT_MARKER

        marker_index = self._calc_marker_index(marker_type=AudioMarkerType.PRACTICE, next_marker=False)
        if marker_index >= 0:
            marker = self.practice_markers[marker_index]
        else:
            marker = AudioMarker(line_index=0, scrubber_index=0,
                                 line_width=self.line_width, marker_type=AudioMarkerType.PRACTICE)
        return marker

    def next_practice_marker(self):
        marker = self.get_next_practice_marker()
        time_ms = self.scrubber_to_time(*marker.scrubber_coords) + self.TIME_TOL_MS
        self._audio_player.setPosition(time_ms)

    def previous_practice_marker(self):
        marker = self.get_previous_practice_marker()
        if self._audio_player.isPlaying():
            time_tol = 500
        else:
            time_tol = self.TIME_TOL_MS
        time_ms = self.scrubber_to_time(*marker.scrubber_coords) - time_tol
        self._audio_player.setPosition(time_ms)

    def _calc_marker_index(self, marker_type=AudioMarkerType.PRACTICE, next_marker=False):
        if marker_type == AudioMarkerType.PRACTICE:
            markers = self.practice_markers
        else:
            markers = self.page_markers

        if next_marker and marker_type == AudioMarkerType.PRACTICE:
            marker_index = self._n_practice_markers  # default value is max
        elif next_marker and marker_type == AudioMarkerType.PAGE:
            marker_index = self._n_page_markers - 1  # default value is max
        else:
            marker_index = -1   # default value is min

        # Find active marker:
        if len(markers) > 0:
            time_ms = self._audio_player.position()
            line_index, scrubber_index = self.time_to_scrubber(time_ms)
            scrubber_coord = scrubber_index + line_index

            coord_list = [0]
            coord_list.extend([np.sum(marker.scrubber_coords) for marker in markers])
            if marker_type == AudioMarkerType.PRACTICE:
                coord_list.append(self.n_lines)
            marker_coords = np.array(coord_list)
            marker_inds = np.arange(marker_coords.shape[0])
            if marker_type == AudioMarkerType.PRACTICE:
                marker_inds -= 1

            err_tol = 1e-3
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

    def scrubber_to_time(self, line_index, scrubber_index) -> int:
        time_ms = int(((scrubber_index + line_index) / self.n_lines) * self.song_duration)
        return time_ms

    def scrubber_to_pos(self, line_index, scrubber_index):
        line = self.audio_lines[line_index]
        line: QGraphicsLineItem

        pos_y = line.boundingRect().center().y()
        pos_x = (line.boundingRect().center().x() + line.boundingRect().width() * (scrubber_index-0.5))
        return QPointF(pos_x, pos_y)

    def time_to_scrubber(self, time_ms):
        norm_val = time_ms / self.song_duration
        line_index = max(0, int(norm_val * self.n_lines))
        scrubber_index = norm_val * self.n_lines - line_index
        return line_index, scrubber_index

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
        # log.debug(f"Setting scrubber position to: {line_index=}, {scrubber_index=}")
        self.scrubber_time_ms = self.scrubber_to_time(line_index, scrubber_index)
        position = self.scrubber_to_pos(line_index, scrubber_index)
        corrected_position = (position - QPointF(self.scrubber.boundingRect().width()/2,
                                                 self.scrubber.boundingRect().height()/2) +
                              QPointF(1, 1) * self.scrubber.pen().width() * 0.5)
        self.scrubber.setPos(corrected_position)

    def set_scrubber_time(self, time_ms):
        self.scrubber_time_ms = time_ms
        line_index, scrubber_index = self.time_to_scrubber(time_ms)
        self.set_scrubber_position(line_index, scrubber_index)
        self.update_active_marker_indexes()

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
        log.info(f"Audio set w/ metadata {self.audio_metadata=}")

    def create_audio_graphics(self):
        if self._audio_player.current_song is None:
            return

        if self._audio_graphics is not None:
            self.removeItem(self._audio_graphics)
            self._audio_graphics = None

        t_start = time()
        raw_audio = self._audio_player.current_song.raw_audio
        ind_r = int(self._audio_player.current_song.sample_rate * 0.002)
        ind_rate = max(10, ind_r)
        log.info(f'{ind_r=}, {ind_rate=}')
        raw_audio = raw_audio[::ind_rate].mean(1)
        n_samples = raw_audio.shape[0]

        t_raw_audio = np.linspace(0, self.song_duration, n_samples)

        norm_val = t_raw_audio / self.song_duration
        line_index = (norm_val * self.n_lines).astype(int)
        scrubber_index = norm_val * self.n_lines - line_index

        pen = QPen(Qt.GlobalColor.black, 1, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap)
        resolution = 10
        for k in range(self.n_lines):
            inds_line = np.nonzero(k == line_index)[0]
            if inds_line.shape[0] == 0:
                continue

            line = self.audio_lines[k]
            line: QGraphicsLineItem

            scrubber_inds = scrubber_index[inds_line]
            normalized_audio = raw_audio[inds_line] / raw_audio.max()

            image_rect = line.boundingRect()
            image_width = int(image_rect.width())
            image_height = int(image_rect.height())

            scrubber_inds_image = (scrubber_inds * image_width * resolution).astype(int)
            split_inds = np.where(np.diff(scrubber_inds_image) == 1)[0]

            bin_split = np.array_split(normalized_audio, split_inds)
            pixel_audio = np.nan_to_num(np.array([np.nanmean(x) for x in bin_split]))
            pixel_audio = pixel_audio / (np.abs(pixel_audio).max() + 1e-15)
            pixel_inds = np.arange(pixel_audio.shape[0])
            pixel_inds = pixel_inds / pixel_inds.max()

            pos_y = (pixel_audio + 1) * image_height / 2
            pos_x = image_width * pixel_inds
            pos = np.column_stack([pos_x, pos_y])

            qpixmap = QPixmap(image_width, image_height)
            qpixmap.fill(Qt.GlobalColor.white)
            qpainter = QPainter(qpixmap)
            painter_path = QPainterPath()
            painter_path.moveTo(pos[0, 0], pos[0, 1])
            for xy in pos[1:]:
                painter_path.lineTo(xy[0], xy[1])
            qpainter.setPen(pen)
            qpainter.drawPath(painter_path)
            qpainter.end()

            audio_line_item = QGraphicsPixmapItem(qpixmap, line)
            audio_line_item.setPos(line.sceneBoundingRect().topLeft())
            audio_line_item.setFlags(QGraphicsPathItem.GraphicsItemFlag.ItemStacksBehindParent)

        log.info(f"Time to generate {self.n_lines} audio images: {time() - t_start} seconds")

    def insert_marker(self, line_index, scrubber_index, marker_type=AudioMarkerType.PRACTICE):
        marker = AudioMarker(line_index, scrubber_index, self.line_width, marker_type=marker_type)
        log.debug(f"Adding marker at position: {line_index=}, {scrubber_index=}, {marker_type=}")

        marker_line_pos = self.scrubber_to_pos(line_index, scrubber_index)
        marker_pos = marker_line_pos - QPointF((marker.boundingRect().width() - 1)/2,
                                               marker.boundingRect().height()/2)
        marker.setPos(marker_pos)
        if marker_type == AudioMarkerType.PRACTICE:
            self._practice_markers.append(marker)
            self.practice_markers = self._practice_markers
        else:
            if self._n_page_markers < self.n_pages:
                self._page_markers.append(marker)
                self.page_markers = self._page_markers
            else:
                QMessageBox.information(self.parent(), "Insert Page Marker Failed",
                                        f"Failed to insert page marker because there are already "
                                        f"{self._n_page_markers} of {self.n_pages} possible.")
                return

        self.addItem(marker)

    def remove_marker(self, marker: AudioMarker):
        self.removeItem(marker)
        marker_ind = self._get_marker_index(marker)

        if marker.marker_type == AudioMarkerType.PRACTICE:
            if self._n_practice_markers == 0:
                return

            log.debug(f"Removing practice marker {marker_ind=}")
            self._practice_markers.pop(marker_ind)
            self._n_practice_markers = len(self._practice_markers)
        else:
            if self._n_page_markers == 0:
                return

            log.debug(f"Removing page marker {marker_ind=}")
            self._page_markers.pop(marker_ind)
            self._n_page_markers = len(self._page_markers)

    def _get_marker_index(self, marker: AudioMarker):
        if marker.marker_type == AudioMarkerType.PRACTICE:
            target_list = self.practice_markers
        else:
            target_list = self.page_markers
        marker_ind = 0
        for m in target_list:
            if marker == m:
                return marker_ind
            marker_ind += 1

    def set_markers(self, page_marker_times, practice_marker_times):
        self._load_practice_marker_times = practice_marker_times
        self._load_page_marker_times = page_marker_times

    def load_markers(self):
        log.info(f'Load Markers: {self._load_practice_marker_times=}, {self._load_page_marker_times=}, '
                 f'{self.audio_metadata.duration}')

        if len(self._load_practice_marker_times) > 0:
            practice_marker_scrub_coords = [self.time_to_scrubber(marker_time*self.song_duration)
                                            for marker_time in self._load_practice_marker_times]
            for practice_marker_scrub in practice_marker_scrub_coords:
                self.insert_marker(practice_marker_scrub[0], practice_marker_scrub[1], marker_type=AudioMarkerType.PRACTICE)
            self._load_practice_marker_times = []

        if len(self._load_page_marker_times) > 0:
            marker_scrub_coords = [self.time_to_scrubber(marker_time*self.song_duration)
                                   for marker_time in self._load_page_marker_times]
            for marker_scrub in marker_scrub_coords:
                self.insert_marker(marker_scrub[0], marker_scrub[1], marker_type=AudioMarkerType.PAGE)
            self._load_page_marker_times = []

    def clear_next_marker(self, marker_type=AudioMarkerType.PRACTICE):
        if marker_type == AudioMarkerType.PRACTICE:
            marker = self.get_next_practice_marker()
        else:
            marker = self.get_next_page_marker()
        self.remove_marker(marker)

    def clear_previous_marker(self, marker_type=AudioMarkerType.PRACTICE):
        if marker_type == AudioMarkerType.PRACTICE:
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

    def clear_markers(self):
        self.scrubber_time_ms = None
        self.clear_practice_markers()
        self.clear_page_markers()

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent):
        clicked_item = self.itemAt(event.scenePos(), self.parent().transform())

        is_audio_line = (clicked_item in self.audio_lines)
        is_marker = (clicked_item in self.practice_markers) or (clicked_item in self.page_markers)
        if is_audio_line or is_marker:
            # Determine line and scrubber index:
            line_index = 0
            for line_index in range(self.n_lines):
                audio_line = self.audio_lines[line_index]
                if audio_line.contains(event.scenePos()):
                    break
            scrubber_index = ((event.scenePos().x() - self.audio_lines[line_index].boundingRect().topLeft().x())/
                              self.audio_lines[line_index].boundingRect().width())
            log.debug(f"QGraphicsSceneMouseEvent {event.scenePos()} at position: {line_index=}, {scrubber_index=}")

            if is_audio_line:
                if (event.modifiers() == Qt.KeyboardModifier.ShiftModifier and
                        event.button() == Qt.MouseButton.LeftButton):
                    # insert audio marker at line index and scrubber index
                    self.insert_marker(line_index, scrubber_index, marker_type=AudioMarkerType.PRACTICE)
                elif (event.modifiers() == Qt.KeyboardModifier.ControlModifier and
                      event.button() == Qt.MouseButton.LeftButton):
                    self.insert_marker(line_index, scrubber_index, marker_type=AudioMarkerType.PAGE)

            else:
                if ((event.modifiers() == Qt.KeyboardModifier.ShiftModifier or
                     event.modifiers() == Qt.KeyboardModifier.ControlModifier) and
                        event.button() == Qt.MouseButton.RightButton):
                    # Remove marker if present
                    self.remove_marker(clicked_item)

            if event.modifiers() == Qt.KeyboardModifier.NoModifier:
                time_ms = self.scrubber_to_time(line_index, scrubber_index)
                self._audio_player.setPosition(int(time_ms))
        else:
            # Else add at scrubber if left-click + shift/ctrl
            time_ms = self._audio_player.position()
            line_index, scrubber_index = self.time_to_scrubber(time_ms)
            if (event.modifiers() == Qt.KeyboardModifier.ShiftModifier and
                    event.button() == Qt.MouseButton.LeftButton):
                # insert audio marker at line index and scrubber index
                self.insert_marker(line_index, scrubber_index, marker_type=AudioMarkerType.PRACTICE)
            elif (event.modifiers() == Qt.KeyboardModifier.ControlModifier and
                  event.button() == Qt.MouseButton.LeftButton):
                self.insert_marker(line_index, scrubber_index, marker_type=AudioMarkerType.PAGE)
