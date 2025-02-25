import logging
from time import time
from typing import Union
from enum import Enum

from PySide6.QtWidgets import (QGraphicsScene, QGraphicsLineItem, QGraphicsRectItem, QGraphicsView,
                               QGraphicsSceneMouseEvent, QMessageBox, QGraphicsPathItem, QGraphicsPixmapItem, QGraphicsItemGroup)
from PySide6.QtCore import Qt, QPointF, Signal
from PySide6.QtGui import QPen, QBrush, QColor, QPainterPath, QPixmap, QPainter, QKeyEvent, QTransform

import numpy as np

from .audio_player import AudioPlayer
from .audio_graphics import AudioMarker, Scrubber, GraphicsType

log = logging.getLogger(__name__)



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
    n_lines = 11
    line_width = 70
    max_x = 1000
    max_y = 1000
    line_length = max_x-2*x_off

    scrubber: Scrubber
    scrubber_time_ms = 0

    scrubber_color = QColor(0, 255, 0, 125)
    practice_marker_color = Qt.GlobalColor.red
    line_color = Qt.GlobalColor.transparent
    background_color = Qt.GlobalColor.gray

    audio_lines = []
    audio_metadata = None
    _unlock_markers = False

    _load_practice_marker_times = []
    _practice_markers = []
    _active_practice_marker_index = 0
    _n_practice_markers = 0

    _load_page_marker_times = []
    _page_markers = []
    _active_page_marker_index = 0
    _n_page_markers = 0
    _n_pages = 0

    TIME_TOL_LOW_MS = 30
    TIME_TOL_HIGH_MS = 500
    START_MARKER = AudioMarker(line_index=0, scrubber_index=0, line_width=line_width)
    END_MARKER = AudioMarker(line_index=n_lines-1, scrubber_index=0.9999, line_width=line_width)

    _audio_graphics = None

    page_changed_signal = Signal(int)

    def __init__(self, parent, audio_player: AudioPlayer):
        super().__init__(0, 0, self.max_x, self.max_y, parent)
        self.setBackgroundBrush(QBrush(self.background_color))

        self._audio_player = audio_player
        self._audio_player.positionChanged.connect(self.set_scrubber_time)
        self._audio_player.audio_ready_signal.connect(self.set_audio)

    def init_graphics(self):
        x0 = self.x_off
        x1 = self.line_length

        dy = self.max_y/(self.n_lines + 1)
        y = np.arange(0, self.n_lines) * dy + self.line_width

        line_pen = QPen(Qt.GlobalColor.black, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        line_brush = QBrush(self.line_color)
        for k_line in range(self.n_lines):
            audio_line = QGraphicsRectItem(x0, y[k_line] - self.line_width, x1 - x0, self.line_width)
            audio_line.setPen(line_pen)
            audio_line.setBrush(line_brush)
            audio_line.setZValue(0)

            self.addItem(audio_line)
            self.audio_lines.append(audio_line)

        self.scrubber = Scrubber(self.line_width)

        scrubber_brush = QBrush(self.scrubber_color, Qt.BrushStyle.SolidPattern)
        scrubber_pen = QPen(Qt.GlobalColor.black, 1, Qt.PenStyle.SolidLine)
        self.scrubber.setPen(scrubber_pen)
        self.scrubber.setBrush(scrubber_brush)
        self.scrubber.setZValue(2)

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
            max(0, self._calc_marker_index(marker_type=AudioMarker.TYPE.PRACTICE, next_marker=True)),
            self._n_practice_markers)
        self.active_page_marker_index = max(0, self._calc_marker_index(marker_type=AudioMarker.TYPE.PAGE, next_marker=True)-1)

    @staticmethod
    def _sort_markers(marker_list):
        if len(marker_list) > 1:
            marker_coords = [np.sum(marker.scrubber_coords) for marker in marker_list]
            marker_list = [marker_list[k] for k in np.argsort(marker_coords)]
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
        time_ms = self.scrubber_to_time(*marker.scrubber_coords) + self.TIME_TOL_LOW_MS
        self._audio_player.setPosition(time_ms)

    def previous_page(self):
        log.info("Previous Page")
        marker = self.get_previous_page_marker()

        if self._audio_player.isPlaying():
            time_tol = self.TIME_TOL_HIGH_MS
        else:
            time_tol = self.TIME_TOL_LOW_MS

        time_ms = self.scrubber_to_time(*marker.scrubber_coords) - time_tol
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
        time_ms = self.scrubber_to_time(*marker.scrubber_coords) + self.TIME_TOL_LOW_MS
        self._audio_player.setPosition(time_ms)

    def previous_practice_marker(self):
        marker = self.get_previous_practice_marker()
        if self._audio_player.isPlaying():
            time_tol = self.TIME_TOL_HIGH_MS
        else:
            time_tol = self.TIME_TOL_LOW_MS
        time_ms = self.scrubber_to_time(*marker.scrubber_coords) - time_tol
        self._audio_player.setPosition(time_ms)

    def _calc_marker_index(self, marker_type=AudioMarker.TYPE.PRACTICE, next_marker=False):
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

        # Find active marker:
        if len(markers) > 0:
            time_ms = self._audio_player.position()
            line_index, scrubber_index = self.time_to_scrubber(time_ms)
            scrubber_coord = scrubber_index + line_index

            coord_list = [0]
            coord_list.extend([np.sum(marker.scrubber_coords) for marker in markers])
            if marker_type == AudioMarker.TYPE.PRACTICE:
                coord_list.append(self.n_lines)
            marker_coords = np.array(coord_list)
            marker_inds = np.arange(marker_coords.shape[0])
            if marker_type == AudioMarker.TYPE.PRACTICE:
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

    def time_to_scrubber(self, time_ms):
        norm_val = time_ms / self.song_duration
        line_index = max(0, min(int(norm_val * self.n_lines), self.n_lines - 1))
        scrubber_index = max(min(norm_val * self.n_lines - line_index, 1), 0)
        return line_index, scrubber_index

    def scrubber_to_pos(self, line_index, scrubber_index):
        line = self.audio_lines[line_index]
        line: QGraphicsRectItem

        pos_y = line.rect().center().y()
        pos_x = line.rect().x() + line.rect().width() * scrubber_index
        return QPointF(pos_x, pos_y)

    def pos_to_scrubber(self, scene_pos: QPointF):
        line_index = 0
        for line_index in range(self.n_lines):
            audio_line = self.audio_lines[line_index]
            rect = audio_line.rect()
            if line_index == 0 and scene_pos.y() <= rect.bottom():
                break
            elif rect.top() <= scene_pos.y() <= rect.bottom():
                break
        scrubber_index = ((scene_pos.x() - self.audio_lines[line_index].rect().topLeft().x()) /
                          self.audio_lines[line_index].rect().width())
        log.debug(f"{scene_pos} -> {line_index=}, {scrubber_index=}")
        return line_index, scrubber_index

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
        # Take bins at size .2% of 1 second:
        ind_r = int(self._audio_player.current_song.sample_rate * 0.002)
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
            normalized_audio = raw_audio[inds_line] / raw_audio.max()

            # Make bins:
            scrubber_inds_image = (scrubber_inds * image_width * resolution).astype(int)
            split_inds = np.where(np.diff(scrubber_inds_image) == 1)[0]
            bin_split = np.array_split(normalized_audio, split_inds)

            # Average by taking mean in each bin and construct new signal. Normalize to unit range.
            pixel_audio = np.nan_to_num(np.array([np.nanmean(x) for x in bin_split]))
            pixel_audio = pixel_audio / (np.abs(pixel_audio).max() + 1e-15)
            pixel_inds = np.arange(pixel_audio.shape[0])
            pixel_inds = pixel_inds / pixel_inds.max()

            # Lets pack this into an image. First convert to image coordinates:
            pos_y = (pixel_audio + 1) / 2 * image_height
            pos_x = image_width * pixel_inds
            pos = np.column_stack([pos_x, pos_y])

            # Make pixmap and paint the audio image onto the pixmap using QPainter:
            qpixmap = QPixmap(image_width, image_height)
            qpixmap.fill(Qt.GlobalColor.white)
            qpainter = QPainter(qpixmap)
            painter_path = QPainterPath()
            painter_path.moveTo(pos[0, 0], pos[0, 1])
            for xy in pos[1:]:
                painter_path.lineTo(xy[0], xy[1])
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
        marker = AudioMarker(line_index, scrubber_index, self.line_width, marker_type=marker_type)
        log.debug(f"Adding marker at position: {line_index=}, {scrubber_index=}, {marker_type=}")

        marker_line_pos = self.scrubber_to_pos(line_index, scrubber_index)
        marker.setPos(marker_line_pos)
        if marker_type == AudioMarker.TYPE.PRACTICE:
            self._practice_markers.append(marker)
            self.practice_markers = self._practice_markers
        else:
            if self._n_page_markers < self.n_pages:
                marker.page_index = self._n_page_markers + 1
                self._page_markers.append(marker)
                self.page_markers = self._page_markers
            else:
                QMessageBox.information(self.parent(), "Insert Page Marker Failed",
                                        f"Failed to insert page marker because there are already "
                                        f"{self._n_page_markers} of {self.n_pages} possible.")
                return

        for item in marker.items(parent_first=True):
            item.setZValue(1)
            self.addItem(item)

    def remove_marker(self, marker: AudioMarker):
        for item in marker.items(parent_first=False):
            self.removeItem(item)

        marker_ind = self._get_marker_index(marker)

        if marker.marker_type == AudioMarker.TYPE.PRACTICE:
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
        if marker.marker_type == AudioMarker.TYPE.PRACTICE:
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
                self.insert_marker(practice_marker_scrub[0], practice_marker_scrub[1], marker_type=AudioMarker.TYPE.PRACTICE)
            self._load_practice_marker_times = []

        if len(self._load_page_marker_times) > 0:
            marker_scrub_coords = [self.time_to_scrubber(marker_time*self.song_duration)
                                   for marker_time in self._load_page_marker_times]
            for marker_scrub in marker_scrub_coords:
                self.insert_marker(marker_scrub[0], marker_scrub[1], marker_type=AudioMarker.TYPE.PAGE)
            self._load_page_marker_times = []

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

    def clear_markers(self):
        self.scrubber_time_ms = None
        self.clear_practice_markers()
        self.clear_page_markers()

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent):
        clicked_item = self.itemAt(event.scenePos(), self.parent().transform())

        # Determine item type:
        item_type = GraphicsType.get_item_type(clicked_item)
        is_marker = item_type == GraphicsType.MARKER.name
        is_audio_line = item_type == GraphicsType.LINE.name
        is_scrubber = item_type == GraphicsType.SCRUBBER.name
        log.info(f"mousePressEvent: {item_type=}, {clicked_item=}")

        # If marker and we are in marker adjust mode, then send the event to the marker.
        if is_marker and self._unlock_markers:
            clicked_item.mousePressEvent(event)
            return

        # If not marker, scrubber, or line, add a marker at the scrubber if left-click + shift/ctrl
        if not (is_marker or is_audio_line):
            time_ms = self._audio_player.position()
            line_index, scrubber_index = self.time_to_scrubber(time_ms)
            if (event.modifiers() == Qt.KeyboardModifier.ShiftModifier and
                    event.button() == Qt.MouseButton.LeftButton):
                # insert audio marker at line index and scrubber index
                self.insert_marker(line_index, scrubber_index, marker_type=AudioMarker.TYPE.PRACTICE)
            elif (event.modifiers() == Qt.KeyboardModifier.ControlModifier and
                  event.button() == Qt.MouseButton.LeftButton):
                self.insert_marker(line_index, scrubber_index, marker_type=AudioMarker.TYPE.PAGE)
            return

        # If line and shift+click, add Practice Marker
        # If line and ctrl+click, add Page Marker
        # If marker and shift+click OR ctrl+click, delete marker
        shift_left_click = (event.modifiers() == Qt.KeyboardModifier.ShiftModifier and
                            event.button() == Qt.MouseButton.LeftButton)
        ctrl_left_click = (event.modifiers() == Qt.KeyboardModifier.ControlModifier and
                           event.button() == Qt.MouseButton.LeftButton)
        right_click = ((event.modifiers() == Qt.KeyboardModifier.ControlModifier or
                       event.modifiers() == Qt.KeyboardModifier.ShiftModifier) and
                       event.button() == Qt.MouseButton.RightButton)
        line_index, scrubber_index = self.pos_to_scrubber(event.scenePos())
        if (is_audio_line or is_scrubber or is_marker) and shift_left_click:
            self.insert_marker(line_index, scrubber_index, marker_type=AudioMarker.TYPE.PRACTICE)
            return
        elif (is_audio_line or is_scrubber or is_marker) and ctrl_left_click:
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
        clicked_item = self.itemAt(event.scenePos(), self.parent().transform())
        item_type = GraphicsType.get_item_type(clicked_item)
        log.info(f"mouseMoveEvent: {item_type=}, {clicked_item=}")

        if self._unlock_markers and item_type == GraphicsType.MARKER.name:
            clicked_item.scrubber_coords = self.pos_to_scrubber(event.scenePos())
            return

        left_click = (Qt.MouseButton.LeftButton in event.buttons() and event.modifiers() == Qt.KeyboardModifier.NoModifier)
        if left_click and item_type in [GraphicsType.LINE.name, GraphicsType.SCRUBBER.name]:
            # Determine line and scrubber index:
            self._set_audio_player_scrubber_coords(*self.pos_to_scrubber(event.scenePos()))

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent):
        clicked_item = self.itemAt(event.scenePos(), self.parent().transform())
        item_type = GraphicsType.get_item_type(clicked_item)
        log.info(f"mouseReleaseEvent: {item_type=}, {clicked_item=}")

        if self._unlock_markers and item_type == GraphicsType.MARKER.name:
            clicked_item.scrubber_coords = self.pos_to_scrubber(event.scenePos())
            return

        left_click = (event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.NoModifier)
        if left_click and item_type in [GraphicsType.LINE.name, GraphicsType.SCRUBBER.name]:
            # Determine line and scrubber index:
            self._set_audio_player_scrubber_coords(*self.pos_to_scrubber(event.scenePos()))

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

        if add_practice_marker:
            # insert practice marker at line index and scrubber index
            self.insert_marker(line_index, scrubber_index, marker_type=AudioMarker.TYPE.PRACTICE)
        elif add_page_marker:
            self.insert_marker(line_index, scrubber_index, marker_type=AudioMarker.TYPE.PAGE)

