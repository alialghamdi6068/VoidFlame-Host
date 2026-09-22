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
DATA.mkdir(exist_ok=True)
PROJECTS.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024

processes = {}
logs = {}
installing = set()
lock = threading.RLock()


def load():
    if not DB.exists():
        return {}
    try:
        data = json.loads(DB.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


bots = load()


def save():
    with lock:
        DB.write_text(json.dumps(bots, ensure_ascii=False, indent=2), encoding="utf-8")


def safe(value):
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip("-._")
    return value[:48] or "bot"


def append_log(bot_id, line):
    with lock:
        logs.setdefault(bot_id, []).append(str(line).rstrip())
        logs[bot_id] = logs[bot_id][-500:]


def github_info(url):
    p = urlparse(url.strip())
    if p.netloc.lower() not in ("github.com", "www.github.com"):
        raise ValueError("Use a GitHub repository URL.")
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2:
        raise ValueError("Invalid GitHub repository URL.")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    return owner, repo


def folder(bot_id):
    return PROJECTS / bot_id


def command_for(bot):
    if bot.get("command"):
        return bot["command"]
    f = folder(bot["_id"])
    if (f / "package.json").exists():
        try:
            package = json.loads((f / "package.json").read_text(encoding="utf-8"))
            start_cmd = package.get("scripts", {}).get("start")
            if start_cmd:
                return start_cmd
            for name in ("index.js", "main.js", "bot.js"):
                if (f / name).exists():
                    return "node " + name
        except Exception:
            pass
    for name in ("bot.py", "main.py", "app.py", "index.py"):
        if (f / name).exists():
            return f"{sys.executable} {name}"
    return ""


def bot_state(bot_id):
    proc = processes.get(bot_id)
    if not proc:
        return "offline"
    code = proc.poll()
    if code is None:
        return "installing" if bot_id in installing else "online"
    processes.pop(bot_id, None)
    return "crashed" if code else "offline"


def reader(bot_id, proc):
    try:
        for line in iter(proc.stdout.readline, ""):
            if not line:
                break
            append_log(bot_id, line)
    except Exception as exc:
        append_log(bot_id, "[VoidFlame] Console reader error: " + str(exc))
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        processes.pop(bot_id, None)


def start(bot_id):
    bot = bots.get(bot_id)
    if not bot:
        return False, "Bot not found."
    if bot_state(bot_id) == "online":
        return False, "Bot is already running."

    cmd = command_for({**bot, "_id": bot_id})
    if not cmd:
        return False, "Start command was not detected."

    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in bot.get("env", {}).items()})
    env["VOIDFLAME_BOT_ID"] = bot_id

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(folder(bot_id)),
            env=env,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except Exception as exc:
        return False, "Start failed: " + str(exc)

    processes[bot_id] = proc
    logs[bot_id] = []
    append_log(bot_id, "[VoidFlame] Started: " + cmd)
    threading.Thread(target=reader, args=(bot_id, proc), daemon=True).start()
    return True, "Bot started."


def stop(bot_id):
    proc = processes.get(bot_id)
    if not proc or proc.poll() is not None:
        processes.pop(bot_id, None)
        return False, "Bot is not running."
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        append_log(bot_id, "[VoidFlame] Stop requested.")
        return True, "Bot stopped."
    except Exception as exc:
        return False, "Stop failed: " + str(exc)


def install_dependencies(bot_id):
    if bot_id in installing:
        return False, "Dependency installation is already running."
    if bot_id not in bots:
        return False, "Bot not found."

    installing.add(bot_id)

    def worker():
        try:
            f = folder(bot_id)
            append_log(bot_id, "[VoidFlame] Installing dependencies...")

            req = f / "requirements.txt"
            if req.exists():
                proc = subprocess.Popen(
                    [sys.executable, "-m", "pip", "install", "-r", str(req)],
                    cwd=str(f),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                for line in proc.stdout:
                    append_log(bot_id, line)
                code = proc.wait()
                append_log(bot_id, f"[VoidFlame] pip finished with code {code}.")

            package = f / "package.json"
            if package.exists():
                if shutil.which("npm"):
                    proc = subprocess.Popen(
                        ["npm", "install"],
                        cwd=str(f),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    for line in proc.stdout:
                        append_log(bot_id, line)
                    code = proc.wait()
                    append_log(bot_id, f"[VoidFlame] npm finished with code {code}.")
                else:
                    append_log(bot_id, "[VoidFlame] npm is not available on this device.")

            if not req.exists() and not package.exists():
                append_log(bot_id, "[VoidFlame] No requirements.txt or package.json found.")
            append_log(bot_id, "[VoidFlame] Dependency setup finished.")
        except Exception as exc:
            append_log(bot_id, "[VoidFlame] Dependency setup failed: " + str(exc))
        finally:
            installing.discard(bot_id)

    threading.Thread(target=worker, daemon=True).start()
    return True, "Dependency installation started."


def remove_bot(bot_id):
    if bot_id not in bots:
        return False, "Bot not found."
    proc = processes.get(bot_id)
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            pass
    processes.pop(bot_id, None)
    installing.discard(bot_id)
    shutil.rmtree(folder(bot_id), ignore_errors=True)
    bots.pop(bot_id, None)
    logs.pop(bot_id, None)
    save()
    return True, "Bot deleted."


def install_repo(bot_id, source_dir):
    target = folder(bot_id)
    target.mkdir(parents=True, exist_ok=True)
    for item in source_dir.iterdir():
        destination = target / item.name
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        shutil.move(str(item), str(destination))


HTML = r"""<!doctype html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{{ title }}</title>
<style>
:root{--bg:#07080c;--panel:#0e1118;--panel2:#121621;--line:#202634;--text:#f5f7fb;--muted:#8e98aa;--good:#59e391;--bad:#ff6678;--warn:#ffc857}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% -10%,#1a1830 0,#07080c 42%);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,textarea{font:inherit}button{cursor:pointer}.shell{max-width:1280px;margin:auto;padding:22px 18px 48px}.top{display:flex;justify-content:space-between;align-items:center;gap:16px}.brand{display:flex;align-items:center;gap:12px}.logo{width:44px;height:44px;border-radius:14px;background:linear-gradient(135deg,#fff,#777);color:#090a0e;display:grid;place-items:center;font-weight:900}.brand h1{font-size:22px;margin:0}.muted{color:var(--muted)}.top-actions,.actions{display:flex;gap:8px;flex-wrap:wrap}.btn{border:1px solid var(--line);background:#0c0f15;color:var(--text);padding:10px 14px;border-radius:11px}.btn.primary{background:#f5f7fb;color:#07080c;border-color:#f5f7fb;font-weight:700}.hero{margin-top:22px;padding:26px;border:1px solid var(--line);border-radius:22px;background:linear-gradient(135deg,rgba(255,255,255,.06),rgba(255,255,255,.015));box-shadow:0 20px 60px rgba(0,0,0,.25)}.hero h2{font-size:32px;margin:0 0 8px}.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:18px}.stat{padding:16px;border:1px solid var(--line);background:var(--panel);border-radius:15px}.stat b{font-size:24px}.section{margin-top:20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}.card{border:1px solid var(--line);background:rgba(14,17,24,.94);border-radius:18px;padding:18px;box-shadow:0 10px 35px rgba(0,0,0,.15)}.bot-head{display:flex;justify-content:space-between;gap:12px}.bot-name{font-size:19px;font-weight:800}.badge{font-size:12px;border-radius:999px;padding:6px 9px;background:#171b24;color:var(--muted)}.badge.online{color:var(--good)}.badge.crashed{color:var(--bad)}.badge.installing{color:var(--warn)}.repo{font-size:12px;color:var(--muted);word-break:break-all;margin:8px 0 14px}.field{margin-top:12px}.field label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px}input,textarea{width:100%;border:1px solid var(--line);background:#090b10;color:var(--text);border-radius:11px;padding:11px;outline:none}textarea{resize:vertical}.console{background:#050609;border:1px solid #171b23;border-radius:12px;padding:12px;min-height:150px;max-height:240px;overflow:auto;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap}.empty{padding:35px;text-align:center;border:1px dashed var(--line);border-radius:18px}.toast{position:fixed;right:16px;bottom:16px;background:#f5f7fb;color:#07080c;padding:12px 15px;border-radius:12px;display:none;max-width:90vw}.danger{color:#ff8290}.hint{font-size:12px;color:var(--muted);margin-top:7px}@media(max-width:700px){.stats{grid-template-columns:1fr}.hero h2{font-size:25px}.shell{padding:16px 12px 35px}.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="shell">
<header class="top">
  <div class="brand"><div class="logo">VF</div><div><h1>VoidFlame Host</h1><div class="muted">Private bot hosting control center</div></div></div>
  <div class="top-actions"><button class="btn" onclick="location.reload()">Refresh</button></div>
</header>

<section class="hero">
  <h2>Your bots. Your device. Your control.</h2>
  <div class="muted">Upload a complete project folder or deploy directly from GitHub. Dependencies, processes and console logs are managed locally.</div>
  <div class="stats">
    <div class="stat"><div class="muted">Bots</div><b>{{ total }}</b></div>
    <div class="stat"><div class="muted">Running</div><b>{{ running }}</b></div>
    <div class="stat"><div class="muted">Device host</div><b>LOCAL</b></div>
  </div>
</section>

<section class="section card">
  <h2 style="margin-top:0">Deploy a bot</h2>
  <form method="post" action="/bots/upload" enctype="multipart/form-data" onsubmit="deploying(this)">
    <div class="field"><label>Bot name</label><input name="name" placeholder="VoidFlame System" required></div>
    <div class="field"><label>Project folder</label><input type="file" name="files" webkitdirectory directory multiple required></div>
    <div class="hint">Choose the whole bot folder. All files and subfolders are uploaded together.</div>
    <div class="actions" style="margin-top:14px"><button class="btn primary" type="submit">Upload & Deploy Folder</button></div>
  </form>
  <hr style="border:0;border-top:1px solid var(--line);margin:20px 0">
  <form method="post" action="/bots/github">
    <div class="field"><label>GitHub repository</label><input name="name" placeholder="Bot name" required><input name="github" placeholder="https://github.com/owner/repository" required></div>
    <div class="actions" style="margin-top:4px"><button class="btn" type="submit">Deploy from GitHub main</button></div>
  </form>
</section>

<section class="section">
{% if bot_list %}
<div class="grid">
{% for b in bot_list %}
<article class="card">
  <div class="bot-head"><div><div class="bot-name">{{ b.name }}</div><div class="repo">{{ b.source }}</div></div><span class="badge {{ b.state }}">{{ b.state|upper }}</span></div>
  <div class="field"><label>Start command</label><input id="cmd-{{ b.id }}" value="{{ b.command }}" placeholder="python bot.py"></div>
  <div class="actions" style="margin-top:10px">
    {% if b.state == "online" %}<button class="btn" onclick="act('{{b.id}}','stop')">Stop</button>{% else %}<button class="btn primary" onclick="act('{{b.id}}','start')">Start</button>{% endif %}
    <button class="btn" onclick="act('{{b.id}}','restart')">Restart</button>
    <button class="btn" onclick="act('{{b.id}}','install')">Install deps</button>
    <button class="btn" onclick="saveCommand('{{b.id}}')">Save command</button>
    <button class="btn" onclick="deleteBot('{{b.id}}')">Delete</button>
  </div>
  <div class="field"><label>Environment Variables — JSON</label><textarea id="env-{{b.id}}" rows="5">{{ b.env }}</textarea></div>
  <div class="actions"><button class="btn" onclick="saveEnv('{{b.id}}')">Save variables</button></div>
  <div class="field"><label>Console</label><pre class="console" id="log-{{b.id}}">{{ b.log }}</pre></div>
</article>
</div>
{% endfor %}
{% else %}
<div class="empty"><h2>No bots deployed</h2><div class="muted">Upload your complete bot folder above to get started.</div></div>
{% endif %}
</section>
</div>
<div id="toast" class="toast"></div>
<script>
function toast(message){const t=document.getElementById("toast");t.textContent=message;t.style.display="block";setTimeout(()=>t.style.display="none",2600)}
async function act(id,op){const r=await fetch("/api/bots/"+id+"/"+op,{method:"POST"});const d=await r.json();toast(d.message||d.error);setTimeout(()=>location.reload(),500)}
async function saveCommand(id){const command=document.getElementById("cmd-"+id).value;const body=new URLSearchParams({command});const r=await fetch("/api/bots/"+id+"/command",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body});const d=await r.json();toast(d.message||d.error)}
async function saveEnv(id){const env=document.getElementById("env-"+id).value;const body=new URLSearchParams({env});const r=await fetch("/api/bots/"+id+"/env",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body});const d=await r.json();toast(d.message||d.error)}
async function deleteBot(id){if(!confirm("Delete this bot and all uploaded files?"))return;const r=await fetch("/api/bots/"+id+"/delete",{method:"POST"});const d=await r.json();toast(d.message||d.error);setTimeout(()=>location.reload(),500)}
function deploying(form){const b=form.querySelector("button");b.disabled=true;b.textContent="Uploading..."}
</script>
</body>
</html>"""


def page_data():
    items = []
    running = 0
    for bid, bot in bots.items():
        bot["_id"] = bid
        state = bot_state(bid)
        if state == "online":
            running += 1
        items.append({
            "id": bid,
            "name": bot.get("name", bid),
            "source": bot.get("github") or "Uploaded folder",
            "command": command_for(bot),
            "state": state,
            "env": json.dumps(bot.get("env", {}), ensure_ascii=False, indent=2),
            "log": "\n".join(logs.get(bid, [])[-120:]),
        })
    return items, running


@app.get("/")
def index():
    items, running = page_data()
    return render_template_string(HTML, title=APP_NAME, bot_list=items, total=len(items), running=running)


@app.post("/bots/upload")
def upload_bot():
    name = safe(request.form.get("name", "bot"))
    files = request.files.getlist("files")
    files = [f for f in files if f and f.filename]
    if not files:
        return "No folder files received.", 400

    bid = safe(name + "-" + str(int(time.time())))
    target = folder(bid)
    target.mkdir(parents=True, exist_ok=True)

    try:
        for file in files:
            relative = (file.filename or "").replace("\\", "/").lstrip("/")
            parts = [p for p in relative.split("/") if p not in ("", ".", "..")]
            if not parts:
                continue
            destination = target.joinpath(*parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            file.save(str(destination))

        bots[bid] = {"name": name, "github": "", "command": "", "env": {}}
        save()
        install_dependencies(bid)
        return redirect("/")
    except Exception as exc:
        shutil.rmtree(target, ignore_errors=True)
        return "Folder deployment failed: " + str(exc), 500


@app.post("/bots/github")
def github_bot():
    name = safe(request.form.get("name", "bot"))
    url = request.form.get("github", "").strip()
    try:
        owner, repo = github_info(url)
        url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/main"
        response = requests.get(url, timeout=60)
        response.raise_for_status()
    except Exception as exc:
        return "GitHub download failed: " + str(exc), 400

    bid = safe(name + "-" + str(int(time.time())))
    target = folder(bid)
    target.mkdir(parents=True, exist_ok=True)
    try:
        archive = target / "repo.zip"
        archive.write_bytes(response.content)
        with zipfile.ZipFile(archive) as z:
            z.extractall(target / "_extract")
        roots = [x for x in (target / "_extract").iterdir() if x.is_dir()]
        source = roots[0] if len(roots) == 1 else target / "_extract"
        for item in source.iterdir():
            shutil.move(str(item), str(target / item.name))
        shutil.rmtree(target / "_extract", ignore_errors=True)
        archive.unlink(missing_ok=True)
        bots[bid] = {"name": name, "github": f"https://github.com/{owner}/{repo}", "command": "", "env": {}}
        save()
        install_dependencies(bid)
        return redirect("/")
    except Exception as exc:
        shutil.rmtree(target, ignore_errors=True)
        return "Repository setup failed: " + str(exc), 500


@app.post("/api/bots/<bid>/command")
def api_command(bid):
    if bid not in bots:
        return jsonify(error="Bot not found."), 404
    bots[bid]["command"] = request.form.get("command", "").strip()
    save()
    return jsonify(ok=True, message="Start command saved.")


@app.post("/api/bots/<bid>/env")
def api_env(bid):
    if bid not in bots:
        return jsonify(error="Bot not found."), 404
    try:
        value = json.loads(request.form.get("env", "{}"))
        if not isinstance(value, dict):
            raise ValueError("Environment must be a JSON object.")
        bots[bid]["env"] = value
        save()
        return jsonify(ok=True, message="Environment variables saved.")
    except Exception as exc:
        return jsonify(error="Invalid environment JSON: " + str(exc)), 400


@app.post("/api/bots/<bid>/<op>")
def action(bid, op):
    if op == "start":
        ok, message = start(bid)
    elif op == "stop":
        ok, message = stop(bid)
    elif op == "restart":
        stop(bid)
        time.sleep(0.4)
        ok, message = start(bid)
    elif op == "install":
        ok, message = install_dependencies(bid)
    elif op == "delete":
        ok, message = remove_bot(bid)
    else:
        return jsonify(error="Unknown action."), 400
    return jsonify(ok=ok, message=message), (200 if ok else 400)


@app.get("/api/bots/<bid>/logs")
def api_logs(bid):
    if bid not in bots:
        return jsonify(error="Bot not found."), 404
    return jsonify(state=bot_state(bid), logs=logs.get(bid, [])[-200:])


if __name__ == "__main__":
    app.run(
        host=os.getenv("VOIDFLAME_HOST", "127.0.0.1"),
        port=int(os.getenv("VOIDFLAME_PORT", "9670")),
        debug=False,
    )
