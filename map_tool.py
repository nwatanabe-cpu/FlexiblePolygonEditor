from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsMapTool, QgsVertexMarker, QgsRubberBand, QgsSnapIndicator
from qgis.core import (
    QgsGeometry, Qgis,
    QgsPointXY,
    QgsSnappingConfig,
    QgsWkbTypes,
    QgsProject,
    QgsPointLocator, QgsFeatureRequest,
    QgsTolerance,
    QgsVectorLayer,
    QgsCoordinateTransform,
)

_GT_POINT   = 0
_GT_LINE    = 1
_GT_POLYGON = 2


# ===========================================================================
# TopologicalEditTool  ― 頂点・辺のトポロジカル移動（2クリック方式）
# ===========================================================================

class TopologicalEditTool(QgsMapTool):
    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.state = None

        self.affected_info  = {}
        self.affected_geoms = {}   # プレビュー用（移動のたびにbackupから再生成）
        self.backup_geoms   = {}   # 選択時の確定コピー（書き換えない）
        self.is_multipart   = {}
        self.geom_types     = {}   # 実フィーチャのジオメトリ型 {lid: {fid: gt}}
        self.rb_list        = []

        self.marker = QgsVertexMarker(self.canvas)
        self.marker.setColor(QColor(255, 50, 50))
        self.marker.setIconType(QgsVertexMarker.ICON_X)
        self.marker.setPenWidth(3)
        self.marker.setIconSize(10)
        self.marker.hide()

        self.hover_marker = QgsVertexMarker(self.canvas)
        self.hover_marker.setColor(QColor(255, 105, 180))
        self.hover_marker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.hover_marker.setPenWidth(2)
        self.hover_marker.setIconSize(8)
        self.hover_marker.hide()

    # ------------------------------------------------------------------
    # ユーティリティ
    # ------------------------------------------------------------------

    def log(self, msg, duration=3000):
        self.canvas.window().statusBar().showMessage(msg, duration)

    def activate(self):
        super().activate()
        self.canvas.setCursor(Qt.CrossCursor)
        self.log("頂点/辺をクリックして選択", 0)

    def _snap(self, pos):
        map_pt = self.toMapCoordinates(pos)
        res = self.canvas.snappingUtils().snapToMap(map_pt)
        return res.point() if res.isValid() else map_pt

    def _deep_copy_struct(self, struct):
        return [
            [[QgsPointXY(pt) for pt in ring] for ring in poly]
            for poly in struct
        ]

    def _is_target_layer(self, layer):
        if not isinstance(layer, QgsVectorLayer):
            return False
        if not layer.isEditable():
            return False
        gt = int(layer.geometryType())
        return gt in (_GT_LINE, _GT_POLYGON)

    def get_geom_structure(self, feature, layer):
        """ジオメトリを [[ring[pt]]] の統一構造（multipart形式）へ変換"""
        geom = feature.geometry()
        if not geom or geom.isEmpty():
            return None

        actual_type = int(geom.type())

        if actual_type == _GT_POLYGON:
            if geom.isMultipart():
                raw = geom.asMultiPolygon()
                if not raw:
                    return None
                return [[list(ring) for ring in poly] for poly in raw]
            else:
                raw = geom.asPolygon()
                if not raw:
                    return None
                return [[list(ring) for ring in raw]]

        elif actual_type == _GT_LINE:
            if geom.isMultipart():
                raw = geom.asMultiPolyline()
                if not raw:
                    return None
                return [[[QgsPointXY(pt) for pt in line]] for line in raw]
            else:
                raw = geom.asPolyline()
                if not raw:
                    return None
                return [[[QgsPointXY(pt) for pt in raw]]]

        return None

    def _struct_to_geometry(self, struct, layer, fid):
        gt = self.geom_types.get(layer.id(), {}).get(fid, int(layer.geometryType()))
        multi = self.is_multipart.get(layer.id(), {}).get(fid, True)
        if gt == _GT_POLYGON:
            return QgsGeometry.fromMultiPolygonXY(struct) if multi else QgsGeometry.fromPolygonXY(struct[0])
        else:
            flat = [ring for poly in struct for ring in poly]
            return QgsGeometry.fromMultiPolylineXY(flat) if multi else QgsGeometry.fromPolylineXY(flat[0])

    def _geom_type_for_fid(self, lid, fid):
        layer = QgsProject.instance().mapLayer(lid)
        if layer:
            return int(layer.geometryType())
        return _GT_POLYGON

    # ------------------------------------------------------------------
    # 第1クリック：頂点/辺を選択
    # ------------------------------------------------------------------

    def select_vertex(self, point):
        self.affected_info  = {}
        self.affected_geoms = {}
        self.backup_geoms   = {}
        self.is_multipart   = {}
        self._clear_rubberbands()

        tol = self.canvas.mapUnitsPerPixel() * 12

        found = False
        for layer in QgsProject.instance().mapLayers().values():
            if not self._is_target_layer(layer):
                continue

            rect = QgsGeometry.fromPointXY(point).boundingBox().buffered(tol)
            lid  = layer.id()

            for f in layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)):
                fgeom = f.geometry()
                if not fgeom or fgeom.isEmpty():
                    continue
                if fgeom.distance(QgsGeometry.fromPointXY(point)) > tol:
                    continue

                struct = self.get_geom_structure(f, layer)
                if not struct:
                    continue

                if lid not in self.is_multipart:
                    self.is_multipart[lid] = {}
                    self.geom_types[lid]   = {}
                self.is_multipart[lid][f.id()] = fgeom.isMultipart()
                self.geom_types[lid][f.id()]   = int(fgeom.type())

                gt = int(fgeom.type())
                v_info = []
                for pi, poly in enumerate(struct):
                    for ri, ring in enumerate(poly):
                        v_idx = -1

                        # ① 既存頂点（最近傍）
                        best_dist = tol
                        for vi, v in enumerate(ring):
                            d = QgsPointXY(v).distance(point)
                            if d <= best_dist:
                                best_dist = d
                                v_idx = vi

                        # ② 辺上への頂点挿入
                        if v_idx == -1:
                            n = len(ring)
                            seg_count = n - 1 if gt == _GT_LINE else n - 1
                            for i in range(seg_count):
                                seg = QgsGeometry.fromPolylineXY([ring[i], ring[i + 1]])
                                if seg.distance(QgsGeometry.fromPointXY(point)) <= tol:
                                    new_pt = seg.nearestPoint(
                                        QgsGeometry.fromPointXY(point)
                                    ).asPoint()
                                    ring.insert(i + 1, QgsPointXY(new_pt))
                                    v_idx = i + 1
                                    break

                        if v_idx != -1:
                            v_info.append((f.id(), pi, ri, v_idx))
                            found = True

                if v_info:
                    if lid not in self.affected_info:
                        self.affected_info[lid]  = []
                        self.affected_geoms[lid] = {}
                        self.backup_geoms[lid]   = {}

                    self.affected_info[lid].extend(v_info)
                    self.backup_geoms[lid][f.id()] = self._deep_copy_struct(struct)
                    self.affected_geoms[lid][f.id()] = self._deep_copy_struct(struct)

                    rb = QgsRubberBand(self.canvas, layer.geometryType())
                    rb.setColor(QColor(255, 200, 0, 160))
                    rb.setWidth(3)
                    self.rb_list.append((lid, f.id(), rb))
                    geom = self._struct_to_geometry(struct, layer, f.id())
                    rb.setToGeometry(geom, layer)

        return found

    # ------------------------------------------------------------------
    # マウスイベント
    # ------------------------------------------------------------------

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._cancel()
            return
        if event.button() != Qt.LeftButton:
            return

        point = self._snap(event.pos())

        if self.state is None:
            found = self.select_vertex(point)
            if found:
                self.state = 'selected'
                self.marker.setCenter(point)
                self.marker.show()
                self.hover_marker.hide()
                self.log("移動先をクリックして確定（右クリック/Escでキャンセル）", 0)
            else:
                self.log("頂点または辺の上をクリックしてください", 2000)

        elif self.state == 'selected':
            self._rebuild_from_backup()
            self._apply_move(point)
            self.finish_move()

    def canvasMoveEvent(self, event):
        map_pt = self._snap(event.pos())

        if self.state == 'selected':
            self._rebuild_from_backup()
            self._apply_move(map_pt)
            self._update_rubberbands()
            self.marker.setCenter(map_pt)
        else:
            self.hover_marker.setCenter(map_pt)
            self.hover_marker.show()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._cancel()

    # ------------------------------------------------------------------
    # 移動の適用
    # ------------------------------------------------------------------

    def _rebuild_from_backup(self):
        """プレビュー・確定の前にbackupから作業用構造体を再生成する"""
        for lid, fid_struct in self.backup_geoms.items():
            if lid not in self.affected_geoms:
                self.affected_geoms[lid] = {}
            for fid, struct in fid_struct.items():
                self.affected_geoms[lid][fid] = self._deep_copy_struct(struct)

    def _apply_move(self, map_pt):
        """affected_geoms内の対象頂点を map_pt へ移動する（コピーを代入）"""
        for lid, info in self.affected_info.items():
            layer = QgsProject.instance().mapLayer(lid)
            gt = int(layer.geometryType()) if layer else _GT_POLYGON

            for fid, pi, ri, vi in info:
                struct = self.affected_geoms[lid][fid]
                ring   = struct[pi][ri]
                rl     = len(ring)

                ring[vi] = QgsPointXY(map_pt)

                if gt == _GT_POLYGON:
                    if vi == 0:
                        ring[rl - 1] = QgsPointXY(map_pt)
                    elif vi == rl - 1:
                        ring[0] = QgsPointXY(map_pt)

    def _update_rubberbands(self):
        for lid, fid, rb in self.rb_list:
            layer = QgsProject.instance().mapLayer(lid)
            if not layer:
                continue
            struct = self.affected_geoms[lid].get(fid)
            if struct is None:
                continue
            rb.setToGeometry(self._struct_to_geometry(struct, layer, fid), layer)

    # ------------------------------------------------------------------
    # 確定
    # ------------------------------------------------------------------

    def finish_move(self):
        PRECISION = 6

        for lid, info_list in self.affected_info.items():
            layer = QgsProject.instance().mapLayer(lid)
            if not layer:
                continue

            layer.beginEditCommand(f"頂点移動: {layer.name()}")
            success = True
            for fid in {info[0] for info in info_list}:
                struct = self.affected_geoms.get(lid, {}).get(fid)
                if struct is None:
                    continue

                for pi, poly in enumerate(struct):
                    for ri, ring in enumerate(poly):
                        for vi, pt in enumerate(ring):
                            struct[pi][ri][vi] = QgsPointXY(
                                round(pt.x(), PRECISION),
                                round(pt.y(), PRECISION),
                            )

                geom = self._struct_to_geometry(struct, layer, fid)
                if not geom.isGeosValid():
                    fixed = geom.makeValid()
                    if fixed.isEmpty():
                        self.log(f"ジオメトリが無効: fid={fid}")
                        success = False
                        break
                    geom = fixed

                layer.changeGeometry(fid, geom)

            if success:
                layer.endEditCommand()
            else:
                layer.destroyEditCommand()

        self._reset_state()
        self.canvas.refreshAllLayers()
        self.log("移動を確定しました")

    # ------------------------------------------------------------------
    # キャンセル・クリーンアップ
    # ------------------------------------------------------------------

    def _cancel(self):
        """
        [FIX] キャンセル時も必ず beginEditCommand/endEditCommand で囲む。
        囲まずに changeGeometry を呼ぶと編集コマンドスタックの外側で
        ジオメトリが書き換わり、後続の commitChanges でトランザクションが
        破壊されて disk I/O error の原因になる。
        """
        if self.state == 'selected':
            for lid, fid_struct in self.backup_geoms.items():
                layer = QgsProject.instance().mapLayer(lid)
                if not layer:
                    continue
                # レイヤがすでに編集モードでない場合はキャンセル処理をスキップ
                if not layer.isEditable():
                    continue
                layer.beginEditCommand("頂点移動キャンセル")
                for fid, struct in fid_struct.items():
                    # [FIX] fid がレイヤに実在するか確認してから書き戻す。
                    # 新規地物を選択後にキャンセルした場合、その地物が
                    # まだ確定していないケースで存在しないfidへの
                    # changeGeometry 呼び出しがトランザクションを壊す。
                    request = QgsFeatureRequest().setFilterFid(fid)
                    if not any(True for _ in layer.getFeatures(request)):
                        continue
                    layer.changeGeometry(
                        fid, self._struct_to_geometry(struct, layer, fid))
                layer.endEditCommand()
            self.canvas.refreshAllLayers()
            self.log("移動をキャンセルしました")
        self._reset_state()

    def _reset_state(self):
        self.state = None
        self.marker.hide()
        self.hover_marker.show()
        self._clear_rubberbands()
        self.affected_info  = {}
        self.affected_geoms = {}
        self.backup_geoms   = {}
        self.is_multipart   = {}
        self.geom_types     = {}
        self.canvas.refresh()
        self.log("頂点/辺をクリックして選択", 0)

    def _clear_rubberbands(self):
        for _, _, rb in self.rb_list:
            try:
                self.canvas.scene().removeItem(rb)
            except Exception:
                pass
        self.rb_list = []

    def deactivate(self):
        self._reset_state()
        self.hover_marker.hide()
        self.marker.hide()
        super().deactivate()


# ===========================================================================
# QuickSplitTool  ― 複数頂点指定によるポリゴン/ライン分割（曲線対応）
#
# 操作方法:
#   左クリック    : 分割ラインに頂点を追加
#   ダブルクリック: 現在位置を最終頂点として分割を確定
#   Enterキー     : 最後にクリックした頂点までで分割を確定（2点以上必要）
#   右クリック    : 直前の頂点を1つ取り消し（頂点がなければキャンセル）
#   Backspaceキー : 直前の頂点を1つ取り消し
#   Escキー       : 分割操作を全キャンセル
#   Cキー         : 曲線モードON/OFF切り替え
#                   （ONの場合、確定時にChaikinスムージングで頂点間を曲線化してから分割）
# ===========================================================================

class QuickSplitTool(QgsMapTool):
    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas

        self.points = []          # 確定済み頂点（QgsPointXY）
        self.curve_mode = False
        self.smooth_iterations = 3   # Chaikinスムージングの反復回数
        self.smooth_offset = 0.25    # スムージングのオフセット（0〜0.5）

        # 確定済み頂点をつなぐ実線
        self.rb = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.rb.setColor(QColor(0, 255, 255, 200))
        self.rb.setWidth(3)

        # 最終頂点からカーソルまでのプレビュー破線
        self.preview_rb = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.preview_rb.setColor(QColor(0, 255, 255, 120))
        self.preview_rb.setWidth(2)
        self.preview_rb.setLineStyle(Qt.DashLine)

        self.vertex_markers = []

        self.snap_indicator = QgsSnapIndicator(self.canvas)

    # ------------------------------------------------------------------
    # スナップ
    # ------------------------------------------------------------------

    def _snap(self, pos):
        utils = self.canvas.snappingUtils()
        config = QgsSnappingConfig()
        config.setEnabled(True)
        # 頂点だけでなく辺（セグメント）にもスナップする。
        # 分割ラインの始点・終点はポリゴン境界の「辺の途中」に乗せたいケースが多く、
        # 頂点のみのスナップだと境界線上に正確に乗らず、境界を横切りきれずに
        # splitFeaturesが不発（NothingHappened等）になりやすい。
        config.setType(QgsSnappingConfig.VertexAndSegment)
        config.setTolerance(20)
        config.setUnits(QgsTolerance.Pixels)
        config.setMode(QgsSnappingConfig.AllLayers)
        utils.setConfig(config)
        map_pt = self.toMapCoordinates(pos)
        res = utils.snapToMap(map_pt)
        return res, (res.point() if res.isValid() else map_pt)

    # ------------------------------------------------------------------
    # ステータス表示
    # ------------------------------------------------------------------

    def activate(self):
        super().activate()
        self._show_status()

    def _show_status(self):
        mode = "曲線" if self.curve_mode else "直線"
        n = len(self.points)
        self.canvas.window().statusBar().showMessage(
            f"[{mode}モード(Cで切替)] 頂点{n}個 - "
            f"クリックで頂点追加 / ダブルクリックorEnterで確定 / "
            f"右クリックorBackspaceで1つ戻す / Escでキャンセル", 0)

    # ------------------------------------------------------------------
    # 頂点マーカー
    # ------------------------------------------------------------------

    def _add_vertex_marker(self, point):
        m = QgsVertexMarker(self.canvas)
        m.setColor(QColor(0, 255, 255))
        m.setIconType(QgsVertexMarker.ICON_BOX)
        m.setPenWidth(2)
        m.setIconSize(8)
        m.setCenter(point)
        self.vertex_markers.append(m)

    def _clear_vertex_markers(self):
        for m in self.vertex_markers:
            try:
                self.canvas.scene().removeItem(m)
            except Exception:
                pass
        self.vertex_markers = []

    def _rebuild_rubberband(self):
        self.rb.reset(QgsWkbTypes.LineGeometry)
        for pt in self.points:
            self.rb.addPoint(pt)

    def _dedup_tolerance(self):
        """同一頂点とみなす距離（画面3px相当をマップ単位に換算）"""
        return max(self.canvas.mapUnitsPerPixel() * 3, 1e-9)

    def _dedupe_points(self, points):
        """連続する近接頂点（重複クリック由来）を除去する"""
        if not points:
            return points
        tol = self._dedup_tolerance()
        result = [points[0]]
        for pt in points[1:]:
            if result[-1].distance(pt) > tol:
                result.append(pt)
        return result

    # ------------------------------------------------------------------
    # マウスイベント
    # ------------------------------------------------------------------

    def canvasMoveEvent(self, event):
        res, point = self._snap(event.pos())
        self.snap_indicator.setMatch(res)
        if self.points:
            self.preview_rb.reset(QgsWkbTypes.LineGeometry)
            self.preview_rb.addPoint(self.points[-1])
            self.preview_rb.addPoint(point)

    def canvasPressEvent(self, event):
        if event.button() == Qt.RightButton:
            if self.points:
                self._undo_last()
            else:
                self.reset()
                self.canvas.window().statusBar().showMessage("分割をキャンセルしました", 2000)
            return
        if event.button() != Qt.LeftButton:
            return

        _, point = self._snap(event.pos())

        # 直前の頂点とほぼ同じ位置（同じ頂点にスナップした重複クリック）は無視する。
        # これをしないと、ダブルクリックの1回目・2回目の両方がcanvasPressEventで
        # 拾われた際に同一座標の頂点が2つ並び、最後のセグメントが長さゼロになって
        # splitFeaturesが失敗する。
        if self.points and self.points[-1].distance(point) <= self._dedup_tolerance():
            return

        self.points.append(point)
        self._add_vertex_marker(point)
        self._rebuild_rubberband()
        self._show_status()

    def canvasDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        _, point = self._snap(event.pos())

        # ダブルクリックの1回目の押下はcanvasPressEventで既に頂点として追加済みのため、
        # ほぼ同じ座標であれば追加しない
        if not self.points or self.points[-1].distance(point) > self._dedup_tolerance():
            self.points.append(point)

        points = self._dedupe_points(self.points)
        if len(points) < 2:
            self.canvas.window().statusBar().showMessage("分割には2点以上必要です", 2000)
            return

        self.execute_split(points)
        self.reset()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reset()
            self.canvas.window().statusBar().showMessage("分割をキャンセルしました", 2000)

        elif event.key() == Qt.Key_C:
            self.curve_mode = not self.curve_mode
            self._show_status()

        elif event.key() in (Qt.Key_Return, Qt.Key_Enter):
            points = self._dedupe_points(self.points)
            if len(points) >= 2:
                self.execute_split(points)
                self.reset()
            else:
                self.canvas.window().statusBar().showMessage("分割には2点以上必要です", 2000)

        elif event.key() == Qt.Key_Backspace:
            if self.points:
                self._undo_last()

    def _undo_last(self):
        self.points.pop()
        if self.vertex_markers:
            m = self.vertex_markers.pop()
            try:
                self.canvas.scene().removeItem(m)
            except Exception:
                pass
        self._rebuild_rubberband()
        self._show_status()

    # ------------------------------------------------------------------
    # 分割実行
    # ------------------------------------------------------------------

    def _build_split_line(self, points):
        """曲線モード時は頂点列をChaikinスムージングで曲線化した点列に変換する。
        頂点が2点のみの場合は直線のまま返す。"""
        if not self.curve_mode or len(points) < 3:
            return points

        line_geom = QgsGeometry.fromPolylineXY(points)
        smoothed = line_geom.smooth(self.smooth_iterations, self.smooth_offset)
        if smoothed and not smoothed.isEmpty():
            pts = smoothed.asPolyline()
            if pts:
                return [QgsPointXY(p) for p in pts]
        return points

    def _to_layer_crs(self, points, layer):
        """キャンバス（プロジェクト）CRSの座標をレイヤCRSへ変換する。
        再投影（オンザフライ変換）されているレイヤでは、キャンバス上の見た目の位置と
        レイヤ内部の座標系が異なるため、splitFeaturesに渡す前に必ず変換する必要がある。"""
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        layer_crs = layer.crs()
        if canvas_crs == layer_crs:
            return points
        transform = QgsCoordinateTransform(canvas_crs, layer_crs, QgsProject.instance())
        try:
            return [transform.transform(pt) for pt in points]
        except Exception as e:
            self.canvas.window().statusBar().showMessage(f"座標変換に失敗しました: {e}", 4000)
            return points

    def execute_split(self, points):
        layer = self.canvas.currentLayer()
        if not layer or not layer.isEditable():
            self._show_failure_dialog(
                "レイヤ未選択/未編集",
                "アクティブレイヤが分割対象のポリゴンレイヤになっているか、"
                "編集モードが有効か確認してください。\n"
                f"現在のアクティブレイヤ: {layer.name() if layer else 'なし'}"
            )
            return

        split_line = self._build_split_line(points)
        split_line = self._to_layer_crs(split_line, layer)

        result = self._try_split(layer, split_line, "複数点/曲線分割")
        code_name = result.name if hasattr(result, "name") else str(result)

        # 対象ポリゴンのジオメトリ自体が無効な場合は自動修復して1回だけ再試行する
        if code_name == "InvalidBaseGeometry":
            if self._autofix_invalid_geometries(layer, split_line):
                self.canvas.window().statusBar().showMessage(
                    "無効なジオメトリを自動修復しました。分割を再試行します…", 3000)
                result = self._try_split(layer, split_line, "複数点/曲線分割（再試行）")
                code_name = result.name if hasattr(result, "name") else str(result)

        if result == Qgis.GeometryOperationResult.Success:
            self.canvas.window().statusBar().showMessage("分割成功", 2000)
        else:
            detail = self._explain_result_code(result)
            if code_name == "NothingHappened":
                detail += "\n\n" + self._diagnose_no_effect(layer, split_line)
            self._show_failure_dialog(
                f"分割失敗（{code_name} / Code: {int(result)}）",
                detail + "\n\n"
                f"対象レイヤ: {layer.name()}\n"
                f"分割ライン頂点数: {len(split_line)}"
            )

        self.canvas.refresh()

    def _try_split(self, layer, split_line, cmd_name):
        layer.beginEditCommand(cmd_name)
        result = layer.splitFeatures(split_line, True)
        if result == Qgis.GeometryOperationResult.Success:
            layer.endEditCommand()
        else:
            layer.destroyEditCommand()
        return result

    def _autofix_invalid_geometries(self, layer, split_line):
        """分割ライン周辺の地物のうち、無効なジオメトリをmakeValid()で修復する。
        1件でも修復できればTrueを返す。"""
        line_geom = QgsGeometry.fromPolylineXY(split_line)
        rect = line_geom.boundingBox()
        try:
            nearby = list(layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)))
        except Exception:
            return False

        to_fix = []
        for f in nearby:
            g = f.geometry()
            if g and not g.isEmpty() and not g.isGeosValid():
                fixed = g.makeValid()
                if fixed and not fixed.isEmpty():
                    to_fix.append((f.id(), fixed))

        if not to_fix:
            return False

        layer.beginEditCommand("無効ジオメトリの自動修復")
        for fid, fixed_geom in to_fix:
            layer.changeGeometry(fid, fixed_geom)
        layer.endEditCommand()
        return True

    def _diagnose_no_effect(self, layer, split_line):
        """NothingHappened時に、分割ラインと実際に交差する地物があるかを診断する"""
        line_geom = QgsGeometry.fromPolylineXY(split_line)
        rect = line_geom.boundingBox()
        try:
            nearby = list(layer.getFeatures(QgsFeatureRequest().setFilterRect(rect)))
        except Exception as e:
            return f"診断中にエラー: {e}"

        crossing = 0
        for f in nearby:
            g = f.geometry()
            if g and not g.isEmpty() and g.intersects(line_geom):
                crossing += 1

        total = layer.featureCount()
        return (
            "--- 診断結果 ---\n"
            f"レイヤ全体の地物数: {total}\n"
            f"分割ライン周辺(bbox内)の地物数: {len(nearby)}\n"
            f"分割ラインと実際に交差している地物数: {crossing}\n"
            + ("→ 周辺に地物が0件です。表示されている境界線は別レイヤの可能性が高いです。"
               "レイヤパネルでアクティブレイヤ（太字）を確認してください。"
               if len(nearby) == 0 else
               "→ 交差している地物はあるのに分割されない場合、ポリゴンの端をわずかに"
               "かすっているだけ（境界を貫通せず接しているだけ）の可能性があります。")
        )

    def _explain_result_code(self, result):
        # Qgis.GeometryOperationResult の実際のenum名で判定する
        # （数値は QGIS のバージョンによってズレる可能性があるため名前で照合する）
        name = result.name if hasattr(result, "name") else str(result)
        explanations = {
            "NothingHappened":
                "分割ラインがポリゴンの境界を横切って完全に突き抜けていません。"
                "始点・終点をポリゴンの外側まで延ばして描画してください。",
            "InvalidBaseGeometry":
                "対象ポリゴンのジオメトリ自体が無効です（自己交差など）。"
                "自動修復を試みましたが解消できませんでした。QGISのジオメトリ検証ツール"
                "（プロセッシング「Fix geometries」等）で対象フィーチャを確認してください。",
            "GeometryEngineError":
                "ジオメトリエンジンでのエラーです。分割ラインが自己交差していないか確認してください。",
            "InvalidInputGeometryType":
                "分割ラインのジオメトリタイプが不正です。",
            "LayerNotEditable":
                "レイヤが編集モードではありません。",
            "SplitCannotSplitPoint":
                "ポイントレイヤは分割できません。",
        }
        return explanations.get(
            name,
            f"詳細不明のエラーです（{name}）。QGISの「ログメッセージパネル」も合わせて確認してください。"
        )

    def _show_failure_dialog(self, title, message):
        from qgis.PyQt.QtWidgets import QMessageBox
        self.canvas.window().statusBar().showMessage(f"失敗：{title}", 4000)
        box = QMessageBox(self.canvas.window())
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("地物分割エラー")
        box.setText(title)
        box.setInformativeText(message)
        box.exec_()

    # ------------------------------------------------------------------
    # リセット
    # ------------------------------------------------------------------

    def reset(self):
        self.points = []
        self.rb.reset(QgsWkbTypes.LineGeometry)
        self.preview_rb.reset(QgsWkbTypes.LineGeometry)
        self._clear_vertex_markers()
        self.snap_indicator.setMatch(QgsPointLocator.Match())
        self.canvas.window().statusBar().clearMessage()

    def deactivate(self):
        self.reset()
        super().deactivate()
