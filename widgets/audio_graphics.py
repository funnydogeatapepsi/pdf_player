from enum import Enum
from typing import TYPE_CHECKING, Union
import logging

from PySide6.QtWidgets import QGraphicsSceneMouseEvent, QGraphicsPathItem, QGraphicsItemGroup, QGraphicsRectItem, \
    QGraphicsTextItem, QGraphicsSimpleTextItem, QGraphicsScene
from PySide6.QtCore import Qt, QPointF, Signal
from PySide6.QtGui import QPen, QBrush, QColor, QPainterPath, QPixmap, QPainter, QKeyEvent, QFont

if TYPE_CHECKING:
    from .graphics import GraphicsScene

log = logging.getLogger(__name__)


class AudioMarkerType(Enum):
    PRACTICE = 0
    PAGE = 1
    TEMPO = 2


class AudioMarker(QGraphicsItemGroup):
    TYPE = AudioMarkerType
    practice_color = Qt.GlobalColor.red
    page_color = Qt.GlobalColor.blue
    tempo_color = Qt.GlobalColor.darkGreen

    show_marker_style = QColor(250, 0, 0, 100)
    SHOW_MARKER = True

    _mouse_down = False
    _page_index: int = None

    _arrow_height = 23
    _arrow_width = int(_arrow_height * 0.6)
    _vertical_inset = - int(_arrow_height * 0.5)

    def __init__(self, norm_time: float, line_width=10, marker_type=AudioMarkerType.PRACTICE):
        """
        :param norm_time:   position in the song, normalised to [0, 1]. This is the marker's source of truth; its
                            scene position is derived from it by the GraphicsScene (and changes with zoom).
        """
        super().__init__()

        # Setup Graphics:
        color = {AudioMarkerType.PRACTICE: self.practice_color,
                 AudioMarkerType.PAGE: self.page_color,
                 AudioMarkerType.TEMPO: self.tempo_color}[marker_type]
        pen = QPen(Qt.GlobalColor.black, 2, Qt.PenStyle.SolidLine)
        brush = QBrush(color, Qt.BrushStyle.SolidPattern)

        width, height = max(2, int(line_width / 10)), int(line_width)
        hitbox = QGraphicsRectItem(0, 0, width, height)
        hitbox.setPen(QPen(Qt.PenStyle.NoPen))
        if not self.SHOW_MARKER:
            self.show_marker_style = Qt.BrushStyle.NoBrush
        hitbox.setBrush(QBrush(self.show_marker_style))
        hitbox.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        hitbox.setAcceptHoverEvents(False)
        hitbox.setFlag(self.GraphicsItemFlag.ItemStacksBehindParent)

        arrow_width = self._arrow_width
        arrow_height = self._arrow_height
        vertical_inset = self._vertical_inset
        arrow_tip_y = line_width/2 + vertical_inset

        self.arrow_left = QPointF(width/2 - arrow_width,  arrow_tip_y - arrow_height)
        self.arrow_tip = QPointF(width/2, arrow_tip_y)
        self.arrow_right = QPointF(width/2 + arrow_width, arrow_tip_y - arrow_height)
        self.arrow_center = QPointF(width/2, arrow_tip_y - 2/3*arrow_height)
        painter_path = QPainterPath()
        painter_path.moveTo(self.arrow_left)
        painter_path.lineTo(self.arrow_tip)
        painter_path.lineTo(self.arrow_right)
        painter_path.lineTo(self.arrow_left)
        marker_arrow = QGraphicsPathItem(painter_path)
        marker_arrow.setPen(pen)
        marker_arrow.setBrush(brush)
        marker_arrow.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        marker_arrow.setAcceptHoverEvents(False)
        marker_arrow.setFlag(self.GraphicsItemFlag.ItemStacksBehindParent)

        self.hitbox = hitbox
        self.marker_arrow = marker_arrow
        self._norm_time = float(norm_time)
        self.marker_type = marker_type
        self._label_item = None
        # Tempo markers: tempo in effect from this marker until the next one.
        self.bpm: float = 120.0
        self.beats_per_bar: int = 4

        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setAcceptHoverEvents(False)

        self.addToGroup(hitbox)
        self.addToGroup(marker_arrow)

    def items(self, parent_first=False):
        items = self.childItems()
        if parent_first:
            items.insert(0, self)
        else:
            items.append(self)
        return items

    def scene(self) -> Union['GraphicsScene', QGraphicsScene]:
        return super().scene()

    @property
    def norm_time(self) -> float:
        """Position in the song, normalised to [0, 1]."""
        return self._norm_time

    @norm_time.setter
    def norm_time(self, value: float):
        self._norm_time = float(max(0.0, min(1.0, value)))
        self.update_position()

    def update_position(self):
        """Move the graphics to wherever the scene currently draws this marker's time (call after a zoom change)."""
        scene = self.scene()
        if scene is not None:
            self.setPos(scene.norm_to_pos(self._norm_time))

    @property
    def scrubber_coords(self):
        """(line_index, scrubber_index) at the scene's current zoom. Derived; kept for logging/compatibility."""
        scene = self.scene()
        if scene is None:
            return 0, self._norm_time
        return scene.norm_to_scrubber(self._norm_time)

    @property
    def page_index(self):
        return self._page_index

    @page_index.setter
    def page_index(self, page_index_):
        self._page_index = int(page_index_)
        self.set_label(str(self._page_index))

    def set_tempo(self, bpm: float, beats_per_bar: int):
        self.bpm = float(bpm)
        self.beats_per_bar = int(beats_per_bar)
        self.set_label(f"{self.beats_per_bar}| {self.bpm:g}")   # beats per bar | bpm

    def set_label(self, text: str):
        """Show a short text next to the arrow (page number, tempo, ...)."""
        self.clear_child_items()
        label = QGraphicsSimpleTextItem(text)
        label.setParentItem(self.marker_arrow)
        label.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        label.setAcceptHoverEvents(False)
        label.setPen(QPen(Qt.GlobalColor.black, 1))
        label.setBrush(QBrush(Qt.GlobalColor.white))
        label.setFont(QFont("Times", 12, QFont.Weight.Bold))
        self._label_item = label

    def setPos(self, pos: QPointF):
        marker_rect = self.hitbox.boundingRect()
        super().setPos(pos - QPointF(marker_rect.width()/2, marker_rect.height()/2))

    def clear_child_items(self):
        child_items = self.marker_arrow.childItems()
        if child_items and len(child_items) > 0 and self.scene():
            for item in child_items:
                self.scene().removeItem(item)


class Scrubber(QGraphicsRectItem):
    _mouse_down = True

    def __init__(self, line_width):
        super().__init__(0, 0, 5, int(line_width + 2))
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setAcceptTouchEvents(False)
        self.setAcceptHoverEvents(False)

    def scene(self) -> 'GraphicsScene':
        return super().scene()

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        rect = self.boundingRect()
        if self._mouse_down:
            rect = rect.adjusted(-1000, 0, 1000, 0)
        path.addRect(rect)
        return path

    def setPos(self, pos: QPointF):
        super().setPos(pos - QPointF(self.boundingRect().width()/2, self.boundingRect().height()/2))


class GraphicsType(Enum):
    SCRUBBER = Scrubber
    MARKER = AudioMarker
    LINE = QGraphicsRectItem

    @classmethod
    def get_all_items(cls):
        return cls.__members__.values()

    @classmethod
    def get_all_types(cls):
        return tuple(val.name for val in cls.get_all_items())

    @classmethod
    def get_all_classes(cls):
        return tuple(val.value for val in cls.get_all_items())

    @classmethod
    def _get_item_ind(cls, item):
        item_ind = None
        for k_item, item_class in enumerate(cls.get_all_classes()):
            if isinstance(item, item_class):
                item_ind = k_item
                return item_ind
        return item_ind

    @classmethod
    def get_item_type(cls, item):
        item_type = None
        item_ind = cls._get_item_ind(item)
        if item_ind is not None:    # index 0 (SCRUBBER) is a valid match
            item_type = cls.get_all_types()[item_ind]
        return item_type
