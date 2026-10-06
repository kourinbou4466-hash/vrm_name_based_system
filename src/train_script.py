# ==============================================================================
# VRM-NX VNS (VRM Name-based System) レイアウト制御スクリプト Ver. 1.2.0
# 参照設計書: Train Control by Name詳細設計書 Ver. 1.2 / 機能設計書 Ver. 1.2
# ==============================================================================

import vrmapi

def vrmevent_107(obj, ev, param):
    if ev == 'couple':
        survived_id = obj.GetID()
        
        # param 辞書から delid を数値として抽出
        deleted_id = param.get('delid', 0)
 
        # レイアウトスクリプト側の連結イベントハンドラを呼び出し
        on_train_couple_event(survived_id, deleted_id)
