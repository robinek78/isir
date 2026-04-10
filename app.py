"""
Hlídač insolvence — webová aplikace + automatický monitor
Podporuje IČO (právnické osoby) i RČ (fyzické osoby).
Scraping HTML lustrace z isir.justice.cz.
"""

import os, json, time, smtplib, logging, threading, requests, schedule, re
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from flask import Flask, jsonify, request, render_template_string
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

GMAIL_USER     = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")
NOTIFY_EMAIL   = os.environ.get("NOTIFY_EMAIL", "robert@sedlacek.rs")
CHECK_HOUR     = int(os.environ.get("CHECK_HOUR", "7"))
CHECK_DAY      = os.environ.get("CHECK_DAY", "monday")  # monday/tuesday/.../sunday nebo "daily"
DATA_FILE      = Path("data.json")

ISIR_BASE   = "https://isir.justice.cz/isir/ueu/vysledek_lustrace.do"
ISIR_ICO    = ISIR_BASE + "?ic={ico}&aktualnost=AKTUALNI_I_UKONCENA"
ISIR_RC     = ISIR_BASE + "?rc={rc}&aktualnost=AKTUALNI_I_UKONCENA"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "cs-CZ,cs;q=0.9",
}

app = Flask(__name__)
_lock = threading.Lock()

# --- Datová vrstva ---
def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"subjects": [], "results": {}, "known": {}, "last_check": None}

def save_data(d: dict):
    DATA_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2))

# --- Validace ---
def validate_ico(ico: str) -> str | None:
    """Vrátí normalizované IČO nebo None."""
    ico = ico.strip().replace(" ", "").replace("/", "")
    if re.fullmatch(r'\d{6,8}', ico):
        return ico.zfill(8)
    return None

def validate_rc(rc: str) -> str | None:
    """Vrátí normalizované RČ (bez lomítka, 9-10 číslic) nebo None."""
    rc = rc.strip().replace(" ", "").replace("/", "")
    if re.fullmatch(r'\d{9,10}', rc):
        return rc
    return None

def subject_key(s: dict) -> str:
    """Unikátní klíč subjektu pro ukládání výsledků."""
    return s.get("ico") or s.get("rc") or s["nazev"]

# --- ISIR scraping ---
def fetch_isir(s: dict) -> dict:
    """Načte lustrace stránku pro IČO nebo RČ."""
    if s.get("ico"):
        url = ISIR_ICO.format(ico=s["ico"])
        ident = f"IČO {s['ico']}"
    else:
        url = ISIR_RC.format(rc=s["rc"])
        ident = f"RČ {s['rc'][:6]}/***"  # maskujeme v logu
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        result = parse_lustrace(soup, url)
        log.info(f"    {ident}: {len(result.get('rizeni',[]))} řízení")
        return result
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": str(e)}

def parse_lustrace(soup: BeautifulSoup, url: str) -> dict:
    """Parsuje HTML lustrace stránky ISIR."""
    text = soup.get_text()
    zadny = any(p in text for p in [
        "Nic nenalezeno", "nebyl nalezen", "nebyly nalezeny",
        "žádné záznamy", "nenalezeny žádné", "Nebyly nalezeny"
    ])

    rizeni = []
    tables = soup.find_all("table")
    result_table = None
    for t in tables:
        headers = [th.get_text(strip=True) for th in t.find_all("th")]
        if any(kw in " ".join(headers) for kw in ["Spisová", "Soud", "Stav", "znač", "Dlužník"]):
            result_table = t
            break

    if zadny and not result_table:
        return {"ok": True, "rizeni": [], "url": url}

    if result_table:
        rows = result_table.find_all("tr")
        header_cells = []
        if rows:
            header_cells = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        for row in rows[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if not cells or len(cells) < 2:
                continue
            rz = {}
            if header_cells:
                for i, h in enumerate(header_cells):
                    if i >= len(cells):
                        break
                    h_l = h.lower()
                    v = cells[i].strip()
                    if not v:
                        continue
                    if "znač" in h_l or "spis" in h_l:
                        rz["spz"] = v
                    elif "soud" in h_l:
                        rz["soud"] = v
                    elif "stav" in h_l:
                        rz["stav"] = v
                    elif "druh" in h_l or "typ" in h_l:
                        rz["druh"] = v
                    elif "datum" in h_l or "zah" in h_l:
                        rz["datum"] = v
                    elif "dlužník" in h_l or "název" in h_l or "jméno" in h_l or "příjmení" in h_l:
                        rz["dluznik"] = v
            else:
                rz = {"spz": cells[0], "soud": cells[1] if len(cells)>1 else None,
                      "stav": cells[2] if len(cells)>2 else None}

            link_tag = row.find("a", href=True)
            if link_tag:
                href = link_tag["href"]
                rz["isir_url"] = href if href.startswith("http") else "https://isir.justice.cz" + href
            else:
                rz["isir_url"] = url

            if any(v for k, v in rz.items() if k != "isir_url" and v):
                rizeni.append(rz)

    # Fallback: hledej spisové značky v textu
    if not result_table and not zadny:
        for spz in set(re.findall(r'[A-Z]{2,5}\s*\d+\s*INS\s*\d+/\d{4}', text)):
            rizeni.append({"spz": spz.strip(), "isir_url": url})

    return {"ok": True, "rizeni": rizeni, "url": url}

# --- Kontrola ---
def run_check(notify=True):
    log.info("=== Spouštím kontrolu ISIR ===")
    with _lock:
        d = load_data()

    nove = []
    results_new = {}

    for s in d["subjects"]:
        key = subject_key(s)
        log.info(f"  {s['nazev']} ({key})")
        res = fetch_isir(s)
        ts = datetime.now().isoformat()

        if not res["ok"]:
            results_new[key] = {"status": "error", "error": res["error"], "ts": ts, "has_rizeni": False, "rizeni": []}
            time.sleep(1)
            continue

        rizeni = res["rizeni"]
        has = len(rizeni) > 0
        results_new[key] = {"status": "ok", "has_rizeni": has, "rizeni": rizeni, "ts": ts, "url": res.get("url", "")}

        if has and notify:
            znacky = sorted(set(r.get("spz") or str(i) for i, r in enumerate(rizeni)))
            klic = ",".join(znacky)
            if d["known"].get(key) != klic:
                nove.append({"key": key, "nazev": s["nazev"], "rizeni": rizeni,
                             "url": res.get("url", "")})
                d["known"][key] = klic
                log.info(f"    NOVÝ nález ({len(rizeni)} řízení)")
            else:
                log.info(f"    Již známé řízení")
        elif not has:
            d["known"].pop(key, None)

        time.sleep(1.5)

    with _lock:
        d2 = load_data()
        d2["results"] = results_new
        d2["last_check"] = datetime.now().isoformat()
        d2["known"] = d["known"]
        save_data(d2)

    if nove and notify:
        send_email(nove)
    log.info("=== Kontrola dokončena ===")

def send_email(nalezene):
    if not GMAIL_USER or not GMAIL_APP_PASS:
        log.error("Gmail není nakonfigurován")
        return
    datum = datetime.now().strftime("%d. %m. %Y")
    predmet = f"⚠️ Hlídač insolvence: nový nález ({len(nalezene)} subjektů) — {datum}"
    radky = [f"Hlídač insolvence — {datum}\n"]
    for n in nalezene:
        radky += [f"\n{'─'*50}", f"SUBJEKT: {n['nazev']}", f"Odkaz:   {n['url']}\n"]
        for i, rz in enumerate(n["rizeni"], 1):
            radky.append(f"  Řízení č. {i}:")
            for k, v in [("Spisová značka", rz.get("spz")), ("Soud", rz.get("soud")),
                         ("Stav", rz.get("stav")), ("Datum zahájení", rz.get("datum")),
                         ("Dlužník", rz.get("dluznik"))]:
                if v: radky.append(f"    {(k+':'):<18} {v}")
            radky.append("")
    msg = MIMEMultipart()
    msg["Subject"] = predmet
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_EMAIL
    msg.attach(MIMEText("\n".join(radky), "plain", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASS)
            smtp.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())
        log.info(f"Email odeslán → {NOTIFY_EMAIL}")
    except Exception as e:
        log.error(f"Chyba emailu: {e}")

# --- REST API ---
@app.route("/api/subjects", methods=["GET"])
def api_get():
    with _lock: d = load_data()
    return jsonify(d["subjects"])

@app.route("/api/subjects", methods=["POST"])
def api_add():
    body = request.get_json()
    nazev = str(body.get("nazev", "")).strip()
    typ = body.get("typ", "ico")  # "ico" nebo "rc"
    raw = str(body.get("hodnota", "")).strip()

    if typ == "ico":
        ico = validate_ico(raw)
        if not ico:
            return jsonify({"error": "Neplatné IČO — musí mít 6–8 číslic."}), 400
        key = ico
        subjekt = {"ico": ico, "nazev": nazev or f"IČO {ico}"}
    else:
        rc = validate_rc(raw)
        if not rc:
            return jsonify({"error": "Neplatné RČ — zadejte 9–10 číslic bez lomítka nebo s lomítkem."}), 400
        key = rc
        subjekt = {"rc": rc, "nazev": nazev or f"FO {rc[:6]}/{'*'*(len(rc)-6)}"}

    with _lock:
        d = load_data()
        if any(subject_key(s) == key for s in d["subjects"]):
            return jsonify({"error": "Tento subjekt již sledujete."}), 409
        d["subjects"].append(subjekt)
        save_data(d)
    return jsonify({"ok": True})

@app.route("/api/subjects/<path:key>", methods=["DELETE"])
def api_del(key):
    with _lock:
        d = load_data()
        d["subjects"] = [s for s in d["subjects"] if subject_key(s) != key]
        d["results"].pop(key, None)
        d["known"].pop(key, None)
        save_data(d)
    return jsonify({"ok": True})

@app.route("/api/results", methods=["GET"])
def api_results():
    with _lock: d = load_data()
    return jsonify({"results": d["results"], "last_check": d["last_check"]})

@app.route("/api/check", methods=["POST"])
def api_check():
    threading.Thread(target=run_check, daemon=True).start()
    return jsonify({"ok": True})

# --- Frontend ---
HTML = r"""<!DOCTYPE html>
<html lang="cs">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hlídač insolvence</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#f4f3ef;--surface:#fff;--s2:#eeede8;--border:#dddbd2;--text:#181816;--muted:#7a7970;--ok:#2d6a4f;--ok-bg:#d8f3dc;--warn:#9b2226;--warn-bg:#fde8e8;--info:#1d4e89;--info-bg:#dbeafe;--mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--sans);background:var(--bg);color:var(--text);min-height:100vh}
header{background:var(--surface);border-bottom:1px solid var(--border);padding:0 2rem;height:54px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}
.logo{font-family:var(--mono);font-size:14px;font-weight:500;letter-spacing:-.02em}
.logo em{opacity:.3;font-style:normal}
.header-r{display:flex;align-items:center;gap:12px}
.last-check{font-family:var(--mono);font-size:11px;color:var(--muted)}
button{font-family:var(--mono);font-size:12px;padding:6px 12px;border:1px solid var(--border);border-radius:3px;background:var(--surface);color:var(--text);cursor:pointer;transition:background .12s}
button:hover{background:var(--s2)} button:disabled{opacity:.4;cursor:not-allowed}
button.primary{background:var(--text);color:var(--surface);border-color:var(--text)}
button.primary:hover{background:#333}
main{max-width:860px;margin:0 auto;padding:2rem 1.5rem;display:grid;grid-template-columns:300px 1fr;gap:1.5rem;align-items:start}
@media(max-width:680px){main{grid-template-columns:1fr}}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:4px}
.panel-head{padding:10px 16px;border-bottom:1px solid var(--border);font-family:var(--mono);font-size:10px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);display:flex;justify-content:space-between;align-items:center}
.panel-body{padding:16px}
.field-label{font-size:11px;color:var(--muted);font-family:var(--mono);margin-bottom:4px;margin-top:10px}
.field-label:first-child{margin-top:0}
input[type=text]{font-family:var(--mono);font-size:13px;border:1px solid var(--border);border-radius:3px;padding:8px 10px;background:var(--s2);color:var(--text);width:100%;transition:border-color .15s}
input[type=text]:focus{outline:none;border-color:var(--text)}
.seg{display:flex;gap:0;margin-bottom:0;border:1px solid var(--border);border-radius:3px;overflow:hidden}
.seg button{flex:1;border:none;border-radius:0;border-right:1px solid var(--border);padding:7px 6px;font-size:12px;background:var(--s2);color:var(--muted)}
.seg button:last-child{border-right:none}
.seg button.active{background:var(--text);color:var(--surface)}
.seg button:hover:not(.active){background:var(--border)}
.add-btn{width:100%;margin-top:10px;padding:9px}
.err{font-family:var(--mono);font-size:11px;color:var(--warn);margin-top:6px;min-height:16px}
.hint{font-family:var(--mono);font-size:10px;color:var(--muted);margin-top:3px}
.tags-section{margin-top:14px;padding-top:14px;border-top:1px solid var(--border)}
.tags-title{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px}
.tag{display:flex;align-items:center;justify-content:space-between;padding:7px 10px;border:1px solid var(--border);border-radius:3px;margin-bottom:5px;background:var(--s2)}
.tag-left{display:flex;align-items:center;gap:8px}
.tag-typ{font-family:var(--mono);font-size:9px;font-weight:500;padding:2px 5px;border-radius:2px;background:var(--border);color:var(--muted);flex-shrink:0}
.tag-typ.ico{background:var(--info-bg);color:var(--info)}
.tag-typ.rc{background:#f3e8ff;color:#6b21a8}
.tag-name{font-size:13px;font-weight:500}
.tag-val{font-family:var(--mono);font-size:11px;color:var(--muted)}
.tag-del{background:none;border:none;padding:0 2px;font-size:16px;color:var(--muted);cursor:pointer;line-height:1}
.tag-del:hover{color:var(--warn);background:none}
.empty{font-family:var(--mono);font-size:12px;color:var(--muted);text-align:center;padding:1.5rem 0}
.ri{padding:16px 0;border-bottom:1px solid var(--border)}
.ri:last-child{border-bottom:none}
.ri-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.ri-name{font-size:15px;font-weight:600}
.ri-meta{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:3px}
.badge{font-family:var(--mono);font-size:10px;font-weight:500;padding:3px 9px;border-radius:3px;white-space:nowrap;flex-shrink:0}
.b-ok{background:var(--ok-bg);color:var(--ok)} .b-warn{background:var(--warn-bg);color:var(--warn)} .b-muted{background:var(--s2);color:var(--muted)}
.rzbox{margin-top:10px;border:1px solid #f09595;border-radius:3px;overflow:hidden}
.rzhead{background:var(--warn-bg);padding:7px 12px;font-family:var(--mono);font-size:10px;font-weight:600;color:var(--warn);text-transform:uppercase;letter-spacing:.07em;border-bottom:1px solid #f7c1c1}
.rzt{width:100%;font-size:12px;border-collapse:collapse}
.rzt td{padding:5px 12px;border-bottom:1px solid #fde8e8}
.rzt tr:last-child td{border-bottom:none}
.rzt .rk{color:var(--muted);width:38%;font-family:var(--mono);font-size:11px}
.rzt .rv{color:var(--text);font-weight:500;word-break:break-word}
.rzlink{font-family:var(--mono);font-size:11px;padding:6px 12px;display:block;color:var(--info);text-decoration:none;border-top:1px solid #fde8e8;background:#fff9f9}
.rzlink:hover{text-decoration:underline}
.lustr{display:block;margin-top:8px;font-family:var(--mono);font-size:11px;color:var(--info);text-decoration:none}
.lustr:hover{text-decoration:underline}
.no-rz{font-family:var(--mono);font-size:12px;color:var(--muted);margin-top:8px}
.prog{height:2px;background:var(--border);overflow:hidden;margin:0 16px}
.prog-fill{height:100%;background:var(--text);transition:width .4s;width:0}
.checking{font-family:var(--mono);font-size:11px;color:var(--muted);text-align:center;padding:8px 16px 12px}
.results-body{padding:0 16px}
</style>
</head>
<body>
<header>
  <div class="logo">hlídač<em>/</em>insolvence</div>
  <div class="header-r">
    <span class="last-check" id="lc"></span>
    <button id="btnNow" onclick="checkNow()">Zkontrolovat nyní</button>
  </div>
</header>
<main>
  <aside>
    <div class="panel">
      <div class="panel-head">Přidat subjekt</div>
      <div class="panel-body">
        <div class="field-label">Typ subjektu</div>
        <div class="seg">
          <button class="active" id="btnIco" onclick="setTyp('ico')">Firma / IČO</button>
          <button id="btnRc" onclick="setTyp('rc')">Fyzická osoba / RČ</button>
        </div>
        <div class="field-label" id="hodnotaLabel">IČO</div>
        <input type="text" id="hodnotaIn" placeholder="např. 27082440" maxlength="11" onkeydown="if(event.key==='Enter')add()">
        <div class="hint" id="hodnotaHint">6–8 číslic</div>
        <div class="field-label">Název / jméno (volitelné)</div>
        <input type="text" id="nameIn" placeholder="Název nebo jméno" onkeydown="if(event.key==='Enter')add()">
        <button class="add-btn primary" onclick="add()">+ Přidat do sledování</button>
        <div class="err" id="err"></div>
        <div class="tags-section">
          <div class="tags-title">Sledované subjekty</div>
          <div id="tags"></div>
        </div>
      </div>
    </div>
  </aside>
  <section>
    <div class="panel">
      <div class="panel-head"><span>Výsledky</span><span id="sb"></span></div>
      <div id="progWrap" style="display:none"><div class="prog"><div class="prog-fill" id="pf"></div></div><div class="checking">Probíhá kontrola…</div></div>
      <div class="results-body" id="rl"></div>
    </div>
  </section>
</main>
<script>
let subjects=[],results={},lastCheck=null,checking=false,typ='ico';

function setTyp(t){
  typ=t;
  document.getElementById('btnIco').classList.toggle('active',t==='ico');
  document.getElementById('btnRc').classList.toggle('active',t==='rc');
  document.getElementById('hodnotaLabel').textContent=t==='ico'?'IČO':'Rodné číslo';
  document.getElementById('hodnotaIn').placeholder=t==='ico'?'např. 27082440':'např. 8001011234 nebo 800101/1234';
  document.getElementById('hodnotaIn').maxLength=t==='ico'?8:11;
  document.getElementById('hodnotaHint').textContent=t==='ico'?'6–8 číslic':'9–10 číslic, lomítko se doplní automaticky';
}

async function api(m,p,b){const o={method:m,headers:{'Content-Type':'application/json'}};if(b)o.body=JSON.stringify(b);return (await fetch(p,o)).json();}
async function load(){const [s,r]=await Promise.all([api('GET','/api/subjects'),api('GET','/api/results')]);subjects=s;results=r.results||{};lastCheck=r.last_check;render();}

function subjectKey(s){return s.ico||s.rc||s.nazev;}

async function add(){
  const raw=document.getElementById('hodnotaIn').value.trim();
  const nazev=document.getElementById('nameIn').value.trim();
  const e=document.getElementById('err');e.textContent='';
  const r=await api('POST','/api/subjects',{typ,hodnota:raw,nazev});
  if(r.error){e.textContent=r.error;return;}
  document.getElementById('hodnotaIn').value='';document.getElementById('nameIn').value='';
  await load();
}

async function del(key){
  if(!confirm('Odebrat ze sledování?'))return;
  await api('DELETE','/api/subjects/'+encodeURIComponent(key));await load();
}

async function checkNow(){
  if(checking)return;checking=true;
  const btn=document.getElementById('btnNow');btn.disabled=true;btn.textContent='Kontroluji…';
  document.getElementById('progWrap').style.display='block';
  const prev=lastCheck;
  await api('POST','/api/check');
  let t=0;
  const iv=setInterval(async()=>{
    t+=2;document.getElementById('pf').style.width=Math.min(88,t/(subjects.length*3+4)*100)+'%';
    const r=await api('GET','/api/results');
    if(r.last_check&&r.last_check!==prev){
      clearInterval(iv);results=r.results||{};lastCheck=r.last_check;
      checking=false;btn.disabled=false;btn.textContent='Zkontrolovat nyní';
      document.getElementById('progWrap').style.display='none';
      document.getElementById('pf').style.width='0';render();
    }
    if(t>300){clearInterval(iv);checking=false;btn.disabled=false;btn.textContent='Zkontrolovat nyní';}
  },2000);
}

function ts(iso){try{return new Date(iso).toLocaleString('cs-CZ');}catch{return iso;}}

function render(){
  document.getElementById('lc').textContent=lastCheck?'Poslední kontrola: '+ts(lastCheck):'';
  // Tags
  const tg=document.getElementById('tags');
  if(!subjects.length){tg.innerHTML='<div class="empty">Zatím žádné subjekty</div>';}
  else tg.innerHTML=subjects.map(s=>{
    const key=subjectKey(s);
    const isRc=!!s.rc;
    const typLabel=isRc?'RČ':'IČO';
    const val=isRc?(s.rc.slice(0,6)+'/'+s.rc.slice(6)):s.ico;
    return`<div class="tag">
      <div class="tag-left">
        <span class="tag-typ ${isRc?'rc':'ico'}">${typLabel}</span>
        <div><div class="tag-name">${s.nazev}</div><div class="tag-val">${val}</div></div>
      </div>
      <button class="tag-del" onclick="del('${key}')">×</button>
    </div>`;
  }).join('');
  // Results
  const rl=document.getElementById('rl');
  if(!subjects.length){rl.innerHTML='<div class="empty" style="padding:2rem 0">Přidejte subjekt a spusťte kontrolu.</div>';document.getElementById('sb').innerHTML='';return;}
  let wc=0;
  rl.innerHTML=subjects.map(s=>{
    const key=subjectKey(s);
    const r=results[key];
    const isRc=!!s.rc;
    const val=isRc?(s.rc.slice(0,6)+'/'+s.rc.slice(6)):s.ico;
    const meta=(isRc?'RČ: ':'IČO: ')+val+(r&&r.ts?' · '+ts(r.ts):'');
    const lustrUrl=isRc
      ?`https://isir.justice.cz/isir/ueu/vysledek_lustrace.do?rc=${s.rc}&aktualnost=AKTUALNI_I_UKONCENA`
      :`https://isir.justice.cz/isir/ueu/vysledek_lustrace.do?ic=${s.ico}&aktualnost=AKTUALNI_I_UKONCENA`;
    if(!r)return row(s,meta,'<span class="badge b-muted">Nekontrolováno</span>','');
    if(r.status==='error')return row(s,meta,'<span class="badge b-warn">Chyba</span>',
      `<div class="no-rz">${r.error}</div><a class="lustr" href="${lustrUrl}" target="_blank">Ověřit ručně v ISIR →</a>`);
    if(r.has_rizeni)wc++;
    const badge=r.has_rizeni?'<span class="badge b-warn">Řízení zahájeno</span>':'<span class="badge b-ok">Bez řízení</span>';
    const det=r.has_rizeni?(r.rizeni||[]).map(rz=>rzbox(rz)).join(''):'<div class="no-rz">Žádný záznam v insolvenčním rejstříku.</div>';
    return row(s,meta,badge,det+`<a class="lustr" href="${lustrUrl}" target="_blank">Otevřít lustraci v ISIR →</a>`);
  }).join('');
  document.getElementById('sb').innerHTML=wc?`<span class="badge b-warn">${wc} s řízením</span>`:'';
}

function row(s,meta,badge,det){
  return`<div class="ri"><div class="ri-top"><div><div class="ri-name">${s.nazev}</div><div class="ri-meta">${meta}</div></div>${badge}</div>${det}</div>`;
}
function rzbox(rz){
  const fields=[['Spisová značka',rz.spz],['Soud',rz.soud],['Stav řízení',rz.stav],['Druh řízení',rz.druh],['Datum zahájení',rz.datum],['Dlužník',rz.dluznik]].filter(([,v])=>v);
  const link=rz.isir_url||'https://isir.justice.cz/';
  return`<div class="rzbox"><div class="rzhead">Insolvenční řízení</div><table class="rzt">${fields.map(([k,v])=>`<tr><td class="rk">${k}</td><td class="rv">${v}</td></tr>`).join('')}</table><a class="rzlink" href="${link}" target="_blank">Zobrazit v insolvenčním rejstříku →</a></div>`;
}
load();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

def scheduler_thread():
    days = {"monday":"pondělí","tuesday":"úterý","wednesday":"středa","thursday":"čtvrtek","friday":"pátek","saturday":"sobota","sunday":"neděle","daily":"každý den"}
    log.info(f"Scheduler spuštěn — kontrola: {days.get(CHECK_DAY, CHECK_DAY)} v {CHECK_HOUR:02d}:00")
    if CHECK_DAY == "daily":
        schedule.every().day.at(f"{CHECK_HOUR:02d}:00").do(run_check)
    else:
        getattr(schedule.every(), CHECK_DAY).at(f"{CHECK_HOUR:02d}:00").do(run_check)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=run_check, daemon=True).start()
    threading.Thread(target=scheduler_thread, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
