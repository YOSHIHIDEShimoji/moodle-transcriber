# 障害一覧 — moodle-transcriber 安定性調査 (2026-07-05)

コード全読 + `logs/moodle-transcriber.log` + `out/` の実出力の突き合わせによる調査結果。
コードは未修正（read-only 調査）。

## TL;DR — なぜ「安定しない」のか

このツールの制御系（再生開始・進捗取得・終了検出・keep-alive）は**すべて AppleScript の
`execute javascript` によるトップフレームへの JS 注入**に依存している。SCORM プレーヤーの
iframe 構成が変わる（深いネスト・クロスオリジン・OOPIF 化）と全シグナルが同時に沈黙し、
しかもコードは「取得失敗」を「まだ終わっていない (`False`) / 進捗なし (`-1`)」に潰すため、
**失敗しても止まらず走り続ける**。これが不安定さの根本構造。

ログ上の決定的事実: **2026-05-01 を最後に `ended=True` は一度も記録されていない**。
6/9・6/26・7/5 の実行は全セグメント `pct=-1 ended=False` のまま（例: 7/5 は 57 セグメント
＝約 25 分間、終了を検出できずに走り続けている）。

---

## 修正状況 (2026-07-05 修正完了)

全 15 件を修正した。test_tool.py は 108/108 パス（回帰テスト追加済み）。

### 修正中に判明した追加の根本原因

`_GET_PCT_JS` に含まれていた正規表現 `\d` は **AppleScript の文字列リテラルとして
構文エラー**になる（`osacompile -e 'return "(\d+)"'` → compilation error で実証）。
つまり 6273bce（5/15 の「多段 iframe 対応」）で `\d` 入りの正規表現が入って以降、
`get_viewing_percentage` は **JS が実行される前に AppleScript ごと失敗**して常に -1 を
返していた。5/15 以降ログが `pct=-1` 一色だった直接原因はこれ。正規表現を `[0-9]` に
変更して修正した（埋め込み JS 全 8 種を osacompile + node で構文検証するテストも実施）。

### 各障害の修正内容

| # | 障害 | 修正 |
|---|------|------|
| 1 | 終了検出できず止まらない | `get_video_ended` を三値化（True/False/None=判定不能）。`PlaybackMonitor` を新設し、video.ended に加え「currentTime 停滞 / 総尺付近到達 / 視聴状況100% / 無音継続 / 総尺超過 watchdog」のフォールバックで必ず停止する。`\d` バグ修正で pct 検出自体も復活 |
| 2 | 終了後に最初から再生 | keep-alive のガードを修正（ended 観測を記憶する `_ended` フラグ + `mark_ended()`）。`_VISIBILITY_JS` を書き換え: 探索深度を 4 に統一、`window.__mtEnded` フラグで ended 観測後は play/dispatch を恒久停止、`currentTime>0.5` の動画のみ再開（0 秒からの再ループ禁止）、visibilitychange の dispatch は video のある document のみに限定 |
| 3 | バッチ2本目の冒頭欠落 | `Transcriber.reset()` を追加し `_process_one_url` 冒頭で呼ぶ（回帰テストで旧バグを再現・修正を確認） |
| 4 | アクティブタブ誤爆 | navigate 直後にタブ id を取得し、以後の全操作（JS注入・タイトル取得・keep-alive）をタブ id で追跡。固定 6 秒 sleep を `wait_for_page_ready()` のポーリングに置換 |
| 5 | TimeoutExpired で全体クラッシュ | osascript 呼び出しを `_osascript()` ラッパーに集約し、TimeoutExpired / OSError を捕捉してログ + 失敗値を返す |
| 6 | 無音で幻覚を量産 | RMS ベースの無音ゲート（`is_silent`、閾値 5e-4）を追加。無音セグメントは Whisper に渡さない。無音が続く場合は再生を1回自動リトライ → それでも無音なら停止 |
| 7 | 30秒ごとにモデル再ロード | whisper-server を常駐化（起動 0.5s、1セグメント 0.1〜0.2s に短縮を実測）。server が無ければ whisper-cli にフォールバック |
| 8 | stop() 二重呼び出しで末尾重複 | `stop()` を冪等化、`_flush_remaining()` がバッファをクリアするよう修正 |
| 9 | 例外時のストリームリーク | `_process_one_url` / シングルモードの finally に `capture.stop()` を追加（冪等化前提） |
| 10 | keep-alive を join しない競合 | `stop()` が join(15s) するよう変更。tick 冒頭・osascript 前に stop チェック。終了処理は「keep_alive.stop() → 保存 → 離脱」の順に変更 |
| 11 | 境界の重複テキスト | 出力済み区間に食い込むセグメントのみ、直近テキストと正規化比較して除去（連続セグメントの正当な繰り返しは保持） |
| 12 | Ctrl+C 後のリネームプロンプト | stop_event / shutdown_requested 時はスキップ |
| 13 | whisper.cpp パスのハードコード | `WHISPER_CPP_DIR` を USAGE.md に明記、エラーメッセージにも記載 |
| 14 | ヘッダの (int8) 虚偽表記 | `Transcriber.backend_desc` を出力ヘッダに記載（例: `Backend: whisper.cpp server (Metal/CoreML)`） |
| 15 | Windows/Linux 経路 | WindowsAudioCapture の start_time を実サンプル数ベースに修正、stop 冪等化、xdotool 呼び出しのタイムアウト捕捉。経路自体は温存 |

**未実施（将来課題）**: CDP（Chrome DevTools Protocol）への移行。現行の AppleScript 注入でも
上記フォールバックで「止まらない・巻き戻る・突然死ぬ」は解消するが、クロスオリジン/OOPIF な
SCORM 教材で pct/ended を直接取るには CDP が必要（その場合も無音判定で数分以内に停止する）。

---

## Critical

### 1. 動画終了を検出できず、文字起こしが永遠に止まらない（ユーザー認識済み障害の本体）

- **症状**: 動画が終わってもループが break せず、いつ終わったのか分からない。
- **証拠**:
  - `logs/moodle-transcriber.log`: 5/1 以降 `INFO video ended` が皆無。6/26・7/5 の実行は
    最初から最後まで `pct=-1 ended=False`。
  - `out/20260501/講義動画2_医師の役割機能と教育.txt`: 本編終了 `[00:07:03]` の後に
    `[00:13:14]` の断片 — 終了後も 6 分以上録音が継続していた。
- **原因**:
  1. `_VIDEO_ENDED_JS` / `_GET_PCT_JS`（[platform_utils.py:69](../platform_utils.py#L69),
     [platform_utils.py:130](../platform_utils.py#L130)）が `<video>` に到達できていない。
     AppleScript の `execute javascript` はトップフレームで実行され、`w.frames[i].document`
     での iframe 潜りは**同一オリジンかつ同一プロセスのフレームのみ**有効。SCORM コンテンツが
     クロスオリジン／OOPIF（out-of-process iframe）だと深さ 4 の再帰でも届かない。
     5/1 の講義（IPE 系）では動いたが 5/15 以降の講義（工学系）で全滅していることから、
     コース側の SCORM 教材の構造差で壊れる。
  2. `get_video_ended()`（[platform_utils.py:400](../platform_utils.py#L400)）が
     `'unknown'`（video 不到達）も osascript エラー（空文字）もすべて `False` に潰す。
     「終わっていない」と「見えていない」を区別できないため、検出不能時のフォールバックが
     一切効かない。
- **修正すべき箇所と方針**:
  - [platform_utils.py:400-403](../platform_utils.py#L400): 戻り値を三値
    （`ended / not_ended / unknown`）にし、`unknown` を呼び出し側へ伝える。
  - [main.py:383-396](../main.py#L383)（`_process_one_url` のループ）と
    [main.py:703-719](../main.py#L703)（シングルモード）: `unknown` が続く場合の
    **フォールバック終了判定**を追加する。候補（併用推奨）:
    - `get_video_time()` の `currentTime` が N 回連続で停滞したら終了扱い
    - `pct == 100` で終了扱い
    - 音声セグメントの RMS がほぼ 0 の状態が N 分続いたら終了扱い（→ 障害 6 と共通基盤）
    - 総尺（duration）が取れた場合は「経過時間 > duration × 1.2」のウォッチドッグ
  - **根本対策**: AppleScript 注入を Chrome DevTools Protocol (CDP) に移行する。CDP は
    ターゲット列挙で OOPIF・クロスオリジン iframe 内にもアタッチでき、`Input.dispatchMouseEvent`
    で autoplay policy も正攻法で満たせる（`issue.md` の案βと同じ。案αのクリックより堅牢）。
    `--remote-debugging-port` 起動が必要になる点だけ運用が変わる。

### 2. 動画終了後に最初から再生が再開される（再ループの再発）

- **症状**: 動画が終わると SCORM プレーヤーが巻き戻り、文字起こしが最初からやり直しになる。
- **原因**: [3f97091] で入れたガード（`_tick_macos` 冒頭の `get_video_ended()` チェック、
  [platform_utils.py:458-459](../platform_utils.py#L458)）が、障害 1 により
  **常に `False` を返すため実質無効**。その結果 keep-alive が 20 秒ごとに
  `_VISIBILITY_JS`（[platform_utils.py:18](../platform_utils.py#L18)）を注入し続ける。
  再ループを引き起こす経路は 2 つ:
  1. video が JS から見える構成のとき: `if(v&&v.paused&&!v.ended){v.play()}` が、
     SCORM プレーヤーがリセットした動画（`ended=false, paused=true, currentTime=0`）を
     再生し直す — 3f97091 のコミットメッセージに書かれたメカニズムそのもの。
  2. video が見えない構成のときも: トップページへの
     `d.dispatchEvent(new Event('visibilitychange'))` ＋ `visibilityState` 偽装が、
     千葉大 Moodle プレーヤー側の視聴管理 JS に「タブが可視になった → 再開」を促す可能性。
- **付随バグ**: `_VISIBILITY_JS` の video 探索は**深さ 1**（`frames[i].document`）なのに、
  終了判定 `_VIDEO_ENDED_JS` は**深さ 4**。探索深度が不整合で、「play はできるのに
  ended は見えない」状況を作り得る。
- **修正すべき箇所と方針**:
  - [platform_utils.py:449-498](../platform_utils.py#L449)（`_tick_macos`）:
    - ガードを障害 1 の三値化とセットで直す（`unknown` でも「一度 ended を観測済み」なら注入停止）。
    - keep-alive 側に状態を持たせる: 「currentTime が duration 付近まで到達した後に
      currentTime=0 へ戻った」＝終了→リセットと判定し、以後 `v.play()` を打たない。
    - 無条件 `v.play()` をやめ、「直前 tick で再生中だった」場合のみ再開する。
  - `_VISIBILITY_JS` の探索深度を `_VIDEO_ENDED_JS` と揃える。
  - **切り分け手順**（どちらの経路か確認したい場合）: `--save-interval 0 --keep-interval 99999`
    で keep-alive をほぼ無効化して 1 本流し、終了後に再ループするか観察する。
    再ループしなければ犯人は keep-alive 注入で確定。

### 3. バッチモード（--urls）で 2 本目以降の文字起こし冒頭が丸ごと欠落する

- **症状（潜在・未認識と思われる）**: `--urls URL1 URL2` で処理すると、2 本目の動画の
  冒頭部分のテキストが出力されない。欠落量は「1 本目の動画の長さ」に等しい
  （例: 1 本目が 30 分なら、2 本目の最初の 30 分ぶんが全部捨てられる）。
- **原因**: 重複除去用の `Transcriber._prev_end`
  （[transcriber.py:52](../transcriber.py#L52), [transcriber.py:121-128](../transcriber.py#L121),
  [transcriber.py:144-151](../transcriber.py#L144)）がインスタンス変数で、
  `_run_batch()` は 1 つの Transcriber を全 URL で使い回す
  （[main.py:517-522](../main.py#L517)）。各 URL で capture は 0 秒から始まるため、
  2 本目のセグメントは `abs_end <= self._prev_end`（前の動画の最終時刻）に該当し
  **無言でスキップ**される。
- **修正すべき箇所と方針**: `Transcriber` に `reset()` を追加して `_prev_end = 0.0` に戻し、
  `_process_one_url()` の冒頭で呼ぶ。1 行級の修正で直る。

---

## High

### 4. タブ/URL の解決がアクティブタブ依存で、別ページを誤って掴む

- **症状**: 出力ファイルのタイトル・URL が別ページのものになる。以降の JS 注入も全部
  誤ったタブへ飛び、進捗・終了検出・keep-alive がすべて空振りする。
- **証拠**: 2026-05-17 18:55 のログ
  `url_pattern resolved: 'moodle.gs.chiba-u.jp/moodle/mod/quiz/startattempt.php'` —
  SCORM 動画を処理しているのに **quiz ページ**を解決している。
  `out/20260517/講義動画③身だしなみと態度.txt` のヘッダ URL も quiz の URL になっている。
- **原因**: [main.py:330-338](../main.py#L330) の流れが
  `navigate_to_url()`（front window の active tab を書き換え）→ **固定 6 秒 sleep** →
  `get_active_tab_url()`（その時点の front window / active tab を読む）であるため、
  - リダイレクトが 6 秒で終わらないと `view.php?id=` のままパターン化 → player タブに二度と一致しない
  - ユーザーが 6 秒の間にタブ/ウィンドウを切り替えると別ページを掴む
  - そもそも実行中は Chrome を一切触れない設計になっている
- **修正すべき箇所と方針**:
  - [platform_utils.py:294-320](../platform_utils.py#L294): 専用タブを `make new tab` で
    開いて AppleScript のタブ参照（tab id）を保持し、active tab には依存しない。
  - 固定 sleep をやめ、「URL に `player.php` が現れるまで最大 N 秒ポーリング」に変える。
  - `_url_to_pattern()`（[main.py:292](../main.py#L292)）の `a=XXXXX` 部分一致は
    他タブ誤マッチの余地あり（`&a=` 境界を含めるだけでも改善）。

### 5. osascript の TimeoutExpired が捕捉されず、実行全体がクラッシュする

- **症状**: 長時間実行の途中で突然 traceback を吐いて終了する（Chrome が重い・タブが多い・
  ビーチボール時に発生しやすい）。
- **原因**: `subprocess.run(..., timeout=5/10)` は時間切れで **`TimeoutExpired` を送出**するが、
  どこにも try/except がない:
  - [platform_utils.py:287-291](../platform_utils.py#L287)（`_run_js_in_tab`, timeout=10）
  - [platform_utils.py:210-213](../platform_utils.py#L210)（`get_moodle_page_info`, timeout=5）
  - [platform_utils.py:308](../platform_utils.py#L308), [platform_utils.py:317](../platform_utils.py#L317)
  メインループ内の `get_viewing_percentage()` / `get_video_ended()` 経由で発生すると
  `_process_one_url` ごと落ち、バッチ全体が中断する。
- **修正すべき箇所と方針**: `_run_js_in_tab` 等の低レベル関数で `TimeoutExpired` と
  `OSError` を捕捉し、空文字を返してログに残す。呼び出し側は既に空文字を失敗扱いできる
  作りなので、捕捉するだけで安定する。

### 6. 無音でも幻覚テキストを延々と出力し、失敗に気づけない（whisper.cpp 経路に VAD なし）

- **症状**: 再生が始まっていない／音声ルーティングが外れているのに気づかず、
  幻覚だらけのファイルができあがる。
- **証拠**: `out/20260626/自己相関と相互相関の動画_2.txt` は約 10 分間
  「ご視聴ありがとうございました」の繰り返し**のみ**。Whisper が無音入力で出す典型的幻覚。
- **原因**:
  - faster-whisper 経路には `vad_filter=True` がある
    （[transcriber.py:133-139](../transcriber.py#L133)）が、macOS で実際に使われる
    whisper.cpp 経路（[transcriber.py:113-128](../transcriber.py#L113)）には
    **無音対策が何もない**。無音 30 秒をそのまま whisper-cli に渡している。
  - さらに「無音が続く」は再生失敗・ルーティング失敗の重要シグナルなのに、警告も停止もしない。
- **修正すべき箇所と方針**:
  - [main.py:373](../main.py#L373) 付近 or [transcriber.py:104](../transcriber.py#L104):
    セグメントの RMS を計算し、閾値未満なら transcribe をスキップ。
  - 無音セグメントが連続 N 個（例: 2 分）続いたら「音声が来ていない」警告を表示し、
    開始直後なら再生リトライ、動画中盤以降なら終了とみなす（障害 1 のフォールバックと共通化）。
  - whisper-cli 側でも `--no-speech-thold` の指定を検討。

---

## Medium

### 7. 30 秒ごとに whisper-cli を起動し、毎回モデルをロードし直している

- **症状**: 文字起こしが遅く、キューが滞留 → 終了検出（セグメント消費時にしか走らない）も
  遅延 → 停止がさらに遅れる、という形で安定性に波及する。
- **原因**: [transcriber.py:74-102](../transcriber.py#L74) がセグメントごとに
  `subprocess.run(whisper-cli ...)` を実行。large-v3-turbo のモデルロードが毎回発生する
  （`__init__` の「ウォームアップ」はプロセスが毎回消えるため CoreML コンパイルキャッシュを
  温める以上の意味がない）。
- **修正すべき箇所と方針**: whisper.cpp の **server モード（whisper-server）を常駐**させて
  HTTP で投げるか、`pywhispercpp` でプロセス内にモデルを保持する（ROADMAP 案 7 の元方針）。

### 8. capture.stop() の二重呼び出しで末尾セグメントが重複する

- **原因**: ended 経路（[main.py:395](../main.py#L395)）と SIGINT ハンドラ
  （[main.py:527-534](../main.py#L527)）の両方から `capture.stop()` が呼ばれ得る。
  `AudioCapture.stop()`（[capture.py:62-67](../capture.py#L62)）は冪等でなく、
  `_flush_remaining()`（[capture.py:98-108](../capture.py#L98)）は**バッファをクリアしない**
  ため、2 回目の stop で同じ末尾音声がもう一度キューに積まれ、テキストが重複する。
- **修正すべき箇所と方針**: `stop()` に stopped フラグで冪等化、flush 後に
  `self._buffer = []; self._buffer_size = 0`。

### 9. 例外時に capture が止まらない（オーディオストリーム／スレッドのリーク）

- **原因**: `_process_one_url` の finally（[main.py:397-399](../main.py#L397)）は
  `keep_alive.stop()` と `writer.finalize()` だけで **`capture.stop()` がない**。
  障害 5 の例外などでループを抜けると、BlackHole の InputStream が開いたまま次の URL へ
  進む／終了する。コールバックはキューに積み続けるためメモリも漏れる。
- **修正すべき箇所と方針**: finally に `capture.stop()` を追加（障害 8 の冪等化が前提）。

### 10. keep-alive スレッドを join しておらず、終了処理後に注入が走る競合がある

- **原因**: `WindowKeepAlive.stop()`（[platform_utils.py:435-436](../platform_utils.py#L435)）は
  Event を立てるだけで join しない。実行中の `_tick_macos` は最後まで走るため、
  メイン側が「視聴状況を保存 → 活動から抜ける」を済ませた**直後**に、残った tick が
  visibility 偽装＋`v.play()` を注入し得る。障害 2 の再ループを助長する。
- **修正すべき箇所と方針**: `stop()` で `self._thread.join(timeout=...)`。
  `_tick` 内の要所（osascript 実行前）で `self._stop.is_set()` を確認して中断。

### 11. オーバーラップ境界の文が重複して出力される

- **証拠**: `out/20260501/講義動画2_医師の役割機能と教育.txt` の
  `[00:00:23]`〜`[00:00:32]` で「医師は他者を侵害する仕事です。」が連続重複。
- **原因**: 5 秒オーバーラップに対し、重複除去が「セグメント全体が `prev_end` 以前なら捨てる」
  （[transcriber.py:121-122](../transcriber.py#L121)）という粗い判定のため、
  境界をまたぐセグメントは両方通過する。
- **修正すべき箇所と方針**: `abs_start < prev_end < abs_end` のセグメントはテキストを
  時間比で切り詰める、または前セグメント末尾とのテキスト類似度で重複除去。

---

## Low

### 12. Ctrl+C で全停止した後もファイル名リネームのプロンプトが 60 秒ずつ出る

- [main.py:401](../main.py#L401): stop_event セット済みでも `_prompt_rename()` が呼ばれる。
  バッチ中断時は URL ごとに入力待ちが発生。stop_event 時はスキップすべき。

### 13. whisper.cpp のパスがハードコード

- [transcriber.py:16](../transcriber.py#L16):
  `/Users/yoshihide/my-projects/whisper.cpp` がデフォルト。`WHISPER_CPP_DIR` で上書き可能だが、
  README/USAGE.md にその記載がない。ドキュメント追記か相対探索を。

### 14. 出力ヘッダの「(int8)」表記が whisper.cpp バックエンドでは虚偽

- [output.py:37](../output.py#L37) と [main.py:650](../main.py#L650): バックエンドに関わらず
  `Model: xxx (int8)` と書く。whisper.cpp (Metal/CoreML) では正しくない。
  Transcriber からバックエンド名を取って表示すべき。

### 15. Windows / Linux 経路が実質未検証のまま残っている

- `WindowsAudioCapture` の `start_time` は「セグメント番号 × (長さ−オーバーラップ)」の
  近似でドリフトし得る（[capture.py:171](../capture.py#L171)）、
  `_tick_windows` はフォアグラウンド強奪（[platform_utils.py:508-519](../platform_utils.py#L508)）、
  Linux は xdotool 頼みで JS 注入なし。macOS 専用ツールとして割り切るなら削除も選択肢
  （コードパスが減るぶん安定する）。

---

## 修正の優先順位（提案）

| 順 | 障害 | 効果 | 規模 |
|---|------|------|------|
| 1 | #3 `_prev_end` リセット | バッチの出力欠落が即直る | 数行 |
| 2 | #5 TimeoutExpired 捕捉 + #9 finally で capture.stop | 突然死・リークが消える | 小 |
| 3 | #1+#6 終了フォールバック（無音検出・currentTime 停滞・ウォッチドッグ） | 「止まらない」が実用レベルで解消 | 中 |
| 4 | #2+#10 keep-alive の注入抑制と join | 再ループ解消 | 中 |
| 5 | #4 専用タブ + ポーリング | 誤タブ事故が消え、実行中も Chrome を使える | 中 |
| 6 | #7 whisper-server 常駐 | 速度・遅延改善 | 中〜大 |
| 7 | #1 根本対策: CDP 移行 | AppleScript 注入の構造的脆さを一掃 | 大 |

CDP 移行（`--remote-debugging-port` + websocket）まで行けば #1/#2/#4 は構造ごと解決するが、
先に 1〜4 の小改修だけでも「終わったのに止まらない・巻き戻る・突然死ぬ」は概ね解消できる見込み。

## 検証に使った一次情報

- `logs/moodle-transcriber.log`（2026-05-01〜07-05）
- `out/20260501/`, `out/20260517/`, `out/20260626/`, `out/20260705/` の実出力
- git 履歴: 3f97091（再ループ修正 — ガードが検出系故障で無効化されている）、
  6273bce（多段 iframe 対応 — 適用後も 5/15 以降 `pct=-1` が継続しており未解決）
- `issue.md`（自動再生問題の過去調査。案βの CDP が本ドキュメントの根本対策と一致）
