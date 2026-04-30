# moodle-transcriber ロードマップ

将来実装したい機能案。

---

## 案1: GUI アプリ（優先度: 低・急がない）

ターミナルを使わずに操作できる macOS ネイティブ GUI アプリ。

### 想定 UI

- URL 入力フィールド（`--moodle-url` 相当）
- モデル選択ドロップダウン（tiny / small / medium / large-v3）
- 出力形式選択（TXT / SRT / VTT）
- 出力先フォルダ選択ボタン
- 開始 / 停止ボタン
- リアルタイム文字起こしプレビュー

### 技術候補

| 選択肢 | 特徴 |
|---|---|
| `tkinter` | Python 標準。依存追加なし。UIは素朴 |
| `PyQt6` / `PySide6` | モダンな UI が作りやすい。要インストール |
| `rumps` (macOS menu bar) | メニューバーアプリとして常駐。軽量 |
| Swift / SwiftUI | ネイティブ最高品質。Python との連携が必要 |

### 実装メモ

- `main.py` の `run()` をそのまま `threading.Thread` で呼び出せる設計になっているため GUI 化しやすい
- 文字起こし結果は `OutputWriter` を wrap して GUI にストリーム表示できる

---

## 案2: 複数 URL の順次自動処理（優先度: 高）

複数の Moodle 講義 URL を渡すと、1本ずつ順番に全自動で処理してくれる機能。

### やりたいこと（フロー）

```
URL リスト [url1, url2, url3, ...]
        ↓
  url1 を Chrome で開く
        ↓
  再生ボタンを自動クリック → 文字起こし開始
        ↓
  動画終了を検知
        ↓
  「視聴履歴を保存」ボタンを自動クリック
        ↓
  文字起こしファイルを out/<日付>/<タイトル>.txt に保存
        ↓
  url2 を Chrome で開く → 繰り返し
        ↓
  全 URL 処理完了 → 通知
```

### CLI イメージ

```bash
# 複数 URL をスペース区切りで渡す
python main.py \
  --moodle-url "https://moodle.example.com/mod/scorm/player.php?id=101" \
  --moodle-url "https://moodle.example.com/mod/scorm/player.php?id=102" \
  --moodle-url "https://moodle.example.com/mod/scorm/player.php?id=103"

# または URL リストをファイルで渡す
python main.py --url-file urls.txt
```

`urls.txt` の形式:
```
https://moodle.example.com/mod/scorm/player.php?id=101
https://moodle.example.com/mod/scorm/player.php?id=102
https://moodle.example.com/mod/scorm/player.php?id=103
```

### 実装上の課題

| 課題 | アプローチ |
|---|---|
| **動画終了の検知** | JS で `video.ended` イベントを監視。ended になったら次へ進む |
| **再生ボタンのクリック** | AppleScript `execute tab javascript` で `video.play()` を直接呼ぶ |
| **「視聴履歴を保存」ボタン** | Moodle の SCORM API の `LMSFinish()` を JS で直接呼び出す（ボタン操作と同等） |
| **URL 遷移** | AppleScript `set URL of active tab of front window to "..."` |
| **SCORM iframe** | ボタンが `<iframe>` 内にある場合は `frames[0].document` 経由でアクセス |

### 詳細設計メモ

#### 動画終了検知 (JS)

```javascript
// 動画要素を探して ended イベントを監視
(function() {
  const v = document.querySelector('video') 
    || frames[0]?.document.querySelector('video');
  if (!v) return 'no_video';
  if (v.ended) return 'ended';
  return 'playing';
})()
```

これを `_tick_macos()` と同じ AppleScript 経由で定期ポーリングする。

#### SCORM 視聴完了送信 (JS)

```javascript
// SCORM 2004 の場合
window.API_1484_11?.Terminate('');
// SCORM 1.2 の場合
window.API?.LMSFinish('');
// iframe 内の場合
frames[0]?.API_1484_11?.Terminate('') || frames[0]?.API?.LMSFinish('');
```

#### 実装の追加ポイント

- `--moodle-url` を複数指定可能にする（`argparse` の `nargs='+'` または `action='append'`）
- 各 URL の処理が終わるたびにファイルを確定保存（現在は Ctrl+C まで保存しない）
- 全完了時に macOS 通知（`osascript -e 'display notification ...'`）

### 期待する出力構造

```
out/
└── 20260501/
    ├── 細胞膜の構造と機能.txt
    ├── タンパク質合成のメカニズム.txt
    └── 遺伝子発現の調節.txt
```
