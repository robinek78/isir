"""
Hlídač insolvence — webová aplikace + automatický monitor
Flask server: vizuální přehled + REST API pro správu IČO
Scheduler: každý den zkontroluje ISIR a pošle email při nálezu
"""

import os, json, time, smtplib, logging, threading, requests, schedule
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from flask import Flask, jsonify, request, render_template_string

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# --- Konfigurace (nastavte v Render.com jako Environment Variables) ---
GMAIL_USER     = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASS = os.environ.get("GMAIL_APP_PASS", "")
NOTIFY_EMAIL   = os.environ.get("NOTIFY_EMAIL", "robert@sedlacek.rs")
CHECK_HOUR     = int(os.environ.get("CHECK_HOUR", "7"))
DATA_FILE      = Path("data.json")

ISIR_API = "https://isir.justice.cz/isir/common/rest/rizeni?ic={ico}"
ISIR_WEB = "https://isir.justice.cz/isir/ueu/vysledek_lustrace.do?ic={ico}&aktualnost=AKTUALNI_I_UKONCENA"

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

# --- ISIR logika ---
def fetch_isir(ico: str) -> dict:
    try:
        r = requests.get(ISIR_API.format(ico=ico), headers={"Accept": "application/json"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            rizeni = data
        elif isinstance(data, dict):
            rizeni = data.get("rizeni") or data.get("items") or []
            if not rizeni and (data.get("spisovaZnacka") or data.get("stav")):
                rizeni = [data]
        else:
            rizeni = []
        return {"ok": True, "rizeni": rizeni}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def fmt_date(v):
    if not v: return None
    try: return datetime.fromisoformat(v[:10]).strftime("%d. %m. %Y")
    except: return v

def parse_rizeni(r: dict) -> dict:
    spz = r.get("spisovaZnacka") or r.get("spZn") or r.get("cisloJednaci") or ""
    dluznik = r.get("dluznik")
    if not dluznik and r.get("dluznici"):
        d0 = r["dluznici"][0]
        dluznik = d0.get("nazev") or " ".join(filter(None,[d0.get("jmeno"), d0.get("prijmeni")])) or None
    spravce = r.get("insolvencniSpravce")
    if not spravce and r.get("spravci"):
        s0 = r["spravci"][0]
        spravce = s0.get("nazev") or " ".join(filter(None,[s0.get("jmeno"), s0.get("prijmeni")])) or None
    return {
        "spz":     spz or None,
        "soud":    r.get("soud") or r.get("nazevSoudu"),
        "stav":    r.get("stav") or r.get("stavRizeni"),
        "druh":    r.get("druhRizeni"),
        "datum":   fmt_date(r.get("datumZahajeni") or r.get("datumZapisu")),
        "dluznik": dluznik,
        "spravce": spravce,
    }

# --- Kontrola ---
def run_check(notify=True):
    log.info("=== Spouštím kontrolu ISIR ===")
    with _lock:
        d = load_data()

    nove = []
    results_new = {}

    for s in d["subjects"]:
        ico = s["ico"]
        log.info(f"  {s['nazev']} ({ico})")
        res = fetch_isir(ico)
        ts = datetime.now().isoformat()
        if not res["ok"]:
            results_new[ico] = {"status": "error", "error": res["error"], "ts": ts, "has_rizeni": False, "rizeni": []}
            log.warning(f"    Chyba: {res['error']}")
            time.sleep(0.5)
            continue
        rizeni = [parse_rizeni(r) for r in res["rizeni"]]
        has = len(rizeni) > 0
        results_new[ico] = {"status": "ok", "has_rizeni": has, "rizeni": rizeni, "ts": ts}
        if has and notify:
            znacky = sorted(set(r["spz"] or str(i) for i, r in enumerate(rizeni)))
            klic = ",".join(znacky)
            if d["known"].get(ico) != klic:
                nove.append({"ico": ico, "nazev": s["nazev"], "rizeni": rizeni})
                d["known"][ico] = klic
                log.info(f"    NOVÝ nález ({len(rizeni)} řízení)")
            else:
                log.info(f"    Již známé řízení")
        elif not has:
            d["known"].pop(ico, None)
            log.info(f"    Bez řízení")
        time.sleep(0.8)

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
        log.error("Gmail není nakonfigurován — přidejte GMAIL_USER a GMAIL_APP_PASS do Environment Variables")
        return
    datum = datetime.now().strftime("%d. %m. %Y")
    predmet = f"⚠️ Hlídač insolvence: nový nález ({len(nalezene)} IČO) — {datum}"
    radky = [f"Hlídač insolvence — {datum}\n"]
    for n in nalezene:
        radky += [f"\n{'─'*50}", f"SUBJEKT: {n['nazev']}  (IČO: {n['ico']})",
                  f"Odkaz:   {ISIR_WEB.format(ico=n['ico'])}\n"]
        for i, rz in enumerate(n["rizeni"], 1):
            radky.append(f"  Řízení č. {i}:")
            for k, v in [("Spisová značka", rz["spz"]),("Soud", rz["soud"]),("Stav", rz["stav"]),
                         ("Druh", rz["druh"]),("Datum zahájení", rz["datum"]),
                         ("Dlužník", rz["dluznik"]),("Ins. správce", rz["spravce"])]:
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
    ico = str(body.get("ico", "")).strip().replace(" ", "").zfill(8)
    nazev = str(body.get("nazev", "")).strip() or f"IČO {ico}"
    if not ico.isdigit() or len(ico) != 8:
        return jsonify({"error": "Neplatné IČO"}), 400
    with _lock:
        d = load_data()
        if any(s["ico"] == ico for s in d["subjects"]):
            return jsonify({"error": "IČO již sledujete"}), 409
        d["subjects"].append({"ico": ico, "nazev": nazev})
        save_data(d)
    return jsonify({"ok": True, "ico": ico, "nazev": nazev})

@app.route("/api/subjects/<ico>", methods=["DELETE"])
def api_del(ico):
    with _lock:
        d = load_data()
        d["subjects"] = [s for s in d["subjects"] if s["ico"] != ico]
        d["results"].pop(ico, None)
        d["known"].pop(ico, None)
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

# --- Frontend HTML ---
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
button:hover{background:var(--s2)}
button:disabled{opacity:.4;cursor:not-allowed}
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
.add-btn{width:100%;margin-top:10px;padding:9px}
.err{font-family:var(--mono);font-size:11px;color:var(--warn);margin-top:6px;min-height:16px}
.tags-section{margin-top:14px;padding-top:14px;border-top:1px solid var(--border)}
.tags-title{font-family:var(--mono);font-size:10px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px}
.tag{display:flex;align-items:center;justify-content:space-between;padding:7px 10px;border:1px solid var(--border);border-radius:3px;margin-bottom:5px;background:var(--s2)}
.tag-name{font-size:13px;font-weight:500}
.tag-ico{font-family:var(--mono);font-size:11px;color:var(--muted)}
.tag-del{background:none;border:none;padding:0 2px;font-size:16px;color:var(--muted);cursor:pointer;line-height:1}
.tag-del:hover{color:var(--warn);background:none}
.empty{font-family:var(--mono);font-size:12px;color:var(--muted);text-align:center;padding:1.5rem 0}
.ri{padding:16px 0;border-bottom:1px solid var(--border)}
.ri:last-child{border-bottom:none}
.ri-top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.ri-name{font-size:15px;font-weight:600}
.ri-meta{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:3px}
.badge{font-family:var(--mono);font-size:10px;font-weight:500;padding:3px 9px;border-radius:3px;white-space:nowrap;flex-shrink:0}
.b-ok{background:var(--ok-bg);color:var(--ok)}
.b-warn{background:var(--warn-bg);color:var(--warn)}
.b-muted{background:var(--s2);color:var(--muted)}
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
.prog{height:2px;background:var(--border);overflow:hidden;margin:0 16px 0}
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
      <div class="panel-head">Přidat IČO</div>
      <div class="panel-body">
        <div class="field-label">IČO (8 číslic)</div>
        <input type="text" id="icoIn" placeholder="např. 27082440" maxlength="8" onkeydown="if(event.key==='Enter')add()">
        <div class="field-label">Název firmy (volitelné)</div>
        <input type="text" id="nameIn" placeholder="Název s.r.o." onkeydown="if(event.key==='Enter')add()">
        <button class="add-btn primary" onclick="add()">+ Přidat do sledování</button>
        <div class="err" id="err"></div>
        <div class="tags-section">
          <div class="tags-title">Sledovaná IČO</div>
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
let subjects=[],results={},lastCheck=null,checking=false;

async function api(m,p,b){
  const o={method:m,headers:{'Content-Type':'application/json'}};
  if(b)o.body=JSON.stringify(b);
  return (await fetch(p,o)).json();
}
async function load(){
  const [s,r]=await Promise.all([api('GET','/api/subjects'),api('GET','/api/results')]);
  subjects=s; results=r.results||{}; lastCheck=r.last_check; render();
}
async function add(){
  const ico=document.getElementById('icoIn').value.trim().replace(/\D/g,'').padStart(8,'0');
  const nazev=document.getElementById('nameIn').value.trim();
  const e=document.getElementById('err'); e.textContent='';
  if(!/^\d{8}$/.test(ico)){e.textContent='IČO musí mít 8 číslic.';return;}
  const r=await api('POST','/api/subjects',{ico,nazev});
  if(r.error){e.textContent=r.error;return;}
  document.getElementById('icoIn').value='';
  document.getElementById('nameIn').value='';
  await load();
}
async function del(ico){
  if(!confirm('Odebrat IČO '+ico+' ze sledování?'))return;
  await api('DELETE','/api/subjects/'+ico); await load();
}
async function checkNow(){
  if(checking)return;
  checking=true;
  const btn=document.getElementById('btnNow');
  btn.disabled=true; btn.textContent='Kontroluji…';
  document.getElementById('progWrap').style.display='block';
  const prev=lastCheck;
  await api('POST','/api/check');
  let t=0;
  const iv=setInterval(async()=>{
    t+=2; document.getElementById('pf').style.width=Math.min(88,t/(subjects.length*2+4)*100)+'%';
    const r=await api('GET','/api/results');
    if(r.last_check&&r.last_check!==prev){
      clearInterval(iv); results=r.results||{}; lastCheck=r.last_check;
      checking=false; btn.disabled=false; btn.textContent='Zkontrolovat nyní';
      document.getElementById('progWrap').style.display='none';
      document.getElementById('pf').style.width='0';
      render();
    }
    if(t>180){clearInterval(iv);checking=false;btn.disabled=false;btn.textContent='Zkontrolovat nyní';}
  },2000);
}
function ts(iso){try{return new Date(iso).toLocaleString('cs-CZ');}catch{return iso;}}
function render(){
  document.getElementById('lc').textContent=lastCheck?'Poslední kontrola: '+ts(lastCheck):'';
  // Tags
  const tg=document.getElementById('tags');
  tg.innerHTML=subjects.length?subjects.map(s=>`<div class="tag"><div><div class="tag-name">${s.nazev}</div><div class="tag-ico">${s.ico}</div></div><button class="tag-del" onclick="del('${s.ico}')">×</button></div>`).join(''):'<div class="empty">Zatím žádná IČO</div>';
  // Results
  const rl=document.getElementById('rl');
  if(!subjects.length){rl.innerHTML='<div class="empty" style="padding:2rem 0">Přidejte IČO a spusťte kontrolu.</div>';document.getElementById('sb').innerHTML='';return;}
  let wc=0;
  rl.innerHTML=subjects.map(s=>{
    const r=results[s.ico];
    const meta='IČO: '+s.ico+(r&&r.ts?' · '+ts(r.ts):'');
    if(!r) return row(s,meta,'<span class="badge b-muted">Nekontrolováno</span>','');
    if(r.status==='error') return row(s,meta,'<span class="badge b-warn">Chyba</span>',`<div class="no-rz">${r.error}</div>`);
    if(r.has_rizeni) wc++;
    const badge=r.has_rizeni?'<span class="badge b-warn">Řízení zahájeno</span>':'<span class="badge b-ok">Bez řízení</span>';
    const det=r.has_rizeni?(r.rizeni||[]).map(rz=>rzbox(rz,s.ico)).join(''):'<div class="no-rz">Žádný záznam v insolvenčním rejstříku.</div>';
    return row(s,meta,badge,det+`<a class="lustr" href="https://isir.justice.cz/isir/ueu/vysledek_lustrace.do?ic=${s.ico}&aktualnost=AKTUALNI_I_UKONCENA" target="_blank">Otevřít lustraci v ISIR →</a>`);
  }).join('');
  document.getElementById('sb').innerHTML=wc?`<span class="badge b-warn">${wc} s řízením`:'';
}
function row(s,meta,badge,det){
  return`<div class="ri"><div class="ri-top"><div><div class="ri-name">${s.nazev}</div><div class="ri-meta">${meta}</div></div>${badge}</div>${det}</div>`;
}
function rzbox(rz,ico){
  const fields=[['Spisová značka',rz.spz],['Soud',rz.soud],['Stav řízení',rz.stav],['Druh řízení',rz.druh],['Datum zahájení',rz.datum],['Dlužník',rz.dluznik],['Ins. správce',rz.spravce]].filter(([,v])=>v);
  const link=rz.spz?`https://isir.justice.cz/isir/ueu/rizeni_detail.do?id=${encodeURIComponent(rz.spz)}`:`https://isir.justice.cz/isir/ueu/vysledek_lustrace.do?ic=${ico}&aktualnost=AKTUALNI_I_UKONCENA`;
  return`<div class="rzbox"><div class="rzhead">Insolvenční řízení</div><table class="rzt">${fields.map(([k,v])=>`<tr><td class="rk">${k}</td><td class="rv">${v}</td></tr>`).join('')}</table><a class="rzlink" href="${link}" target="_blank">Zobrazit v insolvenčním rejstříku →</a></div>`;
}
load();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(HTML)

# --- Scheduler vlákno ---
def scheduler_thread():
    log.info(f"Scheduler spuštěn — denní kontrola v {CHECK_HOUR:02d}:00")
    schedule.every().day.at(f"{CHECK_HOUR:02d}:00").do(run_check)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=run_check, daemon=True).start()
    threading.Thread(target=scheduler_thread, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
