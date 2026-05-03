# moodle-transcriber ロードマップ

将来実装したい機能案。

各案はGitHub Issueでも追跡: [area:moodle-transcriber](https://github.com/YOSHIHIDEShimoji/my-projects/issues?q=is%3Aissue+label%3Aarea%3Amoodle-transcriber)

---

## 案11: リネームプロンプトのカウントダウン表示（優先度: 低・GUI アプリ化時に実装）(#10)

現在の `_prompt_rename()` はカウントダウン（60, 59, 58...）を ANSI エスケープで入力行を上書きしていたため、
ファイル名を入力できない問題があった。CLI では削除済み（SIGALRM でタイムアウトのみ）。
GUI の TextField では残り秒数をラベル表示できるため、GUI アプリ化時に実装する。

---

## 案10: Chrome プロファイル自動検出（優先度: 低）(#7)

- `~/Library/Application Support/Google Chrome/Local State` の JSON を読み、
  `profile.info_cache.<profile_dir>.gaia_name` / `user_name`（メールアドレス相当）を取得
- CLI: `--chrome-profile` に `"Profile 1"` の代わりにメールアドレスや名前で指定可能
- `--list-chrome-profiles` で一覧を表示（例: `Profile 1 → taro@example.com`）

---

## 案9: GUI アプリ — 必須オプション最小化 + 設定プロファイル（優先度: 低・急がない）(#9)

現在のCLIオプションは多く、初回利用のハードルが高い。

### 方針

- GUI 起動時に入力するのは Moodle URL（またはURLファイル）のみ
- モデル・出力形式・デバイス設定などは「詳細設定」タブまたは設定ファイル（TOML/JSON）に分離
- URLリストをアプリ内で追加・削除・並び替えできるリストビュー
- 設定プロファイル（大学用・個人用など）の切り替え

---

## 案8: ファイル名入力欄のプリフィル（優先度: 低・GUI アプリ化時に実装）(#8)

現在の案6（ファイル名タイムアウト入力）では、入力欄は空欄から始まる。
GUI アプリ化（案1）の際に、テキストフィールドにページタイトルから生成したファイル名を
初期値として入力済みにしておく。

### CUI での試み・断念理由

ターミナルの `input()` + `readline.set_startup_hook` でプリフィルは実現できるが、
Delete キーを押すと readline の内部バッファに積まれた文字列が一括削除されてしまい、
ユーザー体験が悪い。GUI の TextField では自然に実現できるため、CLI では不採用とした。

---

## 案7: whisper.cpp への移行（優先度: 中・Apple Silicon なら効果大）(#3)

現在は `faster-whisper`（CTranslate2 / CPU int8）を使用。
Apple Silicon (M1〜M4) では **whisper.cpp + Metal** に切り替えると GPU が使われ、
large-v3 モデルで **3〜5倍高速** になる可能性がある。

### 実装方針

- `feature/whisper-cpp` ブランチを切って `transcriber.py` だけを差し替える
- 他のファイル（`main.py`, `capture.py`, `output.py` など）は変更不要
- Python バインディングは [`pywhispercpp`](https://github.com/abdeladim-s/pywhispercpp) が有力候補

### インターフェース互換性

現在の `Transcriber` は `transcribe(audio: np.ndarray, time_offset: float)` を返す。
`pywhispercpp` でも同じシグネチャに合わせて `TranscriptSegment` を生成すれば他に影響なし。

### 速度比較（参考、M4 Pro / large-v3）

| 実装 | 30秒セグメント処理時間 |
|---|---|
| faster-whisper (CPU int8) | ~6秒 |
| whisper.cpp (Metal GPU) | ~1〜2秒（推定） |

### 手順メモ

```bash
git checkout -b feature/whisper-cpp
pip install pywhispercpp
# transcriber.py を pywhispercpp ベースに書き換え
# requirements.txt: faster-whisper → pywhispercpp
# python test_tool.py で [5] Whisper テストを通す
```

---

## 案3: 途中再開 `--resume`（優先度: 低・急がない）(#5)

途中で中断した文字起こしを、前回の続きから再開する機能。

### やりたいこと

```bash
# 前回 Ctrl+C で中断したファイルに続きを追記
python main.py --moodle-url "moodle.example.com" --resume out/20260501/講義動画１.txt
```

### 実装メモ

1. `--resume FILE` で既存の出力ファイルを指定
2. ファイルの最後の `[HH:MM:SS]` タイムスタンプを読み取る
3. その時刻以降の音声を録音・文字起こしして **追記** (`open("a")`)
4. TXT ヘッダーの重複書き込みをスキップ

#### タイムスタンプの読み取り (Python)

```python
import re
from pathlib import Path

def read_last_timestamp(path: Path) -> float:
    text = path.read_text(encoding="utf-8")
    matches = re.findall(r'\[(\d{2}):(\d{2}):(\d{2})\]', text)
    if not matches:
        return 0.0
    h, m, s = matches[-1]
    return int(h) * 3600 + int(m) * 60 + int(s)
```

5. `OutputWriter` に `append_mode=True` を追加して `open("a")` に切り替える
6. `Transcriber` の `time_offset` に読み取った秒数を渡す

### 注意点

- SRT / VTT 形式では連番・タイムコードがずれるため TXT 形式のみ対応でよい
- 録音を再開した瞬間から文字起こしが始まるため、前回終了から今回開始まで
  の無音区間は飛ぶ（問題なし）

---

## 案6: ファイル名入力タイムアウト ✅ 実装済み（シングルURLのみ）

終了後のファイル名変更プロンプトで、60秒間入力がなければ自動確定する。

### 実装済みの挙動

```
現在のファイル名: out/20260501/講義動画１.txt
自動確定まで: 60秒...
変更する場合は新しい名前を入力 (Enter でそのまま): 
```

- `signal.SIGALRM`（Unix）で60秒タイムアウト
- カウントダウンはプロンプト上の行を `\033[F` で毎秒上書き
- Windows はタイムアウトなしのフォールバック
- マルチURLモードはリネームプロンプトなし（自動保存）

---

## 案5: 無音スキップ（優先度: 低）(#6)

録音した30秒セグメントの RMS（音量）が閾値以下の場合、Whisper に送らずスキップする機能。

### メリット

- 無音区間（スライド切替、間）をスキップして処理速度向上
- Whisperの**幻覚**（無音に対して「ありがとうございました」などを生成する現象）を防ぐ

### 実装メモ

```python
import numpy as np

def is_silent(audio: np.ndarray, threshold: float = 0.002) -> bool:
    return float(np.sqrt(np.mean(audio ** 2))) < threshold
```

- `capture.py` のセグメント生成後、または `main.py` のループ内で呼ぶ
- `--silence-threshold 0.002` フラグで閾値を調整可能にする
- 閾値のデフォルト値は実測で調整が必要（小声の講師で 0.001〜0.005 程度）

---

## 案4: 視聴進捗バー（優先度: 低）✅ 実装済み

`get_viewing_percentage()` で取得した視聴状況%をターミナルにリアルタイム表示。

### 表示イメージ（実装済み）

```
  視聴 ██████████░░░░░░░░░░  50%  [00:30:00 / 01:00:20]
```

- 視聴状況%: `get_viewing_percentage()` を30秒ごとに取得
- 現在位置・総尺: `get_video_time()` で `video.currentTime | video.duration` をJS取得
- シングルURL・マルチURL両モードで表示

---

## 案1: GUI アプリ（優先度: 低・急がない）(#4)

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

## 案2: 複数 URL の順次自動処理 ✅ 実装済み

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
| **視聴状況の定期保存** | 1分ごとに「視聴状況を保存」ボタンを JS でクリック |
| **視聴完了の検知** | ページ内 `視聴状況: XX%` テキストを JS でパースして 100% を検出 |
| **再生開始** | URL 遷移後に `video.play()` を JS で呼ぶ |
| **URL 遷移** | AppleScript `set URL of active tab of front window to "..."` |
| **SCORM iframe** | ボタン・動画が `<iframe>` 内にある場合は `frames[i].document` 経由でアクセス |

### 実際の SCORM プレーヤー挙動（scorm-hlsPlayer2.js から判明）

「視聴状況を保存」ボタンを押したときのコンソール:
```
savedscore: 2.607132267241141
SetValue Score: curr=94.396438;maxp=94.396438;duration=3620.7;score=2.607...
save!!true
```

再生ボタンを押したときのコンソール:
```
sessionTime:begin 00:00:22 1777567430 22
play!!1777567430
SetValue Score: curr=115.53;maxp=115.53;duration=3620.7;score=3.19...
save!!true
```

- **`save!!true`** が出れば保存成功
- `duration=3620.7`（秒）が動画の総尺。`curr` が現在の視聴済み秒数
- 視聴状況 % = `curr / duration * 100`（ページ表示の `視聴状況: 3%` と対応）
- **「視聴状況を保存」ボタンを1分に1回クリックすれば視聴状況が更新される**
- 100% に達したら視聴完了

### 詳細設計メモ

#### 「視聴状況を保存」ボタンのクリック (JS)

ボタンのセレクターは DevTools Console で確認する:
```javascript
// テキストでボタンを探す
[...document.querySelectorAll('button, input[type=button], input[type=submit]')]
  .find(el => (el.textContent || el.value || '').includes('視聴状況を保存'))

// iframe 内にある場合
for (let i = 0; i < frames.length; i++) {
  try {
    const btn = [...frames[i].document.querySelectorAll('button, input')]
      .find(el => (el.textContent || el.value || '').includes('視聴状況'));
    if (btn) console.log('frame', i, btn);
  } catch(e) {}
}
```

確認済みセレクター: **`#save > input[type=button]`**

1分ごとの自動クリック JS:
```javascript
(function(){
  const btn = document.querySelector('#save > input[type=button]');
  if (btn) { btn.click(); return 'clicked'; }
  return 'not_found';
})()
```

これを `_tick_macos()` と同じ AppleScript 経由で60秒ごとに実行する。

#### 視聴完了の検知 (JS)

```javascript
// ページ内の「視聴状況: XX%」テキストをパース
(function(){
  const match = document.body.innerText.match(/視聴状況[：:]\s*(\d+)%/);
  return match ? parseInt(match[1]) : -1;
})()
```

戻り値が `100` になったら次の URL へ進む。

#### 再生開始 (JS)

```javascript
// URL 遷移後、動画の再生を開始する
(function(){
  const v = document.querySelector('video')
    || [...frames].map(f => { try { return f.document.querySelector('video'); } catch(e){} }).find(Boolean);
  if (v) { v.play(); return 'play_triggered'; }
  return 'no_video';
})()
```

#### 実装の追加ポイント

- `--moodle-url` を複数指定可能にする（`argparse` の `action='append'`）
- 各 URL の処理が終わるたびにファイルを確定保存（現在は Ctrl+C まで保存しない）
- 全完了時に macOS 通知（`osascript -e 'display notification ...'`）
- URL 遷移後はページ読み込み完了を待ってから JS を実行（数秒の `sleep` が必要）

### 期待する出力構造

```
out/
└── 20260501/
    ├── 細胞膜の構造と機能.txt
    ├── タンパク質合成のメカニズム.txt
    └── 遺伝子発現の調節.txt
```
