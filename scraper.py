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
  NOTIFY_TO           通知先アドレス(未指定なら GMAIL_ADDRESS)
"""
import os
import sys
import json
import smtplib
import traceback
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

JST = ZoneInfo("Asia/Tokyo")
WELCOME_URL = "https://g-kyoto.growone.net/eshisetsu/menu/Welcome.cgi"
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
LOG_FILE = os.path.join(os.path.dirname(__file__), "log.txt")

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

JS_CLICK_TEXT = """
(re) => {
  const rx=new RegExp(re);
  const el=[...document.querySelectorAll('button,a')].find(e=>rx.test(e.textContent));
  if(el){ el.click(); return true; } return false;
}
"""

JS_SET_WINDOW = """
(iso) => {
  const t31=[...document.querySelectorAll('button,a,label,span,div')].find(e=>e.textContent.trim()==='31日間');
  if(t31) t31.click();
  const di=document.getElementById('startDate');
  const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
  if(di){ s.call(di, iso); di.dispatchEvent(new Event('change',{bubbles:true})); }
  const b=document.querySelector('button.btn_sort'); if(b) b.click();
  return true;
}
"""

JS_EXTRACT = """
(args) => {
  const ty=args[0], tm=args[1];
  const S={'icn_scche_ok.png':'空き','icn_scche_noset.png':'予約済','icn_scche_haifun.png':'利用不可'};
  const dw=d=>{const m=d.match(/(月|火|水|木|金|土|日)/);return m?m[1]:'';};
  const p=n=>String(n).padStart(2,'0');
  const gs=[...document.querySelectorAll('table.box_calendar')];
  const a={};
  gs.forEach(t=>{
    let h3='';
    let c=t.closest('ul.box_schedule')?t.closest('ul.box_schedule').parentElement:t.parentElement;
    while(c){let s2=c.previousElementSibling;while(s2){if(s2.tagName==='H3'){h3=s2.innerText.replace(/\\s+/g,' ').trim();break;}s2=s2.previousElementSibling;}if(h3)break;c=c.parentElement;}
    const tr=[...t.querySelectorAll('tr')]; if(!tr.length) return;
    const hd=[...tr[0].querySelectorAll('th,td')].map(x=>x.innerText.replace(/\\s+/g,' ').trim());
    const ds=hd.slice(1);
    tr.slice(1).forEach(r=>{
      const cs=[...r.querySelectorAll('th,td')];
      const tmr=cs[0].innerText.replace(/\\s+/g,' ').trim();
      const m=tmr.match(/(\\d{2}:\\d{2})\\s*-\\s*(\\d{2}:\\d{2})/); if(!m) return;
      const st=m[1], ti=m[1]+'-'+m[2];
      cs.slice(1).forEach((cc,i)=>{
        const dstr=ds[i]; if(!dstr) return; const w=dw(dstr);
        let k=false; if(w==='土'||w==='日')k=true; else if(w==='木')k=(st==='19:00'||st==='20:00');
        if(!k) return;
        const dm=dstr.match(/(\\d+)月(\\d+)日/); if(!dm) return;
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


def scrape():
    """Return list of aggregated slot dicts, or raise on failure."""
    now = datetime.now(JST)
    ty, tm = now.year, now.month
    today = now.date()
    start1 = today.isoformat()
    start2 = (today + timedelta(days=31)).isoformat()
    cap = end_of_next_month(today)

    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(locale="ja-JP",
                                  user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                                              "Chrome/125.0 Safari/537.36"))
        page = ctx.new_page()
        page.set_default_timeout(30000)

        page.goto(WELCOME_URL, wait_until="networkidle")
        # reveal the public search panel (best effort)
        page.evaluate("""() => { const a=[...document.querySelectorAll('a')]
            .find(e=>/ログインせずに空き状況を検索/.test(e.textContent)); if(a) a.click(); }""")
        page.wait_for_timeout(500)

        # 1) search conditions
        page.evaluate(JS_SELECT_CONDITIONS, REGIONS)
        # 2) 選択した条件で次へ  (navigates)
        with page.expect_navigation(wait_until="networkidle"):
            page.evaluate(JS_CLICK_TEXT, "選択した条件で次へ")
        # 3) facility list
        page.wait_for_selector('input[name="checkMeisaiUniqKey"]', timeout=30000)
        n = page.evaluate(JS_SELECT_FACILITIES, KEYS)
        if n < len(KEYS):
            raise RuntimeError(f"facility select mismatch: {n}/{len(KEYS)}")
        # 4) 選択した施設で検索 (navigates)
        with page.expect_navigation(wait_until="networkidle"):
            page.evaluate(JS_CLICK_TEXT, "選択した施設で検索")
        page.wait_for_selector('#startDate', timeout=30000)

        # 5) two 31-day windows
        for start_iso in (start1, start2):
            with page.expect_navigation(wait_until="networkidle"):
                page.evaluate(JS_SET_WINDOW, start_iso)
            page.wait_for_selector('table.box_calendar', timeout=30000)
            rows = page.evaluate(JS_EXTRACT, [ty, tm])
            for r in rows:
                results[r["key"]] = r  # windows are disjoint by date

        browser.close()

    if not results:
        raise RuntimeError("no grids parsed (site layout changed or blocked?)")

    # cap to current + next month
    slots = [r for r in results.values()
             if date.fromisoformat(r["iso"]) <= cap]
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


def send_email(subject, body):
    addr = os.environ["GMAIL_ADDRESS"]
    pw = os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("NOTIFY_TO", addr)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("体育館空き通知", "utf-8")), addr))
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(addr, pw)
        s.sendmail(addr, [to], msg.as_string())


def main():
    now = datetime.now(JST)
    # quiet hours 00:00-05:00 JST -> do nothing
    if now.hour < 5:
        print(f"[skip] quiet hours (JST {now:%H:%M})")
        return 0

    try:
        slots = scrape()
    except Exception:
        log("ERROR scrape failed; no notify, state kept\n" + traceback.format_exc())
        # exit 0 so the workflow doesn't mark red for transient site issues;
        # state is intentionally NOT overwritten.
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
