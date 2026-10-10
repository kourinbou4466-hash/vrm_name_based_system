# ==============================================================================
# VRM-NX VNS (VRM Name-based System) レイアウト制御スクリプト Ver. 1.2.0
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
import re

# ==============================================================================
# 【グローバル変数の設計】
# ==============================================================================

# 1. 閉塞・進路構成の排他制御（コアデータ）
TRAIN_TO_BLOCK = {}        # { train_id: ["進路構成-...", "閉塞-...", ...] }
BLOCK_TO_TRAIN = {}        # { block_name: [train_id, ...] }
POINT_TO_TRAIN = {}        # { point_name: [train_id, ...] }

# 2. 進路待ち・予備進路管理
ROUTE_WAIT_QUEUE = []      # [ {'train_id': int, 'route_option': str}, ... ]
TRAIN_BACKUP_ROUTES = {}   # { train_id: [backup_route_option, ...] }

# 3. タイマー・非同期制御（集中タイマー方式）
SYSTEM_TIMER_ID = 100
SYSTEM_TIMER_INTERVAL = 0.2  # タイマー更新周期（秒）
TRAIN_SMOOTH_MAP = {}      # { train_id: {'target': float, 'current': float, 'step': float} }
TRAIN_COMMAND_QUEUE = {}   # { train_id: [ command_dict, ... ] }

# 4. 連結待機管理
PENDING_COUPLE_COMMANDS = {} # { train_id: [残りのコマンドリスト] }

# 5. システム起動時に1回だけ取得するレイアウト要素リスト
SIGNAL_LIST = []
POINT_LIST = []

# 内部フラグ（起動コマンド等による1回限定無条件進入許可）
TRAIN_ALLOW_ONCE_BLOCK = {} # { train_id: True }


# ==============================================================================
# 第5階層：ユーティリティ
# ==============================================================================

def get_train_info(train_obj):
    """対象列車オブジェクトの属性（ID、TCN名、表示名、両数、速度など）をまとめた辞書を返す"""
    if not train_obj:
        return {'id': 0, 'data_name': 'UNKNOWN', 'tcn_name': 'UNKNOWN', 'type': '', 'origin': '', 'dest': '', 'display_name': 'UNKNOWN'}

    data_name = str(train_obj.GetNAME())
    tcn_name = train_obj.GetStatusDataString("tcn_name")
    if not tcn_name:
        tcn_name = data_name

    parts = tcn_name.split('-')
    train_type = parts[0] if len(parts) >= 1 else ""
    st1 = parts[1] if len(parts) >= 2 else ""
    st2 = parts[2] if len(parts) >= 3 else ""

    rev = train_obj.GetStatusDataInt("reverse")
    if rev == 1:
        origin, dest = st2, st1
    else:
        origin, dest = st1, st2

    display_name = f"【データ名: '{data_name}' | tcn_name: '{tcn_name}'】"

    return {
        'id': train_obj.GetID(),
        'data_name': data_name,
        'tcn_name': tcn_name,
        'type': train_type,
        'origin': origin,
        'dest': dest,
        'parts': parts,
        'display_name': display_name
    }


def is_filter_matched(filter_dict, train_info):
    """通過した列車情報が、センサーに設定されたフィルタ条件に一致するか判定して返す"""
    if not filter_dict:
        return True

    f_type = filter_dict['type']
    conds = filter_dict['conds']

    targets = []
    if f_type == "種別":
        targets = [train_info['type']]
    elif f_type == "始発":
        targets = [train_info['origin']]
    elif f_type == "終着":
        targets = [train_info['dest']]
    elif f_type == "始終着":
        targets = [train_info['origin'], train_info['dest']]

    for cond in conds:
        for t in targets:
            if cond in t:
                return True
    return False

def clear_train_smooth_task(train_id):
    """該当列車の変速タスク（TRAIN_SMOOTH_MAP）をクリアして自動加減速を強制停止する"""
    if train_id in TRAIN_SMOOTH_MAP:
        del TRAIN_SMOOTH_MAP[train_id]

def stop_train_immediately(train_obj):
    """変速タスクをクリアした上で即座に電圧を0にする"""
    train_id = train_obj.GetID()
    
    # 1. 自動センサー等による変速補間タスクを消去
    clear_train_smooth_task(train_id)
    
    # 2. 電圧を0（停止）に設定
    train_obj.SetVoltage(0.0)

def _update_block_signals(layout, block_name, is_occupied):
    """信号制御ヘルパー（閉塞の状態に合わせて信号機の色を変更）"""
    target_stat = 1 if is_occupied else 6
    color_label = "赤(停止)" if is_occupied else "青(進行)"

    for sig in SIGNAL_LIST:
        sig_name = str(sig.GetNAME())
        if block_name in sig_name:
            sig.SetStat(0, target_stat)
            vrmapi.LOG(f"  -> [信号制御] 信号機『{sig_name}』(閉塞:『{block_name}』) を {color_label} (Stat:{target_stat}) に変更しました")


# ==============================================================================
# 第3階層：閉塞・進路の状態管理・排他制御 (route_)
# ==============================================================================

def route_can_enter(layout, train_id, target_option, is_route_setting=False):
    """
    指定された閉塞や進路構成に列車が進入可能か（空き状態・ポイント排他）を検証・チェックする。
    [読み出しのみ可能]
    """
    train_obj = layout.GetTrain(train_id)
    t_info = get_train_info(train_obj)
    t_name = t_info['data_name']
    tcn = t_info['tcn_name']

    can_enter = True
    blocking_train_str = ""

    if not is_route_setting:
        # 単純閉塞の進入可否チェック
        occupying = BLOCK_TO_TRAIN.get(target_option, [])
        other_trains = [tid for tid in occupying if tid != train_id]
        if len(other_trains) > 0:
            can_enter = False
            b_info = get_train_info(layout.GetTrain(other_trains[0]))
            blocking_train_str = f" [ブロック列車: 列車名={b_info['data_name']}, tcn_name={b_info['tcn_name']}]"
    else:
        # 進路構成のチェック（すべてのポイントおよび目的閉塞）
        parts = [p.strip() for p in target_option.split('>') if p.strip()]
        if parts:
            target_block = parts[-1]
            point_cmds = parts[:-1]

            # 1. ポイントの予約状態チェック（グローバル POINT_LIST を使用）
            for pt_cmd in point_cmds:
                pt_name_sub = pt_cmd.replace("分岐", "").replace("直進", "")
                matched_pts = [pt for pt in POINT_LIST if pt_name_sub in str(pt.GetNAME())]

                for pt in matched_pts:
                    p_full_name = str(pt.GetNAME())
                    reservers = POINT_TO_TRAIN.get(p_full_name, [])
                    other_reservers = [tid for tid in reservers if tid != train_id]
                    if len(other_reservers) > 0:
                        can_enter = False
                        b_info = get_train_info(layout.GetTrain(other_reservers[0]))
                        blocking_train_str = f" [ブロック列車(ポイント): 列車名={b_info['data_name']}, tcn_name={b_info['tcn_name']}]"
                        break
                if not can_enter:
                    break

            # 2. 目的閉塞の占有状態チェック（ポイント側でブロックされていなければ判定）
            if can_enter:
                occupying = BLOCK_TO_TRAIN.get(target_block, [])
                other_trains = [tid for tid in occupying if tid != train_id]
                if len(other_trains) > 0:
                    can_enter = False
                    b_info = get_train_info(layout.GetTrain(other_trains[0]))
                    blocking_train_str = f" [ブロック列車(閉塞): 列車名={b_info['data_name']}, tcn_name={b_info['tcn_name']}]"

    # [ログ出力仕様準拠]
    vrmapi.LOG(f"[route_can_enter] 列車名={t_name}, tcn_name={tcn}, 対象={target_option}, 可否={can_enter}{blocking_train_str}")
    return can_enter


def route_enter(layout, train_id, target_option, is_route_setting=False):
    """
    進入可能と判断された閉塞・進路をロックして列車を進入させる。
    [読み書き可能]
    """
    if not is_route_setting:
        # 単純閉塞進入
        block_name = target_option
        if train_id not in TRAIN_TO_BLOCK:
            TRAIN_TO_BLOCK[train_id] = []
        if block_name not in TRAIN_TO_BLOCK[train_id]:
            TRAIN_TO_BLOCK[train_id].append(block_name)

        if block_name not in BLOCK_TO_TRAIN:
            BLOCK_TO_TRAIN[block_name] = []
        if train_id not in BLOCK_TO_TRAIN[block_name]:
            BLOCK_TO_TRAIN[block_name].append(train_id)

        _update_block_signals(layout, block_name, is_occupied=True)

    else:
        # 進路構成進入
        parts = [p.strip() for p in target_option.split('>') if p.strip()]
        if not parts:
            return

        target_block = parts[-1]
        point_cmds = parts[:-1]

        # TRAIN_TO_BLOCK の先頭に登録
        route_key = f"進路構成-{target_option}"
        if train_id not in TRAIN_TO_BLOCK:
            TRAIN_TO_BLOCK[train_id] = []
        TRAIN_TO_BLOCK[train_id].append(route_key)

        # ポイント切り替えおよび POINT_TO_TRAIN 登録（グローバル POINT_LIST を使用）
        for pt_cmd in point_cmds:
            direction = 1 if pt_cmd.startswith("分岐") else 0
            pt_name_sub = pt_cmd.replace("分岐", "").replace("直進", "")
            matched_pts = [pt for pt in POINT_LIST if pt_name_sub in str(pt.GetNAME())]

            for pt in matched_pts:
                p_full_name = str(pt.GetNAME())
                pt.SetBranch(direction)

                if p_full_name not in POINT_TO_TRAIN:
                    POINT_TO_TRAIN[p_full_name] = []
                if train_id not in POINT_TO_TRAIN[p_full_name]:
                    POINT_TO_TRAIN[p_full_name].append(train_id)

        # 目的閉塞の登録
        if target_block not in TRAIN_TO_BLOCK[train_id]:
            TRAIN_TO_BLOCK[train_id].append(target_block)

        if target_block not in BLOCK_TO_TRAIN:
            BLOCK_TO_TRAIN[target_block] = []
        if train_id not in BLOCK_TO_TRAIN[target_block]:
            BLOCK_TO_TRAIN[target_block].append(train_id)

        # 予備進路があれば消去
        if train_id in TRAIN_BACKUP_ROUTES:
            del TRAIN_BACKUP_ROUTES[train_id]

        _update_block_signals(layout, target_block, is_occupied=True)


def route_leave(layout, train_id, target_name, is_route_setting=False):
    """
    閉塞や進路をアンロックして開放する。
    [読み書き可能]
    仕様に従い1行のログを出力する。
    """
    train_obj = layout.GetTrain(train_id)
    t_info = get_train_info(train_obj)
    t_name = t_info['data_name']
    tcn = t_info['tcn_name']

    history = TRAIN_TO_BLOCK.get(train_id, [])
    released_blocks = []
    released_points = []

    if not is_route_setting:
        # 閉塞最後尾通過時：当該閉塞より後ろの要素を削除・解放
        if target_name in history:
            idx = history.index(target_name)
            to_remove = history[:idx]
            TRAIN_TO_BLOCK[train_id] = history[idx:]

            for item in to_remove:
                if item.startswith("進路構成-"):
                    # ポイント開放
                    opts = item.replace("進路構成-", "").split('>')
                    for pt_cmd in opts[:-1]:
                        pt_name_sub = pt_cmd.replace("分岐", "").replace("直進", "")
                        for p_name in list(POINT_TO_TRAIN.keys()):
                            if pt_name_sub in p_name and train_id in POINT_TO_TRAIN[p_name]:
                                POINT_TO_TRAIN[p_name].remove(train_id)
                                if not POINT_TO_TRAIN[p_name]:
                                    del POINT_TO_TRAIN[p_name]
                                released_points.append(p_name)
                else:
                    # 閉塞開放
                    if item in BLOCK_TO_TRAIN and train_id in BLOCK_TO_TRAIN[item]:
                        BLOCK_TO_TRAIN[item].remove(train_id)
                        if not BLOCK_TO_TRAIN[item]:
                            del BLOCK_TO_TRAIN[item]
                            _update_block_signals(layout, item, is_occupied=False)
                        released_blocks.append(item)

    # [ログ出力仕様準拠] 実際に開放された閉塞・ポイントをログ出力
    released_str = f"閉塞={released_blocks if released_blocks else None}, ポイント={released_points if released_points else None}"
    vrmapi.LOG(f"[route_leave] 列車名={t_name}, tcn_name={tcn}, 解放={released_str}")

    # 開放された閉塞・ポイントに基づいて再試行
    if released_blocks or released_points:
        route_retry(layout)

def route_retry(layout):
    """
    ポイントや閉塞が開放されたタイミングで呼び出され、ブロックされていた列車の進行・進路構成を試みる。
    [読み書き可能]
    """
    global ROUTE_WAIT_QUEUE
    for wait_info in list(ROUTE_WAIT_QUEUE):
        t_id = wait_info['train_id']
        r_opt = wait_info['route_option']
        tr = layout.GetTrain(t_id)

        if tr and route_can_enter(layout, t_id, r_opt, is_route_setting=True):
            ROUTE_WAIT_QUEUE = [q for q in ROUTE_WAIT_QUEUE if q['train_id'] != t_id]
            route_enter(layout, t_id, r_opt, is_route_setting=True)
            
            # 発車・再加速
            train_info = get_train_info(tr)
            vrmapi.LOG(f"  -> [進路再開] 待機中だった列車 {train_info['display_name']} の進路を構成し発車させます")
            TRAIN_SMOOTH_MAP[t_id] = {'target': 1.0, 'current': tr.GetVoltage(), 'step': 0.1}

def route_manage_by_coupling(master_id, target_id):
    """
    連結発生時に、消滅編成ID(target_id)のデータ・キューを
    存続編成ID(master_id)へ引き継ぎ・クリーンアップして再始動する。
    """
    layout = vrmapi.LAYOUT()
    vrmapi.LOG(f"[連結情報整理] 存続ID:{master_id} / 消滅ID:{target_id} の情報整理・引き継ぎを開始します")

    # 1. TRAIN_TO_BLOCK に登録されている閉塞のみを対象に BLOCK_TO_TRAIN 内の target_id を master_id に置換
    target_blocks = TRAIN_TO_BLOCK.get(target_id, [])
    master_blocks = TRAIN_TO_BLOCK.get(master_id, [])
    
    for bname in set(target_blocks + master_blocks):
        if bname in BLOCK_TO_TRAIN:
            BLOCK_TO_TRAIN[bname] = list(dict.fromkeys([master_id if tid == target_id else tid for tid in BLOCK_TO_TRAIN[bname]]))

    # POINT_TO_TRAIN 内の target_id も必要に応じて置換
    for pname, t_list in POINT_TO_TRAIN.items():
        if target_id in t_list:
            POINT_TO_TRAIN[pname] = list(dict.fromkeys([master_id if tid == target_id else tid for tid in t_list]))

    # 2. TRAIN_TO_BLOCK の統合
    t_blocks = TRAIN_TO_BLOCK.pop(target_id, [])
    m_blocks = TRAIN_TO_BLOCK.setdefault(master_id, [])
    for b in t_blocks:
        if b not in m_blocks:
            m_blocks.append(b)

    # 3. 予備進路の削除
    TRAIN_BACKUP_ROUTES.pop(target_id, None)

    # 4. ROUTE_WAIT_QUEUE の統合とログ出力
    global ROUTE_WAIT_QUEUE
    updated_queue = []
    transferred_count = 0

    for q in ROUTE_WAIT_QUEUE:
        if isinstance(q, dict) and q.get('train_id') in (master_id, target_id):
            q['train_id'] = master_id
            if not any(item.get('train_id') == master_id and item.get('route_option') == q.get('route_option') for item in updated_queue):
                updated_queue.append(q)
                transferred_count += 1
        else:
            updated_queue.append(q)

    ROUTE_WAIT_QUEUE = updated_queue
    vrmapi.LOG(f"[連結情報整理] ROUTE_WAIT_QUEUE 統合完了: 存続ID:{master_id} (引き継ぎ/保持数: {transferred_count}件, キュー全件数: {len(ROUTE_WAIT_QUEUE)}件)")

    # 5. PENDING_COUPLE_COMMANDS から target_id または master_id の連結保留コマンドを統合
    pending_cmds = PENDING_COUPLE_COMMANDS.pop(target_id, []) or PENDING_COUPLE_COMMANDS.pop(master_id, [])
    if pending_cmds:
        cmd_dicts = [{'cmd': c} for c in pending_cmds]
        TRAIN_COMMAND_QUEUE[master_id] = cmd_dicts
        vrmapi.LOG(f"[連結情報整理] 連結保留コマンドを存続ID:{master_id} に引き継ぎました: {pending_cmds}")
    elif master_id not in TRAIN_COMMAND_QUEUE or not TRAIN_COMMAND_QUEUE[master_id]:
        # 残りコマンドがない場合はデフォルト起動（前進加速）
        TRAIN_COMMAND_QUEUE[master_id] = [{'cmd': '変速100%2秒'}]
        vrmapi.LOG(f"[連結情報整理] 残存コマンドなしのため、存続ID:{master_id} にデフォルト起動コマンドを設定しました")

    # 6. 変速タスクをクリアして再起動トリガーを発行
    clear_train_smooth_task(master_id)
    route_retry(layout)

# ==============================================================================
# 第4階層：タイマー・非同期実行制御 (execute_)
# ==============================================================================

def execute_speed_control(layout):
    """列車の速度（電圧）を時間経過に合わせて滑らかに変化させる"""
    for train_id, data in list(TRAIN_SMOOTH_MAP.items()):
        train = layout.GetTrain(train_id)
        if not train:
            del TRAIN_SMOOTH_MAP[train_id]
            continue

        new_voltage = data['current'] + data['step']
        target = data['target']

        is_complete = False
        if data['step'] >= 0 and new_voltage >= target:
            new_voltage = target
            is_complete = True
        elif data['step'] < 0 and new_voltage <= target:
            new_voltage = target
            is_complete = True

        train.SetVoltage(new_voltage)
        data['current'] = new_voltage

        if is_complete:
            del TRAIN_SMOOTH_MAP[train_id]

def execute_train_command(layout):
    """時系列で登録された列車制御コマンド群を順次実行する"""
    for train_id, cmd_list in list(TRAIN_COMMAND_QUEUE.items()):
        if not cmd_list:
            del TRAIN_COMMAND_QUEUE[train_id]
            continue

        train_obj = layout.GetTrain(train_id)
        if not train_obj:
            del TRAIN_COMMAND_QUEUE[train_id]
            continue

        # 【重要】変速処理（自動加減速・補間タスク）が実行中の場合は、
        # すべての列車制御コマンド（停止、反転、折返、分割、連結等）の実行を待機する
        if train_id in TRAIN_SMOOTH_MAP:
            continue

        # 先頭コマンドを参照（停止カウント中の場合は pop しない）
        cmd_info = cmd_list[0]
        cmd = cmd_info.get('cmd', '')

        if cmd.startswith("変速"):
            cmd_list.pop(0)
            m = re.search(r'変速(?:(\d+)%)?(?:(\d+)秒)?', cmd)
            speed = float(m.group(1)) / 100.0 if m and m.group(1) else 1.0
            dur = float(m.group(2)) if m and m.group(2) else 0.0

            step_val = (speed - train_obj.GetVoltage()) / max(1, int(dur / SYSTEM_TIMER_INTERVAL)) if dur > 0 else (speed - train_obj.GetVoltage())
            TRAIN_SMOOTH_MAP[train_id] = {'target': speed, 'current': train_obj.GetVoltage(), 'step': step_val}

        elif cmd.startswith("停止"):
            # 初回実行時に停止秒数を解析してカウントダウン用カウンターを設定
            if 'wait_time' not in cmd_info:
                m = re.search(r'停止(?:(\d+)秒)?', cmd)
                stop_sec = float(m.group(1)) if m and m.group(1) else 0.0
                cmd_info['wait_time'] = stop_sec
                
                # 電圧を完全に0に設定
                train_obj.SetVoltage(0.0)

            # 0.2秒周期タイマーでカウントダウン
            cmd_info['wait_time'] -= SYSTEM_TIMER_INTERVAL

            # 待機時間が完了したらキューから削除して次のコマンドへ進める
            if cmd_info['wait_time'] <= 0:
                cmd_list.pop(0)
                
        elif cmd == "反転":
            cmd_list.pop(0)
            train_obj.Turn()

        elif cmd == "折返":
            cmd_list.pop(0)
            train_obj.Turn()
            cur_rev = train_obj.GetStatusDataInt("reverse")
            train_obj.SetStatusDataInt("reverse", 0 if cur_rev == 1 else 1)

        elif cmd.startswith("分割先頭") or cmd.startswith("分割最後尾"):
            cmd_list.pop(0)
            is_head = cmd.startswith("分割先頭")
            new_tcn = cmd.replace("分割先頭", "").replace("分割最後尾", "")
            car_list = train_obj.GetCarList()
            if car_list and len(car_list) >= 2:
                split_idx = 1 if is_head else len(car_list) - 1
                new_id = train_obj.SplitTrain(split_idx)
                if new_id and new_id > 0:
                    new_tr = layout.GetTrain(new_id)
                    orig_tcn = train_obj.GetStatusDataString("tcn_name")
                    if is_head:
                        if new_tr: new_tr.SetStatusDataString("tcn_name", orig_tcn)
                        train_obj.SetStatusDataString("tcn_name", new_tcn)
                    else:
                        train_obj.SetStatusDataString("tcn_name", orig_tcn)
                        if new_tr: new_tr.SetStatusDataString("tcn_name", new_tcn)

        elif cmd == "連結":
            cmd_list.pop(0)
            # 「連結」コマンド実行以降に残っているコマンド群を train_id をキーとして格納
            PENDING_COUPLE_COMMANDS[train_id] = [c.get('cmd') for c in cmd_list]
            TRAIN_COMMAND_QUEUE[train_id] = [] # 連結完了まで旧編成のコマンド実行を中断

        elif cmd.startswith("起動"):
            cmd_list.pop(0)
            is_turn = cmd.startswith("起動反転")
            prefix = "起動反転" if is_turn else "起動"
            m = re.search(rf'{prefix}(?:(\d+)%)?([^?]*)(?:\?\(([^)]+)\))?', cmd)
            speed = float(m.group(1)) / 100.0 if m and m.group(1) else 1.0
            target_blocks = [b.strip() for b in (m.group(2) if m else "").split('|') if b.strip()]

            for bname in target_blocks:
                occupying_ids = BLOCK_TO_TRAIN.get(bname, [])
                if occupying_ids:
                    target_tr_id = occupying_ids[0]
                    target_tr = layout.GetTrain(target_tr_id)
                    if target_tr:
                        TRAIN_ALLOW_ONCE_BLOCK[target_tr_id] = True
                        if is_turn: target_tr.Turn()
                        TRAIN_SMOOTH_MAP[target_tr_id] = {'target': speed, 'current': 0.0, 'step': 0.1}
                        break

# ==============================================================================
# 第2階層：センサー通過時制御 (pass_)
# ==============================================================================

def pass_train_control(layout, train_obj, train_id, train_info, func_option):
    """
    列車制御（変速や停止待ちなど）のセンサーを通過した際の処理。
    仕様に従い1行のログを出力する。
    """
    t_name = train_info['data_name']
    tcn = train_info['tcn_name']

    cmds = func_option.split('>')
    cmd_dicts = [{'cmd': c} for c in cmds]
    TRAIN_COMMAND_QUEUE[train_id] = cmd_dicts

    # [ログ出力仕様準拠]
    vrmapi.LOG(f"[pass_train_control] 列車名={t_name}, tcn_name={tcn}, コマンド={func_option}")


def pass_switch_control(layout, train_obj, train_id, train_info, func_type, location_name, matched):
    """
    ポイント操作のセンサーを通過した際の処理。
    仕様に従い1行のログを出力する。
    """
    t_name = train_info['data_name']
    tcn = train_info['tcn_name']

    for pt in POINT_LIST:
        pt_name = str(pt.GetNAME())
        if location_name in pt_name:
            if func_type == "ポイント直進" and matched:
                pt.SetBranch(0)
            elif func_type == "ポイント分岐" and matched:
                pt.SetBranch(1)
            elif func_type == "ポイント制御":
                pt.SetBranch(1 if matched else 0)

    # [ログ出力仕様準拠]
    vrmapi.LOG(f"[pass_switch_control] 列車名={t_name}, tcn_name={tcn}, ポイント名={location_name}, マッチ={matched}")


def pass_block_head(layout, train_obj, train_id, train_info, block_name):
    """列車先頭が閉塞境界センサーを通過した際の処理"""
    allow_once = TRAIN_ALLOW_ONCE_BLOCK.pop(train_id, False)

    if route_can_enter(layout, train_id, block_name, is_route_setting=False) or allow_once:
        route_enter(layout, train_id, block_name, is_route_setting=False)
    else:
        # 進入不可のため緊急停止・待機キューへ登録
        clear_train_smooth_task(train_id)
        train_obj.SetVoltage(0.0)

        if not any(q['train_id'] == train_id for q in ROUTE_WAIT_QUEUE):
            ROUTE_WAIT_QUEUE.append({
                'train_id': train_id,
                'route_option': block_name,
                'is_route_setting': False  # 単純閉塞フラグ
            })

def pass_block_tail(layout, train_obj, train_id, train_info, block_name):
    """列車最後尾が閉塞境界センサーを通過（閉塞を完全に進出）した際の処理"""
    route_leave(layout, train_id, block_name, is_route_setting=False)

def pass_route_setting_head(layout, train_obj, train_id, train_info, route_option):
    """列車先頭が進路構成センサーを通過した際の処理"""
    candidates = [route_option]
    if train_id in TRAIN_BACKUP_ROUTES:
        candidates.extend(TRAIN_BACKUP_ROUTES[train_id])

    # 構成可能か判定
    selected_option = None
    for opt in candidates:
        if route_can_enter(layout, train_id, opt, is_route_setting=True):
            selected_option = opt
            break

    if selected_option:
        route_enter(layout, train_id, selected_option, is_route_setting=True)
    else:
        # 変速タスクをクリアして強制停止
        clear_train_smooth_task(train_id)
        train_obj.SetVoltage(0.0)

        if not any(q['train_id'] == train_id for q in ROUTE_WAIT_QUEUE):
            ROUTE_WAIT_QUEUE.append({'train_id': train_id, 'route_option': route_option})

def pass_route_setting_tail(layout, train_obj, train_id, train_info, route_option):
    """列車最後尾が進路構成センサーを通過完了した際の処理"""
    pass_block_tail(layout, train_obj, train_id, train_info, route_option)


def pass_backup_route(layout, train_obj, train_id, train_info, route_option):
    """本進路が塞がっていた場合に備え、予備進路を登録・通過処理する"""
    if train_id not in TRAIN_BACKUP_ROUTES:
        TRAIN_BACKUP_ROUTES[train_id] = []
    # 後勝ち（後に登録されたものを優先）にするため先頭に挿入
    TRAIN_BACKUP_ROUTES[train_id].insert(0, route_option)


# ==============================================================================
# 第1階層：VRMNXイベントエントリーポイント (on_ / 例外 vrmevent)
# ==============================================================================

def vrmevent(obj, ev, param):
    """タイマー発生や各種システムイベントを受信するイベントエントリーポイント"""
    layout = vrmapi.LAYOUT()

    if ev == 'init':
        vrmapi.LOG("[初期化] VNS 制御スクリプト Ver. 2.0 (詳細設計書準拠) 起動")

        # 1. 信号機一覧を取得して初期化 (全青)
        global SIGNAL_LIST
        SIGNAL_LIST = []
        layout.ListSignal(SIGNAL_LIST)
        for sig in SIGNAL_LIST:
            sig.SetStat(0, 6)

        # 2. ポイント一覧を取得してグローバル変数へ保存
        global POINT_LIST
        POINT_LIST = []
        layout.ListPoint(POINT_LIST)

        # 3. 列車初期ステータス設定
        tr_list = []
        layout.ListTrain(tr_list)
        for tr in tr_list:
            tr.SetStatusDataInt("reverse", 0)
            data_name = str(tr.GetNAME())
            tr.SetStatusDataString("tcn_name", data_name)

        # 4. 単一タイマー(SYSTEM_TIMER_ID=100)の開始 (0.2秒周期)
        layout.SetEventTimer(SYSTEM_TIMER_INTERVAL, SYSTEM_TIMER_ID)

    elif ev == 'timer':
        timer_id = param.get('eventUID') if isinstance(param, dict) else param
        if timer_id == SYSTEM_TIMER_ID:
            # タイマー駆動によるスムーズ変速およびコマンドの実行
            execute_speed_control(layout)
            execute_train_command(layout)
            # ループタイマー再設定
            layout.SetEventTimer(SYSTEM_TIMER_INTERVAL, SYSTEM_TIMER_ID)


def on_sensor_catch(sensor_obj, param):
    """列車がセンサーを踏んだ際に呼び出される。センサー名のパース（文字列解析）もこの内部で完結して実行する"""
    sensor_dir = param.get('dir') if 'dir' in param else param.get('direction', None)
    if sensor_dir is not None and sensor_dir != 1:
        return

    tire_type = param.get('tire', 1)  # 1: 先頭車輪, 2: 最後尾車輪
    layout = vrmapi.LAYOUT()
    train_id = param.get('trainid')
    train = layout.GetTrain(train_id)
    if not train:
        return

    # 関数の引数統一のため一括取得
    train_info = get_train_info(train)
    t_name = train_info['data_name']
    tcn = train_info['tcn_name']
    sensor_name = str(sensor_obj.GetNAME())
    vrmapi.LOG(f"[on_sensor_catch] 列車名={t_name}, tcn_name={tcn}, センサー名={sensor_name}, tire_type={tire_type}")

    # --- センサー名のパース (内部完結) ---
    filter_dict = None
    rest = sensor_name
    match_filter = re.match(r'^\((種別|始発|終着|始終着)=([^)]+)\)(.*)$', sensor_name)
    if match_filter:
        filter_dict = {'type': match_filter.group(1), 'conds': match_filter.group(2).split('|')}
        rest = match_filter.group(3)

    parts = rest.split('-', 1)
    func_type = parts[0]
    func_option = parts[1] if len(parts) > 1 else ""

    matched = is_filter_matched(filter_dict, train_info)

    # --- センサー機能別のルーティング（統一引数渡し）---
    if tire_type == 1 and func_type in ["ポイント直進", "ポイント分岐", "ポイント制御"]:
        pass_switch_control(layout, train, train_id, train_info, func_type, func_option, matched)

    elif tire_type == 1 and func_type == "列車制御":
        if matched:
            pass_train_control(layout, train, train_id, train_info, func_option)

    elif tire_type == 1 and func_type == "予備進路":
        if matched:
            pass_backup_route(layout, train, train_id, train_info, func_option)

    elif func_type == "閉塞":
        if matched:
            if tire_type == 1:
                pass_block_head(layout, train, train_id, train_info, func_option)
            elif tire_type == 2:
                pass_block_tail(layout, train, train_id, train_info, func_option)

    elif func_type == "進路構成":
        if matched:
            if tire_type == 1:
                pass_route_setting_head(layout, train, train_id, train_info, func_option)
            elif tire_type == 2:
                pass_route_setting_tail(layout, train, train_id, train_info, func_option)

def on_train_couple_event(survived_id, deleted_id):
    """
    連結イベントハンドラ
    survived_id: 存続する編成ID
    deleted_id : 統合されて空になった編成ID (delid)
    """
    vrmapi.LOG(f"[連結イベント] 存続ID: {survived_id}, 消滅ID(delid): {deleted_id}")
    
    # 消滅したID(deleted_id)が保持していた閉塞・ポイントのロック開放や master_id への移管処理
    route_manage_by_coupling(master_id=survived_id, target_id=deleted_id)
