Train Control by Name

I.MAGIC社製の[鉄道模型シミュレータNX](https://www.imagic.co.jp/hobby/)を自動制御するためのスクリプトです。

# チュートリアル
チュートリアル動画(製作中)

# スクリプト
[レイアウトスクリプト](https://raw.githubusercontent.com/kourinbou4466-hash/vrm_name_based_system/refs/heads/main/src/layout_script.py)
鉄道模型シミュレータNXでレイアウトスクリプトエディターを開いて、張り付けてください。

[自動センサースクリプト](https://raw.githubusercontent.com/kourinbou4466-hash/vrm_name_based_system/refs/heads/main/src/sensor_script.py)
自動センサーのプロパティ→スクリプト→スクリプトエディタを開いて、張り付けてください。
スクリプト内18行目のvrmevent_1067とある数字部分を、自動センサーの部品IDに書き換えてください。
閉塞、進路構成の機能を使うときは、プロパティ→ATS基本設定→台車検出条件を「先頭と最後尾台車を検出」に変更してください。

[列車スクリプト](https://raw.githubusercontent.com/kourinbou4466-hash/vrm_name_based_system/refs/heads/main/src/train_script.py)
編成配置のプロパティ→スクリプト→スクリプトエディタを開いて、張り付けてください。
スクリプト内18行目のvrmevent_784とある数字部分を、編成配置の部品IDに書き換えてください。
