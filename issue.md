# Issue: SCORM動画の自動再生が機能しない

## 一言で言うと

macOS Chrome で Moodle の SCORM プレーヤーページを開いた後、
プログラムから動画を自動再生させようとしているが、再生されない。

---

## 環境

- macOS (Apple Silicon, Darwin 25.3.0)
- Python 3.x + `platform_utils.py` (AppleScript 経由で Chrome を操作)
- 対象ページ URL 例:
  `https://moodle.gs.chiba-u.jp/moodle/mod/scorm/player.php?a=176267&currentorg&scoid=352590`
- ページ構造: Moodle SCORM プレーヤー (`player.php`) → `<iframe>` → SCORM コンテンツ → `<video>` (video.js)

---

## 現在の実装 (`platform_utils.py`)

### Step 1: 再生ボタンのスクリーン座標を JS で取得

```python
_GET_PLAY_BTN_POS_JS = (
    "(function(){"
    "var cH=window.outerHeight-window.innerHeight;"
    "var sels=['.vjs-big-play-button','.vjs-play-control','.big-play-button','video'];"
    "function findPos(doc,ox,oy){"
    "for(var s=0;s<sels.length;s++){"
    "try{var b=doc.querySelector(sels[s]);"
    "if(b){var r=b.getBoundingClientRect();return [ox+r.left+r.width/2,oy+r.top+r.height/2];}}"
    "catch(e){}}"
    "return null;}"
    "var p=findPos(document,0,0);"
    "if(p)return Math.round(window.screenX+p[0])+','+Math.round(window.screenY+cH+p[1]);"
    "var ifs=document.querySelectorAll('iframe');"
    "for(var i=0;i<ifs.length;i++){"
    "try{"
    "var ir=ifs[i].getBoundingClientRect();"
    "var fd=ifs[i].contentDocument||ifs[i].contentWindow.document;"
    "p=findPos(fd,ir.left,ir.top);"
    "if(p)return Math.round(window.screenX+p[0])+','+Math.round(window.screenY+cH+p[1]);"
    "if(ir.width>0&&ir.height>0)return Math.round(window.screenX+ir.left+ir.width/2)+','+Math.round(window.screenY+cH+ir.top+ir.height/2);"
    "}catch(e){}}"
    "return Math.round(window.screenX+window.innerWidth/2)+','+Math.round(window.screenY+cH+window.innerHeight/2);"
    "})()"
)
```

### Step 2: AppleScript でその座標をクリック（ユーザージェスチャー登録）

```python
script = (
    f'tell application "Google Chrome" to activate\n'
    f'delay 0.3\n'
    f'tell application "System Events" to click at {{{x}, {y}}}'
)
subprocess.run(["osascript", "-e", script], ...)
```

### Step 3: ユーザージェスチャー後に JS で `v.play()` を呼ぶ（フォールバック）

```python
time.sleep(0.5)
_run_js_in_tab(
    "(function(){"
    "var v=document.querySelector('video');"
    "if(!v){for(var i=0;i<frames.length;i++){try{v=frames[i].document.querySelector('video');if(v)break;}catch(e){}}}"
    "if(v){try{v.play();}catch(e){}}"
    "})()",
    url_pattern, browser,
)
```

`_run_js_in_tab` は AppleScript の `execute t javascript "..."` で実行する。

---

## 実際のログ（問題の証拠）

```
# URL パターン解決は成功している
19:12:30 INFO  url_pattern resolved: 'a=176267'
                (actual_url: 'https://moodle.gs.chiba-u.jp/.../player.php?a=176267&...')

# 座標取得 → クリック実行（AppleScript returncode=0）
19:12:31 DEBUG trigger_video_play raw='71,536' pattern='a=176267'
19:12:31 INFO  video play triggered (attempt 1)

# しかし動画は再生されていない（30秒ごとのセグメントが無音で延々と続く）
19:13:01 DEBUG segment 0:  pct=-1 ended=False
19:13:26 DEBUG segment 1:  pct=-1 ended=False
19:13:51 DEBUG segment 2:  pct=-1 ended=False
...（20セグメント = 10分以上続いて打ち切り）

# v.play() フォールバック追加後（19:53 の実行）でも同じ結果
19:53:58 INFO  url_pattern resolved: 'a=176267' ...
19:53:59 DEBUG trigger_video_play raw='71,536' pattern='a=176267'
19:54:00 INFO  video play triggered (attempt 1)
19:54:29 DEBUG segment 0: pct=-1 ended=False   ← 再生されていない
```

---

## 問題の切り分け

### 問題A: 座標 `x=71` が再生ボタンに当たっていない（確定）

`raw='71,536'` → x=71 はページ左端付近。
video.js の `.vjs-big-play-button`（動画中央の大きなボタン）は通常ページ中央付近にあるはず。
x=71 はおそらく `.vjs-play-control`（コントロールバー左端の小さな再生/停止ボタン）か、
または完全に外れた位置。

**疑われる原因**:
- SCORM iframe が `left=71px` 相当の位置から始まっており、iframe 内の要素の `getBoundingClientRect().left` が 0 付近 → `ir.left + element.left + width/2 = 71 + 0 + α` になっている
- もしくは iframe が深くネストしており（player.php → iframe → iframe）、1段しか潜っていないため座標がずれている
- セレクター順で先にマッチした要素が意図しない要素（非表示・ゼロサイズ・コントロールバーの小ボタン）

### 問題B: `v.play()` が Chrome の autoplay policy にブロックされている（確定）

Chrome の autoplay policy は「同一フレームまたは同一オリジン親フレームでのユーザージェスチャー」が必要。

AppleScript `System Events click at {71, 536}` は OS レベルのクリックイベントを送るが、
クリック先が SCORM `<iframe>` の外（または別フレーム）であった場合、
そのフレームへのユーザーアクティベーションは伝播しない。

その後 `execute t javascript "v.play()"` を呼んでも、
SCORM iframe のフレームコンテキストにユーザーアクティベーションがなければ `NotAllowedError` になる。

### 問題C: `pct=-1`（視聴進捗が取得できない）

`_GET_PCT_JS` は `document.body.innerText` で `視聴状況` テキストを探すが、
そのテキストが SCORM iframe の中にある場合は見つからない。
（終了判定は `video.ended` に切り替え済みなので致命的ではないが、進捗バーが表示されない）

---

## 求める解決策

**核心: SCORM iframe 内の `<video>` を確実に再生させる手段**

### 制約
- Python から AppleScript / JS 経由で操作する
- Chrome の autoplay policy を満たす必要がある
- `v.play()` を単独で JS 実行するだけでは `NotAllowedError` になる

### 試したこと（すべて失敗）
1. `v.play()` 直接呼び出し → `NotAllowedError`
2. `v.muted=true; v.play().then(()=>v.muted=false)` → Chrome がミュート解除時に一時停止
3. AppleScript で再生ボタン座標をクリック → 座標がずれており当たらない
4. クリック後に `v.play()` を呼ぶ（Step1+2） → クリックが SCORM iframe に当たっていないためフレームのユーザーアクティベーションなし → play() もブロック

### 有力なアプローチ候補

**案α: SCORM iframe の中心を直接クリック**
- `player.php` ページには大きな SCORM iframe が1つある
- その iframe の BoundingClientRect の中心座標をスクリーン座標に変換してクリックする
- iframe 中心クリック → SCORM フレームにユーザーアクティベーションが入る → その後 `v.play()` が通る

```javascript
// iframe の中心スクリーン座標を返す JS
(function(){
  var cH=window.outerHeight-window.innerHeight;
  var ifs=document.querySelectorAll('iframe');
  // 最大面積の iframe を選ぶ
  var best=null, bestArea=0;
  for(var i=0;i<ifs.length;i++){
    var r=ifs[i].getBoundingClientRect();
    var area=r.width*r.height;
    if(area>bestArea){bestArea=area;best=r;}
  }
  if(best){
    return Math.round(window.screenX+best.left+best.width/2)+','+
           Math.round(window.screenY+cH+best.top+best.height/2);
  }
  return Math.round(window.screenX+window.innerWidth/2)+','+
         Math.round(window.screenY+cH+window.innerHeight/2);
})()
```

**案β: Chrome DevTools Protocol (CDP) 経由**
- CDP の `Input.dispatchMouseEvent` を使えばフレーム指定でクリックイベントを送れる
- Python から `websocket-client` などで CDP に接続して直接操作

**案γ: iframe の `allow="autoplay"` 属性を JS で付与**
- `iframe.allow = "autoplay"` を親ページから設定することで、
  そのフレームでの autoplay を許可できる場合がある（Chrome のパーミッションポリシー）

---

## 関連ファイル

- `platform_utils.py` — `trigger_video_play()`, `_GET_PLAY_BTN_POS_JS`, `_run_js_in_tab()`
- `main.py` — `_trigger_with_retry()`, `_process_one_url()`

## 実行コマンド例

```bash
python main.py \
  --url-file urls.txt \
  --keep-active chrome \
  --model large-v3
```

`urls.txt` の内容例（ページ読み込み後 `player.php?a=XXXXX` にリダイレクトされる）:
```
https://moodle.gs.chiba-u.jp/moodle/mod/scorm/view.php?id=176267
```
