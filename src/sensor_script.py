# ==============================================================================
# VRM-NX VNS (VRM Name-based System) 自動センサー制御スクリプト
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
import sys

def vrmevent_1067(obj, ev, param):
    if ev == 'catch':
        main_mod = sys.modules['__main__']
        if hasattr(main_mod, 'on_sensor_catch'):
            main_mod.on_sensor_catch(obj, param)
