#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
京都府・市町村共同施設予約システム 体育館バドミントン枠 空き通知 (クラウド版)

- Playwright(headless Chromium)で「ログイン不要の空き照会」を再現
- 対象施設×対象曜日時間の空き状況を取得
- 「予約済→空き」(空きコート数 0→1以上)に変わった枠のみ Gmail(SMTP) で通知
- 施設×日時で1通に集約
- JST 00:00-05:00 は通知しない(そもそも処理を行わず終了)
- state.json で前回状態を保持(GitHub Actions側でリポジトリにコミット)

環境変数 (GitHub Secrets):
  GMAIL_ADDRESS       送信元Gmailアドレス
  GMAIL_APP_PASSWORD  Gmailアプリパスワード(16桁)
  NOTIFY_TO           通知先アドレス(カンマ区切りで複数指定可。未指定なら GMAIL_ADDRESS)
"""
import os
import sys
import json
import time
import random
import time
import smtplib
import traceback
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout


class BusyError(Exception):
    """サイトが混雑/アクセス制限ページを返した場合。"""


BUSY_PAT = ("混みあって", "混み合って", "しばらく時間をおいて", "アクセスが集中")


def _is_busy(page):
    try:
        txt = page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return False
    return any(p in txt for p in BUSY_PAT)

JST = ZoneInfo("Asia/Tokyo")
WELCOME_URL = "https://g-kyoto.growone.net/eshisetsu/menu/Welcome.cgi"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
LOG_FILE = os.path.join(HERE, "log.txt")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# ---- 設定 --------------------------------------------------------------
REGIONS = ["京都市北区","京都市左京区","京都市中京区","京都市東山区","京都市下京区",
           "京都市南区","京都市右京区","京都市伏見区","京都市山科区","京都市西京区","宇治市"]

FACILITIES = [
    {"key":"261009_001_24_01_03","h3":"京都市 市民スポーツ会館","display":"市民スポーツ会館(1/4面)"},
    {"key":"261009_001_27_01_03","h3":"京都市 桂川地域体育館","display":"桂川地域体育館(1/4面)"},
    {"key":"261009_001_28_01_02","h3":"京都市 伏見北堀公園地域体育館","display":"伏見北堀公園地域体育館(1/2面)"},
    {"key":"261009_001_26_01_03","h3":"京都市 山科地域体育館","display":"山科地域体育館(1/4面)"},
    {"key":"261009_001_29_01_03","h3":"京都市 醍醐地域体育館","display":"醍醐地域体育館(1/4面)"},
    {"key":"261009_001_30_01_03","h3":"京都市 右京地域体育館","display":"右京地域体育館(1/4面)"},
    {"key":"261009_001_32_01_01","h3":"京都市 中京地域体育館","display":"中京地域体育館"},
    {"key":"261009_001_34_01_01","h3":"京都市 久世地域体育館","display":"久世地域体育館"},
    {"key":"261009_001_35_01_01","h3":"京都市 伏見東部地域体育館","display":"伏見東部地域体育館"},
    {"key":"261009_001_36_01_01","h3":"京都市 伏見北部地域体育館","display":"伏見北部地域体育館"},
    {"key":"261009_001_37_01_02","h3":"京都市 下京地域体育館","display":"下京地域体育館(1/2面)"},
]
KEYS = [f["key"] for f in FACILITIES]
H3_TO_DISPLAY = {f["h3"]: f["display"] for f in FACILITIES}

# ---- ブラウザ内で実行するJS ---------------------------------------------
JS_SELECT_CONDITIONS = """
(regions) => {
  document.querySelectorAll('input[name="catSub4"]').forEach(cb=>{
    const l=document.querySelector('label[for="'+cb.id+'"]');
    if(l && regions.includes(l.textContent.trim()) && !cb.checked) cb.click();
  });
  const ym=document.getElementById('yoyakuMode_1');
  if(ym && !ym.checked){ const l=document.querySelector('label[for="yoyakuMode_1"]'); if(l) l.click(); }
  const j=document.querySelector('label[for="jouken1_1"]'); if(j) j.click();
  const c=document.querySelector('label[for="catSel1_1"]'); if(c) c.click();
  const bl=[...document.querySelectorAll('label')].find(l=>l.textContent.trim()==='バドミントン'); if(bl) bl.click();
  return true;
}
"""

JS_SELECT_FACILITIES = """
(keys) => {
  let n=0;
  document.querySelectorAll('input[name="checkMeisaiUniqKey"]').forEach(cb=>{
    if(keys.includes(cb.value)){ if(!cb.checked) cb.click(); n++; }
  });
  return n;
}
"""

# 表示切替=31日間、曜日絞込=火/木/土/日/土日祝(他は外す)、開始日をセットして
# 「選択した条件で表示」(モーダルを開くボタン)をクリックする。
# ※この後、モーダル内の確認ボタン「選択した条件で表示する」を別途クリックして初めて反映される。
JS_SET_CONTROLS = """
(args) => {
  const startISO=args[0], keep=args[1];
  const r31=document.getElementById('31day');
  if(r31 && !r31.checked){ const l=document.querySelector('label[for="31day"]'); (l||r31).click(); }
  document.querySelectorAll('input[name="checkShowDay"]').forEach(cb=>{
    const want = keep.indexOf(cb.value) >= 0;
    if(cb.checked !== want){ const l=document.querySelector('label[for="'+cb.id+'"]'); (l||cb).click(); }
  });
  const di=document.getElementById('startDate');
  const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
  if(di){ s.call(di, startISO); di.dispatchEvent(new Event('change',{bubbles:true})); }
  const ob=[...document.querySelectorAll('button.open_modal, button.btn_sort')]
            .find(b=>/選択した条件で表示/.test(b.textContent));
  if(ob) ob.click();
  return true;
}
"""

# 監視する曜日(サイトのcheckShowDay値): 火/木/土/日/土日祝
KEEP_DAYS = ["TUE", "THU", "SAT", "SUN", "HOL"]

# window描画完了の判定:
#  - 空き記号アイコンが存在
#  - 列数>=20 (31日間モード。7日間なら8列)
#  - 先頭日 >= 要求開始日 (window2 が反映されたことの確認)
#  - 先頭日と末尾日の間隔>=40日 (曜日絞込が効いている=飛び飛びの日付)
JS_WINDOW_READY = r"""
(startISO) => {
  const gs=[...document.querySelectorAll('table.box_calendar')];
  const t=gs.find(g=>g.querySelector('img[src*="icn_scche"]'));
  if(!t) return false;
  const head=[...t.querySelector('tr').querySelectorAll('th,td')].slice(1)
              .map(x=>x.innerText.replace(/\s+/g,' ').trim());
  if(head.length < 20) return false;
  const sy=+startISO.slice(0,4), sm=+startISO.slice(5,7), sd=+startISO.slice(8,10);
  const startD=new Date(sy, sm-1, sd);
  const parse=(str)=>{const m=str.match(/(\d+)月(\d+)日/); if(!m) return null;
    let mo=+m[1], da=+m[2]; let yr=sy; if(mo<sm) yr=sy+1; return new Date(yr, mo-1, da);};
  const first=parse(head[0]), last=parse(head[head.length-1]);
  if(!first || !last) return false;
  const spanDays=(last-first)/86400000;
  return first>=startD && spanDays>=40;
}
"""

JS_EXTRACT = r"""
(args) => {
  const ty=args[0], tm=args[1];
  const S={'icn_scche_ok.png':'空き','icn_scche_noset.png':'予約済','icn_scche_haifun.png':'利用不可'};
  const dw=d=>{const m=d.match(/([月火水木金土日])\s*$/);return m?m[1]:'';};
  const p=n=>String(n).padStart(2,'0');
  const gs=[...document.querySelectorAll('table.box_calendar')];
  const a={};
  gs.forEach(t=>{
    let h3='';
    let c=t.closest('ul.box_schedule')?t.closest('ul.box_schedule').parentElement:t.parentElement;
    while(c){let s2=c.previousElementSibling;while(s2){if(s2.tagName==='H3'){h3=s2.innerText.replace(/\s+/g,' ').trim();break;}s2=s2.previousElementSibling;}if(h3)break;c=c.parentElement;}
    const tr=[...t.querySelectorAll('tr')]; if(!tr.length) return;
    const hd=[...tr[0].querySelectorAll('th,td')].map(x=>x.innerText.replace(/\s+/g,' ').trim());
    const ds=hd.slice(1);
    tr.slice(1).forEach(r=>{
      const cs=[...r.querySelectorAll('th,td')];
      const tmr=cs[0].innerText.replace(/\s+/g,' ').trim();
      const m=tmr.match(/(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})/); if(!m) return;
      const st=m[1], ti=m[1]+'-'+m[2];
      cs.slice(1).forEach((cc,i)=>{
        const dstr=ds[i]; if(!dstr) return; const w=dw(dstr);
        // サイト側で火/木/土/日/土日祝に絞込済み。火・木は19:00/20:00開始のみ、それ以外(土日祝)は全時間帯。
        let k; if(w==='火'||w==='木') k=(st==='19:00'||st==='20:00'); else k=true;
        if(!k) return;
        const dm=dstr.match(/(\d+)月(\d+)日/); if(!dm) return;
        const mo=+dm[1], da=+dm[2]; const yr=(mo<tm)?ty+1:ty;
        const iso=yr+'-'+p(mo)+'-'+p(da); const jp=mo+'月'+da+'日('+w+')';
        const im=cc.querySelector('img'); const sr=im?im.getAttribute('src').split('/').pop():'';
        const stt=S[sr]||(im?im.alt:'');
        const key=h3+'|'+iso+'|'+st;
        if(!a[key]) a[key]={key:key,h3:h3,iso:iso,jp:jp,time:ti,avail:0,tot:0};
        a[key].tot++; if(stt==='空き')a[key].avail++;
      });
    });
  });
  return Object.values(a);
}
"""


def log(msg):
    line = f"{datetime.now(JST):%Y-%m-%d %H:%M:%S} JST\t{msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def end_of_next_month(d: date) -> date:
    m2 = d.month + 2
    y2 = d.year + (m2 - 1) // 12
    m2 = (m2 - 1) % 12 + 1
    return date(y2, m2, 1) - timedelta(days=1)


def _click_text(page, text):
    """テキストを含む button/a を1つクリック(遷移対応)。"""
    page.locator("button, a").filter(has_text=text).first.click()


def _save_debug(page, tag=""):
    """失敗時の診断情報をログとファイルに残す。"""
    try:
        info = page.evaluate("""() => ({
          url: location.href,
          title: document.title,
          grids: document.querySelectorAll('table.box_calendar').length,
          hasStartDate: !!document.getElementById('startDate'),
          facCbs: document.querySelectorAll('input[name="checkMeisaiUniqKey"]').length,
          bodyHead: (document.body ? document.body.innerText.replace(/\\s+/g,' ').slice(0,600) : '')
        })""")
        log(f"[debug{tag}] url={info['url']} title={info['title']} "
            f"grids={info['grids']} startDate={info['hasStartDate']} facCbs={info['facCbs']}")
        log(f"[debug{tag}] body: {info['bodyHead']}")
    except Exception:
        pass
    try:
        with open(os.path.join(HERE, "debug_page.html"), "w", encoding="utf-8") as fh:
            fh.write(page.content())
        page.screenshot(path=os.path.join(HERE, "debug.png"), full_page=True)
    except Exception:
        pass


def scrape():
    """対象枠の集計結果リストを返す。失敗時は例外。"""
    now = datetime.now(JST)
    ty, tm = now.year, now.month
    today = now.date()
    start1 = today.isoformat()
    start2 = (today + timedelta(days=31)).isoformat()
    cap = end_of_next_month(today)

    # 実行モード(環境変数 HEADLESS):
    #   未指定/0 … 本物Chromeを画面表示(ウィンドウが出る。最も確実)
    #   new     … Chromeの新ヘッドレス(ウィンドウを出さず、本物Chromeに近い。おすすめ)
    #   1       … 旧ヘッドレス(ウィンドウなしだがサイトに弾かれやすい)
    mode = os.environ.get("HEADLESS", "new").strip().lower()
    channel = os.environ.get("CHROME_CHANNEL", "chrome")
    base_args = ["--disable-blink-features=AutomationControlled"]
    if mode in ("1", "true", "yes"):
        headless = True
        launch_args = base_args
    elif mode == "new":
        headless = False
        launch_args = base_args + ["--headless=new", "--window-size=1400,900"]
    else:
        headless = False
        launch_args = base_args + ["--start-minimized"]
    results = {}
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel=channel, headless=headless, args=launch_args)
        except Exception:
            # Chrome未検出時は同梱Chromiumにフォールバック
            browser = pw.chromium.launch(headless=headless, args=launch_args)
        ctx = browser.new_context(locale="ja-JP", user_agent=UA,
                                  viewport={"width": 1400, "height": 900})
        # ヘッドレス/自動化の痕跡を軽く隠す(公開ページ閲覧の範囲)
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'languages',{get:()=>['ja-JP','ja']});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
            "window.chrome=window.chrome||{runtime:{}};"
        )
        page = ctx.new_page()
        page.set_default_timeout(45000)
        def wait_or_busy(fn):
            try:
                fn()
            except PWTimeout:
                if _is_busy(page):
                    raise BusyError()
                raise

        try:
            page.goto(WELCOME_URL, wait_until="domcontentloaded")
            if _is_busy(page):
                raise BusyError()
            # 公開検索パネルを開く(best effort, 遷移なし)
            page.evaluate("""() => { const a=[...document.querySelectorAll('a')]
                .find(e=>/ログインせずに空き状況を検索/.test(e.textContent)); if(a) a.click(); }""")
            page.wait_for_timeout(random.randint(700, 1400))

            # 1) 検索条件(地区・空き照会・利用目的・スポーツ・バドミントン)
            page.evaluate(JS_SELECT_CONDITIONS, REGIONS)
            page.wait_for_timeout(random.randint(700, 1400))
            # 2) 選択した条件で次へ → 施設一覧へ
            _click_text(page, "選択した条件で次へ")
            wait_or_busy(lambda: page.wait_for_selector('input[name="checkMeisaiUniqKey"]', timeout=45000))
            # 3) 対象施設を選択
            n = page.evaluate(JS_SELECT_FACILITIES, KEYS)
            if n < len(KEYS):
                if _is_busy(page):
                    raise BusyError()
                raise RuntimeError(f"facility select mismatch: {n}/{len(KEYS)}")
            page.wait_for_timeout(random.randint(700, 1400))
            # 4) 選択した施設で検索 → 空き照会へ
            _click_text(page, "選択した施設で検索")
            wait_or_busy(lambda: page.wait_for_selector('#startDate', timeout=90000))

            # 5) 31日間+曜日絞込(火/木/土/日/土日祝)。曜日絞込により31列が約2か月分に広がり、
            #    今日開始の1回だけで「今月+翌月」を丸ごとカバーできる(2枚目は不要)。
            start_iso = start1
            page.evaluate(JS_SET_CONTROLS, [start_iso, KEEP_DAYS])
            page.wait_for_timeout(5000)  # モーダル表示待ち
            # 確認モーダルが出れば「選択した条件で表示する」を押す(出ない構成でも動くよう任意扱い)
            try:
                confirm = page.locator("button").filter(has_text="選択した条件で表示する").first
                confirm.wait_for(state="visible", timeout=80000)
                confirm.click()
            except PWTimeout:
                pass
            # グリッド描画(31日間+曜日絞込)完了まで待つ
            wait_or_busy(lambda: page.wait_for_function(JS_WINDOW_READY, arg=start_iso, timeout=60000))
            rows = page.evaluate(JS_EXTRACT, [ty, tm])
            for r in rows:
                results[r["key"]] = r
        except Exception:
            _save_debug(page)
            browser.close()
            raise
        browser.close()

    if not results:
        raise RuntimeError("no target slots parsed")

    slots = [r for r in results.values() if date.fromisoformat(r["iso"]) <= cap]
    return slots


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def save_state(slots):
    data = {"updatedAt": datetime.now(JST).isoformat(),
            "slots": {r["key"]: r["avail"] for r in slots}}
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=0)


def _load_local_secrets():
    """ローカル実行用: 同フォルダの secrets.json があれば環境変数に反映。"""
    p = os.path.join(HERE, "secrets.json")
    if not os.path.exists(p):
        return
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
        for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "NOTIFY_TO"):
            if d.get(k) and not os.environ.get(k):
                os.environ[k] = str(d[k])
    except Exception:
        pass


def send_email(subject, body):
    addr = os.environ["GMAIL_ADDRESS"]
    pw = os.environ["GMAIL_APP_PASSWORD"]
    raw_to = os.environ.get("NOTIFY_TO") or addr
    to_list = [t.strip() for t in raw_to.split(",") if t.strip()]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("体育館空き通知", "utf-8")), addr))
    msg["To"] = ", ".join(to_list)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(addr, pw)
        s.sendmail(addr, to_list, msg.as_string())


def main():
    _load_local_secrets()
    now = datetime.now(JST)
    if now.hour < 5:  # 00:00-05:00 JST は何もしない
        print(f"[skip] quiet hours (JST {now:%H:%M})")
        return 0

    slots = None
    attempts = 4
    for i in range(1, attempts + 1):
        try:
            slots = scrape()
            break
        except BusyError:
            log(f"site busy page returned (attempt {i}/{attempts})")
            if i < attempts:
                time.sleep(random.randint(25, 55))
        except Exception:
            log("ERROR scrape failed; no notify, state kept\n" + traceback.format_exc())
            return 0  # サイト一時不調でもワークフローを赤にしない/状態は保持
    if slots is None:
        log("site busy: gave up this run (データセンターIPが弾かれている可能性)。通知なし・状態保持")
        return 0

    prev = load_state()
    avail_now = [r for r in slots if r["avail"] > 0]

    if prev is None or not prev.get("slots"):
        save_state(slots)
        log(f"baseline slots={len(slots)} avail={len(avail_now)} notified=0")
        return 0

    prev_slots = prev["slots"]
    notified = 0
    for r in slots:
        if r["avail"] >= 1 and prev_slots.get(r["key"]) == 0:
            display = H3_TO_DISPLAY.get(r["h3"], r["h3"])
            subject = f"{display}/{r['jp']} {r['time']}"
            body = (f"体育館の空きが出ました。\n\n"
                    f"施設: {display}\n"
                    f"空き日時: {r['jp']} {r['time']}\n"
                    f"空きコート数: {r['avail']}/{r['tot']}\n"
                    f"照会URL: {WELCOME_URL}\n")
            try:
                send_email(subject, body)
                notified += 1
            except Exception:
                log("ERROR email failed: " + subject + "\n" + traceback.format_exc())

    save_state(slots)
    log(f"slots={len(slots)} avail={len(avail_now)} notified={notified}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
