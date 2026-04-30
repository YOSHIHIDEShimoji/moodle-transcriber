# moodle-transcriber 使い方

Moodleの講義動画音声をオンデバイス（ローカルAI）で自動文字起こしするツール。
サーバーへの送信なし、Moodle側のログに影響なし。

---

## 初期セットアップ（1回だけ）

### 1. BlackHole インストール（macOS）

```bash
brew install --cask blackhole-2ch
brew install portaudio
```

インストール後 **macOS を再起動**。

### 2. Audio MIDI Setup で Multi-Output Device 作成

```bash
open -a "Audio MIDI Setup"
```

1. 左下の「＋」→「Create Multi-Output Device」（日本語: 「複数出力装置を作成」）
2. **MacBook Pro Speakers** と **BlackHole 2ch** の両方にチェック
3. 「Multi-Output Device」を右クリック → 「Use This Device For Sound Output」
4. システム設定 → サウンド → 出力 → **Multi-Output Device** を選択

> この設定は1回だけ。スクリプト起動後は **Audio MIDI Setup を閉じてもOK**（バックグラウンド動作）。
> 日本語 macOS では「複数出力装置」という名前でも自動認識する。

### 3. SwitchAudioSource インストール（自動切替に必要）

```bash
brew install switchaudio-osx
```

### 4. Python 環境セットアップ

```bash
cd ~/my-projects/moodle-transcriber
pyenv local moodle-transcriber-3.11.9
pip install -r requirements.txt
```

---

## 基本的な使い方

### Chrome で Moodle を開いてから起動

```bash
python main.py --moodle-url "your-univ.moodle.com"
```

- `--moodle-url` を指定すると `--keep-active chrome` が自動的に有効になる
- スクリプト起動直後に Moodle タブに JS を注入 → 別タブ・別アプリに移動しても動画が止まらない
- `your-univ.moodle.com` はMoodleのドメインの一部を指定（例: `moodle`, `lms.example.ac.jp`）
- 音声は Multi-Output Device に切り替わる（スピーカーから聞こえる + BlackHole で録音）
- Ctrl+C で終了 → スピーカー出力が元のデバイスに自動復元

### 音量について

| 状態 | スピーカー | BlackHole録音 |
|---|---|---|
| 音量 100% | 聞こえる | される |
| **音量 0（キーボード）** | **無音** | **される（継続）** |

音量 0 にしても BlackHole への信号は維持されるため、**無音で文字起こしを続けられる**。

---

## コマンド例

```bash
# 基本（--moodle-url だけで Chrome keep-alive が自動有効、large-v3 モデル）
python main.py --moodle-url "moodle.example.com"

# モデルを small に変更（初回テスト用・高速）
python main.py --moodle-url "moodle.example.com" --model small

# SRT 字幕形式で出力
python main.py --moodle-url "moodle.example.com" --format srt

# 出力ファイル名を直接指定（カレントディレクトリに保存）
python main.py --moodle-url "moodle.example.com" --output 解剖学_第3回
# → ./解剖学_第3回.txt に保存（out/YYYYMMDD/ は使わない）

# 自動音声切替を使わない（すでに手動でMulti-Output Deviceに設定済みの場合）
python main.py --moodle-url "moodle.example.com" --no-auto-routing

# 終了後にスピーカーに戻す（Multi-Output Deviceに戻らないよう指定）
python main.py --moodle-url "moodle.example.com" --restore-to "MacBook Proのスピーカー"

# Arc ブラウザを使う場合（デフォルトの chrome を上書き）
python main.py --moodle-url "moodle.example.com" --keep-active arc

# 利用可能なデバイス一覧を確認
python main.py --list-devices

# 音声出力をスピーカーに戻す（スクリプト中断後などに手動復元）
python main.py --reset-audio
python main.py --reset-audio --restore-to "MacBook Proのスピーカー"
```

---

## 複数 URL の順次処理

複数の講義 URL を渡すと、1本ずつ自動で処理してくれる。

### 使い方

```bash
# --urls に URL をスペース区切りで並べる
python main.py --urls \
  "https://moodle.example.com/mod/scorm/player.php?id=101" \
  "https://moodle.example.com/mod/scorm/player.php?id=102" \
  "https://moodle.example.com/mod/scorm/player.php?id=103"

# URL をファイルで渡す（1行1URL）
python main.py --url-file urls.txt

# 両方の組み合わせも可能
python main.py --urls "https://..." --url-file urls.txt
```

`urls.txt` の書き方:
```
https://moodle.example.com/mod/scorm/player.php?id=101
https://moodle.example.com/mod/scorm/player.php?id=102
https://moodle.example.com/mod/scorm/player.php?id=103
```

### 自動処理の流れ

1. URL1 を Chrome で開く（自動遷移）
2. 動画を自動再生
3. 文字起こし開始
4. 60秒ごとに「視聴状況を保存」ボタンを自動クリック
5. 視聴状況が 100% になったら次の URL へ
6. 全 URL 完了後に macOS 通知

### 注意事項

- **Chrome が前面にある状態で実行すること**（URL 遷移が Chrome のアクティブタブに対して行われる）
- 動画再生中はブラウザを別タブに移動しても問題ない（JS 注入で視聴状況は維持される）
- Ctrl+C で途中停止した場合、その時点までの文字起こしは保存される
- 同じタイトルの講義が複数ある場合、ファイル名に `_2`, `_3` ... が自動付与される（上書きしない）

```
out/20260501/
├── 講義動画１.txt       # 1本目
├── 講義動画１_2.txt     # 同名タイトルの2本目
└── 講義動画２.txt
```

---

## オプション一覧

| オプション | デフォルト | 説明 |
|---|---|---|
| `--keep-active BROWSER` | `--moodle-url` / `--urls` 指定時は `chrome` | Moodleタブを維持するブラウザ: `chrome` / `arc` / `safari` / `firefox` |
| `--moodle-url URL_PATTERN` | なし | MoodleタブのURLパターン（例: `moodle.example.com`）。指定すると `--keep-active chrome` が自動有効 |
| `--urls URL [URL ...]` | なし | 複数 URL を順次処理（フル URL を渡す） |
| `--url-file FILE` | なし | URL を1行1件で記載したファイル |
| `--save-interval 秒` | 60 | 「視聴状況を保存」ボタンのクリック間隔（0 で無効） |
| `--keep-interval 秒` | 20 | JS再注入の間隔（秒） |
| `-m MODEL` | `large-v3` | Whisperモデル: `tiny` / `small` / `medium` / `large-v3` |
| `-f FORMAT` | `txt` | 出力形式: `txt` / `srt` / `vtt` |
| `-o NAME` | 自動生成 | 出力ファイルのベース名。**指定時はカレントディレクトリに `NAME.txt` として保存**（`out/YYYYMMDD/` を使わない） |
| `-l LANG` | `ja` | 文字起こし言語（ISO 639-1） |
| `--no-auto-routing` | — | 音声出力の自動切替を無効化 |
| `--restore-to DEVICE` | 起動前のデバイス | 終了時に戻す出力デバイス名（例: `'MacBook Proのスピーカー'`） |
| `--reset-audio` | — | 音声出力をスピーカーに戻して終了 |
| `--list-devices` | — | 入力デバイス一覧を表示して終了 |
| `--timestamps` | — | 各行に `[HH:MM:SS]` タイムスタンプを付与（デフォルト: オフ） |
| `-v` | — | 詳細ログを表示 |

---

## 出力ファイル

### 保存先の決まり方

| 状況 | 保存先 |
|---|---|
| `-o NAME` 指定 | `./NAME.txt`（カレントディレクトリ直下） |
| `-o` 未指定 + `--moodle-url` あり | `./out/YYYYMMDD/講義動画タイトル.txt`（h1から自動生成） |
| `-o` 未指定 + `--moodle-url` なし | `./out/YYYYMMDD/lecture_HHMMSS.txt` |

### TXT 形式（デフォルト）

```
# 講義動画１　専門職連携実践IPWと教育IPE　意義と歴史的背景
# Date: 2026-04-30 14:30:00
# URL: https://moodle.example.com/mod/scorm/player.php?...
# Model: large-v3 (int8) | Language: ja
# ──────────────────────────────────────────────────────

[00:00:03] 本日の講義を始めます。今日のテーマは細胞膜の構造についてです。
[00:00:28] まず、リン脂質二重層の基本的な配置から見ていきましょう。
```

- ファイル先頭にページタイトル（h1タグ取得）・日時・URL・モデル情報を記録
- `--moodle-url` 未指定の場合、タイトル行とURL行は省略される

### SRT 形式（`--format srt`）

```
1
00:00:03,000 --> 00:00:08,500
本日の講義を始めます。

2
00:00:28,200 --> 00:00:35,100
まず、リン脂質二重層の基本的な配置から見ていきましょう。
```

---

## keep-alive の仕組み

`--moodle-url` を指定すると（`--keep-active` 省略時は `chrome` が自動選択）：

1. スクリプト起動直後にAppleScriptでChromeの全タブを検索
2. URLが `--moodle-url` のパターンに一致するタブを見つける
3. そのタブに JavaScript を注入：

```javascript
Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});
Object.defineProperty(document, 'hidden', {get: () => false});
document.dispatchEvent(new Event('visibilitychange'));
```

Moodle の動画プレーヤーは `visibilityState` を読んで一時停止するが、常に `"visible"` を返すため止まらなくなる。

**SCORMプレーヤーについて**: Moodleの動画は `<iframe>` 内で再生されることがある（SCORM形式）。タイトル取得時は `window.top.document` でメインフレームを参照するため、iframe内で実行されても正しく取得できる。

**サーバーへの影響**: この変更はブラウザのDOM内のみ。サーバーへのリクエストは一切発生しない。

---

## トラブルシューティング

### `BlackHole デバイスが見つかりません`
```bash
brew install --cask blackhole-2ch
# → macOS 再起動
```

### `Multi-Output Device が見つかりません`
Audio MIDI Setup で Multi-Output Device（または複数出力装置）が作成済みか確認。
システム設定 → サウンド → 出力 → Multi-Output Device が選択されているか確認。

### 終了後に音量キーが効かない（複数出力装置のまま）
```bash
# 手動で復元
python main.py --reset-audio --restore-to "MacBook Proのスピーカー"
```
または次回起動時に `--restore-to "MacBook Proのスピーカー"` を付けると終了時に自動復元する。

### `ヒント: brew install switchaudio-osx`
```bash
brew install switchaudio-osx
```
これがないと自動音声切替が無効になるが、手動でMulti-Output Deviceに切り替えれば動作する。

### Moodle の動画が止まる
1. `--moodle-url` を指定しているか確認（指定すると Chrome keep-alive が自動有効）
2. `--moodle-url` にMoodleのドメインが正しく含まれているか確認
3. Chrome の場合、AppleScript 権限が必要: システム設定 → プライバシーとセキュリティ → オートメーション → ターミナル → Google Chrome にチェック
4. Chrome の場合、JS 実行権限が必要: Chrome → 表示 → 開発/管理 → Apple Events からの JavaScript を許可

### ファイル名が `lecture_HHMMSS.txt` になる
`--moodle-url` を指定していないか、指定したパターンがタブURLに一致していない。
`--moodle-url "moodle.gs.chiba-u.jp"` のようにドメインの一部を指定する。

### Windows での使い方
```bash
pip install pyaudiowpatch
python main.py  # WASAPI Loopback を自動検出
```
> Windows では `--keep-active` は動作しません（macOS 専用機能）。

---

## モデルサイズの選び方

| モデル | 精度 | 30秒の処理時間 (M4) | 用途 |
|---|---|---|---|
| `tiny` | 低 | ~0.5秒 | テスト用 |
| `small` | 中 | ~2秒 | 動作確認 |
| `medium` | 高 | ~4秒 | 一般講義 |
| `large-v3` | 最高 | ~6秒 | **専門用語が多い講義（推奨）** |

初回は `--model small` でテスト → 問題なければ `large-v3`（初回DL約1.5GB）。
