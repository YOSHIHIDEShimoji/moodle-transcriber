# 修正プラン: タブ id 照合の型不一致（ブラインドモードの真因）

作成: 2026-10-14 / 状態: 全修正適用済み。2026-10-16 にガイダンス動画（262 秒）で実機 E2E 合格（再生→文字起こし 59 行→停止→保存/離脱→音声復元）。test-auditor 監査対応済み・test_tool.py 187/187

## 結論

7/5 のコミット a8c5cb3 以降、macOS Chrome では **JS 注入が 1 回も成功していない**。
原因は `platform_utils._tab_condition()` が生成する AppleScript の 1 行:

```applescript
if id of t is 2144314444 then
```

Chrome 155 の `id of tab` は **text** で返る（実機で `class of x` → `text` を確認）。
text と整数リテラルの比較は常に false。さらに 2^29 を超える整数リテラルは
AppleScript 内で real になるため、仮に型が合っても一致しない。

実機検証（2026-10-14・Chrome 155.0.8059.41・同一タブ id 2144332276）:

| 比較式 | 結果 |
|---|---|
| `id of t is 2144332276` | NO MATCH |
| `(id of t as string) is "2144332276"` | MATCH |

## 影響範囲（全部この 1 行に収束する）

`_tab_condition` を通る関数すべてが「タブが見つからない＝空文字」で無言失敗する。

- `_run_js_in_tab` → `trigger_video_play`（no_video ×5 → gave up）、
  `get_viewing_percentage` / `get_video_time` / `get_video_ended`（pct=-1, ended=None）、
  `click_save_button` / `click_exit_activity_button`（視聴状況保存・離脱が押されない）
- `get_tab_url` → `wait_for_page_ready` が 25 秒タイムアウトまで空回り
  （ログ: whisper ready 11:19:28 → target resolved 11:19:54）
- `get_moodle_page_info` → タイトル取れず `lecture_HHMMSS.txt`
- `WindowKeepAlive._tick_macos` → visibility 維持 JS が入らない（タブを離れると停止）

ログ上の裏付け: 7/5 11:00（URL パターン時代）は `video play triggered` 成功。
同日 13:54 と 7/16 11:19（タブ id 以降）は `no_video` → `blind mode`。
7/16 の出力 6.6KB は手動再生＋Ctrl+C で止めた断片。

録音・whisper.cpp・音声ルーティングは健全（本日 `whisper-server` 起動・
BlackHole / 複数出力装置 / SwitchAudioSource すべて存在を確認）。

## 修正内容

### 1. `_tab_condition` を文字列比較にする（必須・1 行）

```python
def _tab_condition(target: TabTarget) -> str:
    if isinstance(target, int):
        return f'      if (id of t as string) is "{target}" then'
    return f'      if URL of t contains "{target}" then'
```

意図: Chrome の id が text でも integer でも一致する。巨大整数の real 化も回避。
`get_active_tab_id` / `get_tab_id_by_pattern` は `int(stdout)` で受けているので
Python 側の型は変えない（既存テスト・ログ出力との互換を保つ）。

### 2. 回帰テストを追加（必須）

`test_tool.py` に 2 件:

- `_tab_condition(2144314444)` の出力に `as string` と `"2144314444"` が含まれる
  （数値リテラル比較への逆戻りを機械的に止める）
- `_tab_condition("a=123")` は従来通り `URL of t contains "a=123"`

注意: 既存 108 件は osascript をモックするため、この障害を検出できなかった
（false confidence）。上の 2 件も AppleScript の意味までは検証できない。
だからこそ 3 の実機スモークを merge 条件に入れる。

### 3. 実機スモーク（merge 条件・人手 1 回）

Chrome で Moodle の SCORM プレーヤーを開いた状態で:

```bash
cd /Users/yoshihide/my-projects/moodle-transcriber
python -c "
from platform_utils import get_tab_id_by_pattern, _run_js_in_tab, get_moodle_page_info
tid = get_tab_id_by_pattern('moodle.gs.chiba-u.jp')
print('tab_id', tid)
print('title', get_moodle_page_info(tid))
print('js', _run_js_in_tab('document.title', tid))
"
```

合格条件: `tab_id` が整数、`title` が講義名、`js` が非空。
その後 `python main.py --moodle-url moodle.gs.chiba-u.jp --model tiny` を
1 本だけ流し、ログに `video play triggered` と `stopping: video.ended`（または
フォールバック理由）が出て、`out/YYYYMMDD/<講義名>.txt` が生成されることを確認。

### 4. 無言失敗を可視化する（推奨・小）

`_run_js_in_tab` でタブが見つからなかった場合と、見つかったが JS が空を返した
場合を区別できない。AppleScript 末尾に `return "__NO_TAB__"` を足し、
Python 側でその値を受けたら `logger.warning("tab not found: %r", target)` を
1 回だけ出す。意図: 次に同種の障害が起きたとき「blind mode」ではなく
「タブ未発見」と即座に分かる。

## やらないこと

- タブ id 追跡を URL パターンに戻す（7/5 の「アクティブタブ誤爆」修正を失うため）
- `TabTarget` を str に変える（変更範囲が広く、利点がない）

## 手順

1. `platform_utils._tab_condition` 修正（1 行）
2. `test_tool.py` に回帰テスト 2 件 → `python test_tool.py` 110/110
3. 実機スモーク（上記 3）
4. （任意）4 の警告追加
5. commit → merge。ロジック変更なので merge 前に test-auditor を起動

---

# 追記 2026-10-16: 第 2 の障害 — System Events `click at` が座標を無視する

タブ id 修正後の実機テスト（ガイダンス動画・262 秒）で、再生トリガーが「成功」を
返すのに動画が `paused=true, currentTime=0` のままという障害が出た。

## 実測

| 操作 | 結果 |
|---|---|
| `System Events click at {35,300}` | ページ内 client=(960,569) に着弾 |
| `System Events click at {35,900}` | 同じ (960,569) |
| `System Events click at {1800,900}` | 同じ (960,569) |
| JXA `CGEventPost`（同座標） | 指定どおり client=(35,149) / (35,749) に着弾 |

サンドボックス内外（アプリ内ターミナル）で同一結果。Accessibility は trusted。
Darwin 25.6（macOS 26）で System Events の座標クリックが固定点に落ちる。
5〜7 月に動いていた仕組みが OS 更新で壊れたと判断。結果 `video.play()` は
`NotAllowedError: play() failed because the user didn't interact with the document first`。

さらに旧 `trigger_video_play` は「クリック API の戻り値 0」だけで True を返していたため、
再生できていなくても録音だけ走り、`PlaybackMonitor` が「currentTime 停滞」で
"視聴完了" と誤判定 → 空ファイル保存 → 「活動から抜ける」まで押していた。

## 修正（適用済み）

1. `platform_utils._cg_click`: JXA (`osascript -l JavaScript`) で CoreGraphics の
   マウスイベントを投稿。追加依存なし。失敗時のみ System Events にフォールバック。
2. `trigger_video_play`: activate → 最前面を確認 → クリック → `paused=false` を実測。
   始まらなければ `play()` → 再実測。**実際に再生が始まった場合だけ True**。
3. `PlaybackMonitor`: `currentTime < 1.0` のまま動かない状態を「停滞」と区別し、
   約 1 分ごとに最大 2 回リトライ → 約 4 分で「再生を開始できないため中止」。
   `played` フラグを公開し、未再生なら「視聴状況を保存」「活動から抜ける」を押さない。
4. 回帰テスト: test_tool.py [26][27]（130/130）。

## 副次的な発見

- 録音は Multi-Output Device 経由の**全システム音声**を拾う。テスト中に別アプリの音楽が
  流れていたため英語の歌詞が文字起こしに混入した。実行中は他アプリの音を止めること。
- target 解決 → 再生トリガーの間に 57 秒の空白（原因未特定）。`capture.start()` は 0.05 秒。
  タイミングログを追加済み（`page info resolved` / `capture started` / `keep-alive started`）。

---

# 追記 2026-10-16 (2): 第 3 の障害 — 逆相ステレオを平均して音声が消える

再生・停止判定が直った 2 回目の実機テストで、262 秒を完走したのに本文が
「ご視聴ありがとうございました」1 行（幻覚）だけになった。

## 実測（同じ動画を 20 秒録音）

| ミックス | RMS | whisper 出力 |
|---|---|---|
| (L+R)/2（旧実装） | 0.0076 | 「ご視聴ありがとうございました。」（幻覚） |
| L のみ | 0.0509 | 「授業期間は後期集中で 今年度は第5タームから…」 |
| (L−R)/2 | 0.0505 | 同上 |

corr(L,R) = −0.956。この動画の音声は左右が逆相でエンコードされており、
`capture.py` の `np.mean(indata, axis=1)` で打ち消されていた。
5〜7 月の講義動画は同相だったため顕在化しなかった。

## 修正（適用済み）

- `capture.downmix_to_mono`: L·R の内積が負で、(L−R)/2 の RMS が (L+R)/2 の 2 倍超なら
  差を採用。それ以外は従来どおり平均。macOS (`_callback`) と Windows 経路の両方に適用。
- 回帰テスト: test_tool.py [28]（136/136）。

---

# 追記 2026-10-16 (3): test-auditor 監査への対応

ミューテーション監査で「実装を壊しても緑のまま」の穴が 5 件（blocking）指摘された。
すべて対照テストで固定した（test_tool.py 187/187）。

| 指摘 | 対応 |
|---|---|
| play() フォールバック経路が未実行 | [26] を疑似時計で駆動し、play() 呼び出し回数を 0/1 で assert |
| `monitor.played` ゲート 3 箇所が未検証 | [31] 新設。未再生→保存/離脱 0 回、再生済み→1 回ずつを 3 経路で対照 |
| downmix 閾値 2.0 が未拘束 | [28] に部分逆相→mid / 実測相当→side / 境界 1.94・2.03 / 無相関 / 4ch を追加 |
| 類似度閾値 0.75 が未拘束 | [29] に ratio 0.71 / 0.79 の対、ratio 0.5 の新規内容保持、非境界保持、最小長 7/8 を追加 |
| `_cg_click` 本体が未実行 | subprocess.run をモックして argv・rc・stdout・例外の契約を固定、JXA を osacompile で構文検証 |

挙動変更: 既視聴 (pct=100) で再生が始まらない URL は、未開始判定より先に
「視聴状況 100%」で終了する（旧挙動と同じ 3 poll）。3ch 以上の入力は全 ch 平均に戻す。
