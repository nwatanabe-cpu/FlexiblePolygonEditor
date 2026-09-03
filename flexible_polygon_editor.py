import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsVectorLayer
from .map_tool import TopologicalEditTool

class FlexiblePolygonEditor:
    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.tool = None
        self.plugin_dir = os.path.dirname(__file__)

    def initGui(self):
        self.plugin_dir = os.path.dirname(__file__)
        icon = QIcon(os.path.join(self.plugin_dir, 'icon.png'))
        icon2 = QIcon(os.path.join(self.plugin_dir, 'icon2.png'))
        icon3 = QIcon(os.path.join(self.plugin_dir, 'icon3.png'))
        # 頂点移動ツール
        self.move_action = QAction(icon, "頂点移動", self.iface.mainWindow())
        self.move_action.setCheckable(True)
        self.move_action.triggered.connect(self.set_move_tool)
        self.iface.addToolBarIcon(self.move_action)

        # 複数点/曲線分割ツール
        self.split_action = QAction(icon2, "地物分割（複数点/曲線）", self.iface.mainWindow())
        self.split_action.setToolTip(
            "クリックで頂点追加、ダブルクリック/Enterで確定、右クリック/Backspaceで1つ戻す、"
            "Escでキャンセル、Cキーで曲線モード切替"
        )
        self.split_action.setCheckable(True)
        self.split_action.triggered.connect(self.set_split_tool)
        self.iface.addToolBarIcon(self.split_action)

        # Polygonize ボタン（トグルなし、クリックで即実行）
        self.polygonize_action = QAction(icon3, "Polygonize", self.iface.mainWindow())
        self.polygonize_action.setCheckable(False)
        self.polygonize_action.triggered.connect(self.run_polygonize)
        self.iface.addToolBarIcon(self.polygonize_action)

    # ------------------------------------------------------------------

    def set_move_tool(self):
        if self.move_action.isChecked():
            self.split_action.setChecked(False)
            self.tool = TopologicalEditTool(self.canvas)
            self.canvas.setMapTool(self.tool)
        else:
            self.canvas.unsetMapTool(self.tool)

    def set_split_tool(self):
        if self.split_action.isChecked():
            self.move_action.setChecked(False)
            from .map_tool import QuickSplitTool
            self.tool = QuickSplitTool(self.canvas)
            self.canvas.setMapTool(self.tool)
        else:
            self.canvas.unsetMapTool(self.tool)

    def run_polygonize(self):
        from .polygonize_tool import PolygonizeTool
        PolygonizeTool(self.iface).run()

    # ------------------------------------------------------------------

    def unload(self):
        for action in [
            getattr(self, 'move_action', None),
            getattr(self, 'split_action', None),
            getattr(self, 'polygonize_action', None),
        ]:
            if action:
                self.iface.removeToolBarIcon(action)
