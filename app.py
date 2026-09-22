import json, os, re, shutil, signal, subprocess, sys, threading, time, zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, redirect, render_template_string, request

APP_NAME = "VoidFlame Host"
BASE = Path(__file__).resolve().parent
DATA = BASE / "voidflame_data"
PROJECTS = DATA / "projects"
DB = DATA / "bots.json"
DATA.mkdir(exist_ok=True); PROJECTS.mkdir(exist_ok=True)
app = Flask(__name__)
processes = {}
logs = {}

def load():
    if not DB.exists(): return {}
    try: return json.loads(DB.read_text(encoding="utf-8"))
    except Exception: return {}

bots = load()

def save():
    DB.write_text(json.dumps(bots, ensure_ascii=False, indent=2), encoding="utf-8")

def safe(s):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-._")
    return s[:48] or "bot"

def github_info(url):
    p = urlparse(url.strip())
    if p.netloc.lower() not in ("github.com", "www.github.com"): raise ValueError("Use a GitHub repository URL.")
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2: raise ValueError("Invalid GitHub repository URL.")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    return owner, repo, f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/main"

def folder(bot_id): return PROJECTS / bot_id

def command_for(bot):
    if bot.get("command"): return bot["command"]
    f = folder(bot["_id"])
    if (f/"package.json").exists():
        try:
            p = json.loads((f/"package.json").read_text(encoding="utf-8"))
            if p.get("scripts", {}).get("start"): return p["scripts"]["start"]
            for x in ("index.js","main.js","bot.js"):
                if (f/x).exists(): return "node " + x
        except Exception: pass
    for x in ("bot.py","main.py","app.py","index.py"):
        if (f/x).exists(): return f"{sys.executable} {x}"
    return ""

def reader(bot_id, proc):
    try:
        for line in proc.stdout:
            logs.setdefault(bot_id, []).append(line.rstrip())
            logs[bot_id] = logs[bot_id][-500:]
    finally:
        proc.stdout.close(); processes.pop(bot_id, None)

def start(bot_id):
    bot = bots.get(bot_id)
    if not bot: return False, "Bot not found."
    if bot_id in processes and processes[bot_id].poll() is None: return False, "Bot is already running."
    cmd = command_for(bot)
    if not cmd: return False, "Start command was not detected."
    env = os.environ.copy(); env.update({str(k): str(v) for k,v in bot.get("env",{}).items()})
    env["VOIDFLAME_BOT_ID"] = bot_id
    try:
        proc = subprocess.Popen(cmd, cwd=folder(bot_id), env=env, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True)
    except OSError as e: return False, str(e)
    processes[bot_id] = proc; logs[bot_id] = []
    threading.Thread(target=reader, args=(bot_id,proc), daemon=True).start()
    return True, "Started."

def stop(bot_id):
    proc = processes.get(bot_id)
    if not proc or proc.poll() is not None: return False, "Bot is not running."
    try: os.killpg(os.getpgid(proc.pid), signal.SIGTERM); return True, "Stopped."
    except Exception as e: return False, str(e)

HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}}</title><style>
*{box-sizing:border-box}body{margin:0;background:#08090d;color:#f5f7fb;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif}main{max-width:1100px;margin:auto;padding:24px 16px}.top{display:flex;justify-content:space-between;align-items:center}.brand{font-size:28px;font-weight:800}.muted,small{color:#9299a8}.card{background:#11131a;border:1px solid #252936;border-radius:16px;padding:18px;margin-top:18px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:14px}.row{display:flex;gap:8px;flex-wrap:wrap}input,textarea,button{font:inherit;border-radius:10px;border:1px solid #303544;background:#0b0d12;color:#fff;padding:10px}input,textarea{width:100%;margin:6px 0 12px}button{cursor:pointer}.primary{background:#fff;color:#08090d;border-color:#fff}.online{color:#65e6a1}.offline{color:#9299a8}pre{white-space:pre-wrap;background:#07080b;border-radius:10px;padding:12px;max-height:240px;overflow:auto;font-size:12px}</style></head><body><main>
<div class="top"><div><div class="brand">VoidFlame Host</div><div class="muted">Private Discord bot control panel</div></div><button onclick="location.reload()">Refresh</button></div>
<div class="card"><h2>Add bot</h2><form method="post" action="/bots"><input name="name" placeholder="Bot name" required><input name="github" placeholder="https://github.com/owner/repository" required><input name="command" placeholder="Start command (optional)"><button class="primary">Add from GitHub</button></form></div>
<div class="grid">{% for b in bot_list %}<div class="card"><h2>{{b.name}}</h2><div class="{{ "online" if b.online else "offline" }}">{{ "● Online" if b.online else "○ Offline" }}</div><small>{{b.github}}</small><p><small>Command: {{b.command or "Not detected"}}</small></p><div class="row">{% if b.online %}<button onclick="act('{{b.id}}','stop')">Stop</button>{% else %}<button class="primary" onclick="act('{{b.id}}','start')">Start</button>{% endif %}<button onclick="act('{{b.id}}','restart')">Restart</button></div><pre>{{b.log}}</pre><form method="post" action="/bots/{{b.id}}/env"><textarea name="env" rows="4" placeholder="{&quot;TOKEN&quot;:&quot;...&quot;}">{{b.env}}</textarea><button>Save variables</button></form><form method="post" action="/bots/{{b.id}}/command"><input name="command" value="{{b.command}}"><button>Save command</button></form></div>{% else %}<div class="card"><h2>No bots yet</h2><div class="muted">Add a GitHub repository above.</div></div>{% endfor %}</div>
<script>async function act(id,op){let r=await fetch("/api/bots/"+id+"/"+op,{method:"POST"});let d=await r.json();alert(d.message||d.error);location.reload()}</script></main></body></html>"""

@app.get("/")
def index():
    items=[]
    for bid,b in bots.items():
        b["_id"]=bid
        items.append({"id":bid,"name":b["name"],"github":b["github"],"command":command_for(b),"online":bid in processes and processes[bid].poll() is None,"log":"\n".join(logs.get(bid,[])[-120:]),"env":json.dumps(b.get("env",{}),ensure_ascii=False)})
    return render_template_string(HTML,title=APP_NAME,bot_list=items)

@app.post("/bots")
def add_bot():
    name=safe(request.form.get("name","bot")); url=request.form.get("github","").strip(); command=request.form.get("command","").strip()
    try: owner,repo,zip_url=github_info(url); r=requests.get(zip_url,timeout=30); r.raise_for_status()
    except Exception as e: return "GitHub download failed: "+str(e),400
    bid=safe(owner+"-"+repo+"-"+str(int(time.time()))); f=folder(bid); f.mkdir(parents=True)
    try:
        zpath=f/"repo.zip"; zpath.write_bytes(r.content)
        with zipfile.ZipFile(zpath) as z: z.extractall(f/"_extract")
        roots=[x for x in (f/"_extract").iterdir() if x.is_dir()]; src=roots[0] if len(roots)==1 else f/"_extract"
        for x in src.iterdir(): shutil.move(str(x),str(f/x.name))
        shutil.rmtree(f/"_extract",ignore_errors=True); zpath.unlink(missing_ok=True)
        bots[bid]={"name":name,"github":url,"command":command,"env":{}}; save(); return redirect("/")
    except Exception as e: shutil.rmtree(f,ignore_errors=True); return "Repository setup failed: "+str(e),500

@app.post("/bots/<bid>/env")
def env(bid):
    if bid not in bots:return "Bot not found",404
    try:
        value=json.loads(request.form.get("env","{}")); assert isinstance(value,dict); bots[bid]["env"]=value; save(); return redirect("/")
    except Exception as e:return "Invalid environment JSON: "+str(e),400

@app.post("/bots/<bid>/command")
def cmd(bid):
    if bid not in bots:return "Bot not found",404
    bots[bid]["command"]=request.form.get("command","").strip(); save(); return redirect("/")

@app.post("/api/bots/<bid>/<op>")
def action(bid,op):
    if op=="start": ok,msg=start(bid)
    elif op=="stop": ok,msg=stop(bid)
    elif op=="restart": stop(bid); time.sleep(.4); ok,msg=start(bid)
    else:return jsonify(error="Unknown action"),400
    return jsonify(ok=ok,message=msg),(200 if ok else 400)

if __name__=="__main__": app.run(host=os.getenv("VOIDFLAME_HOST","127.0.0.1"),port=int(os.getenv("VOIDFLAME_PORT","9670")),debug=False)