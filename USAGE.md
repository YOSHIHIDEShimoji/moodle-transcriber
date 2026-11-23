# moodle-transcriber 使い方

Moodleの講義動画音声をオンデバイス（ローカルAI）で自動文字起こしするツール。
動画はダウンロードしない。ブラウザで普通に再生し、その音声をローカルの Whisper で文字にする。
音声や文字起こし結果を外部サーバーへ送ることはない。

**Moodle 側の記録について**: 普通に視聴するのと同じく、Moodle には閲覧と視聴状況が記録される。
これは意図した動作で、視聴が評価に入る科目でも視聴済みになる（「視聴状況を保存」「活動から抜ける」も自動で押す）。
動画ファイルを直接ダウンロードする方式（yt-dlp 等）は採らない。視聴状況が付かないうえ、
多くの大学の利用規約で禁止されているため。

## 2 つのエンジン

| | `--engine page`（既定・2026-10-24〜） | `--engine blackhole`（従来） |
|---|---|---|
| ブラウザ | **専用の Chrome**（別プロファイル `~/.moodle-transcriber/chrome-profile`、別プロセス）を Playwright で制御 | あなたの Chrome を AppleScript で操作 |
| あなたの Chrome | 触らない（前面に出ない・タブ遷移なし・クリックなし） | 前面に出る・タブが遷移する・クリックされる |
| 再生 | `--autoplay-policy=no-user-gesture-required` で `video.play()` | OS レベルのマウスクリックで autoplay policy を通す |
| 音声 | **ページ内**で video を Web Audio に繋いで取り出す。スピーカーには出さない | BlackHole + 複数出力装置で全システム音声を録る |
| 他アプリの音 | 混ざらない（Spotify / YouTube を普通に使える） | 全部混ざる。実行中は止める必要あり |
| 必要なもの | Google Chrome、`pip install -r requirements.txt`（playwright） | BlackHole、SwitchAudioSource、複数出力装置、Chrome の Apple Events 許可 |
| ログイン | 専用ブラウザで初回だけ手でログイン（セッションはプロファイルに残る） | あなたの Chrome のログイン済みタブを使う |

page エンジンの使い方:

```bash
python main.py --urls "https://moodle.gs.chiba-u.jp/moodle/mod/scorm/player.php?a=188066&currentorg&scoid=376188"
```

専用 Chrome は既定で**非表示（ヘッドレス）**で動く。Moodle のセッションが無い／切れたときだけ
ウィンドウ付きで起き直って前面に出るので、そこでログインする（`--login-timeout` 秒、既定 10 分待つ）。
ログイン後は自動でウィンドウを閉じて非表示に戻る。セッションはプロファイルに残るので、以後は無人で進む。
常にウィンドウを見たいなら `--show-browser`、常に非表示（ログインが必要なら失敗扱い）なら `--headless`。

ページ内キャプチャなので講義の音はスピーカーから鳴らない。音を聞きたい場合は従来エンジンを使う。

`--speed 2` で 2 倍速再生にできる（所要時間が半分）。音質は落ちない: ピッチ補正を切った早回しは波形を
時間方向に縮めただけなので、32kHz で取ったサンプルを 16kHz として扱えば元の音声に戻る。
Moodle 側の視聴状況は再生位置で計算されるので 2 倍でも 100% になる（実機で確認してから既定を上げる）。

---

## 初期セットアップ（1回だけ）

page エンジン（既定）だけ使うなら、必要なのは 4（Python 環境）と 5（whisper.cpp）だけ。
1〜3 は従来の blackhole エンジン用。

### 1. BlackHole インストール（macOS・blackhole エンジンのみ）

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

### 5. whisper.cpp（macOS の文字起こしバックエンド）

macOS では [whisper.cpp](https://github.com/ggml-org/whisper.cpp)（Metal + CoreML）を使う。
デフォルトの場所は `~/my-projects/whisper.cpp`。別の場所にある場合は環境変数で指定する:

```bash
export WHISPER_CPP_DIR=/path/to/whisper.cpp
```

必要なもの:
- `build/bin/whisper-server`（推奨。起動時に常駐させ、セグメントごとのモデル再ロードを回避）
- `build/bin/whisper-cli`（whisper-server が無い場合のフォールバック）
- `models/ggml-<model>.bin`（例: `ggml-large-v3-turbo.bin`）

---

## 動作メモ（安定性まわり）

- **終了判定**: `video.ended` が取れない SCORM 教材でも、
  「currentTime 停滞 / 視聴状況100% / 無音の継続 / 総尺超過」のフォールバックで自動停止する。
  終了理由は `視聴完了 (理由)` の形で表示される。
- **無音スキップ**: 無音セグメントは Whisper に渡さない
  （無音入力による「ご視聴ありがとうございました」等の幻覚を防止）。
  録音開始直後に音が来ない場合は再生を1回自動リトライし、それでも無音なら中止する。
- **タブ追跡**: 処理対象タブは URL パターンではなくタブ id で追跡するため、
  実行中に別タブ・別ウィンドウで作業しても誤爆しない
  （ただし対象タブ自体を閉じたり URL を変えたりしないこと）。
- **再生トリガー**: Chrome の autoplay policy を通すため、動画中心を CoreGraphics の
  マウスイベントで実クリックしてから `play()` を呼び、`paused` が false になったことを
  実測してから「再生開始」とみなす（macOS 26 では System Events の `click at` が
  座標を無視するため使わない）。再生が始まらなければ約 1 分ごとに最大 2 回再試行し、
  約 4 分で中止する。未再生のまま終わった場合は「視聴状況を保存」「活動から抜ける」を押さない。
- **逆相ステレオ対策**: 動画によっては左右チャンネルが逆相で、単純な L+R 平均だと
  音声が打ち消されて幻覚だけが出力される。録音時に位相を見て自動で差 (L−R) に切り替える。
- **終了の確定**: SCORM プレーヤーは終了時に `ended` を立てずに 0 秒へ巻き戻すことがある。
  末尾付近まで再生した後に 0 秒へ戻ったら、その時点で終了とみなす。
- **録音は全システム音声（blackhole エンジンのみ）**: Multi-Output Device 経由なので、実行中に鳴った他アプリの音
  （音楽・通知音）も文字起こしに混入する。実行中は他アプリの音を止めること。
  page エンジンはページ内で video の音声だけを取るので、この制約は無い。

---

## Claude スキルで回す（推奨）

Claude Code に Moodle の URL（複数可）を貼って「文字起こしして」と言うだけで、
`.claude/skills/moodle-transcribe/SKILL.md` の手順で事前確認 → 起動 → 監視 → 検品 →
失敗分の再実行 → 講義まとめ（課題があれば課題ファイル）→ `~/Downloads` へコピーまで進む。
Python 本体は変わらない。Claude は「起動・監視・検品・要約」だけを担当する。

Python 側はそのために次を出す:

- `out/YYYYMMDD/run-summary.json`: URL ごとの結果（タイトル・保存先・終了理由・再生したか・
  自己診断 `qa`）。URL を 1 本処理するごとに書き直されるので、中断してもそこまでは残る。
- 自己診断 `qa.status`: `ok` / `warn`（字/分が少ない・同一文の連発・中止理由あり）/
  `failed`（未再生・本文なし・定型句だけ）。終了コードは failed か未処理 URL があれば 2。
- `--json-summary PATH` でサマリーの出力先を変えられる。
- `python tab_text.py --pattern course/view.php` で Chrome の該当タブ本文を読む（読み取り専用。
  コースページの課題確認に使う）。
- 起動時に Spotify / Music が再生中なら警告する（blackhole エンジンのみ関係。録音が全システム音声のため）。
- `python fix_transcript.py --glossary glossary.json out/YYYYMMDD/*.txt` で文字起こしを校正する。
  定型句だけの幻覚行と境界の重複行を消し、辞書（`{"誤": "正"}`）で固有名詞の誤変換を直す。
  原本は `raw/` に退避し、変更は `校正ログ.md` に残す。`--dry-run` で変更内容だけ確認できる。

---

## 基本的な使い方

```bash
python main.py --urls "https://moodle.gs.chiba-u.jp/moodle/mod/scorm/player.php?a=188066&currentorg&scoid=376188"
```

- URL は SCORM プレーヤーの URL（`mod/scorm/player.php?a=...`）を渡す。複数可（次節）。
- 専用 Chrome（別プロファイル）が非表示で起動し、動画を再生しながら文字起こしする。あなたの Chrome には触らない。
- 初回やセッション切れのときだけ専用 Chrome のウィンドウが出るので、そこで Moodle にログインする。
- 講義の音はスピーカーから鳴らない（ページ内で音声を取っているため）。Spotify や YouTube を聞いていても混ざらない。
- `--speed 2` で 2 倍速（所要時間が半分）。
- Ctrl+C で途中停止すると、その時点までの文字起こしは保存される。

従来方式（`--engine blackhole`。あなたの Chrome を操作し、BlackHole でシステム音声を録る）の使い方は
末尾の「従来方式（blackhole エンジン）」を参照。

---

## コマンド例

```bash
# 基本（--moodle-url だけで Chrome keep-alive が自動有効、large-v3 モデル）
python main.py --moodle-url "moodle.example.com"

# モデルを small に変更（初回テスト用・高速）
python main.py --moodle-url "moodle.example.com" --model small

# SRT 字幕形式で出力
python main.py --moodle-url "moodle.example.com" --format srt

# 出力ディレクトリを指定（ページタイトルでファイル名が自動生成される）
python main.py --moodle-url "moodle.example.com" --output 解剖学
# → ./解剖学/講義動画１.txt に保存（out/YYYYMMDD/ の代わりに使われる）

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

1. 専用 Chrome で URL1 を開く（ログイン画面ならウィンドウを出してログインを待つ）
2. 動画を自動再生し、実際に再生が始まったことを確認する
3. 文字起こし開始
4. 60秒ごとに「視聴状況を保存」ボタンを自動クリック
5. 動画の終了を検出したら「視聴状況を保存」→「活動から抜ける」を自動クリック
   （再生できなかった動画では押さない）
6. ファイルを保存し、自己診断（ok / warn / failed）とともに完了サマリーを表示
7. 5秒待機 → 次の URL へ
8. 全 URL 完了後に macOS 通知。結果は `out/YYYYMMDD/run-summary.json` にも残る

### 注意事項

- 実行中もあなたの Chrome や他のアプリは普通に使ってよい
- 専用 Chrome のウィンドウが出ている間（ログイン時）は、閉じたり最小化したりしない
- Ctrl+C で途中停止した場合、その時点までの文字起こしは保存される
```
out/20260501/
├── 講義動画１.txt       # 1本目
├── 講義動画１_2.txt     # 同名タイトルの2本目（自動連番）
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
| `-o DIR` | 自動生成 | 出力ディレクトリ。**指定時は `DIR/ページタイトル.txt` として保存**（`out/YYYYMMDD/` の代わりに使われる） |
| `-l LANG` | `ja` | 文字起こし言語（ISO 639-1） |
| `--no-auto-routing` | — | 音声出力の自動切替を無効化 |
| `--restore-to DEVICE` | 起動前のデバイス | 終了時に戻す出力デバイス名（例: `'MacBook Proのスピーカー'`） |
| `--reset-audio` | — | 音声出力をスピーカーに戻して終了 |
| `--list-devices` | — | 入力デバイス一覧を表示して終了 |
| `--timestamps` | — | 各行に `[HH:MM:SS]` タイムスタンプを付与（デフォルト: オフ） |
| `-v` | — | 詳細ログを表示 |
| `--json-summary PATH` | `出力ディレクトリ/run-summary.json` | 複数 URL モードの実行サマリー(JSON)の出力先 |
| `--engine {page,blackhole}` | `page` | page: 専用 Chrome + ページ内キャプチャ / blackhole: 従来方式 |
| `--profile-dir DIR` | `~/.moodle-transcriber/chrome-profile` | page エンジンの Chrome プロファイル |
| `--headless` | — | page エンジンのブラウザを常に非表示（ログインが必要なら失敗扱い。無人 cron 向け） |
| `--show-browser` | — | page エンジンのブラウザを常にウィンドウ付きで起動（既定は非表示で、ログイン時だけウィンドウ） |
| `--login-timeout 秒` | 600 | page エンジンでログイン画面に落ちたとき本人のログインを待つ秒数 |
| `--player-marker 文字列` | `player.php` | page エンジンで「プレーヤーに到達した」とみなす URL の部分文字列 |
| `--speed 倍率` | `1.0` | page エンジンの再生速度（0.5〜3.0）。`preservesPitch` を切った早回しを 16000×倍率 Hz で取り、元の速度・ピッチに戻して文字起こしする。Chrome は 4 倍超で音声をミュートするため上限 3。2 倍で所要時間が半分になる |

---

## 出力ファイル

### 保存先の決まり方

| 状況 | 保存先 |
|---|---|
| `-o DIR` 指定 + `--moodle-url` あり | `./DIR/講義動画タイトル.txt` |
| `-o DIR` 指定 + `--moodle-url` なし | `./DIR/lecture_HHMMSS.txt` |
| `-o` 未指定 + `--moodle-url` あり | `./out/YYYYMMDD/講義動画タイトル.txt` |
| `-o` 未指定 + `--moodle-url` なし | `./out/YYYYMMDD/lecture_HHMMSS.txt` |

同名ファイルが既に存在する場合は `_2`, `_3` ... が自動付与される（上書きしない）。

終了時（Ctrl+C または視聴率100%の自動終了）にファイル名変更プロンプトが表示される。60秒で無入力なら自動確定。

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

### 完了時の表示

視聴率100%到達または Ctrl+C 後、以下のサマリーがターミナルに表示される。

```
  ✔  講義動画１  done!
  URL    https://moodle.example.com/mod/scorm/player.php?id=101
  保存   out/20260501/講義動画１.txt
  [2/3] 次の URL へ...     ← マルチURLのみ
```

---

## keep-alive の仕組み（blackhole エンジンのみ）

page エンジンでは専用 Chrome の全フレームに同等の処理を起動時に組み込むので、この節は従来方式の説明。

`--engine blackhole --moodle-url` を指定すると（`--keep-active` 省略時は `chrome` が自動選択）：

1. スクリプト起動直後にAppleScriptでChromeの全タブを検索
2. URLが `--moodle-url` のパターンに一致するタブを見つけ、以後はタブ id で追跡する
3. そのタブに JavaScript を注入：

```javascript
Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});
Object.defineProperty(document, 'hidden', {get: () => false});
document.dispatchEvent(new Event('visibilitychange'));
```

Moodle の動画プレーヤーは `visibilityState` を読んで一時停止するが、常に `"visible"` を返すため止まらなくなる。

**SCORMプレーヤーについて**: Moodleの動画は `<iframe>` 内で再生されることがある（SCORM形式）。タイトル取得時は `window.top.document` でメインフレームを参照するため、iframe内で実行されても正しく取得できる。

**サーバーへの影響**: visibilityState の上書き自体はブラウザ内の処理で、追加のリクエストは発生しない。
動画の再生と視聴状況の保存は通常の視聴と同じく Moodle に記録される。

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

未検証。page エンジンは Playwright と Chrome で動くので仕組み上は動く可能性があるが、
文字起こしは faster-whisper 経路になり、実機で確かめていない。
従来方式の WASAPI Loopback 経路（`pip install pyaudiowpatch`）も同様に未検証。

---

## モデルサイズの選び方

| モデル | 精度 | 用途 |
|---|---|---|
| `tiny` | 低 | 動作確認のみ |
| `small` | 中 | 急ぐとき |
| `medium` | 高 | 一般講義 |
| `large-v3` | 最高 | 遅い |
| `large-v3-turbo` | 最高に近い | **既定・推奨**。2 倍速でも処理が追いつく |

モデルファイルは `whisper.cpp/models/ggml-<model>.bin`（large 系は約 1.5GB）。

---

## 従来方式（blackhole エンジン）

`--engine blackhole` で、あなたの Chrome を AppleScript で操作し、BlackHole でシステム音声を録る従来方式に戻せる。
講義の音をスピーカーで聞きながら回したいときに使う。初期セットアップの 1〜3 が必要。

```bash
python main.py --engine blackhole --moodle-url "moodle.gs.chiba-u.jp"
```

- Chrome で Moodle の動画ページを開いてから起動する。Chrome が前面に出て、タブの操作とクリックが行われる。
- 音声出力は複数出力装置に切り替わり、終了時に元へ戻る。
- **全システム音声を録るので、実行中は Spotify などの音を止める**（歌詞が文字起こしに混ざる）。
- 音量を 0 にしても BlackHole への信号は維持されるので、無音で文字起こしを続けられる。
- 本人の Chrome に「表示 → 開発/管理 → Apple Events からの JavaScript を許可」が必要。

GUI 版（`main_gui.py`）は削除済み（コミット 496f627）。
