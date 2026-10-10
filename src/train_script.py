# ==============================================================================
# VRM-NX VNS (VRM Name-based System) 編成制御スクリプト Ver. 1.2.0
# 参照設計書: Train Control by Name詳細設計書 Ver. 1.2 / 機能設計書 Ver. 1.2
# ------------------------------------------------------------------------------
# Created by kourinbou4466@gmail.com
#
# To the extent possible under law, the author(s) have dedicated all copyright
# and related and neighboring rights to this software to the public domain
# worldwide. This software is distributed without any warranty.
#
# You should have received a copy of the CC0 Public Domain Dedication along
# with this software. If not, see <http://creativecommons.org/publicdomain/zero/1.0/>.
# ==============================================================================

import vrmapi

def vrmevent_107(obj, ev, param):
    if ev == 'couple':
        survived_id = obj.GetID()
        
        # param 辞書から delid を数値として抽出
        deleted_id = param.get('delid', 0)
 
        # レイアウトスクリプト側の連結イベントハンドラを呼び出し
        on_train_couple_event(survived_id, deleted_id)
