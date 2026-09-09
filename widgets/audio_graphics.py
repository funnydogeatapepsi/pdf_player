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


class AudioMarker(QGraphicsItemGroup):
    TYPE = AudioMarkerType
    practice_color = Qt.GlobalColor.red
    page_color = Qt.GlobalColor.blue

    show_marker_style = QColor(250, 0, 0, 100)
    SHOW_MARKER = True

    _mouse_down = False
    _page_index: int = None

    _arrow_height = 23
    _arrow_width = int(_arrow_height * 0.6)
    _vertical_inset = - int(_arrow_height * 0.5)

    def __init__(self, line_index, scrubber_index,  line_width=10, marker_type=AudioMarkerType.PRACTICE):
        super().__init__()

        # Setup Graphics:
        color = self.practice_color if marker_type == AudioMarkerType.PRACTICE else self.page_color
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
        self._scrubber_coords = (line_index, scrubber_index)
        self.marker_type = marker_type

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
    def scrubber_coords(self):
        return self._scrubber_coords

    @scrubber_coords.setter
    def scrubber_coords(self, coord_tuple: tuple):
        self._scrubber_coords = coord_tuple
        self.setPos(self.scene().scrubber_to_pos(*coord_tuple))

    @property
    def page_index(self):
        return self._page_index

    @page_index.setter
    def page_index(self, page_index_):
        self._page_index = int(page_index_)

        self.clear_child_items()
        number_graphics = QGraphicsSimpleTextItem(str(self._page_index))
        number_graphics.setParentItem(self.marker_arrow)
        number_graphics.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        number_graphics.setAcceptHoverEvents(False)
        number_graphics.setPen(QPen(Qt.GlobalColor.black, 1))
        number_graphics.setBrush(QBrush(Qt.GlobalColor.white))
        number_graphics.setFont(QFont("Times", 12, QFont.Weight.Bold))

    def setPos(self, pos: QPointF):
        marker_rect = self.hitbox.boundingRect()
        super().setPos(pos - QPointF(marker_rect.width()/2, marker_rect.height()/2))

    def clear_child_items(self):
        child_items = self.marker_arrow.childItems()
        if child_items and len(child_items) > 0 and self.scene():
            for item in child_items:
                self.scene().removeItem(item)


class Scrubber(QGraphicsRectItem):
    _scrubber_coords = (0, 0)
    _mouse_down = True

    def __init__(self, line_width):
        super().__init__(0, 0, 5, int(line_width + 2))
        self.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.setAcceptTouchEvents(False)
        self.setAcceptHoverEvents(False)

    def scene(self) -> 'GraphicsScene':
        return super().scene()

    @property
    def scrubber_coords(self):
        return self._scrubber_coords

    @scrubber_coords.setter
    def scrubber_coords(self, coord_tuple: tuple):
        self._scrubber_coords = coord_tuple
        self.setPos(self.scene().scrubber_to_pos(*coord_tuple))

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
