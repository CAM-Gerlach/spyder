# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Copyright © Spyder Project Contributors
#
# Licensed under the terms of the MIT License
# (see spyder/__init__.py for details)
# ----------------------------------------------------------------------------

"""
Collections (i.e. dictionary, list, set and tuple) editor widget and dialog.
"""

#TODO: Multiple selection: open as many editors (array/dict/...) as necessary,
#      at the same time

# pylint: disable=C0103
# pylint: disable=R0903
# pylint: disable=R0911
# pylint: disable=R0201

# Standard library imports
import datetime
from functools import lru_cache
import io
import re
import sys
import textwrap
from typing import Any, Callable, Optional
import warnings

# Third party imports
from qtpy.compat import getsavefilename, to_qvariant
from qtpy.QtCore import (
    QAbstractTableModel, QItemSelectionModel, QModelIndex, Qt, QTimer, Signal,
    Slot)
from qtpy.QtGui import QColor, QKeySequence
from qtpy.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from spyder_kernels.utils.lazymodules import (
    FakeObject, numpy as np, pandas as pd, PIL)
from spyder_kernels.utils.misc import fix_reference_name
from spyder_kernels.utils.nsview import (
    display_to_value, get_human_readable_type, get_numeric_numpy_types,
    get_numpy_type_string, get_object_attrs, get_size, get_type_string,
    sort_against, try_to_eval, unsorted_unique, value_to_display
)

# Local imports
from spyder.api.fonts import SpyderFontsMixin, SpyderFontType
from spyder.api.widgets.mixins import SpyderWidgetMixin
from spyder.config.base import _, running_under_pytest
from spyder.py3compat import (is_binary_string, to_text_string,
                              is_type_text_string)
from spyder.utils.icon_manager import ima
from spyder.utils.misc import getcwd_or_home
from spyder.utils.qthelpers import mimedata2url
from spyder.utils.stringmatching import get_search_scores, get_search_regex
from spyder.plugins.variableexplorer.widgets.collectionsdelegate import (
    CollectionsDelegate,
    SELECT_ROW_BUTTON_SIZE,
)
from spyder.plugins.variableexplorer.widgets.importwizard import ImportWizard
from spyder.widgets.helperwidgets import CustomSortFilterProxy
from spyder.plugins.variableexplorer.widgets.basedialog import BaseDialog
from spyder.utils.palette import SpyderPalette
from spyder.utils.stylesheet import AppStyle, MAC


# =============================================================================
# ---- Constants
# =============================================================================
class CollectionsEditorActions:
    Close = 'close'
    Copy = 'copy_action'
    Duplicate = 'duplicate_action'
    Edit = 'edit_action'
    Histogram = 'histogram_action'
    Insert = 'insert_action'
    InsertAbove = 'insert_above_action'
    InsertBelow = 'insert_below_action'
    Paste = 'paste_action'
    Plot = 'plot_action'
    Refresh = 'refresh_action'
    Remove = 'remove_action'
    Rename = 'rename_action'
    ResizeColumns = 'resize_columns_action'
    ResizeRows = 'resize_rows_action'
    Save = 'save_action'
    ShowImage = 'show_image_action'
    ViewObject = 'view_object_action'


class CollectionsEditorMenus:
    Context = 'context_menu'
    ContextIfEmpty = 'context_menu_if_empty'
    ConvertTo = 'convert_to_submenu'
    Header = 'header_context_menu'
    Index = 'index_context_menu'
    Options = 'options_menu'


class CollectionsEditorWidgets:
    OptionsToolButton = 'options_button_widget'
    Toolbar = 'toolbar'
    ToolbarStretcher = 'toolbar_stretcher'


class CollectionsEditorContextMenuSections:
    Edit = 'edit_section'
    AddRemove = 'add_remove_section'
    View = 'view_section'


class CollectionsEditorToolbarSections:
    AddDelete = 'add_delete_section'
    ViewAndRest = 'view_section'


# Maximum length of a serialized variable to be set in the kernel
MAX_SERIALIZED_LENGHT = 1e6

# To handle large collections
LARGE_NROWS = 100
ROWS_TO_LOAD = 50

# Numeric types
NUMERIC_TYPES = (int, float) + get_numeric_numpy_types()


# =============================================================================
# ---- Utility functions and classes
# =============================================================================
def natsort(s):
    """
    Natural sorting, e.g. test3 comes before test100.
    Taken from https://stackoverflow.com/a/16090640/3110740
    """
    if not isinstance(s, (str, bytes)):
        return s
    x = [int(t) if t.isdigit() else t.lower() for t in re.split('([0-9]+)', s)]
    return x


class ProxyObject(object):
    """Dictionary proxy to an unknown object."""

    def __init__(self, obj):
        """Constructor."""
        self.__obj__ = obj

    def __len__(self):
        """Get len according to detected attributes."""
        return len(get_object_attrs(self.__obj__))

    def __getitem__(self, key):
        """Get the attribute corresponding to the given key."""
        # Catch NotImplementedError to fix spyder-ide/spyder#6284 in pandas
        # MultiIndex due to NA checking not being supported on a multiindex.
        # Catch AttributeError to fix spyder-ide/spyder#5642 in certain special
        # classes like xml when this method is called on certain attributes.
        # Catch TypeError to prevent fatal Python crash to desktop after
        # modifying certain pandas objects. Fix spyder-ide/spyder#6727.
        # Catch ValueError to allow viewing and editing of pandas offsets.
        # Fix spyder-ide/spyder#6728-
        try:
            attribute_toreturn = getattr(self.__obj__, key)
        except (NotImplementedError, AttributeError, TypeError, ValueError):
            attribute_toreturn = None
        return attribute_toreturn

    def __setitem__(self, key, value):
        """Set attribute corresponding to key with value."""
        # Catch AttributeError to gracefully handle inability to set an
        # attribute due to it not being writeable or set-table.
        # Fix spyder-ide/spyder#6728.
        # Also, catch NotImplementedError for safety.
        try:
            setattr(self.__obj__, key, value)
        except (TypeError, AttributeError, NotImplementedError):
            pass
        except Exception as e:
            if "cannot set values for" not in str(e):
                raise


# =============================================================================
# ---- Widgets
# =============================================================================
class ReadOnlyCollectionsModel(QAbstractTableModel, SpyderFontsMixin):
    """CollectionsEditor Read-Only Table Model"""

    sig_setting_data = Signal()

    def __init__(self, parent, data, title="", names=False,
                 minmax=False, remote=False):
        QAbstractTableModel.__init__(self, parent)
        if data is None:
            data = {}
        self._parent = parent
        self.scores = []
        self.names = names
        self.minmax = minmax
        self.remote = remote
        self.header0 = None
        self.previous_sort = -1
        self._data = None
        self.total_rows = None
        self.showndata = None
        self.keys = None
        self.title = to_text_string(title)  # in case title is not a string
        if self.title:
            self.title = self.title + ' - '
        self.sizes = []
        self.types = []
        self.set_data(data)

    def get_data(self):
        """Return model data"""
        return self._data

    def set_data(self, data, coll_filter=None):
        """Set model data"""
        self._data = data

        if (
            coll_filter is not None
            and not self.remote 
            and isinstance(data, (tuple, list, dict, set, frozenset))
        ):
            data = coll_filter(data)
        self.showndata = data

        self.header0 = _("Index")
        if self.names:
            self.header0 = _("Name")
        if isinstance(data, tuple):
            self.keys = list(range(len(data)))
            self.title += _("Tuple")
        elif isinstance(data, list):
            self.keys = list(range(len(data)))
            self.title += _("List")
        elif isinstance(data, set):
            self.keys = list(range(len(data)))
            self.title += _("Set")
            self._data = list(data)
        elif isinstance(data, frozenset):
            self.keys = list(range(len(data)))
            self.title += _("Frozenset")
            self._data = list(data)
        elif isinstance(data, dict):
            self.keys = list(data.keys())
            self.title += _("Dictionary")
            if not self.names:
                self.header0 = _("Key")
        else:
            self.keys = get_object_attrs(data)
            self._data = data = self.showndata = ProxyObject(data)
            if not self.names:
                self.header0 = _("Attribute")

        if not isinstance(self._data, ProxyObject):
            if len(self.keys) > 1:
                elements = _("elements")
            else:
                elements = _("element")
            self.title += (' (' + str(len(self.keys)) + ' ' + elements + ')')
        else:
            data_type = get_type_string(data)
            self.title += data_type

        self.total_rows = len(self.keys)
        if self.total_rows > LARGE_NROWS:
            self.rows_loaded = ROWS_TO_LOAD
        else:
            self.rows_loaded = self.total_rows

        self.sig_setting_data.emit()
        self.set_size_and_type()

        if len(self.keys):
            # Needed to update search scores when
            # adding values to the namespace
            self.update_search_letters()

        self.reset()

    def set_size_and_type(self, start=None, stop=None):
        data = self._data

        if start is None and stop is None:
            start = 0
            stop = self.rows_loaded
            fetch_more = False
        else:
            fetch_more = True

        # Ignore pandas warnings that certain attributes are deprecated
        # and will be removed, since they will only be accessed if they exist.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message=(r"^\w+\.\w+ is deprecated and "
                                   "will be removed in a future version"))
            if self.remote:
                sizes = [data[self.keys[index]]['size']
                         for index in range(start, stop)]
                types = [data[self.keys[index]]['type']
                         for index in range(start, stop)]
            else:
                sizes = [get_size(data[self.keys[index]])
                         for index in range(start, stop)]
                types = [get_human_readable_type(data[self.keys[index]])
                         for index in range(start, stop)]

        if fetch_more:
            self.sizes = self.sizes + sizes
            self.types = self.types + types
        else:
            self.sizes = sizes
            self.types = types

    def load_all(self):
        """Load all the data."""
        self.fetchMore(number_to_fetch=self.total_rows)

    def sort(
        self,
        column: int,
        order: Qt.SortOrder = Qt.AscendingOrder
    ) -> None:
        """
        Sort model by given column and order.

        This overrides the method in the base class and it's called by Qt if
        the user clicks on the header of a column.

        If the collection editor shows a dictionary and the user changes the
        order of a column from descending to ascending, then instead sort the
        dict by insertion order. The effect is that clicking on a column
        header switches between three states: that column is sorting in
        ascending order, then in descending order, and then the dict reverts
        to its natural state, which is sorted by insertion order. This
        implementation is a bit of a hack. In Qt 6, the flag
        `sortIndicatorClearable` in `QHeaderView` has the same effect.

        If a view uses a proxy model, then the sort function of the proxy model
        overrides this function and the special handling of dictionaries
        described in the previous paragraph does not happen. This happens in
        `RemoteCollectionsEditorTableView` which is used in `NamespaceBrowser`.

        Parameters
        ----------
        column : int
            The column to sort. If column is -1 then return the model to its
            unsorted state.
        order : Qt.SortOrder, optional
            Whether to sort in ascending or descending order. The default is
            Qt.AscendingOrder.
        """

        def all_string(listlike):
            return all([isinstance(x, str) for x in listlike])

        try:
            header = self._parent.horizontalHeader()
        except AttributeError:
            # may happen in tests
            header = None

        if (
            header
            and order == Qt.AscendingOrder
            and column != -1
            and self.previous_sort == column
            and isinstance(self._data, dict)
        ):
            header.setSortIndicator(-1, Qt.AscendingOrder)
            return

        self.previous_sort = column
        reverse = (order == Qt.DescendingOrder)
        sort_key = natsort if all_string(self.keys) else None

        if column == -1:
            self.keys = list(self._data.keys())
            self.set_size_and_type()
        elif column == 0:
            self.sizes = sort_against(self.sizes, self.keys,
                                      reverse=reverse,
                                      sort_key=natsort)
            self.types = sort_against(self.types, self.keys,
                                      reverse=reverse,
                                      sort_key=natsort)
            try:
                self.keys.sort(reverse=reverse, key=sort_key)
            except:
                pass
        elif column == 1:
            self.keys[:self.rows_loaded] = sort_against(self.keys,
                                                        self.types,
                                                        reverse=reverse)
            self.sizes = sort_against(self.sizes, self.types, reverse=reverse)
            try:
                self.types.sort(reverse=reverse)
            except:
                pass
        elif column == 2:
            self.keys[:self.rows_loaded] = sort_against(self.keys,
                                                        self.sizes,
                                                        reverse=reverse)
            self.types = sort_against(self.types, self.sizes, reverse=reverse)
            try:
                self.sizes.sort(reverse=reverse)
            except:
                pass
        elif column in [3, 4]:
            values = [self._data[key] for key in self.keys]
            self.keys = sort_against(self.keys, values, reverse=reverse)
            self.sizes = sort_against(self.sizes, values, reverse=reverse)
            self.types = sort_against(self.types, values, reverse=reverse)
        self.beginResetModel()
        self.endResetModel()

    def columnCount(self, qindex=QModelIndex()):
        """Array column number"""
        if self._parent.proxy_model:
            return 5
        else:
            return 4

    def rowCount(self, index=QModelIndex()):
        """Array row number"""
        if self.total_rows <= self.rows_loaded:
            return self.total_rows
        else:
            return self.rows_loaded

    def canFetchMore(self, index=QModelIndex()):
        if self.total_rows > self.rows_loaded:
            return True
        else:
            return False

    def fetchMore(self, index=QModelIndex(), number_to_fetch=None):
        # fetch more data
        reminder = self.total_rows - self.rows_loaded
        if reminder <= 0:
            # Everything is loaded
            return
        if number_to_fetch is not None:
            items_to_fetch = min(reminder, number_to_fetch)
        else:
            items_to_fetch = min(reminder, ROWS_TO_LOAD)
        self.set_size_and_type(self.rows_loaded,
                               self.rows_loaded + items_to_fetch)
        self.beginInsertRows(QModelIndex(), self.rows_loaded,
                             self.rows_loaded + items_to_fetch - 1)
        self.rows_loaded += items_to_fetch
        self.endInsertRows()

    def get_index_from_key(self, key):
        try:
            return self.createIndex(self.keys.index(key), 0)
        except (RuntimeError, ValueError):
            return QModelIndex()

    def get_key(self, index):
        """Return current key"""
        return self.keys[index.row()]

    def get_value(self, index):
        """Return current value"""
        if index.column() == 0:
            return self.keys[index.row()]
        elif index.column() == 1:
            return self.types[index.row()]
        elif index.column() == 2:
            return self.sizes[index.row()]
        else:
            return self._data[self.keys[index.row()]]

    def get_bgcolor(self, index):
        """Background color depending on value"""
        if index.column() == 0:
            color = QColor(Qt.lightGray)
            color.setAlphaF(.05)
        elif index.column() < 3:
            color = QColor(Qt.lightGray)
            color.setAlphaF(.2)
        else:
            color = QColor(Qt.lightGray)
            color.setAlphaF(.3)
        return color

    def update_search_letters(self, text=""):
        """Update search letters with text input in search box."""
        self.letters = text
        names = [str(key) for key in self.keys]
        results = get_search_scores(text, names, template='<b>{0}</b>')
        if results:
            self.normal_text, _, self.scores = zip(*results)
            self.reset()

    def row_key(self, row_num):
        """
        Get row name based on model index.
        Needed for the custom proxy model.
        """
        return self.keys[row_num]

    def row_type(self, row_num):
        """
        Get row type based on model index.
        Needed for the custom proxy model.
        """
        return self.types[row_num]

    def data(self, index, role=Qt.DisplayRole):
        """Cell content"""
        if not index.isValid():
            return to_qvariant()
        value = self.get_value(index)
        if index.column() == 4 and role == Qt.DisplayRole:
            # TODO: Check the effect of not hiding the column
            # Treating search scores as a table column simplifies the
            # sorting once a score for a specific string in the finder
            # has been defined. This column however should always remain
            # hidden.
            return to_qvariant(self.scores[index.row()])
        if index.column() == 3 and self.remote:
            value = value['view']
        if index.column() == 3:
            display = value_to_display(value, minmax=self.minmax)
        else:
            if is_type_text_string(value):
                display = to_text_string(value, encoding="utf-8")
            elif not isinstance(value, NUMERIC_TYPES):
                display = to_text_string(value)
            else:
                display = value
        if role == Qt.ToolTipRole:
            if self.parent().over_select_row_button:
                if index.row() in self.parent().selected_rows():
                    tooltip = _("Click to deselect this row")
                else:
                    tooltip = _(
                        "Click to select this row. Maintain pressed Ctrl (Cmd "
                        "on macOS) for multiple rows"
                    )
                return '\n'.join(textwrap.wrap(tooltip, 50))
            return display
        if role == Qt.UserRole:
            if isinstance(value, NUMERIC_TYPES):
                return to_qvariant(value)
            else:
                return to_qvariant(display)
        elif role == Qt.DisplayRole:
            return to_qvariant(display)
        elif role == Qt.EditRole:
            return to_qvariant(value_to_display(value))
        elif role == Qt.TextAlignmentRole:
            if index.column() == 3:
                if len(display.splitlines()) < 3:
                    return to_qvariant(int(Qt.AlignLeft | Qt.AlignVCenter))
                else:
                    return to_qvariant(int(Qt.AlignLeft | Qt.AlignTop))
            else:
                return to_qvariant(int(Qt.AlignLeft | Qt.AlignVCenter))
        elif role == Qt.BackgroundColorRole:
            return to_qvariant(self.get_bgcolor(index))
        elif role == Qt.FontRole:
            return to_qvariant(
                self.get_font(SpyderFontType.MonospaceInterface)
            )
        return to_qvariant()

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        """Overriding method headerData"""
        if role != Qt.DisplayRole:
            return to_qvariant()
        i_column = int(section)
        if orientation == Qt.Horizontal:
            headers = (self.header0, _("Type"), _("Size"), _("Value"),
                       _("Score"))
            return to_qvariant(headers[i_column])
        else:
            return to_qvariant()

    def flags(self, index):
        """Overriding method flags"""
        # This method was implemented in CollectionsModel only, but to enable
        # tuple exploration (even without editing), this method was moved here
        if not index.isValid():
            return Qt.ItemFlag.ItemIsEnabled
        return (
            QAbstractTableModel.flags(self, index) | Qt.ItemFlag.ItemIsEditable
        )

    def reset(self):
        self.beginResetModel()
        self.endResetModel()


class CollectionsModel(ReadOnlyCollectionsModel):
    """Collections Table Model"""

    def set_value(self, index, value):
        """Set value"""
        self._data[self.keys[index.row()]] = value
        self.showndata[self.keys[index.row()]] = value
        self.sizes[index.row()] = get_size(value)
        self.types[index.row()] = get_human_readable_type(value)
        self.sig_setting_data.emit()

    def type_to_color(self, python_type, numpy_type):
        """Get the color that corresponds to a Python type."""
        # Color for unknown types
        color = SpyderPalette.GROUP_12

        if numpy_type != 'Unknown':
            if numpy_type == 'Array':
                color = SpyderPalette.GROUP_9
            elif numpy_type == 'Scalar':
                color = SpyderPalette.GROUP_2
        elif python_type == 'bool':
            color = SpyderPalette.GROUP_1
        elif python_type in ['int', 'float', 'complex']:
            color = SpyderPalette.GROUP_2
        elif python_type in ['str', 'unicode']:
            color = SpyderPalette.GROUP_3
        elif 'datetime' in python_type:
            color = SpyderPalette.GROUP_4
        elif python_type == 'list':
            color = SpyderPalette.GROUP_5
        elif python_type in ['set', 'frozenset']:
            color = SpyderPalette.GROUP_6
        elif python_type == 'tuple':
            color = SpyderPalette.GROUP_7
        elif python_type == 'dict':
            color = SpyderPalette.GROUP_8
        elif python_type in ['MaskedArray', 'Matrix', 'NDArray']:
            color = SpyderPalette.GROUP_9
        elif (python_type in ['DataFrame', 'Series'] or
                'Index' in python_type):
            color = SpyderPalette.GROUP_10
        elif python_type == 'PIL.Image.Image':
            color = SpyderPalette.GROUP_11
        else:
            color = SpyderPalette.GROUP_12

        return color

    def get_bgcolor(self, index):
        """Background color depending on value."""
        value = self.get_value(index)
        if index.column() < 3:
            color = ReadOnlyCollectionsModel.get_bgcolor(self, index)
        else:
            if self.remote:
                python_type = value['python_type']
                numpy_type = value['numpy_type']
            else:
                python_type = get_type_string(value)
                numpy_type = get_numpy_type_string(value)
            color_name = self.type_to_color(python_type, numpy_type)
            color = QColor(color_name)
            color.setAlphaF(0.5)
        return color

    def setData(self, index, value, role=Qt.EditRole):
        """Cell content change"""
        if not index.isValid():
            return False
        if index.column() < 3:
            return False
        value = display_to_value(value, self.get_value(index),
                                 ignore_errors=True)
        self.set_value(index, value)
        self.dataChanged.emit(index, index)
        return True


class BaseHeaderView(QHeaderView):
    """
    A header view for the BaseTableView that emits a signal when the width of
    one of its sections is resized by the user.
    """
    sig_user_resized_section = Signal(int, int, int)

    def __init__(self, parent=None):
        super(BaseHeaderView, self).__init__(Qt.Horizontal, parent)
        self._handle_section_is_pressed = False
        self.sectionResized.connect(self.sectionResizeEvent)
        # Needed to enable sorting by column
        # See spyder-ide/spyder#9835
        self.setSectionsClickable(True)

    def mousePressEvent(self, e):
        super(BaseHeaderView, self).mousePressEvent(e)
        self._handle_section_is_pressed = (self.cursor().shape() ==
                                           Qt.SplitHCursor)

    def mouseReleaseEvent(self, e):
        super(BaseHeaderView, self).mouseReleaseEvent(e)
        self._handle_section_is_pressed = False

    def sectionResizeEvent(self, logicalIndex, oldSize, newSize):
        if self._handle_section_is_pressed:
            self.sig_user_resized_section.emit(logicalIndex, oldSize, newSize)


class BaseTableView(QTableView, SpyderWidgetMixin):
    """Base collection editor table view"""
    CONF_SECTION = 'variable_explorer'

    sig_files_dropped = Signal(list)
    redirect_stdio = Signal(bool)
    sig_free_memory_requested = Signal()
    sig_editor_creation_started = Signal()
    sig_editor_shown = Signal()

    def __init__(self, parent):
        super().__init__(parent=parent)

        # Main attributes
        self.array_filename = None
        self.menu = None
        self.empty_ws_menu = None
        self.paste_action = None
        self.copy_action = None
        self.edit_action = None
        self.plot_action = None
        self.hist_action = None
        self.imshow_action = None
        self.save_array_action = None
        self.insert_action = None
        self.insert_action_above = None
        self.insert_action_below = None
        self.remove_action = None
        self.minmax_action = None
        self.rename_action = None
        self.duplicate_action = None
        self.view_action = None
        self.resize_action = None
        self.resize_columns_action = None
        self.delegate = None
        self.proxy_model = None
        self.source_model = None
        self.setAcceptDrops(True)
        self.automatic_column_width = True

        # Headder attributes
        self.setHorizontalHeader(BaseHeaderView(parent=self))
        self.horizontalHeader().sig_user_resized_section.connect(
            self.user_resize_columns)

        # There is no need for us to show this header because we're not using
        # it to show any information on it.
        self.verticalHeader().hide()

        # To use mouseMoveEvent
        self.setMouseTracking(True)

        # Delay editing values for a bit so that when users do a double click
        # (the default behavior for editing since Spyder was created; now they
        # only have to do a single click), our editor dialogs are focused.
        self.__index_clicked = None
        self._edit_value_timer = QTimer(self)
        self._edit_value_timer.setInterval(100)
        self._edit_value_timer.setSingleShot(True)
        self._edit_value_timer.timeout.connect(self._edit_value)

        # To paint the select row button and check if we are over it
        self.hovered_row = -1
        self.over_select_row_button = False

    def setup_table(self):
        """Setup table"""
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionsMovable(True)
        self.adjust_columns()

        # Actions to take when the selection changes
        self.selectionModel().selectionChanged.connect(self.refresh_menu)
        self.selectionModel().selectionChanged.connect(
            # We need this because selected_rows is cached
            self.selected_rows.cache_clear
        )

    def setup_menu(self):
        """Setup actions and context menu"""
        self.resize_action = self.create_action(
            name=CollectionsEditorActions.ResizeRows,
            text=_("Resize rows to contents"),
            icon=ima.icon('collapse_row'),
            triggered=self.resizeRowsToContents,
            register_action=False
        )
        self.resize_columns_action = self.create_action(
            name=CollectionsEditorActions.ResizeColumns,
            text=_("Resize columns to contents"),
            icon=ima.icon('collapse_column'),
            triggered=self.resize_column_contents,
            register_action=False
        )
        self.paste_action = self.create_action(
            name=CollectionsEditorActions.ResizeRows,
            text=_("Paste"),
            icon=ima.icon('editpaste'),
            triggered=self.paste,
            register_action=False
        )
        self.copy_action = self.create_action(
            name=CollectionsEditorActions.Copy,
            text=_("Copy"),
            icon=ima.icon('editcopy'),
            triggered=self.copy,
            register_action=False
        )
        self.edit_action = self.create_action(
            name=CollectionsEditorActions.Edit,
            text=_("Edit"),
            icon=ima.icon('edit'),
            triggered=self.edit_item,
            register_action=False
        )
        self.plot_action = self.create_action(
            name=CollectionsEditorActions.Plot,
            text=_("Plot"),
            icon=ima.icon('plot'),
            triggered=lambda: self.plot_item('plot'),
            register_action=False
        )
        self.plot_action.setVisible(False)
        self.hist_action = self.create_action(
            name=CollectionsEditorActions.Histogram,
            text=_("Histogram"),
            icon=ima.icon('hist'),
            triggered=lambda: self.plot_item('hist'),
            register_action=False
        )
        self.hist_action.setVisible(False)
        self.imshow_action = self.create_action(
            name=CollectionsEditorActions.ShowImage,
            text=_("Show image"),
            icon=ima.icon('imshow'),
            triggered=self.imshow_item,
            register_action=False
        )
        self.imshow_action.setVisible(False)
        self.save_array_action = self.create_action(
            name=CollectionsEditorActions.Save,
            text=_("Save"),
            icon=ima.icon('filesave'),
            triggered=self.save_array,
            register_action=False
        )
        self.save_array_action.setVisible(False)
        self.insert_action = self.create_action(
            name=CollectionsEditorActions.Insert,
            text=_("Insert"),
            icon=ima.icon('insert'),
            triggered=lambda: self.insert_item(below=False),
            register_action=False
        )
        self.insert_action_above = self.create_action(
            name=CollectionsEditorActions.InsertAbove,
            text=_("Insert above"),
            icon=ima.icon('insert_above'),
            triggered=lambda: self.insert_item(below=False),
            register_action=False
        )
        self.insert_action_below = self.create_action(
            name=CollectionsEditorActions.InsertBelow,
            text=_("Insert below"),
            icon=ima.icon('insert_below'),
            triggered=lambda: self.insert_item(below=True),
            register_action=False
        )
        self.remove_action = self.create_action(
            name=CollectionsEditorActions.Remove,
            text=_("Remove"),
            icon=ima.icon('editdelete'),
            triggered=self.remove_item,
            register_action=False
        )
        self.rename_action = self.create_action(
            name=CollectionsEditorActions.Rename,
            text=_("Rename"),
            icon=ima.icon('rename'),
            triggered=self.rename_item,
            register_action=False
        )
        self.duplicate_action = self.create_action(
            name=CollectionsEditorActions.Duplicate,
            text=_("Duplicate"),
            icon=ima.icon('edit_add'),
            triggered=self.duplicate_item,
            register_action=False
        )
        self.view_action = self.create_action(
            name=CollectionsEditorActions.ViewObject,
            text=_("View with the Object Explorer"),
            icon=ima.icon('outline_explorer'),
            triggered=self.view_item,
            register_action=False
        )

        menu = self.create_menu(
            CollectionsEditorMenus.Context,
            register=False
        )

        for action in [self.copy_action, self.paste_action, self.rename_action,
                       self.edit_action, self.save_array_action]:
            self.add_item_to_menu(
                action,
                menu,
                section=CollectionsEditorContextMenuSections.Edit
        )

        for action in [self.insert_action, self.insert_action_above,
                       self.insert_action_below, self.duplicate_action,
                       self.remove_action]:
            self.add_item_to_menu(
                action,
                menu,
                section=CollectionsEditorContextMenuSections.AddRemove
            )

        for action in [self.view_action, self.plot_action,
                       self.hist_action, self.imshow_action]:
            self.add_item_to_menu(
                action,
                menu,
                section=CollectionsEditorContextMenuSections.View
            )

        self.empty_ws_menu = self.create_menu(
            CollectionsEditorMenus.ContextIfEmpty,
            register=False
        )

        for action in [self.insert_action, self.paste_action]:
            self.add_item_to_menu(action, self.empty_ws_menu)

        return menu

    # ------ Remote/local API -------------------------------------------------
    def remove_values(self, keys):
        """Remove values from data"""
        raise NotImplementedError

    def copy_value(self, orig_key, new_key):
        """Copy value"""
        raise NotImplementedError

    def new_value(self, key, value):
        """Create new value in data"""
        raise NotImplementedError

    def is_list(self, key):
        """Return True if variable is a list, a set or a tuple"""
        raise NotImplementedError

    def get_len(self, key):
        """Return sequence length"""
        raise NotImplementedError

    def is_data_frame(self, key):
        """Return True if variable is a pandas dataframe"""
        raise NotImplementedError

    def is_array(self, key):
        """Return True if variable is a numpy array"""
        raise NotImplementedError

    def is_image(self, key):
        """Return True if variable is a PIL.Image image"""
        raise NotImplementedError

    def is_dict(self, key):
        """Return True if variable is a dictionary"""
        raise NotImplementedError

    def get_array_shape(self, key):
        """Return array's shape"""
        raise NotImplementedError

    def get_array_ndim(self, key):
        """Return array's ndim"""
        raise NotImplementedError

    def oedit(self, key):
        """Edit item"""
        raise NotImplementedError

    def plot(self, key, funcname):
        """Plot item"""
        raise NotImplementedError

    def imshow(self, key):
        """Show item's image"""
        raise NotImplementedError

    def show_image(self, key):
        """Show image (item is a PIL image)"""
        raise NotImplementedError
    #--------------------------------------------------------------------------

    def refresh_menu(self):
        """Refresh context menu"""
        index = self.currentIndex()
        data = self.source_model.get_data()
        is_list_instance = isinstance(data, list)
        is_dict_instance = isinstance(data, dict)

        def indexes_in_same_row():
            indexes = self.selectedIndexes()
            if len(indexes) > 1:
                rows = [idx.row() for idx in indexes]
                return len(set(rows)) == 1
            else:
                return True

        # Enable/disable actions
        condition_edit = (
            (not isinstance(data, (tuple, set, frozenset))) and
            index.isValid() and
            (len(self.selectedIndexes()) > 0) and
            indexes_in_same_row() and
            not self.readonly
        )
        self.edit_action.setEnabled(condition_edit)
        self.insert_action_above.setEnabled(condition_edit)
        self.insert_action_below.setEnabled(condition_edit)
        self.duplicate_action.setEnabled(condition_edit)
        self.rename_action.setEnabled(condition_edit)
        self.plot_action.setEnabled(condition_edit)
        self.hist_action.setEnabled(condition_edit)
        self.imshow_action.setEnabled(condition_edit)
        self.save_array_action.setEnabled(condition_edit)

        condition_select = (
            index.isValid() and
            (len(self.selectedIndexes()) > 0)
        )
        self.view_action.setEnabled(
            condition_select and indexes_in_same_row())
        self.copy_action.setEnabled(condition_select)

        condition_remove = (
            (not isinstance(data, (tuple, set, frozenset))) and
            index.isValid() and
            (len(self.selectedIndexes()) > 0) and
            not self.readonly
        )
        self.remove_action.setEnabled(condition_remove)

        self.insert_action.setEnabled(
            is_dict_instance and not self.readonly)
        self.paste_action.setEnabled(
            is_dict_instance and not self.readonly)

        # Hide/show actions
        if index.isValid():
            if self.proxy_model:
                key = self.proxy_model.get_key(index)
            else:
                key = self.source_model.get_key(index)
            is_list = self.is_list(key)
            is_array = self.is_array(key) and self.get_len(key) != 0
            is_dataframe = self.is_data_frame(key) and self.get_len(key) != 0
            condition_plot = (
                is_array and len(self.get_array_shape(key)) <= 2
                ) or is_dataframe
            condition_hist = (is_array and self.get_array_ndim(key) == 1)
            condition_imshow = condition_plot and self.get_array_ndim(key) == 2
            condition_imshow = condition_imshow or self.is_image(key)
        else:
            is_array = condition_plot = condition_imshow = is_list \
                     = condition_hist = False

        self.plot_action.setVisible(condition_plot or is_list)
        self.hist_action.setVisible(condition_hist or is_list)
        self.insert_action.setVisible(is_dict_instance)
        self.insert_action_above.setVisible(is_list_instance)
        self.insert_action_below.setVisible(is_list_instance)
        self.rename_action.setVisible(is_dict_instance)
        self.paste_action.setVisible(is_dict_instance)
        self.imshow_action.setVisible(condition_imshow)
        self.save_array_action.setVisible(is_array)

    def resize_column_contents(self):
        """Resize columns to contents."""
        self.automatic_column_width = True
        self.adjust_columns()

    def user_resize_columns(self, logical_index, old_size, new_size):
        """Handle the user resize action."""
        self.automatic_column_width = False

    def adjust_columns(self):
        """Resize two first columns to contents"""
        if self.automatic_column_width:
            for col in range(3):
                self.resizeColumnToContents(col)

    def set_data(self, data):
        """Set table data"""
        if data is not None:
            self.source_model.set_data(data, self.dictfilter)
            self.source_model.reset()

            # Sort table using current sort column and order
            self.setSortingEnabled(True)

    def _edit_value(self):
        self.edit(self.__index_clicked)

    def _update_hovered_row(self, event):
        current_index = self.indexAt(event.pos())
        if current_index.isValid():
            self.hovered_row = current_index.row()
            self.viewport().update()
        else:
            self.hovered_row = -1

    def mousePressEvent(self, event):
        """Reimplement Qt method"""
        if event.button() != Qt.LeftButton or self.over_select_row_button:
            QTableView.mousePressEvent(self, event)
            return

        index_clicked = self.indexAt(event.pos())
        if index_clicked.isValid():
            if (
                index_clicked == self.currentIndex()
                and index_clicked in self.selectedIndexes()
            ):
                self.clearSelection()
            else:
                row = index_clicked.row()
                # TODO: Remove hard coded "Value" column number (3 here)
                self.__index_clicked = self.model().index(row, 3)

                # Wait for a bit to edit values so dialogs are focused on
                # double clicks. That will preserve the way things worked in
                # Spyder 5 for users that are accustomed to do double clicks.
                self._edit_value_timer.start()
        else:
            self.clearSelection()
            event.accept()

    def mouseDoubleClickEvent(self, event):
        """Reimplement Qt method"""
        # Make this event do nothing because variables are now edited with a
        # single click.
        pass

    def mouseMoveEvent(self, event):
        """Actions to take when the mouse moves over the widget."""
        self.over_select_row_button = False
        self._update_hovered_row(event)

        if self.rowAt(event.y()) != -1:
            # The +3 here is necessary to avoid mismatches when trying to click
            # the button in a position too close to its left border.
            select_row_button_width = SELECT_ROW_BUTTON_SIZE + 3

            # Include scrollbar width when computing the select row button
            # width
            if self.verticalScrollBar().isVisible():
                select_row_button_width += self.verticalScrollBar().width()

            # Decide if the cursor is on top of the select row button
            if (self.width() - event.x()) < select_row_button_width:
                self.over_select_row_button = True
                self.setCursor(Qt.ArrowCursor)
            else:
                self.setCursor(Qt.PointingHandCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

    def keyPressEvent(self, event):
        """Reimplement Qt methods"""
        if event.key() == Qt.Key_Delete:
            self.remove_item()
        elif event.key() == Qt.Key_F2:
            self.rename_item()
        elif event == QKeySequence.Copy:
            self.copy()
        elif event == QKeySequence.Paste:
            self.paste()
        else:
            QTableView.keyPressEvent(self, event)

    def contextMenuEvent(self, event):
        """Reimplement Qt method"""
        if self.source_model.showndata:
            self.refresh_menu()
            self.menu.popup(event.globalPos())
            event.accept()
        else:
            self.empty_ws_menu.popup(event.globalPos())
            event.accept()

    def dragEnterEvent(self, event):
        """Allow user to drag files"""
        if mimedata2url(event.mimeData()):
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        """Allow user to move files"""
        if mimedata2url(event.mimeData()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        """Allow user to drop supported files"""
        urls = mimedata2url(event.mimeData())
        if urls:
            event.setDropAction(Qt.CopyAction)
            event.accept()
            self.sig_files_dropped.emit(urls)
        else:
            event.ignore()

    def leaveEvent(self, event):
        """Actions to take when the mouse leaves the widget."""
        self.hovered_row = -1
        super().leaveEvent(event)

    def wheelEvent(self, event):
        """Actions to take on mouse wheel."""
        self._update_hovered_row(event)
        super().wheelEvent(event)

    def showEvent(self, event):
        """Resize columns when the widget is shown."""
        # This is probably the best we can do to adjust the columns width to
        # their header contents at startup. However, it doesn't work for all
        # fonts and font sizes and perhaps it depends on the user's screen dpi
        # as well. See the discussion in
        # https://github.com/spyder-ide/spyder/pull/20933#issuecomment-1585474443
        # and the comments below for more details.
        self.adjust_columns()
        super().showEvent(event)

    def _deselect_index(self, index):
        """
        Deselect index after any operation that adds or removes rows to/from
        the editor.

        Notes
        -----
        * This avoids showing the wrong buttons in the editor's toolbar when
          the operation is completed.
        * Also, if we leave something selected, then the next operation won't
          introduce the item in the expected row. That's why we need to force
          users to select a row again after this.
        """
        self.selectionModel().select(index, QItemSelectionModel.Select)
        self.selectionModel().select(index, QItemSelectionModel.Deselect)

    @Slot()
    def edit_item(self):
        """Edit item"""
        index = self.currentIndex()
        if not index.isValid():
            return
        # TODO: Remove hard coded "Value" column number (3 here)
        self.edit(self.model().index(index.row(), 3))

    @Slot()
    def remove_item(self, force=False):
        """Remove item"""
        current_index = self.currentIndex()
        indexes = self.selectedIndexes()

        if not indexes:
            return

        for index in indexes:
            if not index.isValid():
                return

        if not force:
            one = _("Do you want to remove the selected item?")
            more = _("Do you want to remove all selected items?")
            answer = QMessageBox.question(self, _("Remove"),
                                          one if len(indexes) == 1 else more,
                                          QMessageBox.Yes | QMessageBox.No)

        if force or answer == QMessageBox.Yes:
            if self.proxy_model:
                idx_rows = unsorted_unique(
                    [self.proxy_model.mapToSource(idx).row()
                     for idx in indexes])
            else:
                idx_rows = unsorted_unique([idx.row() for idx in indexes])
            keys = [self.source_model.keys[idx_row] for idx_row in idx_rows]
            self.remove_values(keys)

        # This avoids a segfault in our tests that doesn't happen when
        # removing items manually.
        if not running_under_pytest():
            self._deselect_index(current_index)

    def copy_item(self, erase_original=False, new_name=None):
        """Copy item"""
        current_index = self.currentIndex()
        indexes = self.selectedIndexes()

        if not indexes:
            return

        if self.proxy_model:
            idx_rows = unsorted_unique(
                [self.proxy_model.mapToSource(idx).row() for idx in indexes])
        else:
            idx_rows = unsorted_unique([idx.row() for idx in indexes])

        if len(idx_rows) > 1 or not indexes[0].isValid():
            return

        orig_key = self.source_model.keys[idx_rows[0]]
        if erase_original:
            if not isinstance(orig_key, str):
                QMessageBox.warning(
                    self,
                    _("Warning"),
                    _("You can only rename keys that are strings")
                )
                return

            title = _('Rename')
            field_text = _('New variable name:')
        else:
            title = _('Duplicate')
            field_text = _('Variable name:')

        data = self.source_model.get_data()
        if isinstance(data, (list, set, frozenset)):
            new_key, valid = len(data), True
        elif new_name is not None:
            new_key, valid = new_name, True
        else:
            new_key, valid = QInputDialog.getText(self, title, field_text,
                                                  QLineEdit.Normal, orig_key)

        if valid and to_text_string(new_key):
            new_key = try_to_eval(to_text_string(new_key))
            if new_key == orig_key:
                return
            self.copy_value(orig_key, new_key)
            if erase_original:
                self.remove_values([orig_key])

        self._deselect_index(current_index)

    @Slot()
    def duplicate_item(self):
        """Duplicate item"""
        self.copy_item()

    @Slot()
    def rename_item(self, new_name=None):
        """Rename item"""
        if isinstance(new_name, bool):
            new_name = None
        self.copy_item(erase_original=True, new_name=new_name)

    @Slot()
    def insert_item(self, below=True):
        """Insert item"""
        index = self.currentIndex()
        if not index.isValid():
            row = self.source_model.rowCount()
        else:
            if self.proxy_model:
                if below:
                    row = self.proxy_model.mapToSource(index).row() + 1
                else:
                    row = self.proxy_model.mapToSource(index).row()
            else:
                if below:
                    row = index.row() + 1
                else:
                    row = index.row()
        data = self.source_model.get_data()

        if isinstance(data, list):
            key = row
            data.insert(row, '')
        elif isinstance(data, dict):
            key, valid = QInputDialog.getText(self, _('Insert'), _('Key:'),
                                              QLineEdit.Normal)
            if valid and to_text_string(key):
                key = try_to_eval(to_text_string(key))
            else:
                return
        else:
            return

        value, valid = QInputDialog.getText(self, _('Insert'), _('Value:'),
                                            QLineEdit.Normal)

        if valid and to_text_string(value):
            self.new_value(key, try_to_eval(to_text_string(value)))

    @Slot()
    def view_item(self):
        """View item with the Object Explorer"""
        index = self.currentIndex()
        if not index.isValid():
            return
        # TODO: Remove hard coded "Value" column number (3 here)
        index = index.model().index(index.row(), 3)
        self.delegate.createEditor(self, None, index, object_explorer=True)

    def __prepare_plot(self):
        try:
            import guiqwt.pyplot   #analysis:ignore
            return True
        except:
            try:
                if 'matplotlib' not in sys.modules:
                    import matplotlib  # noqa
                return True
            except Exception:
                QMessageBox.warning(self, _("Import error"),
                                    _("Please install <b>matplotlib</b>"
                                      " or <b>guiqwt</b>."))

    def plot_item(self, funcname):
        """Plot item"""
        index = self.currentIndex()
        if self.__prepare_plot():
            if self.proxy_model:
                key = self.source_model.get_key(
                    self.proxy_model.mapToSource(index))
            else:
                key = self.source_model.get_key(index)
            try:
                self.plot(key, funcname)
            except (ValueError, TypeError) as error:
                QMessageBox.critical(self, _( "Plot"),
                                     _("<b>Unable to plot data.</b>"
                                       "<br><br>Error message:<br>%s"
                                       ) % str(error))

    @Slot()
    def imshow_item(self):
        """Imshow item"""
        index = self.currentIndex()
        if self.__prepare_plot():
            if self.proxy_model:
                key = self.source_model.get_key(
                    self.proxy_model.mapToSource(index))
            else:
                key = self.source_model.get_key(index)
            try:
                if self.is_image(key):
                    self.show_image(key)
                else:
                    self.imshow(key)
            except (ValueError, TypeError) as error:
                QMessageBox.critical(self, _( "Plot"),
                                     _("<b>Unable to show image.</b>"
                                       "<br><br>Error message:<br>%s"
                                       ) % str(error))

    @Slot()
    def save_array(self):
        """Save array"""
        title = _( "Save array")
        if self.array_filename is None:
            self.array_filename = getcwd_or_home()
        self.redirect_stdio.emit(False)
        filename, _selfilter = getsavefilename(self, title,
                                               self.array_filename,
                                               _("NumPy arrays") + " (*.npy)")
        self.redirect_stdio.emit(True)
        if filename:
            self.array_filename = filename
            data = self.delegate.get_value(self.currentIndex())
            try:
                import numpy as np
                np.save(self.array_filename, data)
            except Exception as error:
                QMessageBox.critical(self, title,
                                     _("<b>Unable to save array</b>"
                                       "<br><br>Error message:<br>%s"
                                       ) % str(error))

    @Slot()
    def copy(self):
        """
        Copy text representation of objects to clipboard.

        Notes
        -----
        For Numpy arrays and dataframes we try to get a better representation
        by using their `savetxt` and `to_csv` methods, respectively.
        """
        clipboard = QApplication.clipboard()
        clipl = []
        retrieve_failed = False
        array_failed = False
        dataframe_failed = False

        for idx in self.selectedIndexes():
            if not idx.isValid():
                continue

            # Prevent error when it's not possible to get the object's value
            # Fixes spyder-ide/spyder#12913
            try:
                obj = self.delegate.get_value(idx)
            except Exception:
                retrieve_failed = True
                continue

            # Check if we are trying to copy a numpy array, and if so make sure
            # to copy the whole thing in a tab separated format
            if (isinstance(obj, (np.ndarray, np.ma.MaskedArray)) and
                    np.ndarray is not FakeObject):
                output = io.BytesIO()
                try:
                    np.savetxt(output, obj, delimiter='\t')
                except Exception:
                    array_failed = True
                    continue
                obj = output.getvalue().decode('utf-8')
                output.close()
            elif (isinstance(obj, (pd.DataFrame, pd.Series)) and
                    pd.DataFrame is not FakeObject):
                output = io.StringIO()
                try:
                    obj.to_csv(output, sep='\t', index=True, header=True)
                except Exception:
                    dataframe_failed = True
                    continue
                obj = output.getvalue()
                output.close()
            elif is_binary_string(obj):
                obj = to_text_string(obj, 'utf8')
            else:
                obj = str(obj)

            clipl.append(obj)

        # Copy to clipboard the final result
        clipboard.setText('\n'.join(clipl))

        # Show appropriate error messages after we tried to copy all objects
        # selected by users.
        if retrieve_failed:
            QMessageBox.warning(
                self.parent(),
                _("Warning"),
                _(
                    "It was not possible to retrieve the value of one or more "
                    "of the variables you selected in order to copy them."
                ),
            )

        if array_failed and dataframe_failed:
            QMessageBox.warning(
                self,
                _("Warning"),
                _(
                    "It was not possible to copy one or more of the "
                    "dataframes and Numpy arrays you selected"
                ),
            )
        elif array_failed:
            QMessageBox.warning(
                self,
                _("Warning"),
                _(
                    "It was not possible to copy one or more of the "
                    "Numpy arrays you selected"
                ),
            )
        elif dataframe_failed:
            QMessageBox.warning(
                self,
                _("Warning"),
                _(
                    "It was not possible to copy one or more of the "
                    "dataframes you selected"
                ),
            )

    def import_from_string(self, text, title=None):
        """Import data from string"""
        data = self.source_model.get_data()
        # Check if data is a dict
        if not hasattr(data, "keys"):
            return
        editor = ImportWizard(
            self, text, title=title, contents_title=_("Clipboard contents"),
            varname=fix_reference_name("data", blacklist=list(data.keys())))
        if editor.exec_():
            var_name, clip_data = editor.get_data()
            self.new_value(var_name, clip_data)

    @Slot()
    def paste(self):
        """Import text/data/code from clipboard"""
        clipboard = QApplication.clipboard()
        cliptext = ''
        if clipboard.mimeData().hasText():
            cliptext = to_text_string(clipboard.text())
        if cliptext.strip():
            self.import_from_string(cliptext, title=_("Import from clipboard"))
        else:
            QMessageBox.warning(self, _( "Empty clipboard"),
                                _("Nothing to be imported from clipboard."))

    @lru_cache(maxsize=1)
    def selected_rows(self):
        """
        Get the rows currently selected.

        Notes
        -----
        The result of this function is cached because it's called in the paint
        method of CollectionsDelegate. So, we need it to run as quickly as
        possible.
        """
        return {
            index.row() for index in self.selectionModel().selectedRows()
        }


class CollectionsEditorTableView(BaseTableView):
    """CollectionsEditor table view"""

    def __init__(self, parent, data, namespacebrowser=None,
                 data_function: Optional[Callable[[], Any]] = None,
                 readonly=False, title="", names=False):
        BaseTableView.__init__(self, parent)
        self.dictfilter = None
        self.namespacebrowser = namespacebrowser
        self.readonly = readonly or isinstance(data, (tuple, set, frozenset))
        CollectionsModelClass = (ReadOnlyCollectionsModel if self.readonly
                                 else CollectionsModel)
        self.source_model = CollectionsModelClass(
            self,
            data,
            title,
            names=names,
            minmax=self.get_conf('minmax')
        )
        self.setModel(self.source_model)
        self.delegate = CollectionsDelegate(
            self, namespacebrowser, data_function
        )
        self.setItemDelegate(self.delegate)

        self.setup_table()
        self.menu = self.setup_menu()

        # Leave unsorted if dict, sort by column 0 otherwise
        if isinstance(data, dict):
            self.horizontalHeader().setSortIndicator(-1, Qt.AscendingOrder)
        else:
            self.horizontalHeader().setSortIndicator(0, Qt.AscendingOrder)
        self.setSortingEnabled(True)

        if isinstance(data, (set, frozenset)):
            self.horizontalHeader().hideSection(0)

    #------ Remote/local API --------------------------------------------------
    def remove_values(self, keys):
        """Remove values from data"""
        data = self.source_model.get_data()
        for key in sorted(keys, reverse=True):
            data.pop(key)
        self.set_data(data)

    def copy_value(self, orig_key, new_key):
        """Copy value"""
        data = self.source_model.get_data()
        if isinstance(data, list):
            data.append(data[orig_key])
        if isinstance(data, (set, frozenset)):
            data.add(data[orig_key])
        else:
            data[new_key] = data[orig_key]
        self.set_data(data)

    def new_value(self, key, value):
        """Create new value in data"""
        index = self.currentIndex()
        data = self.source_model.get_data()
        data[key] = value
        self.set_data(data)
        self._deselect_index(index)

    def is_list(self, key):
        """Return True if variable is a list or a tuple"""
        data = self.source_model.get_data()
        return isinstance(data[key], (tuple, list))

    def is_set(self, key):
        """Return True if variable is a set or a frozenset"""
        data = self.source_model.get_data()
        return isinstance(data[key], (set, frozenset))

    def get_len(self, key):
        """Return sequence length"""
        data = self.source_model.get_data()
        if self.is_array(key):
            return self.get_array_ndim(key)
        else:
            return len(data[key])

    def is_data_frame(self, key):
        """Return True if variable is a pandas dataframe"""
        data = self.source_model.get_data()
        return isinstance(data[key], pd.DataFrame)

    def is_array(self, key):
        """Return True if variable is a numpy array"""
        data = self.source_model.get_data()
        return isinstance(data[key], (np.ndarray, np.ma.MaskedArray))

    def is_image(self, key):
        """Return True if variable is a PIL.Image image"""
        data = self.source_model.get_data()
        return isinstance(data[key], PIL.Image.Image)

    def is_dict(self, key):
        """Return True if variable is a dictionary"""
        data = self.source_model.get_data()
        return isinstance(data[key], dict)

    def get_array_shape(self, key):
        """Return array's shape"""
        data = self.source_model.get_data()
        return data[key].shape

    def get_array_ndim(self, key):
        """Return array's ndim"""
        data = self.source_model.get_data()
        return data[key].ndim

    def oedit(self, key):
        """Edit item"""
        data = self.source_model.get_data()
        from spyder.plugins.variableexplorer.widgets.objecteditor import (
            oedit)
        oedit(data[key])

    def plot(self, key, funcname):
        """Plot item"""
        def plot_function(figure):
            ax = figure.subplots()
            getattr(ax, funcname)(data)

        data = self.source_model.get_data()[key]
        self.namespacebrowser.plot(plot_function)

    def imshow(self, key):
        """Show item's image"""
        data = self.source_model.get_data()
        import spyder.pyplot as plt
        plt.figure()
        plt.imshow(data[key])
        plt.show()

    def show_image(self, key):
        """Show image (item is a PIL image)"""
        data = self.source_model.get_data()
        data[key].show()
    #--------------------------------------------------------------------------

    def set_filter(self, dictfilter=None):
        """Set table dict filter"""
        self.dictfilter = dictfilter


class CollectionsEditorWidget(QWidget, SpyderWidgetMixin):
    """Dictionary Editor Widget"""
    # Dummy conf section to avoid a warning from SpyderConfigurationObserver
    CONF_SECTION = "variable_explorer"

    sig_refresh_requested = Signal()

    def __init__(self, parent, data, namespacebrowser=None,
                 data_function: Optional[Callable[[], Any]] = None,
                 readonly=False, title="", remote=False):
        QWidget.__init__(self, parent)
        if remote:
            self.editor = RemoteCollectionsEditorTableView(
                self, data, readonly, create_menu=True)
        else:
            self.editor = CollectionsEditorTableView(
                self, data, namespacebrowser, data_function, readonly, title
            )

        self.refresh_action = self.create_action(
            name=CollectionsEditorActions.Refresh,
            text=_('Refresh'),
            icon=ima.icon('refresh'),
            tip=_('Refresh editor with current value of variable in console'),
            triggered=lambda: self.sig_refresh_requested.emit(),
            register_action=None
        )

        self.close_action = self.create_action(
            name=CollectionsEditorActions.Close,
            icon=self.create_icon('close_pane'),
            text=_('Close'),
            triggered=self.close_window,
            shortcut=self.get_shortcut(CollectionsEditorActions.Close),
            register_action=False,
            register_shortcut=True
        )
        self.register_shortcut_for_widget(
            name='close', triggered=self.close_window
        )

        toolbar = self.create_toolbar(
            CollectionsEditorWidgets.Toolbar,
            register=False
        )

        stretcher = self.create_stretcher(
            CollectionsEditorWidgets.ToolbarStretcher
        )

        for item in [
            self.editor.insert_action,
            self.editor.insert_action_above,
            self.editor.insert_action_below,
            self.editor.duplicate_action,
            self.editor.remove_action
        ]:
            self.add_item_to_toolbar(
                item,
                toolbar,
                section=CollectionsEditorToolbarSections.AddDelete
            )

        options_menu = self.create_menu(
            CollectionsEditorMenus.Options,
            register=False
        )
        for item in [self.close_action]:
            self.add_item_to_menu(item, options_menu)

        options_button = self.create_toolbutton(
            name=CollectionsEditorWidgets.OptionsToolButton,
            text=_('Options'),
            icon=ima.icon('tooloptions'),
            register=False
        )
        options_button.setPopupMode(QToolButton.InstantPopup)
        options_button.setMenu(options_menu)

        for item in [
            self.editor.view_action,
            self.editor.plot_action,
            self.editor.hist_action,
            self.editor.imshow_action,
            stretcher,
            self.editor.resize_action,
            self.editor.resize_columns_action,
            self.refresh_action,
            options_button
        ]:
            self.add_item_to_toolbar(
                item,
                toolbar,
                section=CollectionsEditorToolbarSections.ViewAndRest
            )

        toolbar.render()

        # Update the toolbar actions state
        self.editor.refresh_menu()
        self.refresh_action.setEnabled(data_function is not None)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(toolbar)
        layout.addWidget(self.editor)
        self.setLayout(layout)

    def set_data(self, data):
        """Set DictEditor data"""
        self.editor.set_data(data)

    def get_title(self):
        """Get model title"""
        return self.editor.source_model.title
    
    def close_window(self):
        if self.parent():
            self.parent().reject()


class CollectionsEditor(BaseDialog):
    """Collections Editor Dialog"""

    def __init__(self, parent=None, namespacebrowser=None,
                 data_function: Optional[Callable[[], Any]] = None):
        super().__init__(parent)

        # Destroying the C++ object right after closing the dialog box,
        # otherwise it may be garbage-collected in another QThread
        # (e.g. the editor's analysis thread in Spyder), thus leading to
        # a segmentation fault on UNIX or an application crash on Windows
        self.setAttribute(Qt.WA_DeleteOnClose)

        self.namespacebrowser = namespacebrowser
        self.data_function = data_function
        self.data_copy = None
        self.widget = None
        self.btn_save_and_close = None
        self.btn_close = None

    def setup(self, data, title='', readonly=False, remote=False,
              icon=None, parent=None):
        """Setup editor."""
        if isinstance(data, (dict, set, frozenset)):
            # dictionary, set
            self.data_copy = data.copy()
        elif isinstance(data, (tuple, list)):
            # list, tuple
            self.data_copy = data[:]
        else:
            # unknown object
            import copy
            try:
                self.data_copy = copy.deepcopy(data)
            except NotImplementedError:
                self.data_copy = copy.copy(data)
            except (TypeError, AttributeError):
                readonly = True
                self.data_copy = data

        # If the copy has a different type, then do not allow editing, because
        # this would change the type after saving; cf. spyder-ide/spyder#6936.
        if type(self.data_copy) != type(data):
            readonly = True

        self.widget = CollectionsEditorWidget(
            self, self.data_copy, self.namespacebrowser, self.data_function,
            title=title, readonly=readonly, remote=remote
        )
        self.widget.sig_refresh_requested.connect(self.refresh_editor)
        self.widget.editor.source_model.sig_setting_data.connect(
            self.save_and_close_enable)

        # Buttons configuration
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        if not readonly:
            self.btn_save_and_close = QPushButton(_('Save and Close'))
            self.btn_save_and_close.setDisabled(True)
            self.btn_save_and_close.clicked.connect(self.accept)
            btn_layout.addWidget(self.btn_save_and_close)

        self.btn_close = QPushButton(_('Close'))
        self.btn_close.setAutoDefault(True)
        self.btn_close.setDefault(True)
        self.btn_close.clicked.connect(self.reject)
        btn_layout.addWidget(self.btn_close)

        # CollectionEditor widget layout
        layout = QVBoxLayout()
        layout.addWidget(self.widget)
        layout.addSpacing((-1 if MAC else 2) * AppStyle.MarginSize)
        layout.addLayout(btn_layout)
        self.setLayout(layout)

        self.setWindowTitle(self.widget.get_title())
        if icon is None:
            self.setWindowIcon(ima.icon('dictedit'))

        if sys.platform == 'darwin':
            # See spyder-ide/spyder#9051
            self.setWindowFlags(Qt.Tool)
        else:
            # Make the dialog act as a window
            self.setWindowFlags(Qt.Window)

    @Slot()
    def save_and_close_enable(self):
        """Handle the data change event to enable the save and close button."""
        if self.btn_save_and_close:
            self.btn_save_and_close.setEnabled(True)
            self.btn_save_and_close.setAutoDefault(True)
            self.btn_save_and_close.setDefault(True)

    def get_value(self):
        """Return modified copy of dictionary or list"""
        # It is import to avoid accessing Qt C++ object as it has probably
        # already been destroyed, due to the Qt.WA_DeleteOnClose attribute
        return self.data_copy

    def refresh_editor(self) -> None:
        """
        Refresh data in editor.
        """
        assert self.data_function is not None

        if self.btn_save_and_close and self.btn_save_and_close.isEnabled():
            if not self.ask_for_refresh_confirmation():
                return

        try:
            new_value = self.data_function()
        except (IndexError, KeyError):
            QMessageBox.critical(
                self,
                _('Collection editor'),
                _('The variable no longer exists.')
            )
            self.reject()
            return

        self.widget.set_data(new_value)
        self.data_copy = new_value
        if self.btn_save_and_close:
            self.btn_save_and_close.setEnabled(False)
        self.btn_close.setAutoDefault(True)
        self.btn_close.setDefault(True)

    def ask_for_refresh_confirmation(self) -> bool:
        """
        Ask user to confirm refreshing the editor.

        This function is to be called if refreshing the editor would overwrite
        changes that the user made previously. The function returns True if
        the user confirms that they want to refresh and False otherwise.
        """
        message = _('Refreshing the editor will overwrite the changes that '
                    'you made. Do you want to proceed?')
        result = QMessageBox.question(
            self,
            _('Refresh collections editor?'),
            message
        )
        return result == QMessageBox.Yes


#==============================================================================
# Remote versions of CollectionsDelegate and CollectionsEditorTableView
#==============================================================================
class RemoteCollectionsDelegate(CollectionsDelegate):
    """CollectionsEditor Item Delegate"""

    def __init__(self, parent=None, namespacebrowser=None):
        CollectionsDelegate.__init__(self, parent, namespacebrowser)

    def get_value(self, index):
        if index.isValid():
            source_index = index.model().mapToSource(index)
            name = source_index.model().keys[source_index.row()]
            return self.parent().get_value(name)

    def set_value(self, index, value):
        if index.isValid():
            source_index = index.model().mapToSource(index)
            name = source_index.model().keys[source_index.row()]
            self.parent().new_value(name, value)

    def make_data_function(
        self,
        index: QModelIndex
    ) -> Optional[Callable[[], Any]]:
        """
        Construct function which returns current value of data.

        The returned function uses the associated console to retrieve the
        current value of the variable. This is used to refresh editors created
        from that variable.

        Parameters
        ----------
        index : QModelIndex
            Index of item whose current value is to be returned by the
            function constructed here.

        Returns
        -------
        Optional[Callable[[], Any]]
            Function which returns the current value of the data, or None if
            such a function cannot be constructed.
        """
        source_index = index.model().mapToSource(index)
        name = source_index.model().keys[source_index.row()]
        parent = self.parent()

        def get_data():
            return parent.get_value(name)

        return get_data


class RemoteCollectionsEditorTableView(BaseTableView):
    """DictEditor table view"""

    def __init__(self, parent, data, shellwidget=None, remote_editing=False,
                 create_menu=False):
        BaseTableView.__init__(self, parent)

        self.namespacebrowser = parent
        self.shellwidget = shellwidget
        self.var_properties = {}
        self.dictfilter = None
        self.readonly = False

        self.source_model = CollectionsModel(
            self, data, names=True,
            minmax=self.get_conf('minmax'),
            remote=True)

        self.horizontalHeader().sectionClicked.connect(
            self.source_model.load_all)

        self.proxy_model = CollectionsCustomSortFilterProxy(self)

        self.proxy_model.setSourceModel(self.source_model)
        self.proxy_model.setDynamicSortFilter(True)
        self.proxy_model.setFilterKeyColumn(0)  # Col 0 for Name
        self.proxy_model.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy_model.setSortRole(Qt.UserRole)
        self.setModel(self.proxy_model)

        self.hideColumn(4)  # Column 4 for Score

        self.delegate = RemoteCollectionsDelegate(self, self.namespacebrowser)
        self.delegate.sig_free_memory_requested.connect(
            self.sig_free_memory_requested)
        self.delegate.sig_editor_creation_started.connect(
            self.sig_editor_creation_started)
        self.delegate.sig_editor_shown.connect(self.sig_editor_shown)
        self.setItemDelegate(self.delegate)

        self.setup_table()

        if create_menu:
            self.menu = self.setup_menu()

        # Sorting columns
        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.AscendingOrder)

    # ------ Remote/local API -------------------------------------------------
    def get_value(self, name):
        """Get the value of a variable"""
        value = self.shellwidget.get_value(name)
        return value

    def new_value(self, name, value):
        """Create new value in data"""
        try:
            self.shellwidget.set_value(name, value)
        except TypeError as e:
            QMessageBox.critical(self, _("Error"),
                                 "TypeError: %s" % to_text_string(e))
        self.namespacebrowser.refresh_namespacebrowser()

    def remove_values(self, names):
        """Remove values from data"""
        for name in names:
            self.shellwidget.remove_value(name)
        self.namespacebrowser.refresh_namespacebrowser()

    def copy_value(self, orig_name, new_name):
        """Copy value"""
        self.shellwidget.copy_value(orig_name, new_name)
        self.namespacebrowser.refresh_namespacebrowser()

    def is_list(self, name):
        """Return True if variable is a list, a tuple or a set"""
        return self.var_properties[name]['is_list']

    def is_dict(self, name):
        """Return True if variable is a dictionary"""
        return self.var_properties[name]['is_dict']

    def get_len(self, name):
        """Return sequence length"""
        return self.var_properties[name]['len']

    def is_array(self, name):
        """Return True if variable is a NumPy array"""
        return self.var_properties[name]['is_array']

    def is_image(self, name):
        """Return True if variable is a PIL.Image image"""
        return self.var_properties[name]['is_image']

    def is_data_frame(self, name):
        """Return True if variable is a DataFrame"""
        return self.var_properties[name]['is_data_frame']

    def is_series(self, name):
        """Return True if variable is a Series"""
        return self.var_properties[name]['is_series']

    def get_array_shape(self, name):
        """Return array's shape"""
        return self.var_properties[name]['array_shape']

    def get_array_ndim(self, name):
        """Return array's ndim"""
        return self.var_properties[name]['array_ndim']

    def plot(self, name, funcname):
        """Plot item"""
        sw = self.shellwidget
        sw.execute("%%varexp --%s %s" % (funcname, name))

    def imshow(self, name):
        """Show item's image"""
        sw = self.shellwidget
        sw.execute("%%varexp --imshow %s" % name)

    def show_image(self, name):
        """Show image (item is a PIL image)"""
        command = "%s.show()" % name
        sw = self.shellwidget
        sw.execute(command)

    # ------ Other ------------------------------------------------------------
    def setup_menu(self):
        """Setup context menu."""
        menu = BaseTableView.setup_menu(self)
        return menu

    def refresh_menu(self):
        if self.var_properties:
            super().refresh_menu()

    def do_find(self, text):
        """Update the regex text for the variable finder."""
        text = text.replace(' ', '').lower()

        # Make sure everything is loaded
        self.source_model.load_all()

        self.proxy_model.set_filter(text)
        self.source_model.update_search_letters(text)

        if text:
            # TODO: Use constants for column numbers
            self.sortByColumn(4, Qt.DescendingOrder)  # Col 4 for index

    def next_row(self):
        """Move to next row from currently selected row."""
        row = self.currentIndex().row()
        rows = self.proxy_model.rowCount()
        if row + 1 == rows:
            row = -1
        self.selectRow(row + 1)

    def previous_row(self):
        """Move to previous row from currently selected row."""
        row = self.currentIndex().row()
        rows = self.proxy_model.rowCount()
        if row == 0:
            row = rows
        self.selectRow(row - 1)


class CollectionsCustomSortFilterProxy(CustomSortFilterProxy):
    """
    Custom column filter based on regex and model data.

    Reimplements 'filterAcceptsRow' to follow NamespaceBrowser model.
    Reimplements 'set_filter' to allow sorting while filtering
    """

    def get_key(self, index):
        """Return current key from source model."""
        source_index = self.mapToSource(index)
        return self.sourceModel().get_key(source_index)

    def get_index_from_key(self, key):
        """Return index using key from source model."""
        source_index = self.sourceModel().get_index_from_key(key)
        return self.mapFromSource(source_index)

    def get_value(self, index):
        """Return current value from source model."""
        source_index = self.mapToSource(index)
        return self.sourceModel().get_value(source_index)

    def set_value(self, index, value):
        """Set value in source model."""
        try:
            source_index = self.mapToSource(index)
            self.sourceModel().set_value(source_index, value)
        except AttributeError:
            # Read-only models don't have set_value method
            pass

    def set_filter(self, text):
        """Set regular expression for filter."""
        self.pattern = get_search_regex(text)
        self.invalidateFilter()

    def filterAcceptsRow(self, row_num, parent):
        """
        Qt override.

        Reimplemented from base class to allow the use of custom filtering
        using to columns (name and type).
        """
        model = self.sourceModel()
        name = to_text_string(model.row_key(row_num))
        variable_type = to_text_string(model.row_type(row_num))
        r_name = re.search(self.pattern, name)
        r_type = re.search(self.pattern, variable_type)

        if r_name is None and r_type is None:
            return False
        else:
            return True

    def lessThan(self, left, right):
        """
        Implements ordering in a natural way, as a human would sort.
        This functions enables sorting of the main variable editor table,
        which does not rely on 'self.sort()'.
        """
        leftData = self.sourceModel().data(left)
        rightData = self.sourceModel().data(right)
        try:
            if isinstance(leftData, str) and isinstance(rightData, str):
                return natsort(leftData) < natsort(rightData)
            else:
                return leftData < rightData
        except TypeError:
            # This is needed so all the elements that cannot be compared such
            # as dataframes and numpy arrays are grouped together in the
            # variable explorer. For more info see spyder-ide/spyder#14527
            return True


# =============================================================================
# Tests
# =============================================================================
def get_test_data():
    """Create test data."""
    image = PIL.Image.fromarray(np.random.randint(256, size=(100, 100)),
                                mode='P')
    testdict = {'d': 1, 'a': np.random.rand(10, 10), 'b': [1, 2]}
    testdate = datetime.date(1945, 5, 8)
    test_timedelta = datetime.timedelta(days=-1, minutes=42, seconds=13)

    try:
        import pandas as pd
    except (ModuleNotFoundError, ImportError):
        test_df = None
        test_timestamp = test_pd_td = test_dtindex = test_series = None
    else:
        test_timestamp = pd.Timestamp("1945-05-08T23:01:00.12345")
        test_pd_td = pd.Timedelta(days=2193, hours=12)
        test_dtindex = pd.date_range(start="1939-09-01T",
                                     end="1939-10-06",
                                     freq="12H")
        test_series = pd.Series({"series_name": [0, 1, 2, 3, 4, 5]})
        test_df = pd.DataFrame({"string_col": ["a", "b", "c", "d"],
                                "int_col": [0, 1, 2, 3],
                                "float_col": [1.1, 2.2, 3.3, 4.4],
                                "bool_col": [True, False, False, True]})

    class Foobar(object):

        def __init__(self):
            self.text = "toto"
            self.testdict = testdict
            self.testdate = testdate

    foobar = Foobar()
    return {'object': foobar,
            'module': np,
            'str': 'kjkj kj k j j kj k jkj',
            'unicode': to_text_string('éù', 'utf-8'),
            'list': [1, 3, [sorted, 5, 6], 'kjkj', None],
            'set': {1, 2, 1, 3, None, 'A', 'B', 'C', True, False},
            'tuple': ([1, testdate, testdict, test_timedelta], 'kjkj', None),
            'dict': testdict,
            'float': 1.2233,
            'int': 223,
            'bool': True,
            'array': np.random.rand(10, 10).astype(np.int64),
            'masked_array': np.ma.array([[1, 0], [1, 0]],
                                        mask=[[True, False], [False, False]]),
            '1D-array': np.linspace(-10, 10).astype(np.float16),
            '3D-array': np.random.randint(2, size=(5, 5, 5)).astype(np.bool_),
            'empty_array': np.array([]),
            'image': image,
            'date': testdate,
            'datetime': datetime.datetime(1945, 5, 8, 23, 1, 0, int(1.5e5)),
            'timedelta': test_timedelta,
            'complex': 2 + 1j,
            'complex64': np.complex64(2 + 1j),
            'complex128': np.complex128(9j),
            'int8_scalar': np.int8(8),
            'int16_scalar': np.int16(16),
            'int32_scalar': np.int32(32),
            'int64_scalar': np.int64(64),
            'float16_scalar': np.float16(16),
            'float32_scalar': np.float32(32),
            'float64_scalar': np.float64(64),
            'bool__scalar': np.bool_(8),
            'timestamp': test_timestamp,
            'timedelta_pd': test_pd_td,
            'datetimeindex': test_dtindex,
            'series': test_series,
            'ddataframe': test_df,
            'None': None,
            'unsupported1': np.arccos,
            'unsupported2': np.asarray,
            # Test for spyder-ide/spyder#3518.
            'big_struct_array': np.zeros(1000, dtype=[('ID', 'f8'),
                                                      ('param1', 'f8', 5000)]),
            }


def editor_test():
    """Test Collections editor."""
    dialog = CollectionsEditor()
    dialog.setup(get_test_data())
    dialog.exec_()


def remote_editor_test():
    """Test remote collections editor."""
    from spyder.config.manager import CONF
    from spyder_kernels.utils.nsview import (make_remote_view,
                                             REMOTE_SETTINGS)

    settings = {}
    for name in REMOTE_SETTINGS:
        settings[name] = CONF.get('variable_explorer', name)

    remote = make_remote_view(get_test_data(), settings)
    dialog = CollectionsEditor()
    dialog.setup(remote, remote=True)
    dialog.exec_()


if __name__ == "__main__":
    from spyder.utils.qthelpers import qapplication

    app = qapplication()  # analysis:ignore
    editor_test()
    remote_editor_test()
