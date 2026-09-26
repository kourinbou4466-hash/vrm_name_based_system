# ==============================================================================
# VRM-NX VNS (VRM Name-based System) レイアウト制御スクリプト
# 参照設計書: Train Control by Name設計書 Ver. 1.0
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

# --- グローバル状態管理 ---
TRAIN_SMOOTH_MAP = {}          # { train_id: {'target': float, 'current': float, 'step': float} }
BLOCK_OCCUPY_MAP = {}          # { block_name: [train_id, ...] }
BLOCK_WAIT_QUEUE = {}          # { block_name: [train_id, ...] }
TRAIN_CURRENT_BLOCKS = {}      # { train_id: [block_name, ...] }
TRAIN_ALLOW_ONCE_BLOCK = {}    # { train_id: True }

# 進路構成用管理
POINT_RESERVE_MAP = {}         # { point_name: train_id }
TRAIN_OCCUPIED_POINTS = {}     # { train_id: [point_name, ...] }
ROUTE_WAIT_QUEUE = []          # [ {'train_id': int, 'sensor_option': str}, ... ]
TRAIN_BACKUP_ROUTES = {}       # { train_id: [backup_sensor_option, ...] } ※後勝ち優先のためリスト先頭に追加

# 連結待機管理: { active_tcn_name: [残りのコマンドリスト] }
PENDING_COUPLE_COMMANDS = {}

NEXT_TIMER_ID = 1000
TIMER_ACTION_MAP = {}

# ----------------------------------------------------
# 0. ログ出力 & 信号機制御ヘルパー
# ----------------------------------------------------

def get_train_display_name(train_obj):
    if not train_obj:
        return "UNKNOWN"
    data_name = str(train_obj.GetNAME())
    tcn_name = train_obj.GetStatusDataString("tcn_name")
    if not tcn_name:
        tcn_name = data_name
    return f"【データ名: '{data_name}' | tcn_name: '{tcn_name}'】"

def update_block_signals(layout, block_name, is_occupied):
    sig_list = []
    layout.ListSignal(sig_list)
    
    target_stat = 1 if is_occupied else 6
    color_label = "赤(停止)" if is_occupied else "青(進行)"

    for sig in sig_list:
        sig_name = str(sig.GetNAME())
        if block_name in sig_name:
            sig.SetStat(0, target_stat)
            vrmapi.LOG(f"  -> [信号制御] 信号機『{sig_name}』(閉塞:『{block_name}』) を {color_label} (Stat:{target_stat}) に変更しました")

# ----------------------------------------------------
# 1. パース & ユーティリティ関数
# ----------------------------------------------------

def cancel_train_timers(train_id):
    if train_id in TRAIN_SMOOTH_MAP:
        del TRAIN_SMOOTH_MAP[train_id]
        
    to_delete = [t_id for t_id, info in TIMER_ACTION_MAP.items() if info.get('train_id') == train_id]
    for t_id in to_delete:
        del TIMER_ACTION_MAP[t_id]

def parse_sensor_name(sensor_name):
    filter_dict = None
    rest = sensor_name

    match_filter = re.match(r'^\((種別|始発|終着|始終着)=([^)]+)\)(.*)$', sensor_name)
    if match_filter:
        f_type = match_filter.group(1)
        f_conds = match_filter.group(2).split('|')
        filter_dict = {'type': f_type, 'conds': f_conds}
        rest = match_filter.group(3)

    func_parts = rest.split('-', 1)
    func_type = func_parts[0]
    func_option = func_parts[1] if len(func_parts) > 1 else ""

    return filter_dict, func_type, func_option

def is_filter_matched(filter_dict, train_info):
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

def get_train_info(train_obj):
    tcn_name = train_obj.GetStatusDataString("tcn_name")
    if not tcn_name:
        tcn_name = str(train_obj.GetNAME())

    parts = tcn_name.split('-')
    train_type = parts[0] if len(parts) >= 1 else ""
    st1 = parts[1] if len(parts) >= 2 else ""
    st2 = parts[2] if len(parts) >= 3 else ""

    rev = train_obj.GetStatusDataInt("reverse")
    if rev == 1:
        origin, dest = st2, st1
    else:
        origin, dest = st1, st2

    return {
        'id': train_obj.GetID(),
        'data_name': str(train_obj.GetNAME()),
        'tcn_name': tcn_name,
        'type': train_type,
        'origin': origin,
        'dest': dest,
        'parts': parts
    }

# ----------------------------------------------------
# 2. 変速 & タイマー制御・コマンドチェーン実行
# ----------------------------------------------------

def set_train_voltage_smooth(layout, train_id, target_voltage, duration_sec=0.0):
    global NEXT_TIMER_ID
    train = layout.GetTrain(train_id)
    if not train:
        return

    current_voltage = train.GetVoltage()
    if train_id in TRAIN_SMOOTH_MAP:
        current_voltage = TRAIN_SMOOTH_MAP[train_id]['current']

    if duration_sec <= 0 or abs(current_voltage - target_voltage) < 0.01:
        train.SetVoltage(target_voltage)
        if train_id in TRAIN_SMOOTH_MAP:
            del TRAIN_SMOOTH_MAP[train_id]
        return

    interval = 0.2
    steps = max(1, int(duration_sec / interval))
    step_val = (target_voltage - current_voltage) / steps

    NEXT_TIMER_ID += 1
    timer_id = NEXT_TIMER_ID

    TRAIN_SMOOTH_MAP[train_id] = {
        'target': target_voltage,
        'current': current_voltage,
        'step': step_val
    }

    TIMER_ACTION_MAP[timer_id] = {
        'train_id': train_id,
        'action': 'smooth_step'
    }
    layout.SetEventTimer(interval, timer_id)

def execute_command_chain(layout, train_obj, commands):
    global NEXT_TIMER_ID
    if not commands:
        return

    cmd = commands[0]
    rem_cmds = commands[1:]
    train_id = train_obj.GetID()
    disp_name = get_train_display_name(train_obj)

    if cmd.startswith("変速"):
        m = re.search(r'変速(?:(\d+)%)?(?:(\d+)秒)?', cmd)
        speed = float(m.group(1)) / 100.0 if m and m.group(1) else 1.0
        dur = float(m.group(2)) if m and m.group(2) else 0.0
        
        vrmapi.LOG(f"  -> [コマンド実行] {disp_name}: 変速 {speed*100:.0f}% ({dur}秒)")
        set_train_voltage_smooth(layout, train_id, speed, dur)
        if rem_cmds:
            NEXT_TIMER_ID += 1
            t_id = NEXT_TIMER_ID
            TIMER_ACTION_MAP[t_id] = {'train_id': train_id, 'action': 'chain', 'cmds': rem_cmds}
            layout.SetEventTimer(dur if dur > 0 else 0.1, t_id)

    elif cmd == "連結":
        tcn_name = train_obj.GetStatusDataString("tcn_name") or str(train_obj.GetNAME())
        PENDING_COUPLE_COMMANDS[tcn_name] = rem_cmds
        vrmapi.LOG(f"  -> [コマンド待機] {disp_name}: 編成イベントからの連結通知(couple)を待機します...")

    elif cmd.startswith("停止"):
        m = re.search(r'停止(?:(\d+)秒)?', cmd)
        cancel_train_timers(train_id)
        train_obj.SetVoltage(0.0)

        if m and m.group(1):
            sec = float(m.group(1))
            vrmapi.LOG(f"  -> [コマンド実行] {disp_name}: 停止 ({sec}秒間)")
            if rem_cmds:
                NEXT_TIMER_ID += 1
                t_id = NEXT_TIMER_ID
                TIMER_ACTION_MAP[t_id] = {
                    'train_id': train_id, 
                    'action': 'chain', 
                    'cmds': rem_cmds
                }
                layout.SetEventTimer(sec, t_id)
        else:
            vrmapi.LOG(f"  -> [コマンド実行] {disp_name}: 無期限停止")

    elif cmd == "反転":
        train_obj.Turn()
        vrmapi.LOG(f"  -> [コマンド実行] {disp_name}: 向きを反転 (Turn)")
        execute_command_chain(layout, train_obj, rem_cmds)

    elif cmd == "折返":
        train_obj.Turn()
        current_rev = train_obj.GetStatusDataInt("reverse")
        new_rev = 0 if current_rev == 1 else 1
        train_obj.SetStatusDataInt("reverse", new_rev)
        vrmapi.LOG(f"  -> [コマンド実行] {disp_name}: 折返 (reverseステータス: {new_rev})")
        execute_command_chain(layout, train_obj, rem_cmds)

    elif cmd.startswith("分割先頭") or cmd.startswith("分割最後尾"):
        is_head = cmd.startswith("分割先頭")
        new_tcn_name = cmd.replace("分割先頭", "").replace("分割最後尾", "")

        car_list = train_obj.GetCarList()
        total_cars = len(car_list) if car_list else 0

        if total_cars < 2:
            vrmapi.LOG(f"  -> [解結エラー] 1両編成のため解結できません (両数: {total_cars})")
            return

        split_index = 1 if is_head else (total_cars - 1)
        mode_str = "分割先頭" if is_head else "分割最後尾"
        vrmapi.LOG(f"  -> [解結準備] {disp_name} の解結({mode_str})を開始します (全{total_cars}両 / 位置: {split_index}両目, 新名称: '{new_tcn_name}')")

        new_train_id = train_obj.SplitTrain(split_index)

        if new_train_id and new_train_id > 0:
            new_train = layout.GetTrain(new_train_id)
            orig_tcn = train_obj.GetStatusDataString("tcn_name")

            if is_head:
                if new_train:
                    new_train.SetStatusDataString("tcn_name", orig_tcn)
                    new_train.SetVoltage(0.0)

                cancel_train_timers(train_id)
                train_obj.SetStatusDataString("tcn_name", new_tcn_name)

                moving_train = train_obj
                stay_train = new_train
            else:
                cancel_train_timers(train_id)
                train_obj.SetStatusDataString("tcn_name", orig_tcn)
                train_obj.SetVoltage(0.0)

                if new_train:
                    new_train.SetStatusDataString("tcn_name", new_tcn_name)

                moving_train = new_train
                stay_train = train_obj

            if train_id in TRAIN_CURRENT_BLOCKS:
                for bname in TRAIN_CURRENT_BLOCKS[train_id]:
                    if bname in BLOCK_OCCUPY_MAP and new_train_id not in BLOCK_OCCUPY_MAP[bname]:
                        BLOCK_OCCUPY_MAP[bname].append(new_train_id)
                    if new_train_id not in TRAIN_CURRENT_BLOCKS:
                        TRAIN_CURRENT_BLOCKS[new_train_id] = []
                    TRAIN_CURRENT_BLOCKS[new_train_id].append(bname)

            vrmapi.LOG(f"  -> [解結完了] 解結成功！")
            vrmapi.LOG(f"     ・停留側: {get_train_display_name(stay_train) if stay_train else 'N/A'}")
            vrmapi.LOG(f"     ・分離側(発車): {get_train_display_name(moving_train) if moving_train else 'N/A'}")

            if moving_train:
                execute_command_chain(layout, moving_train, rem_cmds)
        else:
            vrmapi.LOG(f"  -> [解結エラー] SplitTrain({split_index}) に失敗しました。")

    elif cmd.startswith("起動"):
        is_turn = cmd.startswith("起動反転")
        prefix = "起動反転" if is_turn else "起動"
        
        m = re.search(rf'{prefix}(?:(\d+)%)?([^?]*)(?:\?\(([^)]+)\))?', cmd)
        speed = float(m.group(1)) / 100.0 if m and m.group(1) else 1.0
        target_blocks_str = m.group(2) if m else ""
        cond_block = m.group(3) if m else None

        target_blocks = [b.strip() for b in target_blocks_str.split('|') if b.strip()]

        if cond_block:
            occupying = BLOCK_OCCUPY_MAP.get(cond_block, [])
            if len(occupying) == 0:
                vrmapi.LOG(f"  -> [{prefix}スキップ] 条件閉塞『{cond_block}』に列車が存在しないため起動処理をスキップしました")
                execute_command_chain(layout, train_obj, rem_cmds)
                return

        cmd_label = "起動反転" if is_turn else "起動"
        vrmapi.LOG(f"  -> [{cmd_label}コマンド] 閉塞リスト {target_blocks} からの起動を試みます (指定速度: {speed*100:.0f}%)")

        for bname in target_blocks:
            if bname in BLOCK_OCCUPY_MAP and len(BLOCK_OCCUPY_MAP[bname]) > 0:
                target_train_id = BLOCK_OCCUPY_MAP[bname][0]
                target_train = layout.GetTrain(target_train_id)
                if target_train:
                    TRAIN_ALLOW_ONCE_BLOCK[target_train_id] = True
                    target_disp = get_train_display_name(target_train)
                    
                    if is_turn:
                        target_train.Turn()
                        vrmapi.LOG(f"     ・閉塞区間『{bname}』内の列車 {target_disp} の向きを反転(Turn)しました。")

                    vrmapi.LOG(f"     ・閉塞区間『{bname}』内の列車 {target_disp} を起動しました。(1回限定の閉塞進入許可を付与)")
                    set_train_voltage_smooth(layout, target_train_id, speed, 1.0)
                    break
        execute_command_chain(layout, train_obj, rem_cmds)

# ----------------------------------------------------
# 3. 進路構成制御コア
# ----------------------------------------------------

def register_backup_route(train_id, sensor_option):
    """予備進路を列車IDに登録する（後に登録されたものが優先されるよう先頭に挿入）"""
    if train_id not in TRAIN_BACKUP_ROUTES:
        TRAIN_BACKUP_ROUTES[train_id] = []
    TRAIN_BACKUP_ROUTES[train_id].insert(0, sensor_option)

def try_single_route_setting(layout, train_obj, sensor_option):
    """単一の進路構成オプションの試行（成功時 True、不成立時 False）"""
    train_id = train_obj.GetID()

    parts = [p.strip() for p in sensor_option.split('>') if p.strip()]
    if not parts:
        return True

    target_block = parts[-1]
    point_cmds = parts[:-1]

    pt_list = []
    layout.ListPoint(pt_list)
    parsed_pt_cmds = []

    for pt_cmd in point_cmds:
        direction = 1 if pt_cmd.startswith("分岐") else 0
        pt_name_sub = pt_cmd.replace("分岐", "").replace("直進", "")

        matched_pts = [pt for pt in pt_list if pt_name_sub in str(pt.GetNAME())]
        for pt in matched_pts:
            p_full_name = str(pt.GetNAME())
            reserver = POINT_RESERVE_MAP.get(p_full_name)
            if reserver is not None and reserver != train_id:
                vrmapi.LOG(f"  -> [進路試行不可] ポイント『{p_full_name}』は他列車(ID:{reserver})が予約中")
                return False
            parsed_pt_cmds.append((pt, p_full_name, direction))

    occupying_trains = BLOCK_OCCUPY_MAP.get(target_block, [])
    other_trains = [tid for tid in occupying_trains if tid != train_id]
    if len(other_trains) > 0:
        vrmapi.LOG(f"  -> [進路試行不可] 目的閉塞『{target_block}』は使用中")
        return False

    disp_name = get_train_display_name(train_obj)
    vrmapi.LOG(f"  -> [進路構成成功] {disp_name} の進路を確保・構成します (選択進路: '{sensor_option}')")

    if train_id not in TRAIN_OCCUPIED_POINTS:
        TRAIN_OCCUPIED_POINTS[train_id] = []

    for pt, p_full_name, direction in parsed_pt_cmds:
        pt.SetBranch(direction)
        POINT_RESERVE_MAP[p_full_name] = train_id
        if p_full_name not in TRAIN_OCCUPIED_POINTS[train_id]:
            TRAIN_OCCUPIED_POINTS[train_id].append(p_full_name)
        dir_label = "分岐(1)" if direction == 1 else "直進(0)"
        vrmapi.LOG(f"     ・ポイント『{p_full_name}』を {dir_label} に切替・予約完了")

    if target_block not in BLOCK_OCCUPY_MAP:
        BLOCK_OCCUPY_MAP[target_block] = []
    if train_id not in BLOCK_OCCUPY_MAP[target_block]:
        BLOCK_OCCUPY_MAP[target_block].append(train_id)

    if train_id not in TRAIN_CURRENT_BLOCKS:
        TRAIN_CURRENT_BLOCKS[train_id] = []
    if target_block not in TRAIN_CURRENT_BLOCKS[train_id]:
        TRAIN_CURRENT_BLOCKS[train_id].append(target_block)

    update_block_signals(layout, target_block, is_occupied=True)

    global ROUTE_WAIT_QUEUE
    ROUTE_WAIT_QUEUE = [q for q in ROUTE_WAIT_QUEUE if q['train_id'] != train_id]

    if train_obj.GetVoltage() < 0.01:
        vrmapi.LOG(f"  -> [進路発車] 停止中の列車 {disp_name} を再発車させます")
        set_train_voltage_smooth(layout, train_id, 1.0, 2.0)

    return True

def handle_route_setting(layout, train_obj, primary_sensor_option):
    """本進路および登録された予備進路（後勝ち優先）を順に試行する"""
    train_id = train_obj.GetID()
    disp_name = get_train_display_name(train_obj)

    candidates = [primary_sensor_option]
    if train_id in TRAIN_BACKUP_ROUTES:
        candidates.extend(TRAIN_BACKUP_ROUTES[train_id])

    for option in candidates:
        vrmapi.LOG(f"  -> [進路確保試行] {disp_name}: 候補進路『{option}』を判定中...")
        if try_single_route_setting(layout, train_obj, option):
            # 進路構成に成功した場合、登録された予备進路はすべて消去
            if train_id in TRAIN_BACKUP_ROUTES:
                del TRAIN_BACKUP_ROUTES[train_id]
                vrmapi.LOG(f"  -> [予備進路消去] {disp_name} の登録済み予備進路をクリアしました")
            return True

    # 全進路が不可の場合
    vrmapi.LOG(f"  -> [進路構成失敗] {disp_name}: 本進路および予備進路のすべてが使用不可でした")
    _fail_route(layout, train_obj, primary_sensor_option)
    return False

def handle_route_release(layout, train_obj, sensor_option):
    """進路構成センサーを最後尾車輪が通過した際、前閉塞のみ解放する（ポイントはまだ解放しない）"""
    train_id = train_obj.GetID()
    disp_name = get_train_display_name(train_obj)

    parts = [p.strip() for p in sensor_option.split('>') if p.strip()]
    target_block = parts[-1] if parts else None

    # 現在の閉塞情報からターゲット以外の旧閉塞を解放
    current_blocks = list(TRAIN_CURRENT_BLOCKS.get(train_id, []))
    for old_block in current_blocks:
        if old_block != target_block:
            if old_block in BLOCK_OCCUPY_MAP and train_id in BLOCK_OCCUPY_MAP[old_block]:
                BLOCK_OCCUPY_MAP[old_block].remove(train_id)
                if len(BLOCK_OCCUPY_MAP[old_block]) == 0:
                    del BLOCK_OCCUPY_MAP[old_block]
                    update_block_signals(layout, old_block, is_occupied=False)

                vrmapi.LOG(f"  -> [進路閉塞解放] 列車 {disp_name} (最後尾車輪) が 進路構成センサー通過により閉塞区間『{old_block}』を開放")

            if old_block in TRAIN_CURRENT_BLOCKS[train_id]:
                TRAIN_CURRENT_BLOCKS[train_id].remove(old_block)

            # 閉塞待ち列車の再起動
            if old_block in BLOCK_WAIT_QUEUE and len(BLOCK_WAIT_QUEUE[old_block]) > 0:
                waiting_train_id = BLOCK_WAIT_QUEUE[old_block].pop(0)
                waiting_train = layout.GetTrain(waiting_train_id)
                if waiting_train:
                    if old_block not in BLOCK_OCCUPY_MAP:
                        BLOCK_OCCUPY_MAP[old_block] = []
                    BLOCK_OCCUPY_MAP[old_block].append(waiting_train_id)

                    if waiting_train_id not in TRAIN_CURRENT_BLOCKS:
                        TRAIN_CURRENT_BLOCKS[waiting_train_id] = []
                    TRAIN_CURRENT_BLOCKS[waiting_train_id].append(old_block)

                    w_disp = get_train_display_name(waiting_train)
                    vrmapi.LOG(f"  -> [閉塞再開] 待機中の列車 {w_disp} が 閉塞区間『{old_block}』へ進入発車")

                    update_block_signals(layout, old_block, is_occupied=True)
                    set_train_voltage_smooth(layout, waiting_train_id, 1.0, 3.0)

    retry_route_and_block_waiters(layout)

def _fail_route(layout, train_obj, sensor_option):
    train_id = train_obj.GetID()
    disp_name = get_train_display_name(train_obj)

    cancel_train_timers(train_id)
    train_obj.SetVoltage(0.0)

    if not any(q['train_id'] == train_id for q in ROUTE_WAIT_QUEUE):
        ROUTE_WAIT_QUEUE.append({'train_id': train_id, 'sensor_option': sensor_option})
    
    vrmapi.LOG(f"  -> [進路構成待機] 列車 {disp_name} を直ちに停止させ、進路開放を待機します")

def release_train_points(layout, train_id):
    """列車が予約していたポイントを解放する"""
    if train_id in TRAIN_OCCUPIED_POINTS:
        train = layout.GetTrain(train_id)
        disp_name = get_train_display_name(train) if train else f"ID:{train_id}"
        released_pts = TRAIN_OCCUPIED_POINTS.pop(train_id)
        for pt_name in released_pts:
            if POINT_RESERVE_MAP.get(pt_name) == train_id:
                del POINT_RESERVE_MAP[pt_name]
                vrmapi.LOG(f"  -> [ポイント解放] 列車 {disp_name} によるポイント『{pt_name}』の予約を解放しました")

def retry_route_and_block_waiters(layout):
    global ROUTE_WAIT_QUEUE
    for wait_info in list(ROUTE_WAIT_QUEUE):
        t_id = wait_info['train_id']
        s_opt = wait_info['sensor_option']
        tr = layout.GetTrain(t_id)
        if tr:
            handle_route_setting(layout, tr, s_opt)

# ----------------------------------------------------
# 4. 編成スクリプトからの連結イベント受取関数
# ----------------------------------------------------

def on_train_couple_event(merged_train_obj, param):
    layout = vrmapi.LAYOUT()
    merged_id = merged_train_obj.GetID()
    disp_name = get_train_display_name(merged_train_obj)

    vrmapi.LOG(f"[連結イベント受信] 編成より連結(couple)が通知されました")
    vrmapi.LOG(f"   統合後編成: {disp_name} (ID: {merged_id})")

    for block_name, t_ids in list(BLOCK_OCCUPY_MAP.items()):
        new_t_ids = []
        for tid in t_ids:
            if tid == merged_id or tid not in new_t_ids:
                new_t_ids.append(merged_id)
            vrmapi.LOG(f"  -> [閉塞統合] 閉塞区間『{block_name}』内の旧列車ID:{tid} を 統合後ID:{merged_id} へ引き継ぎました")
        
        if new_t_ids:
            BLOCK_OCCUPY_MAP[block_name] = new_t_ids
        else:
            del BLOCK_OCCUPY_MAP[block_name]

    if merged_id not in TRAIN_CURRENT_BLOCKS:
        TRAIN_CURRENT_BLOCKS[merged_id] = []

    obsolete_ids = [tid for tid in list(TRAIN_CURRENT_BLOCKS.keys()) if tid != merged_id]
    for old_id in obsolete_ids:
        for bname in TRAIN_CURRENT_BLOCKS[old_id]:
            if bname not in TRAIN_CURRENT_BLOCKS[merged_id]:
                TRAIN_CURRENT_BLOCKS[merged_id].append(bname)
                vrmapi.LOG(f"  -> [列車閉塞統合] 旧列車ID:{old_id} が居た閉塞『{bname}』を 統合後ID:{merged_id} の管理下へ追加しました")
        del TRAIN_CURRENT_BLOCKS[old_id]

    if not PENDING_COUPLE_COMMANDS:
        vrmapi.LOG("  -> [通知無視] 連結待機中のコマンドが存在しません")
        return

    _, rem_cmds = PENDING_COUPLE_COMMANDS.popitem()
    vrmapi.LOG(f"  -> [コマンド再開] {disp_name} の残コマンドを実行します: {rem_cmds}")

    execute_command_chain(layout, merged_train_obj, rem_cmds)

# ----------------------------------------------------
# 5. 閉塞制御コア & イベントハンドラ
# ----------------------------------------------------

# ==============================================================================
# VRM-NX VNS (VRM Name-based System) レイアウト制御スクリプト
# 参照設計書: Train Control by Name設計書 Ver. 0.295 (予備進路機能追加版)
# ==============================================================================

import vrmapi
import re

# --- グローバル状態管理 ---
TRAIN_SMOOTH_MAP = {}          # { train_id: {'target': float, 'current': float, 'step': float} }
BLOCK_OCCUPY_MAP = {}          # { block_name: [train_id, ...] }
BLOCK_WAIT_QUEUE = {}          # { block_name: [train_id, ...] }
TRAIN_CURRENT_BLOCKS = {}      # { train_id: [block_name, ...] }
TRAIN_ALLOW_ONCE_BLOCK = {}    # { train_id: True }

# 進路構成用管理
POINT_RESERVE_MAP = {}         # { point_name: train_id }
TRAIN_OCCUPIED_POINTS = {}     # { train_id: [point_name, ...] }
ROUTE_WAIT_QUEUE = []          # [ {'train_id': int, 'sensor_option': str}, ... ]
TRAIN_BACKUP_ROUTES = {}       # { train_id: [backup_sensor_option, ...] } ※後勝ち優先のためリスト先頭に追加

# 連結待機管理: { active_tcn_name: [残りのコマンドリスト] }
PENDING_COUPLE_COMMANDS = {}

NEXT_TIMER_ID = 1000
TIMER_ACTION_MAP = {}

# ----------------------------------------------------
# 0. ログ出力 & 信号機制御ヘルパー
# ----------------------------------------------------

def get_train_display_name(train_obj):
    if not train_obj:
        return "UNKNOWN"
    data_name = str(train_obj.GetNAME())
    tcn_name = train_obj.GetStatusDataString("tcn_name")
    if not tcn_name:
        tcn_name = data_name
    return f"【データ名: '{data_name}' | tcn_name: '{tcn_name}'】"

def update_block_signals(layout, block_name, is_occupied):
    sig_list = []
    layout.ListSignal(sig_list)
    
    target_stat = 1 if is_occupied else 6
    color_label = "赤(停止)" if is_occupied else "青(進行)"

    for sig in sig_list:
        sig_name = str(sig.GetNAME())
        if block_name in sig_name:
            sig.SetStat(0, target_stat)
            vrmapi.LOG(f"  -> [信号制御] 信号機『{sig_name}』(閉塞:『{block_name}』) を {color_label} (Stat:{target_stat}) に変更しました")

# ----------------------------------------------------
# 1. パース & ユーティリティ関数
# ----------------------------------------------------

def cancel_train_timers(train_id):
    if train_id in TRAIN_SMOOTH_MAP:
        del TRAIN_SMOOTH_MAP[train_id]
        
    to_delete = [t_id for t_id, info in TIMER_ACTION_MAP.items() if info.get('train_id') == train_id]
    for t_id in to_delete:
        del TIMER_ACTION_MAP[t_id]

def parse_sensor_name(sensor_name):
    filter_dict = None
    rest = sensor_name

    match_filter = re.match(r'^\((種別|始発|終着|始終着)=([^)]+)\)(.*)$', sensor_name)
    if match_filter:
        f_type = match_filter.group(1)
        f_conds = match_filter.group(2).split('|')
        filter_dict = {'type': f_type, 'conds': f_conds}
        rest = match_filter.group(3)

    func_parts = rest.split('-', 1)
    func_type = func_parts[0]
    func_option = func_parts[1] if len(func_parts) > 1 else ""

    return filter_dict, func_type, func_option

def is_filter_matched(filter_dict, train_info):
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

def get_train_info(train_obj):
    tcn_name = train_obj.GetStatusDataString("tcn_name")
    if not tcn_name:
        tcn_name = str(train_obj.GetNAME())

    parts = tcn_name.split('-')
    train_type = parts[0] if len(parts) >= 1 else ""
    st1 = parts[1] if len(parts) >= 2 else ""
    st2 = parts[2] if len(parts) >= 3 else ""

    rev = train_obj.GetStatusDataInt("reverse")
    if rev == 1:
        origin, dest = st2, st1
    else:
        origin, dest = st1, st2

    return {
        'id': train_obj.GetID(),
        'data_name': str(train_obj.GetNAME()),
        'tcn_name': tcn_name,
        'type': train_type,
        'origin': origin,
        'dest': dest,
        'parts': parts
    }

# ----------------------------------------------------
# 2. 変速 & タイマー制御・コマンドチェーン実行
# ----------------------------------------------------

def set_train_voltage_smooth(layout, train_id, target_voltage, duration_sec=0.0):
    global NEXT_TIMER_ID
    train = layout.GetTrain(train_id)
    if not train:
        return

    current_voltage = train.GetVoltage()
    if train_id in TRAIN_SMOOTH_MAP:
        current_voltage = TRAIN_SMOOTH_MAP[train_id]['current']

    if duration_sec <= 0 or abs(current_voltage - target_voltage) < 0.01:
        train.SetVoltage(target_voltage)
        if train_id in TRAIN_SMOOTH_MAP:
            del TRAIN_SMOOTH_MAP[train_id]
        return

    interval = 0.2
    steps = max(1, int(duration_sec / interval))
    step_val = (target_voltage - current_voltage) / steps

    NEXT_TIMER_ID += 1
    timer_id = NEXT_TIMER_ID

    TRAIN_SMOOTH_MAP[train_id] = {
        'target': target_voltage,
        'current': current_voltage,
        'step': step_val
    }

    TIMER_ACTION_MAP[timer_id] = {
        'train_id': train_id,
        'action': 'smooth_step'
    }
    layout.SetEventTimer(interval, timer_id)

def execute_command_chain(layout, train_obj, commands):
    global NEXT_TIMER_ID
    if not commands:
        return

    cmd = commands[0]
    rem_cmds = commands[1:]
    train_id = train_obj.GetID()
    disp_name = get_train_display_name(train_obj)

    if cmd.startswith("変速"):
        m = re.search(r'変速(?:(\d+)%)?(?:(\d+)秒)?', cmd)
        speed = float(m.group(1)) / 100.0 if m and m.group(1) else 1.0
        dur = float(m.group(2)) if m and m.group(2) else 0.0
        
        vrmapi.LOG(f"  -> [コマンド実行] {disp_name}: 変速 {speed*100:.0f}% ({dur}秒)")
        set_train_voltage_smooth(layout, train_id, speed, dur)
        if rem_cmds:
            NEXT_TIMER_ID += 1
            t_id = NEXT_TIMER_ID
            TIMER_ACTION_MAP[t_id] = {'train_id': train_id, 'action': 'chain', 'cmds': rem_cmds}
            layout.SetEventTimer(dur if dur > 0 else 0.1, t_id)

    elif cmd == "連結":
        tcn_name = train_obj.GetStatusDataString("tcn_name") or str(train_obj.GetNAME())
        PENDING_COUPLE_COMMANDS[tcn_name] = rem_cmds
        vrmapi.LOG(f"  -> [コマンド待機] {disp_name}: 編成イベントからの連結通知(couple)を待機します...")

    elif cmd.startswith("停止"):
        m = re.search(r'停止(?:(\d+)秒)?', cmd)
        cancel_train_timers(train_id)
        train_obj.SetVoltage(0.0)

        if m and m.group(1):
            sec = float(m.group(1))
            vrmapi.LOG(f"  -> [コマンド実行] {disp_name}: 停止 ({sec}秒間)")
            if rem_cmds:
                NEXT_TIMER_ID += 1
                t_id = NEXT_TIMER_ID
                TIMER_ACTION_MAP[t_id] = {
                    'train_id': train_id, 
                    'action': 'chain', 
                    'cmds': rem_cmds
                }
                layout.SetEventTimer(sec, t_id)
        else:
            vrmapi.LOG(f"  -> [コマンド実行] {disp_name}: 無期限停止")

    elif cmd == "反転":
        train_obj.Turn()
        vrmapi.LOG(f"  -> [コマンド実行] {disp_name}: 向きを反転 (Turn)")
        execute_command_chain(layout, train_obj, rem_cmds)

    elif cmd == "折返":
        train_obj.Turn()
        current_rev = train_obj.GetStatusDataInt("reverse")
        new_rev = 0 if current_rev == 1 else 1
        train_obj.SetStatusDataInt("reverse", new_rev)
        vrmapi.LOG(f"  -> [コマンド実行] {disp_name}: 折返 (reverseステータス: {new_rev})")
        execute_command_chain(layout, train_obj, rem_cmds)

    elif cmd.startswith("分割先頭") or cmd.startswith("分割最後尾"):
        is_head = cmd.startswith("分割先頭")
        new_tcn_name = cmd.replace("分割先頭", "").replace("分割最後尾", "")

        car_list = train_obj.GetCarList()
        total_cars = len(car_list) if car_list else 0

        if total_cars < 2:
            vrmapi.LOG(f"  -> [解結エラー] 1両編成のため解結できません (両数: {total_cars})")
            return

        split_index = 1 if is_head else (total_cars - 1)
        mode_str = "分割先頭" if is_head else "分割最後尾"
        vrmapi.LOG(f"  -> [解結準備] {disp_name} の解結({mode_str})を開始します (全{total_cars}両 / 位置: {split_index}両目, 新名称: '{new_tcn_name}')")

        new_train_id = train_obj.SplitTrain(split_index)

        if new_train_id and new_train_id > 0:
            new_train = layout.GetTrain(new_train_id)
            orig_tcn = train_obj.GetStatusDataString("tcn_name")

            if is_head:
                if new_train:
                    new_train.SetStatusDataString("tcn_name", orig_tcn)
                    new_train.SetVoltage(0.0)

                cancel_train_timers(train_id)
                train_obj.SetStatusDataString("tcn_name", new_tcn_name)

                moving_train = train_obj
                stay_train = new_train
            else:
                cancel_train_timers(train_id)
                train_obj.SetStatusDataString("tcn_name", orig_tcn)
                train_obj.SetVoltage(0.0)

                if new_train:
                    new_train.SetStatusDataString("tcn_name", new_tcn_name)

                moving_train = new_train
                stay_train = train_obj

            if train_id in TRAIN_CURRENT_BLOCKS:
                for bname in TRAIN_CURRENT_BLOCKS[train_id]:
                    if bname in BLOCK_OCCUPY_MAP and new_train_id not in BLOCK_OCCUPY_MAP[bname]:
                        BLOCK_OCCUPY_MAP[bname].append(new_train_id)
                    if new_train_id not in TRAIN_CURRENT_BLOCKS:
                        TRAIN_CURRENT_BLOCKS[new_train_id] = []
                    TRAIN_CURRENT_BLOCKS[new_train_id].append(bname)

            vrmapi.LOG(f"  -> [解結完了] 解結成功！")
            vrmapi.LOG(f"     ・停留側: {get_train_display_name(stay_train) if stay_train else 'N/A'}")
            vrmapi.LOG(f"     ・分離側(発車): {get_train_display_name(moving_train) if moving_train else 'N/A'}")

            if moving_train:
                execute_command_chain(layout, moving_train, rem_cmds)
        else:
            vrmapi.LOG(f"  -> [解結エラー] SplitTrain({split_index}) に失敗しました。")

    elif cmd.startswith("起動"):
        is_turn = cmd.startswith("起動反転")
        prefix = "起動反転" if is_turn else "起動"
        
        m = re.search(rf'{prefix}(?:(\d+)%)?([^?]*)(?:\?\(([^)]+)\))?', cmd)
        speed = float(m.group(1)) / 100.0 if m and m.group(1) else 1.0
        target_blocks_str = m.group(2) if m else ""
        cond_block = m.group(3) if m else None

        target_blocks = [b.strip() for b in target_blocks_str.split('|') if b.strip()]

        if cond_block:
            occupying = BLOCK_OCCUPY_MAP.get(cond_block, [])
            if len(occupying) == 0:
                vrmapi.LOG(f"  -> [{prefix}スキップ] 条件閉塞『{cond_block}』に列車が存在しないため起動処理をスキップしました")
                execute_command_chain(layout, train_obj, rem_cmds)
                return

        cmd_label = "起動反転" if is_turn else "起動"
        vrmapi.LOG(f"  -> [{cmd_label}コマンド] 閉塞リスト {target_blocks} からの起動を試みます (指定速度: {speed*100:.0f}%)")

        for bname in target_blocks:
            if bname in BLOCK_OCCUPY_MAP and len(BLOCK_OCCUPY_MAP[bname]) > 0:
                target_train_id = BLOCK_OCCUPY_MAP[bname][0]
                target_train = layout.GetTrain(target_train_id)
                if target_train:
                    TRAIN_ALLOW_ONCE_BLOCK[target_train_id] = True
                    target_disp = get_train_display_name(target_train)
                    
                    if is_turn:
                        target_train.Turn()
                        vrmapi.LOG(f"     ・閉塞区間『{bname}』内の列車 {target_disp} の向きを反転(Turn)しました。")

                    vrmapi.LOG(f"     ・閉塞区間『{bname}』内の列車 {target_disp} を起動しました。(1回限定の閉塞進入許可を付与)")
                    set_train_voltage_smooth(layout, target_train_id, speed, 1.0)
                    break
        execute_command_chain(layout, train_obj, rem_cmds)

# ----------------------------------------------------
# 3. 進路構成制御コア
# ----------------------------------------------------

def register_backup_route(train_id, sensor_option):
    """予備進路を列車IDに登録する（後に登録されたものが優先されるよう先頭に挿入）"""
    if train_id not in TRAIN_BACKUP_ROUTES:
        TRAIN_BACKUP_ROUTES[train_id] = []
    TRAIN_BACKUP_ROUTES[train_id].insert(0, sensor_option)

def try_single_route_setting(layout, train_obj, sensor_option):
    """単一の進路構成オプションの試行（成功時 True、不成立時 False）"""
    train_id = train_obj.GetID()

    parts = [p.strip() for p in sensor_option.split('>') if p.strip()]
    if not parts:
        return True

    target_block = parts[-1]
    point_cmds = parts[:-1]

    pt_list = []
    layout.ListPoint(pt_list)
    parsed_pt_cmds = []

    for pt_cmd in point_cmds:
        direction = 1 if pt_cmd.startswith("分岐") else 0
        pt_name_sub = pt_cmd.replace("分岐", "").replace("直進", "")

        matched_pts = [pt for pt in pt_list if pt_name_sub in str(pt.GetNAME())]
        for pt in matched_pts:
            p_full_name = str(pt.GetNAME())
            reserver = POINT_RESERVE_MAP.get(p_full_name)
            if reserver is not None and reserver != train_id:
                vrmapi.LOG(f"  -> [進路試行不可] ポイント『{p_full_name}』は他列車(ID:{reserver})が予約中")
                return False
            parsed_pt_cmds.append((pt, p_full_name, direction))

    occupying_trains = BLOCK_OCCUPY_MAP.get(target_block, [])
    other_trains = [tid for tid in occupying_trains if tid != train_id]
    if len(other_trains) > 0:
        vrmapi.LOG(f"  -> [進路試行不可] 目的閉塞『{target_block}』は使用中")
        return False

    disp_name = get_train_display_name(train_obj)
    vrmapi.LOG(f"  -> [進路構成成功] {disp_name} の進路を確保・構成します (選択進路: '{sensor_option}')")

    if train_id not in TRAIN_OCCUPIED_POINTS:
        TRAIN_OCCUPIED_POINTS[train_id] = []

    for pt, p_full_name, direction in parsed_pt_cmds:
        pt.SetBranch(direction)
        POINT_RESERVE_MAP[p_full_name] = train_id
        if p_full_name not in TRAIN_OCCUPIED_POINTS[train_id]:
            TRAIN_OCCUPIED_POINTS[train_id].append(p_full_name)
        dir_label = "分岐(1)" if direction == 1 else "直進(0)"
        vrmapi.LOG(f"     ・ポイント『{p_full_name}』を {dir_label} に切替・予約完了")

    if target_block not in BLOCK_OCCUPY_MAP:
        BLOCK_OCCUPY_MAP[target_block] = []
    if train_id not in BLOCK_OCCUPY_MAP[target_block]:
        BLOCK_OCCUPY_MAP[target_block].append(train_id)

    if train_id not in TRAIN_CURRENT_BLOCKS:
        TRAIN_CURRENT_BLOCKS[train_id] = []
    if target_block not in TRAIN_CURRENT_BLOCKS[train_id]:
        TRAIN_CURRENT_BLOCKS[train_id].append(target_block)

    update_block_signals(layout, target_block, is_occupied=True)

    global ROUTE_WAIT_QUEUE
    ROUTE_WAIT_QUEUE = [q for q in ROUTE_WAIT_QUEUE if q['train_id'] != train_id]

    if train_obj.GetVoltage() < 0.01:
        vrmapi.LOG(f"  -> [進路発車] 停止中の列車 {disp_name} を再発車させます")
        set_train_voltage_smooth(layout, train_id, 1.0, 2.0)

    return True

def handle_route_setting(layout, train_obj, primary_sensor_option):
    """本進路および登録された予備進路（後勝ち優先）を順に試行する"""
    train_id = train_obj.GetID()
    disp_name = get_train_display_name(train_obj)

    candidates = [primary_sensor_option]
    if train_id in TRAIN_BACKUP_ROUTES:
        candidates.extend(TRAIN_BACKUP_ROUTES[train_id])

    for option in candidates:
        vrmapi.LOG(f"  -> [進路確保試行] {disp_name}: 候補進路『{option}』を判定中...")
        if try_single_route_setting(layout, train_obj, option):
            # 進路構成に成功した場合、登録された予备進路はすべて消去
            if train_id in TRAIN_BACKUP_ROUTES:
                del TRAIN_BACKUP_ROUTES[train_id]
                vrmapi.LOG(f"  -> [予備進路消去] {disp_name} の登録済み予備進路をクリアしました")
            return True

    # 全進路が不可の場合
    vrmapi.LOG(f"  -> [進路構成失敗] {disp_name}: 本進路および予備進路のすべてが使用不可でした")
    _fail_route(layout, train_obj, primary_sensor_option)
    return False

def handle_route_release(layout, train_obj, sensor_option):
    """進路構成センサーを最後尾車輪が通過した際、前閉塞のみ解放する（ポイントはまだ解放しない）"""
    train_id = train_obj.GetID()
    disp_name = get_train_display_name(train_obj)

    parts = [p.strip() for p in sensor_option.split('>') if p.strip()]
    target_block = parts[-1] if parts else None

    # 現在の閉塞情報からターゲット以外の旧閉塞を解放
    current_blocks = list(TRAIN_CURRENT_BLOCKS.get(train_id, []))
    for old_block in current_blocks:
        if old_block != target_block:
            if old_block in BLOCK_OCCUPY_MAP and train_id in BLOCK_OCCUPY_MAP[old_block]:
                BLOCK_OCCUPY_MAP[old_block].remove(train_id)
                if len(BLOCK_OCCUPY_MAP[old_block]) == 0:
                    del BLOCK_OCCUPY_MAP[old_block]
                    update_block_signals(layout, old_block, is_occupied=False)

                vrmapi.LOG(f"  -> [進路閉塞解放] 列車 {disp_name} (最後尾車輪) が 進路構成センサー通過により閉塞区間『{old_block}』を開放")

            if old_block in TRAIN_CURRENT_BLOCKS[train_id]:
                TRAIN_CURRENT_BLOCKS[train_id].remove(old_block)

            # 閉塞待ち列車の再起動
            if old_block in BLOCK_WAIT_QUEUE and len(BLOCK_WAIT_QUEUE[old_block]) > 0:
                waiting_train_id = BLOCK_WAIT_QUEUE[old_block].pop(0)
                waiting_train = layout.GetTrain(waiting_train_id)
                if waiting_train:
                    if old_block not in BLOCK_OCCUPY_MAP:
                        BLOCK_OCCUPY_MAP[old_block] = []
                    BLOCK_OCCUPY_MAP[old_block].append(waiting_train_id)

                    if waiting_train_id not in TRAIN_CURRENT_BLOCKS:
                        TRAIN_CURRENT_BLOCKS[waiting_train_id] = []
                    TRAIN_CURRENT_BLOCKS[waiting_train_id].append(old_block)

                    w_disp = get_train_display_name(waiting_train)
                    vrmapi.LOG(f"  -> [閉塞再開] 待機中の列車 {w_disp} が 閉塞区間『{old_block}』へ進入発車")

                    update_block_signals(layout, old_block, is_occupied=True)
                    set_train_voltage_smooth(layout, waiting_train_id, 1.0, 3.0)

    retry_route_and_block_waiters(layout)

def _fail_route(layout, train_obj, sensor_option):
    train_id = train_obj.GetID()
    disp_name = get_train_display_name(train_obj)

    cancel_train_timers(train_id)
    train_obj.SetVoltage(0.0)

    if not any(q['train_id'] == train_id for q in ROUTE_WAIT_QUEUE):
        ROUTE_WAIT_QUEUE.append({'train_id': train_id, 'sensor_option': sensor_option})
    
    vrmapi.LOG(f"  -> [進路構成待機] 列車 {disp_name} を直ちに停止させ、進路開放を待機します")

def release_train_points(layout, train_id):
    """列車が予約していたポイントを解放する"""
    if train_id in TRAIN_OCCUPIED_POINTS:
        train = layout.GetTrain(train_id)
        disp_name = get_train_display_name(train) if train else f"ID:{train_id}"
        released_pts = TRAIN_OCCUPIED_POINTS.pop(train_id)
        for pt_name in released_pts:
            if POINT_RESERVE_MAP.get(pt_name) == train_id:
                del POINT_RESERVE_MAP[pt_name]
                vrmapi.LOG(f"  -> [ポイント解放] 列車 {disp_name} によるポイント『{pt_name}』の予約を解放しました")

def retry_route_and_block_waiters(layout):
    global ROUTE_WAIT_QUEUE
    for wait_info in list(ROUTE_WAIT_QUEUE):
        t_id = wait_info['train_id']
        s_opt = wait_info['sensor_option']
        tr = layout.GetTrain(t_id)
        if tr:
            handle_route_setting(layout, tr, s_opt)

# ----------------------------------------------------
# 4. 編成スクリプトからの連結イベント受取関数
# ----------------------------------------------------

def on_train_couple_event(merged_train_obj, param):
    layout = vrmapi.LAYOUT()
    merged_id = merged_train_obj.GetID()
    disp_name = get_train_display_name(merged_train_obj)

    vrmapi.LOG(f"[連結イベント受信] 編成より連結(couple)が通知されました")
    vrmapi.LOG(f"   統合後編成: {disp_name} (ID: {merged_id})")

    # 1. 統合後編成が現在実際に所在している閉塞リストを取得（元々存在していた閉塞のみを統合対象とする）
    # ※BLOCK_OCCUPY_MAP の中から merged_id が登録されている閉塞を特定
    target_blocks = [bname for bname, t_ids in BLOCK_OCCUPY_MAP.items() if merged_id in t_ids]

    # 2. 統合後列車が滞留する閉塞内のみ、旧列車IDのクリアと引き継ぎを行う
    for block_name in target_blocks:
        t_ids = BLOCK_OCCUPY_MAP[block_name]
        # 重複を除去して統合後IDのみに更新
        BLOCK_OCCUPY_MAP[block_name] = [merged_id]
        vrmapi.LOG(f"  -> [閉塞統合] 閉塞区間『{block_name}』の占有列車IDを 統合後ID:{merged_id} に更新しました")

    # 3. TRAIN_CURRENT_BLOCKS の管理も統合後列車が実際に所在する閉塞のみに更新
    TRAIN_CURRENT_BLOCKS[merged_id] = list(target_blocks)

    # 4. 統合によって消滅した旧列車IDのクリーンアップ（他閉塞に不要な参照を残さない）
    for block_name, t_ids in list(BLOCK_OCCUPY_MAP.items()):
        if block_name not in target_blocks:
            # 統合された閉塞以外からは旧ID（または不要に引き継がれたID）を除外
            new_t_ids = [tid for tid in t_ids if tid != merged_id]
            if new_t_ids:
                BLOCK_OCCUPY_MAP[block_name] = new_t_ids
            else:
                del BLOCK_OCCUPY_MAP[block_name]
                update_block_signals(layout, block_name, is_occupied=False)

    # 旧列車の個別滞留リストを破棄
    obsolete_ids = [tid for tid in list(TRAIN_CURRENT_BLOCKS.keys()) if tid != merged_id and tid not in [t.GetID() for t in layout.GetTrainList() if t]]
    for old_id in obsolete_ids:
        del TRAIN_CURRENT_BLOCKS[old_id]

    # 5. 連結待機コマンドの再開
    if not PENDING_COUPLE_COMMANDS:
        vrmapi.LOG("  -> [通知無視] 連結待機中のコマンドが存在しません")
        return

    _, rem_cmds = PENDING_COUPLE_COMMANDS.popitem()
    vrmapi.LOG(f"  -> [コマンド再開] {disp_name} の残コマンドを実行します: {rem_cmds}")

    execute_command_chain(layout, merged_train_obj, rem_cmds)

# ----------------------------------------------------
# 5. 閉塞制御コア & イベントハンドラ
# ----------------------------------------------------

def handle_block_entry(layout, train_obj, target_block_name, tire_type):
    """
    閉塞センサー通過時の処理
    tire_type: 1 = 先頭車輪（進入判定・FIFOリスト末尾追加）
               2 = 最後尾車輪（指定閉塞より前の旧閉塞のみ解放・ポイント解放）
    """
    train_id = train_obj.GetID()
    disp_name = get_train_display_name(train_obj)
    
    if train_id not in TRAIN_CURRENT_BLOCKS:
        TRAIN_CURRENT_BLOCKS[train_id] = []

    # ==========================================
    # 1. 先頭車輪の通過（閉塞進入・FIFOリスト末尾追加）
    # ==========================================
    if tire_type == 1:
        if not target_block_name or target_block_name in ["なし", "解除"]:
            return

        if target_block_name in TRAIN_CURRENT_BLOCKS[train_id]:
            return

        occupying_trains = BLOCK_OCCUPY_MAP.get(target_block_name, [])
        other_trains = [tid for tid in occupying_trains if tid != train_id]
        
        allow_once = TRAIN_ALLOW_ONCE_BLOCK.pop(train_id, False)

        if len(other_trains) > 0 and not allow_once:
            # 他の列車が占有中の場合は非常停止し、待機キューへ登録
            cancel_train_timers(train_id)
            train_obj.SetVoltage(0.0)
            
            if target_block_name not in BLOCK_WAIT_QUEUE:
                BLOCK_WAIT_QUEUE[target_block_name] = []
            if train_id not in BLOCK_WAIT_QUEUE[target_block_name]:
                BLOCK_WAIT_QUEUE[target_block_name].append(train_id)

            vrmapi.LOG(f"  -> [閉塞:進入不可] 閉塞区間『{target_block_name}』は使用中のため、列車 {disp_name} は直ちに停止・待機します")
        else:
            if allow_once:
                vrmapi.LOG(f"  -> [閉塞特例進入] 起動特例パスにより、列車 {disp_name} が占有中の閉塞区間『{target_block_name}』へ進入許可されました")

            # 閉塞の占有登録
            if target_block_name not in BLOCK_OCCUPY_MAP:
                BLOCK_OCCUPY_MAP[target_block_name] = []
            if train_id not in BLOCK_OCCUPY_MAP[target_block_name]:
                BLOCK_OCCUPY_MAP[target_block_name].append(train_id)
            
            # FIFOリストの最後に追加（通過順を保持）
            TRAIN_CURRENT_BLOCKS[train_id].append(target_block_name)
            vrmapi.LOG(f"  -> [閉塞進入] 列車 {disp_name} (先頭車輪) が 閉塞区間『{target_block_name}』に進入 (現在滞留閉塞: {TRAIN_CURRENT_BLOCKS[train_id]})")

            update_block_signals(layout, target_block_name, is_occupied=True)

    # ==========================================
    # 2. 最後尾車輪の通過（ポイント解放・自身より前の旧閉塞解放）
    # ==========================================
    elif tire_type == 2:
        # 目的地閉塞（駅ホーム等）に最後尾が進入したため、通過してきたポイント群を予約解除
        release_train_points(layout, train_id)

        current_blocks = TRAIN_CURRENT_BLOCKS[train_id]

        # FIFOリスト内に今回の閉塞が存在する場合、その前にある閉塞をすべて取り出して解放する
        if target_block_name in current_blocks:
            idx = current_blocks.index(target_block_name)
            blocks_to_release = current_blocks[:idx]  # 自身より前（古い）の閉塞リスト
            TRAIN_CURRENT_BLOCKS[train_id] = current_blocks[idx:]  # 自身以降（現在跨がっている）閉塞を保持

            for old_block in blocks_to_release:
                if old_block in BLOCK_OCCUPY_MAP and train_id in BLOCK_OCCUPY_MAP[old_block]:
                    BLOCK_OCCUPY_MAP[old_block].remove(train_id)
                    if len(BLOCK_OCCUPY_MAP[old_block]) == 0:
                        del BLOCK_OCCUPY_MAP[old_block]
                        update_block_signals(layout, old_block, is_occupied=False)

                    vrmapi.LOG(f"  -> [閉塞解放] 列車 {disp_name} (最後尾車輪) が 閉塞区間『{old_block}』を完全に離脱・開放")

                # 閉塞解放待ちをしていた後続列車があれば発車処理
                if old_block in BLOCK_WAIT_QUEUE and len(BLOCK_WAIT_QUEUE[old_block]) > 0:
                    waiting_train_id = BLOCK_WAIT_QUEUE[old_block].pop(0)
                    waiting_train = layout.GetTrain(waiting_train_id)
                    if waiting_train:
                        if old_block not in BLOCK_OCCUPY_MAP:
                            BLOCK_OCCUPY_MAP[old_block] = []
                        BLOCK_OCCUPY_MAP[old_block].append(waiting_train_id)
                        
                        if waiting_train_id not in TRAIN_CURRENT_BLOCKS:
                            TRAIN_CURRENT_BLOCKS[waiting_train_id] = []
                        TRAIN_CURRENT_BLOCKS[waiting_train_id].append(old_block)

                        w_disp = get_train_display_name(waiting_train)
                        vrmapi.LOG(f"  -> [閉塞再開] 待機中の列車 {w_disp} が 閉塞区間『{old_block}』へ進入発車")
                        
                        update_block_signals(layout, old_block, is_occupied=True)
                        set_train_voltage_smooth(layout, waiting_train_id, 1.0, 3.0)

        # 解放されたポイントや閉塞を待っていた他列車の再評価
        retry_route_and_block_waiters(layout)

def vrmevent(obj, ev, param):
    layout = vrmapi.LAYOUT()

    if ev == 'init':
        vrmapi.LOG("[初期化] VNS 制御スクリプト(Ver. 0.295) を起動します...")

        pt_list = []
        layout.ListPoint(pt_list)
        vrmapi.LOG(f"[初期化] 検出ポイント数: {len(pt_list)}")

        sig_list = []
        layout.ListSignal(sig_list)
        vrmapi.LOG(f"[初期化] 検出信号機数: {len(sig_list)}")
        for sig in sig_list:
            sig.SetStat(0, 6)

        tr_list = []
        layout.ListTrain(tr_list)
        vrmapi.LOG(f"[初期化] 検出列車数: {len(tr_list)}")
        
        for tr in tr_list:
            tr.SetStatusDataInt("reverse", 0)
            data_name = str(tr.GetNAME())
            tr.SetStatusDataString("tcn_name", data_name)

            disp = get_train_display_name(tr)
            vrmapi.LOG(f"  -> 初期登録: {disp}")

        vrmapi.LOG("[初期化] 完了")

    elif ev == 'timer':
        timer_id = None
        if isinstance(param, dict):
            timer_id = param.get('eventUID') or param.get('eventid') or param.get('id')
        elif isinstance(param, int):
            timer_id = param
        else:
            try:
                timer_id = int(param)
            except Exception:
                pass

        if timer_id in TIMER_ACTION_MAP:
            info = TIMER_ACTION_MAP.pop(timer_id)
            train_id = info['train_id']
            action = info['action']
            train = layout.GetTrain(train_id)

            if not train:
                return

            if action == 'smooth_step':
                if train_id in TRAIN_SMOOTH_MAP:
                    data = TRAIN_SMOOTH_MAP[train_id]
                    new_voltage = data['current'] + data['step']
                    target = data['target']

                    is_complete = False
                    if data['step'] > 0 and new_voltage >= target:
                        new_voltage = target
                        is_complete = True
                    elif data['step'] < 0 and new_voltage <= target:
                        new_voltage = target
                        is_complete = True

                    train.SetVoltage(new_voltage)
                    data['current'] = new_voltage

                    if not is_complete:
                        global NEXT_TIMER_ID
                        NEXT_TIMER_ID += 1
                        next_id = NEXT_TIMER_ID
                        TIMER_ACTION_MAP[next_id] = {'train_id': train_id, 'action': 'smooth_step'}
                        layout.SetEventTimer(0.2, next_id)
                    else:
                        del TRAIN_SMOOTH_MAP[train_id]

            elif action == 'chain':
                execute_command_chain(layout, train, info['cmds'])

def on_sensor_catch(sensor_obj, param):
    sensor_dir = param.get('dir') if 'dir' in param else param.get('direction', None)
    if sensor_dir is not None and sensor_dir != 1:
        return

    tire_type = param.get('tire', 1)

    layout = vrmapi.LAYOUT()
    train_id = param.get('trainid')
    train = layout.GetTrain(train_id)
    if not train:
        return

    train_info = get_train_info(train)
    sensor_name = str(sensor_obj.GetNAME())

    filter_dict, func_type, func_option = parse_sensor_name(sensor_name)
    matched = is_filter_matched(filter_dict, train_info)

    wheel_label = "先頭車輪" if tire_type == 1 else "最後尾車輪"
    disp_name = get_train_display_name(train)
    vrmapi.LOG(f"[自動センサー通過] データ名:『{sensor_name}』 ({wheel_label}) 通過列車: {disp_name} (FilterMatch: {matched})")

    if tire_type == 1 and func_type in ["ポイント直進", "ポイント分岐", "ポイント制御"]:
        location_name = func_option
        pt_list = []
        layout.ListPoint(pt_list)

        for pt in pt_list:
            pt_name = str(pt.GetNAME())
            if location_name in pt_name:
                if func_type == "ポイント直進":
                    if matched:
                        pt.SetBranch(0)
                        vrmapi.LOG(f"  -> ポイント『{pt_name}』: 【直進(0)】切替")
                elif func_type == "ポイント分岐":
                    if matched:
                        pt.SetBranch(1)
                        vrmapi.LOG(f"  -> ポイント『{pt_name}』: 【分岐(1)】切替")
                elif func_type == "ポイント制御":
                    if matched:
                        pt.SetBranch(1)
                        vrmapi.LOG(f"  -> ポイント『{pt_name}』: Match -> 【分岐(1)】切替")
                    else:
                        pt.SetBranch(0)
                        vrmapi.LOG(f"  -> ポイント『{pt_name}』: Unmatch -> 【直進(0)】切替")

    elif tire_type == 1 and func_type == "列車制御":
        if matched and func_option:
            commands = func_option.split('>')
            execute_command_chain(layout, train, commands)

    elif func_type == "予備進路":
        if matched and tire_type == 1:
            register_backup_route(train_id, func_option)
            vrmapi.LOG(f"  -> [予備進路登録] 列車 {disp_name} に予備進路『{func_option}』を登録しました")

    elif func_type == "閉塞":
        if matched:
            block_name = func_option
            handle_block_entry(layout, train, block_name, tire_type)

    elif func_type == "進路構成":
        if matched:
            if tire_type == 1:
                handle_route_setting(layout, train, func_option)
            elif tire_type == 2:
                handle_route_release(layout, train, func_option)

def vrmevent(obj, ev, param):
    layout = vrmapi.LAYOUT()

    if ev == 'init':
        vrmapi.LOG("[初期化] VNS 制御スクリプト(Ver. 0.295) を起動します...")

        pt_list = []
        layout.ListPoint(pt_list)
        vrmapi.LOG(f"[初期化] 検出ポイント数: {len(pt_list)}")

        sig_list = []
        layout.ListSignal(sig_list)
        vrmapi.LOG(f"[初期化] 検出信号機数: {len(sig_list)}")
        for sig in sig_list:
            sig.SetStat(0, 6)

        tr_list = []
        layout.ListTrain(tr_list)
        vrmapi.LOG(f"[初期化] 検出列車数: {len(tr_list)}")
        
        for tr in tr_list:
            tr.SetStatusDataInt("reverse", 0)
            data_name = str(tr.GetNAME())
            tr.SetStatusDataString("tcn_name", data_name)

            disp = get_train_display_name(tr)
            vrmapi.LOG(f"  -> 初期登録: {disp}")

        vrmapi.LOG("[初期化] 完了")

    elif ev == 'timer':
        timer_id = None
        if isinstance(param, dict):
            timer_id = param.get('eventUID') or param.get('eventid') or param.get('id')
        elif isinstance(param, int):
            timer_id = param
        else:
            try:
                timer_id = int(param)
            except Exception:
                pass

        if timer_id in TIMER_ACTION_MAP:
            info = TIMER_ACTION_MAP.pop(timer_id)
            train_id = info['train_id']
            action = info['action']
            train = layout.GetTrain(train_id)

            if not train:
                return

            if action == 'smooth_step':
                if train_id in TRAIN_SMOOTH_MAP:
                    data = TRAIN_SMOOTH_MAP[train_id]
                    new_voltage = data['current'] + data['step']
                    target = data['target']

                    is_complete = False
                    if data['step'] > 0 and new_voltage >= target:
                        new_voltage = target
                        is_complete = True
                    elif data['step'] < 0 and new_voltage <= target:
                        new_voltage = target
                        is_complete = True

                    train.SetVoltage(new_voltage)
                    data['current'] = new_voltage

                    if not is_complete:
                        global NEXT_TIMER_ID
                        NEXT_TIMER_ID += 1
                        next_id = NEXT_TIMER_ID
                        TIMER_ACTION_MAP[next_id] = {'train_id': train_id, 'action': 'smooth_step'}
                        layout.SetEventTimer(0.2, next_id)
                    else:
                        del TRAIN_SMOOTH_MAP[train_id]

            elif action == 'chain':
                execute_command_chain(layout, train, info['cmds'])

def on_sensor_catch(sensor_obj, param):
    sensor_dir = param.get('dir') if 'dir' in param else param.get('direction', None)
    if sensor_dir is not None and sensor_dir != 1:
        return

    tire_type = param.get('tire', 1)

    layout = vrmapi.LAYOUT()
    train_id = param.get('trainid')
    train = layout.GetTrain(train_id)
    if not train:
        return

    train_info = get_train_info(train)
    sensor_name = str(sensor_obj.GetNAME())

    filter_dict, func_type, func_option = parse_sensor_name(sensor_name)
    matched = is_filter_matched(filter_dict, train_info)

    wheel_label = "先頭車輪" if tire_type == 1 else "最後尾車輪"
    disp_name = get_train_display_name(train)
    vrmapi.LOG(f"[自動センサー通過] データ名:『{sensor_name}』 ({wheel_label}) 通過列車: {disp_name} (FilterMatch: {matched})")

    if tire_type == 1 and func_type in ["ポイント直進", "ポイント分岐", "ポイント制御"]:
        location_name = func_option
        pt_list = []
        layout.ListPoint(pt_list)

        for pt in pt_list:
            pt_name = str(pt.GetNAME())
            if location_name in pt_name:
                if func_type == "ポイント直進":
                    if matched:
                        pt.SetBranch(0)
                        vrmapi.LOG(f"  -> ポイント『{pt_name}』: 【直進(0)】切替")
                elif func_type == "ポイント分岐":
                    if matched:
                        pt.SetBranch(1)
                        vrmapi.LOG(f"  -> ポイント『{pt_name}』: 【分岐(1)】切替")
                elif func_type == "ポイント制御":
                    if matched:
                        pt.SetBranch(1)
                        vrmapi.LOG(f"  -> ポイント『{pt_name}』: Match -> 【分岐(1)】切替")
                    else:
                        pt.SetBranch(0)
                        vrmapi.LOG(f"  -> ポイント『{pt_name}』: Unmatch -> 【直進(0)】切替")

    elif tire_type == 1 and func_type == "列車制御":
        if matched and func_option:
            commands = func_option.split('>')
            execute_command_chain(layout, train, commands)

    elif func_type == "予備進路":
        if matched and tire_type == 1:
            register_backup_route(train_id, func_option)
            vrmapi.LOG(f"  -> [予備進路登録] 列車 {disp_name} に予備進路『{func_option}』を登録しました")

    elif func_type == "閉塞":
        if matched:
            block_name = func_option
            handle_block_entry(layout, train, block_name, tire_type)

    elif func_type == "進路構成":
        if matched:
            if tire_type == 1:
                handle_route_setting(layout, train, func_option)
            elif tire_type == 2:
                handle_route_release(layout, train, func_option)
