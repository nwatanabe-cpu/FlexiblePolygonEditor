"""
polygonize_tool.py
------------------
全ラインレイヤをマージ → Polygonize → 元ポリゴンの属性を引き継いで上書き保存。

属性引き継ぎの優先順位:
  1. 空間的に重なる元ポリゴンのうち、画地No（IDフィールド）が一致するもの
  2. IDフィールド未指定 or 一致なし → 重複面積最大のものにフォールバック

自動閉合機能:
  A. 孤立端点スナップ: 指定距離内の孤立端点同士を直線で接続
  B. セグメント強制閉合: 各ラインの始点≠終点であれば閉合セグメントを追加
  どちらも元のラインレイヤを直接編集する（Undo可能）
"""

import math
import traceback
from collections import defaultdict

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QComboBox,
    QPushButton, QMessageBox, QProgressDialog,
    QAbstractItemView, QDoubleSpinBox, QGroupBox,
    QRadioButton, QButtonGroup,
)
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsProject, QgsVectorLayer,
    QgsGeometry, QgsFeature, QgsPointXY,
    QgsSpatialIndex, QgsFeatureRequest,
    QgsWkbTypes, QgsField,
    QgsFields, QgsMemoryProviderUtils,
    QgsVertexId,
)

# 自動閉合のデフォルトスナップ距離（座標単位）
_DEFAULT_SNAP_DISTANCE = 0.1


def _get_tolerance():
    """プロジェクトCRSに応じた端点許容差を返す"""
    crs = QgsProject.instance().crs()
    if crs.isGeographic():
        return 1e-8   # 地理座標(度)用 ≒ 1mm
    return 1e-4       # 平面直角(m)用 = 0.1mm


def _round_key(pt, tol=None):
    """座標を許容差で丸めてハッシュキーに変換する"""
    if tol is None:
        tol = _get_tolerance()
    decimals = max(0, int(-1 * round(math.log10(tol))))
    return (round(pt.x(), decimals), round(pt.y(), decimals))


def _collect_segments(line_ids):
    """
    指定レイヤIDのラインを走査し、各セグメント情報を返す。

    Returns
    -------
    list of dict:
        layer_id, feature_id, seg_index, points, start_pt, end_pt
    """
    segments = []
    for lid in line_ids:
        layer = QgsProject.instance().mapLayer(lid)
        if not layer:
            continue
        for f in layer.getFeatures():
            g = f.geometry()
            if not g or g.isEmpty():
                continue
            base = QgsWkbTypes.flatType(g.wkbType())
            if base not in (
                QgsWkbTypes.LineString,
                QgsWkbTypes.MultiLineString,
                QgsWkbTypes.CompoundCurve,
                QgsWkbTypes.MultiCurve,
            ):
                continue
            if g.isMultipart():
                lines = g.asMultiPolyline()
            else:
                lines = [g.asPolyline()]
            for seg_idx, pts in enumerate(lines):
                if len(pts) < 2:
                    continue
                segments.append({
                    'layer_id':   lid,
                    'feature_id': f.id(),
                    'seg_index':  seg_idx,
                    'points':     pts,
                    'start_pt':   pts[0],
                    'end_pt':     pts[-1],
                })
    return segments


# ---------------------------------------------------------------------------
# 自動閉合ロジック
# ---------------------------------------------------------------------------

def _move_endpoint(layer, feature_id, seg_index, role, new_pt):
    """
    既存フィーチャの始点（role='start'）または終点（role='end'）を
    new_pt に移動して changeGeometry で書き込む。
    呼び出し元で beginEditCommand/endEditCommand を管理すること。

    Parameters
    ----------
    layer      : QgsVectorLayer
    feature_id : int
    seg_index  : int  マルチパーツのパーツインデックス（シングルは0）
    role       : 'start' | 'end'
    new_pt     : QgsPointXY  移動先座標
    """
    feat = next(layer.getFeatures(QgsFeatureRequest().setFilterFid(feature_id)))
    g    = feat.geometry()
    if g.isMultipart():
        lines = g.asMultiPolyline()
        pts   = list(lines[seg_index])
        if role == 'start':
            pts[0]  = new_pt
        else:
            pts[-1] = new_pt
        lines[seg_index] = pts
        new_geom = QgsGeometry.fromMultiPolylineXY(lines)
    else:
        pts = list(g.asPolyline())
        if role == 'start':
            pts[0]  = new_pt
        else:
            pts[-1] = new_pt
        new_geom = QgsGeometry.fromPolylineXY(pts)
    layer.changeGeometry(feature_id, new_geom)


def auto_close_snap(line_ids, snap_distance):
    """
    孤立端点スナップ閉合。

    孤立端点ペアの両端点を中点座標に移動して既存フィーチャのジオメトリを
    直接書き換える。新規フィーチャは追加しない。

    Returns
    -------
    tuple: (処理したペア数, 変更したレイヤIDのset, 警告メッセージ or None)
    """
    segments = _collect_segments(line_ids)
    if not segments:
        return 0, set(), "有効なラインセグメントが見つかりませんでした。"

    tol = _get_tolerance()

    # 全端点を丸めキーで集計
    # key -> [{'seg': seg, 'role': 'start'|'end', 'real_pt': QgsPointXY}]
    endpoint_info = defaultdict(list)
    for seg in segments:
        sk = _round_key(seg['start_pt'], tol)
        ek = _round_key(seg['end_pt'], tol)
        if sk == ek:
            continue
        endpoint_info[sk].append({'seg': seg, 'role': 'start', 'real_pt': seg['start_pt']})
        endpoint_info[ek].append({'seg': seg, 'role': 'end',   'real_pt': seg['end_pt']})

    # 孤立端点 = 出現回数1
    dangling_keys = {k: v for k, v in endpoint_info.items() if len(v) == 1}
    dangling_list = list(dangling_keys.items())

    if not dangling_list:
        return 0, set(), None

    # 最近傍マッチング（同一フィーチャ内はスキップ）
    used  = set()
    pairs = []  # [(info1, info2), ...]
    for i, (k1, infos1) in enumerate(dangling_list):
        if i in used:
            continue
        info1 = infos1[0]
        seg1  = info1['seg']
        best_j    = None
        best_dist = float('inf')
        for j, (k2, infos2) in enumerate(dangling_list):
            if j <= i or j in used:
                continue
            info2 = infos2[0]
            seg2  = info2['seg']
            if (seg1['layer_id']   == seg2['layer_id']
                    and seg1['feature_id'] == seg2['feature_id']
                    and seg1['seg_index']  == seg2['seg_index']):
                continue
            d = math.hypot(info2['real_pt'].x() - info1['real_pt'].x(),
                           info2['real_pt'].y() - info1['real_pt'].y())
            if d <= snap_distance and d < best_dist:
                best_dist = d
                best_j    = j
        if best_j is not None:
            pairs.append((info1, dangling_list[best_j][1][0]))
            used.add(i)
            used.add(best_j)

    unpaired = len(dangling_list) - len(used)
    if not pairs:
        return 0, set(), (
            f"孤立端点が {len(dangling_list)} 箇所ありますが、"
            f"スナップ距離 {snap_distance} 以内に対になる端点が見つかりませんでした。\n"
            "スナップ距離を広げるか、手動でスナップしてください。"
        )

    # layer_id ごとに変更をまとめて書き込む
    # { layer_id: [(feature_id, seg_index, role, new_pt), ...] }
    changes = defaultdict(list)
    for info1, info2 in pairs:
        pt1 = info1['real_pt']
        pt2 = info2['real_pt']
        mid = QgsPointXY((pt1.x() + pt2.x()) / 2.0,
                         (pt1.y() + pt2.y()) / 2.0)
        seg1 = info1['seg']
        seg2 = info2['seg']
        changes[seg1['layer_id']].append(
            (seg1['feature_id'], seg1['seg_index'], info1['role'], mid))
        changes[seg2['layer_id']].append(
            (seg2['feature_id'], seg2['seg_index'], info2['role'], mid))

    changed_layers = set()
    for lid, edits in changes.items():
        layer = QgsProject.instance().mapLayer(lid)
        if not layer:
            continue
        if not layer.isEditable():
            layer.startEditing()
        layer.beginEditCommand("自動閉合 — 孤立端点スナップ（中点移動）")
        try:
            for fid, seg_idx, role, new_pt in edits:
                _move_endpoint(layer, fid, seg_idx, role, new_pt)
            layer.endEditCommand()
            changed_layers.add(lid)
        except Exception:
            layer.destroyEditCommand()
            raise

    warn_msg = None
    if unpaired > 0:
        warn_msg = (
            f"{len(pairs)} 箇所の孤立端点ペアを中点にスナップしました。\n"
            f"ただし {unpaired} 箇所の孤立端点はスナップ距離内に対がなく未処理です。\n"
            "スナップ距離を広げるか、手動で修正してください。"
        )

    return len(pairs), changed_layers, warn_msg


def auto_close_force(line_ids):
    """
    セグメント強制閉合。

    各ラインセグメントの始点≠終点であれば、終点を始点座標に移動して
    既存フィーチャのジオメトリを直接書き換える。
    新規フィーチャは追加しない。

    Returns
    -------
    tuple: (閉合したセグメント数, 変更したレイヤIDのset)
    """
    tol      = _get_tolerance()
    segments = _collect_segments(line_ids)

    # layer_id ごとにまとめる
    # { layer_id: [(feature_id, seg_index, end_pt→start_pt), ...] }
    changes = defaultdict(list)
    for seg in segments:
        sp = seg['start_pt']
        ep = seg['end_pt']
        if _round_key(sp, tol) == _round_key(ep, tol):
            continue
        changes[seg['layer_id']].append(
            (seg['feature_id'], seg['seg_index'], sp))  # 終点を始点座標に移動

    closed_cnt     = 0
    changed_layers = set()

    for lid, edits in changes.items():
        layer = QgsProject.instance().mapLayer(lid)
        if not layer:
            continue
        if not layer.isEditable():
            layer.startEditing()
        layer.beginEditCommand("自動閉合 — セグメント強制閉合（終点→始点移動）")
        try:
            for fid, seg_idx, new_pt in edits:
                _move_endpoint(layer, fid, seg_idx, 'end', new_pt)
                closed_cnt += 1
            layer.endEditCommand()
            changed_layers.add(lid)
        except Exception:
            layer.destroyEditCommand()
            raise

    return closed_cnt, changed_layers


# ---------------------------------------------------------------------------
# ダイアログ
# ---------------------------------------------------------------------------

class PolygonizeDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Polygonize — ライン→ポリゴン再生成")
        self.setMinimumWidth(480)
        self._build_ui()
        self._populate()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ──── ラインレイヤ選択 ────
        layout.addWidget(QLabel("使用するラインレイヤ（複数選択可）:"))
        self.line_list = QListWidget()
        self.line_list.setSelectionMode(QAbstractItemView.MultiSelection)
        self.line_list.setMinimumHeight(110)
        layout.addWidget(self.line_list)

        # ──── 端点診断 ────
        diag_layout = QHBoxLayout()
        self.diag_btn = QPushButton("端点チェック（未閉合ライン診断）")
        self.diag_btn.setToolTip(
            "選択したラインレイヤ内で、他のラインと接続されていない端点を検出します。\n"
            "Polygonizeが失敗する原因の特定に使ってください。"
        )
        self.diag_btn.clicked.connect(self._on_diagnose)
        diag_layout.addWidget(self.diag_btn)
        layout.addLayout(diag_layout)

        # ──── 自動閉合グループ ────
        close_group = QGroupBox("自動閉合（実行前にラインを修正）")
        close_layout = QVBoxLayout(close_group)

        self.radio_snap  = QRadioButton(
            "A. 孤立端点スナップ — 指定距離以内の孤立端点同士を直線で接続")
        self.radio_force = QRadioButton(
            "B. セグメント強制閉合 — 各ラインの終点と始点を結ぶ閉合ラインを追加")
        self.radio_snap.setChecked(True)
        close_layout.addWidget(self.radio_snap)

        snap_row = QHBoxLayout()
        self.snap_label = QLabel("　スナップ距離:")
        self.snap_spin  = QDoubleSpinBox()
        self.snap_spin.setRange(0.001, 10000.0)
        self.snap_spin.setValue(_DEFAULT_SNAP_DISTANCE)
        self.snap_spin.setDecimals(3)
        self.snap_spin.setSuffix("  （座標単位）")
        self.snap_spin.setMinimumWidth(180)
        snap_row.addWidget(self.snap_label)
        snap_row.addWidget(self.snap_spin)
        snap_row.addStretch()
        close_layout.addLayout(snap_row)

        close_layout.addWidget(self.radio_force)

        self.auto_close_btn = QPushButton("自動閉合を実行（元レイヤを直接編集）")
        self.auto_close_btn.clicked.connect(self._on_auto_close)
        close_layout.addWidget(self.auto_close_btn)

        self.radio_snap.toggled.connect(self._on_mode_toggled)
        layout.addWidget(close_group)

        # ──── 属性引き継ぎ元ポリゴン ────
        layout.addWidget(QLabel("属性引き継ぎ元ポリゴンレイヤ（所有者名・画地No等）:"))
        self.poly_combo = QComboBox()
        self.poly_combo.currentIndexChanged.connect(self._on_poly_changed)
        layout.addWidget(self.poly_combo)

        id_layout = QHBoxLayout()
        id_layout.addWidget(QLabel("画地NoフィールドID突合:"))
        self.id_field_combo = QComboBox()
        self.id_field_combo.setMinimumWidth(160)
        id_layout.addWidget(self.id_field_combo)
        id_layout.addStretch()
        layout.addLayout(id_layout)
        layout.addWidget(QLabel(
            "　※ 空間的に重なる元ポリゴンのうちIDが一致するものを優先します\n"
            "　　 一致なしの場合は重複面積最大にフォールバック"
        ))

        # ──── 更新先ポリゴン ────
        layout.addWidget(QLabel("更新先ポリゴンレイヤ（なければ新規作成）:"))
        self.dest_combo = QComboBox()
        layout.addWidget(self.dest_combo)

        # ──── OK / キャンセル ────
        btn_layout = QHBoxLayout()
        self.ok_btn     = QPushButton("Polygonize 実行")
        self.cancel_btn = QPushButton("キャンセル")
        btn_layout.addWidget(self.ok_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

        self.ok_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)

    def _on_mode_toggled(self, snap_checked):
        self.snap_label.setEnabled(snap_checked)
        self.snap_spin.setEnabled(snap_checked)

    def _populate(self):
        layers = list(QgsProject.instance().mapLayers().values())

        line_layers = [l for l in layers
                       if isinstance(l, QgsVectorLayer)
                       and l.geometryType() == QgsWkbTypes.LineGeometry]
        poly_layers = [l for l in layers
                       if isinstance(l, QgsVectorLayer)
                       and l.geometryType() == QgsWkbTypes.PolygonGeometry]

        for l in line_layers:
            item = QListWidgetItem(l.name())
            item.setData(Qt.UserRole, l.id())
            self.line_list.addItem(item)
            item.setSelected(True)

        self.poly_combo.addItem("（なし）", None)
        self.dest_combo.addItem("（新規メモリレイヤ）", None)
        for l in poly_layers:
            self.poly_combo.addItem(l.name(), l.id())
            self.dest_combo.addItem(l.name(), l.id())

    def _on_poly_changed(self):
        self.id_field_combo.clear()
        self.id_field_combo.addItem("（指定なし）", None)
        lid = self.poly_combo.currentData()
        if not lid:
            return
        layer = QgsProject.instance().mapLayer(lid)
        if not layer:
            return
        for field in layer.fields():
            self.id_field_combo.addItem(field.name(), field.name())
            name_lower = field.name().lower()
            if any(k in name_lower for k in ('no', 'id', '地番', '画地', 'chiban')):
                self.id_field_combo.setCurrentIndex(self.id_field_combo.count() - 1)

    def _on_diagnose(self):
        """選択ラインレイヤの端点を診断し、孤立端点（未閉合箇所）を報告する"""
        line_ids = self.selected_line_ids()
        if not line_ids:
            QMessageBox.warning(self, "警告", "ラインレイヤを選択してください。")
            return

        tol = _get_tolerance()
        endpoint_info = defaultdict(list)
        total_lines   = 0

        for lid in line_ids:
            layer = QgsProject.instance().mapLayer(lid)
            if not layer:
                continue
            for f in layer.getFeatures():
                g = f.geometry()
                if not g or g.isEmpty():
                    continue
                base = QgsWkbTypes.flatType(g.wkbType())
                if base not in (
                    QgsWkbTypes.LineString,
                    QgsWkbTypes.MultiLineString,
                    QgsWkbTypes.CompoundCurve,
                    QgsWkbTypes.MultiCurve,
                ):
                    continue
                if g.isMultipart():
                    lines = g.asMultiPolyline()
                else:
                    lines = [g.asPolyline()]
                for line in lines:
                    if len(line) < 2:
                        continue
                    sp = line[0]
                    ep = line[-1]
                    if _round_key(sp, tol) == _round_key(ep, tol):
                        continue
                    total_lines += 1
                    for pt in [sp, ep]:
                        endpoint_info[_round_key(pt, tol)].append(pt)

        # 孤立端点 = 出現回数1
        dangling_keys = {k: v for k, v in endpoint_info.items() if len(v) == 1}
        dangling      = list(dangling_keys.keys())

        if not dangling:
            # 孤立0でも許容差ギリギリのズレが潜んでいる可能性を検出
            near_miss_count = 0
            all_pts = [(k, v[0]) for k, v in endpoint_info.items()]
            for i in range(len(all_pts)):
                ki, pi = all_pts[i]
                for j in range(i + 1, len(all_pts)):
                    kj, pj = all_pts[j]
                    if ki == kj:
                        continue
                    d = math.hypot(pi.x() - pj.x(), pi.y() - pj.y())
                    if 0 < d < tol * 10:
                        near_miss_count += 1

            extra = ""
            if near_miss_count > 0:
                extra = (
                    f"\n\n⚠ ただし {near_miss_count} 箇所で許容差ギリギリの微小ズレが検出されました。\n"
                    "端点チェックは「問題なし」でも Polygonize に失敗することがあります。\n"
                    "その場合はスナップ距離を小さく（例: 0.001）設定して自動閉合を再実行してください。"
                )

            QMessageBox.information(
                self, "端点チェック結果",
                f"ライン数: {total_lines} 本\n"
                "孤立端点（未閉合箇所）は検出されませんでした。\n"
                "Polygonizeを実行できる状態です。" + extra
            )
        else:
            msg = (
                f"ライン数: {total_lines} 本\n"
                f"孤立端点: {len(dangling)} 箇所\n\n"
                "これらの箇所でラインが閉じておらず Polygonize が失敗する可能性があります。\n"
                "「自動閉合」ボタンで修正できます。\n\n"
                "孤立端点の座標（先頭10件）:\n"
            )
            for x, y in dangling[:10]:
                msg += f"  X={x:.4f}, Y={y:.4f}\n"
            QMessageBox.warning(self, "端点チェック結果 — 未閉合箇所あり", msg)

    def _on_auto_close(self):
        """自動閉合を実行し、元レイヤを直接編集する"""
        line_ids = self.selected_line_ids()
        if not line_ids:
            QMessageBox.warning(self, "警告", "ラインレイヤを選択してください。")
            return

        use_snap = self.radio_snap.isChecked()

        if use_snap:
            snap_dist = self.snap_spin.value()
            added, changed_layers, msg = auto_close_snap(line_ids, snap_dist)

            if added == 0:
                if msg:
                    QMessageBox.warning(self, "自動閉合 — 結果", msg)
                else:
                    QMessageBox.information(self, "自動閉合 完了",
                                            "孤立端点は検出されませんでした。")
                return

            for lid in changed_layers:
                layer = QgsProject.instance().mapLayer(lid)
                if layer:
                    layer.commitChanges()

            base_msg = (
                f"孤立端点スナップ: {added} 箇所のペアを中点座標にスナップしました。\n\n"
                "「端点チェック」で残存孤立端点がないか再確認してから\n"
                "Polygonize を実行してください。"
            )
            if msg:
                QMessageBox.warning(self, "自動閉合 完了（一部未処理）",
                                    base_msg + "\n\n" + msg)
            else:
                QMessageBox.information(self, "自動閉合 完了", base_msg)

        else:
            confirm = QMessageBox.question(
                self, "確認",
                "各ラインセグメントの終点と始点を繋ぐ閉合ラインを追加します。\n"
                "続行しますか？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return

            try:
                closed_cnt, changed_layers = auto_close_force(line_ids)
            except Exception:
                QMessageBox.critical(self, "エラー", traceback.format_exc())
                return

            for lid in changed_layers:
                layer = QgsProject.instance().mapLayer(lid)
                if layer:
                    layer.commitChanges()

            if closed_cnt == 0:
                QMessageBox.information(
                    self, "自動閉合 完了",
                    "強制閉合の対象セグメントはありませんでした。\n"
                    "（すべてのセグメントは既に閉じています）"
                )
            else:
                QMessageBox.information(
                    self, "自動閉合 完了",
                    f"セグメント強制閉合: {closed_cnt} 本の終点を始点座標に移動しました。\n\n"
                    "「端点チェック」で残存孤立端点がないか再確認してから\n"
                    "Polygonize を実行してください。"
                )

    def selected_line_ids(self):
        return [item.data(Qt.UserRole)
                for item in self.line_list.selectedItems()]

    def source_poly_id(self):
        return self.poly_combo.currentData()

    def id_field_name(self):
        return self.id_field_combo.currentData()

    def dest_poly_id(self):
        return self.dest_combo.currentData()


# ---------------------------------------------------------------------------
# Polygonize 実行本体
# ---------------------------------------------------------------------------

class PolygonizeTool:
    def __init__(self, iface):
        self.iface = iface

    def run(self):
        dlg = PolygonizeDialog(self.iface.mainWindow())
        if dlg.exec_() != QDialog.Accepted:
            return

        line_ids = dlg.selected_line_ids()
        if not line_ids:
            QMessageBox.warning(self.iface.mainWindow(), "エラー",
                                "ラインレイヤを1つ以上選択してください。")
            return

        progress = QProgressDialog("処理中...", "キャンセル", 0, 5,
                                   self.iface.mainWindow())
        progress.setWindowModality(Qt.WindowModal)
        progress.setValue(0)

        try:
            # Step 1: ラインを収集
            progress.setLabelText("ラインを収集中...")
            progress.setValue(1)

            all_geoms = []
            for lid in line_ids:
                layer = QgsProject.instance().mapLayer(lid)
                if not layer:
                    continue
                for f in layer.getFeatures():
                    g = f.geometry()
                    if not g or g.isEmpty():
                        continue
                    base = QgsWkbTypes.flatType(g.wkbType())
                    if base not in (
                        QgsWkbTypes.LineString,
                        QgsWkbTypes.MultiLineString,
                        QgsWkbTypes.CompoundCurve,
                        QgsWkbTypes.MultiCurve,
                    ):
                        continue
                    # 曲線ジオメトリはLineStringに変換（GEOSが曲線を扱えないため）
                    if base in (QgsWkbTypes.CompoundCurve, QgsWkbTypes.MultiCurve):
                        g = g.convertToType(QgsWkbTypes.LineGeometry, destMultipart=False)
                        if not g or g.isEmpty():
                            continue
                    all_geoms.append(g)

            if not all_geoms:
                QMessageBox.warning(self.iface.mainWindow(), "エラー",
                                    "有効なラインジオメトリが見つかりませんでした。")
                return

            # ── 前処理パイプライン ──────────────────────────────────────────
            # polygonize(GEOS) は「座標が完全に一致するノードで繋がった
            # ネットワーク」を前提とする。端点チェック「孤立0」でも
            # 浮動小数点レベルのズレが残るとポリゴン化できない画地が生じる。
            # 対策: 座標グリッドスナップ → unaryUnion → node の3段階で整合。

            crs      = QgsProject.instance().crs()
            snap_tol = 1e-4 if not crs.isGeographic() else 1e-8

            # (1) 全頂点をグリッドにスナップして浮動小数点ズレを除去する。
            #     QgsGeometry.snap() は QGIS 3.20+ 限定のため、
            #     代わりに全頂点座標を snap_tol 単位のグリッドに丸めた
            #     新ジオメトリを作り直す方式で同等の効果を得る。
            def _snap_geom_to_grid(geom, tol):
                """全頂点を tol グリッドに丸めた新しいジオメトリを返す"""
                decimals = max(0, int(-1 * round(math.log10(tol))))
                if geom.isMultipart():
                    lines = geom.asMultiPolyline()
                else:
                    raw = geom.asPolyline()
                    lines = [raw] if raw else []
                snapped_lines = []
                for line in lines:
                    snapped_lines.append(
                        [QgsPointXY(round(pt.x(), decimals),
                                    round(pt.y(), decimals))
                         for pt in line]
                    )
                if not snapped_lines:
                    return geom
                if len(snapped_lines) == 1:
                    return QgsGeometry.fromPolylineXY(snapped_lines[0])
                return QgsGeometry.fromMultiPolylineXY(snapped_lines)

            snapped_geoms = [_snap_geom_to_grid(g, snap_tol) for g in all_geoms]

            # (2) unaryUnion: 重複ラインを除去し位相を整理
            merged = QgsGeometry.unaryUnion(snapped_geoms)
            if merged is None or merged.isEmpty():
                # グリッドスナップ前にフォールバック
                merged = QgsGeometry.unaryUnion(all_geoms)
            if merged is None or merged.isEmpty():
                QMessageBox.warning(self.iface.mainWindow(), "エラー",
                                    "ラインのマージに失敗しました。")
                return

            # (3) node: T字・十字交点に確実にノードを挿入
            #     QGIS 3.x 全バージョンで利用可能
            try:
                noded = merged.node()
                if noded is None or noded.isEmpty():
                    noded = merged
            except AttributeError:
                noded = merged  # 万が一 node() が存在しない場合のフォールバック

            # Step 2: Polygonize
            progress.setLabelText("Polygonize 実行中...")
            progress.setValue(2)

            polygonized = QgsGeometry.polygonize([noded])
            if polygonized.isEmpty():
                QMessageBox.warning(
                    self.iface.mainWindow(), "失敗",
                    "Polygonize に失敗しました。\n\n"
                    "「端点チェック」ボタンで未閉合箇所を確認し、\n"
                    "「自動閉合」で修正してから再実行してください。"
                )
                return

            new_polys = []
            if polygonized.isMultipart():
                for part in polygonized.asGeometryCollection():
                    if not part.isEmpty():
                        new_polys.append(part)
            else:
                new_polys.append(polygonized)

            # Step 3: 属性引き継ぎ（面積最大フォールバック）
            progress.setLabelText("属性を突合中...")
            progress.setValue(3)

            src_poly_id  = dlg.source_poly_id()
            id_field     = dlg.id_field_name()
            attr_map     = {}
            match_method = {}

            if src_poly_id:
                src_layer      = QgsProject.instance().mapLayer(src_poly_id)
                src_fields     = src_layer.fields()
                src_index      = QgsSpatialIndex(src_layer.getFeatures())
                src_feat_cache = {f.id(): f for f in src_layer.getFeatures()}

                for i, new_geom in enumerate(new_polys):
                    candidates       = src_index.intersects(new_geom.boundingBox())
                    best_fid_by_area = None
                    best_area        = -1.0

                    for fid in candidates:
                        src_f = src_feat_cache[fid]
                        inter = new_geom.intersection(src_f.geometry())
                        if not inter or inter.isEmpty():
                            continue
                        area = inter.area()
                        if area > best_area:
                            best_area        = area
                            best_fid_by_area = fid

                    if best_fid_by_area is not None:
                        src_f = src_feat_cache[best_fid_by_area]
                        attr_map[i] = {
                            src_fields[j].name(): src_f.attributes()[j]
                            for j in range(len(src_fields))
                        }
                        match_method[i] = 'area'

            # Step 4: 書き込み
            progress.setLabelText("ポリゴンレイヤを更新中...")
            progress.setValue(4)

            dest_poly_id = dlg.dest_poly_id()
            if dest_poly_id:
                dest_layer = QgsProject.instance().mapLayer(dest_poly_id)
                self._overwrite_layer(dest_layer, new_polys, attr_map, src_poly_id)
            else:
                self._create_memory_layer(new_polys, attr_map, src_poly_id)

            progress.setValue(5)
            self.iface.mapCanvas().refreshAllLayers()

            area_matched = sum(1 for v in match_method.values() if v == 'area')
            no_match     = len(new_polys) - len(match_method)
            QMessageBox.information(
                self.iface.mainWindow(), "完了",
                f"{len(new_polys)} 個のポリゴンを生成しました。\n\n"
                f"属性引き継ぎ結果:\n"
                f"  面積突合: {area_matched} 件\n"
                f"  属性なし: {no_match} 件"
            )

        except Exception:
            progress.close()
            QMessageBox.critical(self.iface.mainWindow(), "エラー",
                                 traceback.format_exc())

    # ------------------------------------------------------------------

    def _overwrite_layer(self, dest_layer, new_polys, attr_map, src_poly_id):
        dest_fields     = dest_layer.fields()
        src_field_names = set()
        if src_poly_id:
            src_layer = QgsProject.instance().mapLayer(src_poly_id)
            if src_layer:
                src_field_names = {f.name() for f in src_layer.fields()}

        dest_layer.beginEditCommand("Polygonize — ポリゴン再生成")
        try:
            dest_layer.selectAll()
            dest_layer.deleteSelectedFeatures()
            for i, geom in enumerate(new_polys):
                feat = QgsFeature(dest_fields)
                feat.setGeometry(geom)
                attrs = attr_map.get(i, {})
                for j, field in enumerate(dest_fields):
                    if field.name() in src_field_names and field.name() in attrs:
                        feat.setAttribute(j, attrs[field.name()])
                dest_layer.addFeature(feat)
            dest_layer.endEditCommand()
        except Exception:
            dest_layer.destroyEditCommand()
            raise

    def _create_memory_layer(self, new_polys, attr_map, src_poly_id):
        crs       = QgsProject.instance().crs()
        mem_layer = QgsVectorLayer(
            f"Polygon?crs={crs.authid()}", "polygonized", "memory")
        provider  = mem_layer.dataProvider()

        if src_poly_id:
            src_layer = QgsProject.instance().mapLayer(src_poly_id)
            if src_layer:
                provider.addAttributes(src_layer.fields().toList())
                mem_layer.updateFields()

        fields = mem_layer.fields()
        feats  = []
        for i, geom in enumerate(new_polys):
            feat = QgsFeature(fields)
            feat.setGeometry(geom)
            attrs = attr_map.get(i, {})
            for j, field in enumerate(fields):
                if field.name() in attrs:
                    feat.setAttribute(j, attrs[field.name()])
            feats.append(feat)

        provider.addFeatures(feats)
        QgsProject.instance().addMapLayer(mem_layer)
