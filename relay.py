#!/usr/bin/env python3
"""relay.py — two brains, one queue.

Claude (paid, sharp) and ODS (local, free, slower) take turns on the same jobs.
Each step, the router picks a brain:
  * Claude while today's spend stays under the daily cap and the step fits its
    working context; the cheap model for routine steps, the stronger one for
    code and deep thinking.
  * ODS when the cap is reached, Claude errors out, or Claude is not configured.
Continuity across brains: every step must end with a short HANDOFF section.
Only that handoff (not the whole history) is sent to the next step, so context
never saturates and either brain can pick up where the other stopped.

Job kinds
  think     long reasoning / research on a goal; may READ files under ~/Projects and ~/Documents
  evaluate  score every project in ~/Projects and write a report
  codefix   find and fix bugs in a git repo; fixes land on branch brain/job-<id>
            in a separate worktree, tests are run there; you merge if you like it

CLI
  relay.py add think "goal text"
  relay.py add evaluate
  relay.py add codefix ~/Projects/my-area/my-project "fix failing tests"
  relay.py tick            # run steps until the time slice ends (the timer calls this)
  relay.py status          # queue, brain used, today's Claude spend
  relay.py show <id>       # full output of a job
  relay.py cancel <id>
  relay.py strategy-all    # rank all projects by money odds, then deep-dive the best ones
  relay.py add strategy ~/Projects/my-area/my-project   # one project
  relay.py telegram-link   # after messaging your bot once: finds and saves your chat ID, sends a test
  relay.py report          # today's plain-English report now (screen + phone + reports/daily-<date>.md)
  relay.py brief <id>      # plain-English brief of one job
  relay.py check-claude    # test the Claude key; clears the cooldown when it works
  relay.py pause | resume
  relay.py notify-test     # send a test message to your phone

Phone (Telegram): short messages when a job starts, when it switches brain, when it finishes
(with a debrief), when the queue is stuck, plus a daily digest. From the phone you can send:
  /report  /status  /pause  /resume  /cancel 12  /show 12  /strategy  /strategy my-area/my-project
  /think <goal>  /evaluate  /codefix <repo> <goal>
Commands are picked up at the next timer run (every 10 minutes).
"""
import json, os, re, sqlite3, subprocess, sys, time, urllib.request, urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

HOME = Path.home()
BASE = Path(os.environ.get("RELAY_HOME", Path(__file__).resolve().parent))
CONFIG = BASE / "relay.json"
DB = BASE / "relay.db"
OUT = BASE / "outputs"
WORKTREES = BASE / "worktrees"

DEFAULTS = {
    "daily_cap_usd": 1.00,
    "claude_api_url": "https://api.anthropic.com/v1/messages",
    "claude_key_file": "~/.secrets/anthropic_key",
    "claude_model_cheap": "claude-haiku-4-5",
    "claude_model_strong": "claude-sonnet-4-6",
    "claude_context_tokens": 60000,     # working budget per step, far below the real window
    "claude_max_output": 2000,
    "ods_url": "http://127.0.0.1:11434/v1/chat/completions",
    "ods_model": "ods/current",
    "ods_context_tokens": 8000,
    "ods_max_output": 1200,
    "ods_timeout_sec": 900,
    "tick_minutes": 8,                  # stop starting new steps after this long
    "max_steps_per_job": 12,
    "max_steps_by_kind": {"triage": 4, "strategy": 6},
    "strategy_top_n": 6,
    "claude_cooldown_min": 30,          # after a Claude error, use ODS for this long
    "read_roots": ["~/Projects", "~/Documents"],
    "max_read_chars": 12000,
    "weekly_evaluation": True,
    "telegram_api": "https://api.telegram.org",
    "telegram_env_file": "~/.secrets/relay_telegram.env",   # TELEGRAM_BOT_TOKEN=... / TELEGRAM_CHAT_ID=...
    "daily_digest_hour": 20,
}
# USD per million tokens (input, output). Unknown models are priced like Opus so the cap errs safe.
PRICES = {"claude-haiku-4-5": (1.0, 5.0), "claude-sonnet-4-6": (3.0, 15.0),
          "claude-sonnet-4-5": (3.0, 15.0), "claude-opus": (5.0, 25.0)}


def max_steps(c, kind):
    return int(c.get("max_steps_by_kind", {}).get(kind, c["max_steps_per_job"]))


def cfg():
    c = dict(DEFAULTS)
    if CONFIG.exists():
        c.update(json.loads(CONFIG.read_text()))
    return c


def log(msg):
    sys.stderr.write(f"[relay {datetime.now():%H:%M:%S}] {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- storage
def db():
    cx = sqlite3.connect(DB, timeout=30)
    cx.row_factory = sqlite3.Row
    cx.executescript("""
    CREATE TABLE IF NOT EXISTS jobs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, goal TEXT, repo TEXT,
      status TEXT DEFAULT 'queued', steps INTEGER DEFAULT 0, handoff TEXT DEFAULT '',
      pending TEXT DEFAULT '', last_brain TEXT DEFAULT '', created TEXT, updated TEXT,
      lastout TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS steps(
      id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, n INTEGER, brain TEXT,
      model TEXT, in_tok INTEGER, out_tok INTEGER, cost REAL, ts TEXT);
    CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
    """)
    cols = [r[1] for r in cx.execute("PRAGMA table_info(jobs)")]
    for col in ("lastout", "brief"):
        if col not in cols:
            cx.execute(f"ALTER TABLE jobs ADD COLUMN {col} TEXT DEFAULT ''")
            cx.commit()
    return cx


def get_state(cx, k, default=""):
    r = cx.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def set_state(cx, k, v):
    cx.execute("INSERT OR REPLACE INTO state(k,v) VALUES(?,?)", (k, str(v)))
    cx.commit()


# ---------------------------------------------------------------- spend (shared with costmeter)
COSTMETER_DIRS = [HOME / "Projects/06_agents-tools/costmeter", HOME / "Projects/06_agents-tools", HOME / "Projects"]
for d in COSTMETER_DIRS:
    if (d / "costmeter.py").exists():
        sys.path.insert(0, str(d))
        break
try:
    import costmeter  # records into ~/.costmeter/ledger.db, same ledger as your other agents
except Exception:
    costmeter = None


def spent_today():
    path = Path(os.environ.get("COSTMETER_DB", HOME / ".costmeter/ledger.db"))
    if not path.exists():
        return 0.0
    cx = sqlite3.connect(path, timeout=30)
    try:
        cx.execute("CREATE TABLE IF NOT EXISTS calls(day TEXT, cost_usd REAL)")
        return cx.execute("SELECT COALESCE(SUM(cost_usd),0) FROM calls WHERE day=?",
                          (date.today().isoformat(),)).fetchone()[0]
    finally:
        cx.close()


def price(model):
    for k, v in PRICES.items():
        if model.startswith(k):
            return v
    return PRICES["claude-opus"]


def est_tokens(text):
    return len(text) // 4 + 1


def record_claude(model, usage, job_id):
    if costmeter is not None:
        u = SimpleNamespace(input_tokens=usage.get("input_tokens", 0),
                            output_tokens=usage.get("output_tokens", 0),
                            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
                            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0))
        return costmeter._record("relay", model, f"relay-job-{job_id}", u)
    # costmeter not found: write the same ledger ourselves so the daily cap still holds
    i, o = price(model)
    cost = (usage.get("input_tokens", 0) * i + usage.get("output_tokens", 0) * o) / 1e6
    path = Path(os.environ.get("COSTMETER_DB", HOME / ".costmeter/ledger.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    lx = sqlite3.connect(path, timeout=30)
    try:
        lx.execute("""CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, iso TEXT, day TEXT,
            agent TEXT, model TEXT, run_id TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER,
            cache_write INTEGER, cost_usd REAL, request_id TEXT)""")
        lx.execute("INSERT INTO calls(ts,iso,day,agent,model,run_id,input_tokens,output_tokens,cache_read,cache_write,cost_usd)"
                   " VALUES(?,?,?,?,?,?,?,?,0,0,?)", (time.time(), datetime.now().isoformat(timespec="seconds"),
                   date.today().isoformat(), "relay", model, f"relay-job-{job_id}", usage.get("input_tokens", 0),
                   usage.get("output_tokens", 0), cost))
        lx.commit()
    finally:
        lx.close()
    return cost


# ---------------------------------------------------------------- phone (Telegram)
def tg_conf(c):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "") or keyring("telegram-bot-token")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "") or keyring("telegram-chat-id")
    f = Path(os.path.expanduser(c["telegram_env_file"]))
    if f.exists() and not (tok and chat):
        for line in f.read_text().splitlines():
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            if k.strip() == "TELEGRAM_BOT_TOKEN" and not tok:
                tok = v
            if k.strip() == "TELEGRAM_CHAT_ID" and not chat:
                chat = v
    return tok, chat


def tg(c, method, payload):
    tok, _ = tg_conf(c)
    if not tok:
        return None
    req = urllib.request.Request(f"{c['telegram_api']}/bot{tok}/{method}", data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"telegram {method} failed: {e}")
        return None


def notify(c, text):
    _, chat = tg_conf(c)
    if chat:
        tg(c, "sendMessage", {"chat_id": chat, "text": text[:3800], "disable_web_page_preview": True})
    log("phone: " + text.replace("\n", " | ")[:160])


def short(t, n=70):
    t = " ".join((t or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


PLAIN = """You write very short updates for the owner, a busy person who is not a programmer.
Plain everyday English. No jargon, no markdown headings, no asterisks. Be specific: names, numbers, files.
Never invent anything that is not in the material."""


def cheap_text(c, cx, system, prompt, max_out=400):
    """One short call on the cheapest brain available. Returns '' if none can answer."""
    model = c["claude_model_cheap"]
    i, o = price(model)
    est = (est_tokens(system + prompt) * i + max_out * o) / 1e6
    cool = get_state(cx, "claude_cooldown_until")
    cooling = bool(cool) and datetime.now() < datetime.fromisoformat(cool)
    try:
        if claude_key(c) and not cooling and spent_today() + est <= c["daily_cap_usd"]:
            text, usage = call_claude(c, model, system, prompt, max_out)
            record_claude(model, usage, 0)
            return text.strip()
        if ods_alive(c):
            return call_ods(c, system, prompt[-(c["ods_context_tokens"] * 3):], max_out)[0].strip()
    except Exception as e:
        log(f"brief failed: {e}")
    return ""


def artifacts(c, job):
    out = []
    if job["kind"] == "codefix":
        out.append(f"Code fixes are on branch brain/job-{job['id']} (not merged). To accept: git merge brain/job-{job['id']}")
    if job["kind"] == "evaluate":
        out.append(f"Report saved: {c['reports_dir']}/evaluation-{date.today()}.md")
    if job["kind"] == "strategy" and job["repo"]:
        out.append(f"Plan saved: {job['repo'].replace(str(HOME), '~')}/_files/strategy-{date.today()}.md")
    return out


def make_brief(c, cx, job):
    lastout = job["lastout"] if "lastout" in job.keys() else ""
    material = (f"Job type: {job['kind']}\nGoal: {job['goal']}\n\nFinal notes:\n{job['handoff']}\n\n"
                f"Main output:\n{lastout[-6000:]}\n\nFiles/changes: {'; '.join(artifacts(c, job)) or 'none'}")
    prompt = (material + "\n\nWrite the update in exactly this shape, max 110 words total:\n"
              "Done: <one sentence>\nFound: <up to 3 short findings separated by ' / '>\n"
              "Changed: <what was saved or changed on the computer, or 'nothing'>\n"
              "Your next step: <one concrete action>")
    text = cheap_text(c, cx, PLAIN, prompt, 350)
    if not text:
        text = (f"Done: {short(job['goal'], 120)}\nFound: {short(job['handoff'], 300)}\n"
                f"Changed: {'; '.join(artifacts(c, job)) or 'nothing'}")
    cx.execute("UPDATE jobs SET brief=? WHERE id=?", (text, job["id"]))
    cx.commit()
    return text


def debrief(c, cx, job):
    rows = cx.execute("SELECT brain, COUNT(*) n, SUM(cost) c FROM steps WHERE job_id=? GROUP BY brain", (job["id"],)).fetchall()
    by = ", ".join(f"{r['brain']} {r['n']}" for r in rows)
    cost = sum((r["c"] or 0) for r in rows)
    mins = (datetime.now() - datetime.fromisoformat(job["created"])).total_seconds() / 60
    head = f"FINISHED #{job['id']} {job['kind']} - {short(job['goal'], 60)}"
    meta = f"({job['steps']} steps: {by} · {mins:.0f} min · ${cost:.3f} · full: relay show {job['id']})"
    return "\n".join([head, make_brief(c, cx, job), meta])


def digest(c, cx):
    today = date.today().isoformat()
    jobs = cx.execute("SELECT * FROM jobs WHERE status='done' AND substr(updated,1,10)=? ORDER BY id", (today,)).fetchall()
    waiting = cx.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('active','queued')").fetchone()[0]
    stalled = cx.execute("SELECT COUNT(*) FROM jobs WHERE status='stalled' AND substr(updated,1,10)=?", (today,)).fetchone()[0]
    rows = cx.execute("SELECT brain, COUNT(*) n FROM steps WHERE substr(ts,1,10)=? GROUP BY brain", (today,)).fetchall()
    by = ", ".join(f"{r['brain']} {r['n']}" for r in rows) or "no work"
    paused = " · PAUSED" if get_state(cx, "paused") == "1" else ""
    header = (f"TODAY IN SHORT - {today}{paused}\n"
              f"Claude ${spent_today():.2f} of ${c['daily_cap_usd']:.2f} · steps: {by} · finished {len(jobs)} · "
              f"waiting {waiting}" + (f" · stalled {stalled}" if stalled else ""))
    if not jobs:
        body = "Nothing finished today."
    else:
        briefs = "\n\n".join(f"#{j['id']} {j['kind']} ({j['goal'][:80]}):\n{j['brief'] or j['handoff'][:500]}" for j in jobs)
        prompt = (briefs + "\n\nCombine these into one daily update, max 170 words, in exactly this shape:\n"
                  "Finished: <one line per job, plain words>\nBiggest findings: <up to 3>\n"
                  "Changes made: <files, reports, branches; or 'nothing'>\nTop 3 next actions for the owner: 1. 2. 3.")
        body = cheap_text(c, cx, PLAIN, prompt, 450) or briefs[:2500]
    text = header + "\n\n" + body
    rep_dir = Path(os.path.expanduser(c["reports_dir"]))
    rep_dir.mkdir(parents=True, exist_ok=True)
    (rep_dir / f"daily-{today}.md").write_text(text + "\n")
    return text


def status_text(c, cx):
    lines = [f"Claude ${spent_today():.2f}/${c['daily_cap_usd']:.2f} · ODS {'up' if ods_alive(c) else 'DOWN'}"
             + (" · PAUSED" if get_state(cx, "paused") == "1" else "")]
    for j in cx.execute("SELECT * FROM jobs WHERE status IN ('active','queued') ORDER BY id LIMIT 8"):
        lines.append(f"#{j['id']} {j['status']} {j['kind']} step {j['steps']} ({j['last_brain'] or '-'}): {short(j['goal'], 50)}")
    if len(lines) == 1:
        lines.append("Queue empty.")
    return "\n".join(lines)


def phone_commands(c, cx):
    tok, chat = tg_conf(c)
    if not (tok and chat):
        return
    off = int(get_state(cx, "tg_offset", "0") or 0)
    res = tg(c, "getUpdates", {"offset": off, "timeout": 0})
    if not res or not res.get("ok"):
        return
    for u in res.get("result", []):
        set_state(cx, "tg_offset", u["update_id"] + 1)
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat):
            continue          # ignore anyone who is not you
        text = (msg.get("text") or "").strip()
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        try:
            if cmd == "/status":
                notify(c, status_text(c, cx))
            elif cmd == "/report":
                notify(c, digest(c, cx))
            elif cmd == "/pause":
                set_state(cx, "paused", "1"); notify(c, "Paused. Nothing new will run until /resume.")
            elif cmd == "/resume":
                set_state(cx, "paused", "0"); notify(c, "Resumed.")
            elif cmd == "/cancel" and arg.strip().isdigit():
                cx.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (int(arg),)); cx.commit()
                notify(c, f"Cancelled #{arg.strip()}.")
            elif cmd == "/show" and arg.strip().isdigit():
                j = cx.execute("SELECT * FROM jobs WHERE id=?", (int(arg),)).fetchone()
                notify(c, f"#{j['id']} {j['status']} step {j['steps']} ({j['last_brain']})\n{short(j['handoff'], 1500)}"
                       if j else "No such job.")
            elif cmd == "/strategy" and not arg.strip():
                jid = add("triage", [], quiet=True)
                notify(c, f"Queued #{jid} triage. Deep-dives on the best projects follow automatically.")
            elif cmd == "/strategy":
                p = arg.strip()
                p = p if p.startswith(("/", "~")) else str(HOME / "Projects" / p)
                jid = add("strategy", [p], quiet=True)
                notify(c, f"Queued #{jid} strategy for {p}.")
            elif cmd in ("/think", "/evaluate", "/codefix"):
                parts = arg.split()
                jid = add(cmd[1:], parts, quiet=True)
                notify(c, f"Queued #{jid} {cmd[1:]}.")
            else:
                notify(c, "Commands: /report /status /pause /resume /cancel N /show N /strategy [project] /think <goal> /evaluate /codefix <repo> <goal>")
        except SystemExit as e:
            notify(c, f"Not queued: {e}")


# ---------------------------------------------------------------- brains
def keyring(label):
    """Read a key stored with the `keys` command (Ubuntu login keyring). Empty string if unavailable."""
    try:
        r = subprocess.run(["secret-tool", "lookup", "service", "apikeys", "name", label],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def claude_key(c):
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if len(env) < 40 or "..." in env:      # placeholders like "sk-ant-..." are ignored
        env = ""
    k = keyring(c.get("claude_key_label", "anthropic")) or env
    if k:
        return k
    p = Path(os.path.expanduser(c["claude_key_file"]))
    return p.read_text().strip() if p.exists() else ""


def call_claude(c, model, system, user, max_out):
    body = {"model": model, "max_tokens": max_out, "system": system,
            "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(c["claude_api_url"], data=json.dumps(body).encode(),
                                 headers={"x-api-key": claude_key(c), "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return text, data.get("usage", {})


def call_ods(c, system, user, max_out):
    body = {"model": c["ods_model"], "max_tokens": max_out, "temperature": 0.3,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(c["ods_url"], data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=c["ods_timeout_sec"]) as r:
        data = json.loads(r.read())
    text = data["choices"][0]["message"]["content"] or ""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    return text, data.get("usage", {})


def ods_alive(c):
    base = c["ods_url"].split("/v1/")[0]
    for path in ("/health", "/v1/models"):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            continue
    return False


current_job_id = None


def choose_brain(c, cx, kind, prompt_tokens):
    """Return ('claude', model) or ('ods', model) or (None, reason)."""
    strong = kind in ("codefix", "think", "strategy")
    model = c["claude_model_strong"] if strong else c["claude_model_cheap"]
    cool = get_state(cx, "claude_cooldown_until")
    cooling = bool(cool) and datetime.now() < datetime.fromisoformat(cool)
    avoid_ods = get_state(cx, "avoid_ods_job") == str(current_job_id)
    if claude_key(c) and not cooling and prompt_tokens <= c["claude_context_tokens"]:
        i, o = price(model)
        est = (prompt_tokens * i + c["claude_max_output"] * o) / 1e6
        if spent_today() + est <= c["daily_cap_usd"]:
            return "claude", model
        # strong model too pricey for what's left? try the cheap one
        if strong:
            i, o = price(c["claude_model_cheap"])
            est = (prompt_tokens * i + c["claude_max_output"] * o) / 1e6
            if spent_today() + est <= c["daily_cap_usd"]:
                return "claude", c["claude_model_cheap"]
    if ods_alive(c) and not avoid_ods:
        return "ods", c["ods_model"]
    if avoid_ods:
        return None, "ODS stalled on this job and Claude is unavailable (cap reached or no key); waiting for Claude"
    return None, "Claude unavailable (cap reached, cooling down or no key) and ODS not answering"


# ---------------------------------------------------------------- prompts
PROTOCOL = """
You work in steps. Another assistant (a different AI model) may do the next step, and it
will see ONLY your HANDOFF, not this conversation. So:
1. Do one solid chunk of work toward the goal.
2. If you need file contents, write lines like:  READ: <absolute path>   (max 4 per step)
3. End with exactly these sections:
## HANDOFF
(max 250 words: what is done, key findings, what remains, exact next action)
## STATUS
CONTINUE or DONE
"""

MONEY_LENS = """
Think like a sharp, independent operator who has to make this pay within 6 months, not like a cheerleader.
Rules:
- Start from who pays, how much, how often, and why they would pick THIS over what already exists.
- Look for the unfair angle: the niche others ignore, data or access the owner has that others don't
  (languages, a professional network, domain skills, a working local AI stack),
  a cheaper delivery model, a faster feedback loop, or a combination of his projects nobody else can copy.
- Give realistic odds. Name base rates (most trading strategies lose after costs; most side projects earn $0),
  costs, time to first dollar, and the single assumption that kills the idea if false.
- Prefer the smallest test that proves or kills the idea in under 2 weeks for under $100.
- For anything touching trading, investing or advice to others: flag licensing/regulatory limits
  (e.g. SEC/FINRA, CFTC for prediction markets) and never promise returns.
- Be specific: names of buyers, channels, prices, numbers. No generic advice.
"""

SYSTEMS = {
    "triage": "You rank the owner's projects by realistic odds of making money soon and of standing out from the crowd."
              + MONEY_LENS + """
Final step: a ranked table (project, money path, odds 1-10, time to first $, why it stands out or not),
then one line per project to deep-dive, best first, in exactly this form:
TOP: <domain folder>/<project folder>
""" + PROTOCOL,
    "strategy": "You are rethinking ONE of the owner's projects from scratch to give it the best odds of making money "
                "and standing out. Read its files first (READ lines), then think outside the box." + MONEY_LENS + """
Final step must contain, in this order:
1. What it is today (3 lines, honest)
2. Three unconventional angles to make money, each with buyer, price, channel, odds 1-10, what makes it stand out
3. The one you recommend and why
4. Two-week test plan: daily steps, budget, the metric that says go / kill
5. Concrete upgrades to the code or product, most valuable first
""" + PROTOCOL,
    "think": "You are a careful research and reasoning partner for the owner. Be concrete, verify claims, "
             "separate facts from guesses, and build toward a written conclusion." + PROTOCOL,
    "evaluate": "You evaluate the owner's software projects for real-world value: is it working, maintained, "
                "useful, worth keeping? Score each 1-10 with one-line reasons, then give keep/park/archive "
                "recommendations. Write the final report in markdown." + PROTOCOL,
    "codefix": "You are a senior engineer fixing bugs in a git repository. Read before changing. "
               "Propose fixes ONLY as a unified diff in a ```diff block with paths relative to the repo root "
               "(a/... b/...). Keep changes minimal. You may also write the line RUN_TESTS to run the test "
               "suite. Your diff is applied on a separate branch and tested; you will see the result." + PROTOCOL,
}


def secretish(p):
    """Never read or send anything that looks like a credential, whatever folder it is in."""
    bad = any(part.startswith(".secret") or part in (".ssh", ".gnupg") for part in p.parts)
    name = p.name.lower()
    return bad or name.startswith(".env") or any(w in name for w in ("key", "token", "credential", "secret", "password")) \
        or name.endswith((".pem", ".pickle", ".db", ".sqlite"))


def allowed(path, c, extra_root=None, want_dir=False):
    lexical = Path(os.path.abspath(os.path.expanduser(path)))
    real = lexical.resolve()
    roots = [Path(os.path.abspath(os.path.expanduser(r))) for r in c["read_roots"]]
    if extra_root:
        roots.append(Path(os.path.abspath(extra_root)))
    ok_kind = real.is_dir() if want_dir else real.is_file()
    inside = any(str(lexical).startswith(str(r) + os.sep) or str(real).startswith(str(r.resolve()) + os.sep)
                 for r in roots)
    return ok_kind and inside and not secretish(lexical) and not secretish(real)


def read_files(text, c, extra_root=None):
    out = []
    for m in re.findall(r"^READ:\s*(.+)$", text, flags=re.M)[:4]:
        p = os.path.expanduser(m.strip().strip("`").strip())
        if extra_root and not p.startswith("/"):
            p = str(Path(extra_root) / p)
        if allowed(p, c, extra_root, want_dir=True):
            names = sorted(x.name + ("/" if x.is_dir() else "") for x in Path(p).iterdir()
                           if not x.name.startswith(".") or x.name in (".github",))[:80]
            readme = next((Path(p) / n for n in ("README.md", "readme.md", "AGENTS.md", "CLAUDE.md")
                           if (Path(p) / n).is_file()), None)
            body = f"--- {p} (folder) contains: " + ", ".join(names)
            if readme:
                body += f"\n--- {readme} (start):\n" + readme.read_text(errors="replace")[:3000]
            out.append(body)
            continue
        if not allowed(p, c, extra_root):
            out.append(f"--- {p}: not readable (outside allowed folders or missing)")
            continue
        try:
            body = Path(p).read_text(errors="replace")[: c["max_read_chars"]]
            out.append(f"--- {p}\n{body}")
        except OSError as e:
            out.append(f"--- {p}: {e}")
    return "\n\n".join(out)


def project_inventory():
    rows = []
    for domain in sorted((HOME / "Projects").glob("[0-9][0-9]_*")):
        for proj in sorted(domain.iterdir()):
            if not proj.is_dir() or proj.name.startswith("_"):
                continue
            real = proj.resolve()
            files = 0
            newest = 0.0
            for dp, dn, fn in os.walk(real):
                dn[:] = [d for d in dn if d not in (".git", "node_modules", ".venv", "venv", "__pycache__", "data", "models")]
                files += len(fn)
                for f in fn[:50]:
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(dp, f)))
                    except OSError:
                        pass
                if files > 5000:
                    break
            last = ""
            if (real / ".git").exists():
                r = subprocess.run(["git", "-C", str(real), "log", "-1", "--format=%cs %s"],
                                   capture_output=True, text=True)
                last = r.stdout.strip()[:90]
            readme = ""
            for name in ("README.md", "readme.md", "AGENTS.md", "CLAUDE.md"):
                if (real / name).exists():
                    readme = (real / name).read_text(errors="replace")[:400].replace("\n", " ")
                    break
            mod = datetime.fromtimestamp(newest).strftime("%Y-%m-%d") if newest else "?"
            git = f"last commit: {last}" if last else "not a git repo"
            rows.append(f"- {domain.name}/{proj.name}: {files}{'+' if files > 5000 else ''} files; newest file: {mod}; "
                        f"{git}; readme: {readme or 'none'}")
    return "\n".join(rows)


def project_brief(path):
    root = Path(path).resolve()
    lines = [f"PROJECT FOLDER: {path}"]
    top = sorted(p.name + ("/" if p.is_dir() else "") for p in root.iterdir() if not p.name.startswith("."))[:80]
    lines.append("TOP-LEVEL ENTRIES: " + ", ".join(top))
    files = root / "_files"
    if files.is_dir():
        lines.append("ITS NOTES/DOCS (_files): " + ", ".join(sorted(p.name for p in files.iterdir()))[:2000])
    for name in ("README.md", "AGENTS.md", "CLAUDE.md", "README.txt"):
        if (root / name).exists():
            lines.append(f"{name} (start):\n" + (root / name).read_text(errors="replace")[:3000])
            break
    if (root / ".git").exists():
        r = subprocess.run(["git", "-C", str(root), "log", "-8", "--format=%cs %s"], capture_output=True, text=True)
        lines.append("RECENT COMMITS:\n" + r.stdout.strip())
    lines.append("You may READ files inside this folder using paths relative to it or absolute paths.")
    return "\n\n".join(lines)


def build_prompt(job, c, pending_chars=None):
    parts = [f"GOAL: {job['goal']}", f"STEP {job['steps'] + 1} of at most {max_steps(c, job['kind'])}"]
    if job["steps"] + 1 >= max_steps(c, job["kind"]):
        parts.append("FINAL STEP: write the complete final deliverable now (full report, tables, recommendations), "
                     "using what you already know. Do not plan further. End with ## STATUS DONE.")
    elif job["steps"] >= 1:
        parts.append("Make real progress this step: either request the exact files you need with READ lines, "
                     "or write a concrete part of the deliverable. Do not repeat the previous handoff.")
    if job["kind"] == "triage" and job["steps"] == 0:
        parts.append("PROJECT INVENTORY:\n" + project_inventory())
    if job["kind"] == "strategy" and job["steps"] == 0:
        parts.append(project_brief(job["repo"]))
    if job["kind"] == "evaluate" and job["steps"] == 0:
        parts.append("PROJECT INVENTORY:\n" + project_inventory())
    if job["kind"] == "codefix":
        parts.append(f"REPO: {job['repo']}  (your fixes go to branch brain/job-{job['id']})")
        if job["steps"] == 0:
            r = subprocess.run(["git", "-C", job["repo"], "ls-files"], capture_output=True, text=True)
            files = r.stdout.splitlines()
            parts.append("TRACKED FILES (first 300):\n" + "\n".join(files[:300]))
            parts.append("TEST RUN:\n" + run_tests(job["repo"])[:4000])
    if job["handoff"]:
        parts.append("HANDOFF FROM PREVIOUS STEP:\n" + job["handoff"])
    lastout = job["lastout"] if "lastout" in job.keys() else ""
    if lastout:
        parts.append("WORK PRODUCED SO FAR (keep and build on it; the final deliverable must include it):\n" + lastout)
    if job["pending"]:
        pend = job["pending"] if pending_chars is None else job["pending"][:max(0, pending_chars)]
        if pend:
            parts.append("RESULTS OF YOUR REQUESTS:\n" + pend + ("\n[...trimmed]" if len(pend) < len(job["pending"]) else ""))
    return "\n\n".join(parts)


def fit_prompt(job, c, system, limit_tokens):
    """Full prompt if it fits, else trim the request results until it does."""
    prompt = build_prompt(job, c)
    over = est_tokens(system + prompt) - limit_tokens
    if over <= 0:
        return prompt
    keep = len(job["pending"]) - over * 4 - 200
    prompt = build_prompt(job, c, pending_chars=keep)
    over = est_tokens(system + prompt) - limit_tokens
    if over > 0 and "lastout" in job.keys() and job["lastout"]:
        j = dict(job); j["lastout"] = job["lastout"][-max(0, len(job["lastout"]) - over * 4 - 200):]
        prompt = build_prompt(j, c, pending_chars=keep)
    return prompt


# ---------------------------------------------------------------- code fixes
def run_tests(repo):
    repo = Path(repo)
    if (repo / "pytest.ini").exists() or (repo / "tests").is_dir() or list(repo.glob("test_*.py")):
        cmd = [sys.executable, "-m", "pytest", "-x", "-q"]
    elif (repo / "package.json").exists():
        cmd = ["npm", "test", "--silent"]
    else:
        return "(no test suite detected)"
    try:
        r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=600)
        return f"$ {' '.join(cmd)}\nexit {r.returncode}\n{(r.stdout + r.stderr)[-6000:]}"
    except Exception as e:
        return f"tests could not run: {e}"


def worktree(job):
    wt = WORKTREES / f"job-{job['id']}"
    if not wt.exists():
        WORKTREES.mkdir(parents=True, exist_ok=True)
        branch = f"brain/job-{job['id']}"
        r = subprocess.run(["git", "-C", job["repo"], "worktree", "add", "-b", branch, str(wt)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["git", "-C", job["repo"], "worktree", "add", str(wt), branch],
                           capture_output=True, text=True)
    return wt


def apply_diff(job, text):
    m = re.search(r"```diff\n(.*?)```", text, flags=re.S)
    if not m:
        return ""
    wt = worktree(job)
    patch = m.group(1)
    chk = subprocess.run(["git", "-C", str(wt), "apply", "--check", "-"], input=patch,
                         capture_output=True, text=True)
    if chk.returncode != 0:
        return f"DIFF REJECTED (did not apply cleanly):\n{chk.stderr[-1500:]}"
    subprocess.run(["git", "-C", str(wt), "apply", "-"], input=patch, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(wt), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-q", "-m", f"relay job {job['id']} step {job['steps'] + 1}"],
                   capture_output=True)
    return "DIFF APPLIED on branch brain/job-%d and committed.\nTEST RESULT:\n%s" % (job["id"], run_tests(wt)[:4000])


# ---------------------------------------------------------------- one step
def step(cx, c, job):
    global current_job_id
    current_job_id = job["id"]
    system = SYSTEMS[job["kind"]]
    claude_prompt = fit_prompt(job, c, system, c["claude_context_tokens"])
    brain, model = choose_brain(c, cx, job["kind"], est_tokens(system + claude_prompt))
    if brain is None:
        log(f"job {job['id']}: waiting — {model}")
        if get_state(cx, "stuck_notified") != date.today().isoformat():
            notify(c, f"STUCK: {model}. Queue waits; it retries every 10 minutes.")
            set_state(cx, "stuck_notified", date.today().isoformat())
        return False

    if brain == "claude":
        prompt = claude_prompt
    else:
        # local window is small: room for the answer, then trim request results to fit
        prompt = fit_prompt(job, c, system, c["ods_context_tokens"] - c["ods_max_output"])
    t0 = time.time()
    try:
        if brain == "claude":
            text, usage = call_claude(c, model, system, prompt, c["claude_max_output"])
            cost = record_claude(model, usage, job["id"])
        else:
            text, usage = call_ods(c, system, prompt, c["ods_max_output"])
            cost = 0.0
    except Exception as e:
        log(f"job {job['id']}: {brain} failed: {e}")
        if brain == "claude":
            until = datetime.now() + timedelta(minutes=c["claude_cooldown_min"])
            set_state(cx, "claude_cooldown_until", until.isoformat(timespec="seconds"))
        return False
    if job["steps"] == 0:
        notify(c, f"START #{job['id']} {job['kind']} on {brain} ({model}): {short(job['goal'])}")
    elif job["last_brain"] and job["last_brain"] != brain:
        why = "Claude back" if brain == "claude" else "Claude cap reached, cooling down, or step too big"
        notify(c, f"SWITCH #{job['id']} {job['last_brain']} -> {brain} ({why}). Continuing from the handoff.")
    n = job["steps"] + 1
    _h = re.search(r"##\s*HANDOFF\s*\n(.*?)(?=\n##\s*STATUS|\Z)", text, flags=re.S)
    _new = (_h.group(1).strip() if _h else text[-1500:])
    import difflib
    same = job["handoff"] and difflib.SequenceMatcher(None, job["handoff"], _new[:2500]).ratio() > 0.85 \
        and "READ:" not in text and "```diff" not in text
    stalls = int(get_state(cx, f"stall_{job['id']}", "0") or 0) + 1 if same else 0
    set_state(cx, f"stall_{job['id']}", stalls)
    if stalls >= 2:
        if brain == "ods":
            set_state(cx, "avoid_ods_job", job["id"])
            notify(c, f"LOOP #{job['id']}: ODS repeated itself {stalls + 1} times. Next steps go to Claude "
                      f"(or wait for it if the cap is reached).")
        else:
            cx.execute("UPDATE jobs SET status='stalled', updated=? WHERE id=?",
                       (datetime.now().isoformat(timespec="seconds"), job["id"]))
            cx.commit()
            notify(c, f"STALLED #{job['id']}: Claude is going in circles too. Stopped it. Reword the goal and re-add it.")
            return True
    handoff = re.search(r"##\s*HANDOFF\s*\n(.*?)(?=\n##\s*STATUS|\Z)", text, flags=re.S)
    handoff = (handoff.group(1).strip() if handoff else text[-1500:])[:2500]
    done = bool(re.search(r"##\s*STATUS\s*\n\s*DONE", text))
    pending = []
    files = read_files(text, c, extra_root=job["repo"] or None)
    if files:
        pending.append(files)
    if job["kind"] == "codefix":
        res = apply_diff(job, text)
        if res:
            pending.append(res)
        if "RUN_TESTS" in text and not res:
            pending.append("TEST RUN:\n" + run_tests(worktree(job))[:4000])
    status = "done" if done or n >= max_steps(c, job["kind"]) else "active"
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / f"job-{job['id']}.md", "a") as f:
        f.write(f"\n\n---\n### Step {n} — {brain} ({model}) — {datetime.now():%Y-%m-%d %H:%M} "
                f"— {time.time() - t0:.0f}s — ${cost:.4f}\n\n{text}\n")
    work = re.split(r"\n##\s*HANDOFF", text)[0]
    work = re.sub(r"^READ:.*$", "", work, flags=re.M).strip()
    prev = job["lastout"] if "lastout" in job.keys() else ""
    # keep the richer of the two so a thin step never wipes out earlier results
    lastout = (work if len(work) >= 0.5 * len(prev) else prev + "\n\n" + work)[-12000:]
    cx.execute("UPDATE jobs SET steps=?, handoff=?, pending=?, status=?, last_brain=?, updated=?, lastout=? WHERE id=?",
               (n, handoff, "\n\n".join(pending), status, brain, datetime.now().isoformat(timespec="seconds"),
                lastout, job["id"]))
    cx.execute("INSERT INTO steps(job_id,n,brain,model,in_tok,out_tok,cost,ts) VALUES(?,?,?,?,?,?,?,?)",
               (job["id"], n, brain, model, usage.get("input_tokens", usage.get("prompt_tokens", 0)),
                usage.get("output_tokens", usage.get("completion_tokens", 0)), cost, datetime.now().isoformat()))
    cx.commit()
    if status == "done" and get_state(cx, "avoid_ods_job") == str(job["id"]):
        set_state(cx, "avoid_ods_job", "")
    if status == "done" and job["kind"] == "strategy" and job["repo"]:
        dest = Path(job["repo"]) / "_files"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / f"strategy-{date.today()}.md").write_text((OUT / f"job-{job['id']}.md").read_text())
    if status == "done" and job["kind"] == "triage":
        queued = []
        for t in re.findall(r"^TOP:\s*(\S+)", (OUT / f"job-{job['id']}.md").read_text(), flags=re.M):
            proj = (HOME / "Projects" / t.strip("/`")).resolve()
            if proj.is_dir() and str(proj).startswith(str((HOME / "Projects").resolve())) and t not in queued:
                cx.execute("INSERT INTO jobs(kind,goal,repo,created,updated) VALUES('strategy',?,?,?,?)",
                           (f"Best odds to make money and stand out: {t}", str(proj),
                            datetime.now().isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds")))
                queued.append(t)
            if len(queued) >= c["strategy_top_n"]:
                break
        cx.commit()
        if queued:
            notify(c, "Deep-dives queued, best odds first:\n" + "\n".join(f"- {q}" for q in queued))
    if status == "done" and job["kind"] == "evaluate":
        rep = Path(os.path.expanduser(c["reports_dir"]))
        rep.mkdir(parents=True, exist_ok=True)
        (rep / f"evaluation-{date.today()}.md").write_text((OUT / f"job-{job['id']}.md").read_text())
    log(f"job {job['id']} step {n} by {brain} ({model}) ${cost:.4f} -> {status}")
    if status == "done":
        notify(c, debrief(c, cx, cx.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()))
    return True


# ---------------------------------------------------------------- commands
def add(kind, rest, quiet=False):
    if kind not in SYSTEMS:
        sys.exit(f"kind must be one of: {', '.join(SYSTEMS)}")
    repo = ""
    if kind == "strategy":
        if not rest:
            sys.exit("usage: relay.py add strategy <project folder> [extra goal]")
        repo = str(Path(os.path.expanduser(rest[0])).resolve())
        if not Path(repo).is_dir():
            sys.exit(f"{repo} is not a folder")
        goal = " ".join(rest[1:]) or f"Best odds to make money and stand out: {Path(repo).name}"
        cx = db()
        now = datetime.now().isoformat(timespec="seconds")
        cur = cx.execute("INSERT INTO jobs(kind,goal,repo,created,updated) VALUES(?,?,?,?,?)", (kind, goal, repo, now, now))
        cx.commit()
        if not quiet:
            print(f"queued job {cur.lastrowid}: strategy — {goal}")
        return cur.lastrowid
    if kind == "codefix":
        if not rest:
            sys.exit("usage: relay.py add codefix <repo path> \"what to fix\"")
        repo = str(Path(os.path.expanduser(rest[0])).resolve())
        if not (Path(repo) / ".git").exists():
            sys.exit(f"{repo} is not a git repository")
        rest = rest[1:]
    goal = " ".join(rest) or {"evaluate": "Evaluate all projects in ~/Projects and write the report.",
                              "triage": "Rank every project by realistic odds of making money soon and standing out; "
                                        "pick the ones worth a deep-dive.",
                              "codefix": "Find and fix the most important bugs; make the tests pass."}.get(kind, "")
    if not goal:
        sys.exit("give the goal text")
    cx = db()
    now = datetime.now().isoformat(timespec="seconds")
    cur = cx.execute("INSERT INTO jobs(kind,goal,repo,created,updated) VALUES(?,?,?,?,?)", (kind, goal, repo, now, now))
    cx.commit()
    if not quiet:
        print(f"queued job {cur.lastrowid}: {kind} — {goal}")
    return cur.lastrowid


def tick():
    c = cfg()
    cx = db()
    phone_commands(c, cx)
    if datetime.now().hour >= c["daily_digest_hour"] and get_state(cx, "digest_day") != date.today().isoformat():
        notify(c, digest(c, cx))
        set_state(cx, "digest_day", date.today().isoformat())
    if get_state(cx, "paused") == "1":
        log("paused — skipping work")
        return
    if c.get("weekly_evaluation"):
        last = get_state(cx, "last_weekly_eval")
        if not last or datetime.now() - datetime.fromisoformat(last) > timedelta(days=7):
            cx.execute("INSERT INTO jobs(kind,goal,repo,created,updated) VALUES('evaluate',?, '', ?, ?)",
                       ("Weekly evaluation of all projects in ~/Projects.", datetime.now().isoformat(), datetime.now().isoformat()))
            cx.commit()
            set_state(cx, "last_weekly_eval", datetime.now().isoformat(timespec="seconds"))
    deadline = time.time() + c["tick_minutes"] * 60
    skip = set()          # jobs that must wait (e.g. for Claude) don't block the rest of the queue
    while time.time() < deadline:
        jobs = cx.execute("SELECT * FROM jobs WHERE status IN ('active','queued') ORDER BY "
                          "CASE status WHEN 'active' THEN 0 ELSE 1 END, id").fetchall()
        job = next((j for j in jobs if j["id"] not in skip), None)
        if not job:
            break
        if not step(cx, c, job):
            skip.add(job["id"])


def status():
    cx = db()
    c = cfg()
    print(f"Claude spend today: ${spent_today():.3f} of ${c['daily_cap_usd']:.2f} cap")
    cool = get_state(cx, "claude_cooldown_until")
    if cool and datetime.now() < datetime.fromisoformat(cool):
        print(f"Claude cooling down until {cool} (ODS covers until then)")
    print(f"ODS: {'up' if ods_alive(c) else 'DOWN'}   Claude key: {'set' if claude_key(c) else 'MISSING'}")
    print()
    for j in cx.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 20"):
        spent = cx.execute("SELECT COALESCE(SUM(cost),0) FROM steps WHERE job_id=?", (j["id"],)).fetchone()[0]
        print(f"#{j['id']:<4} {j['status']:<9} {j['kind']:<9} steps {j['steps']:<3} last {j['last_brain'] or '-':<7} "
              f"${spent:.3f}  {j['goal'][:60]}")


def show(job_id):
    p = OUT / f"job-{job_id}.md"
    print(p.read_text() if p.exists() else "no output yet")


def cancel(job_id):
    cx = db()
    cx.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job_id,))
    cx.commit()
    print(f"job {job_id} cancelled")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__); sys.exit(0)
    if a[0] == "add" and len(a) >= 2:
        add(a[1], a[2:])
    elif a[0] == "tick":
        tick()
    elif a[0] == "status":
        status()
    elif a[0] == "show" and len(a) == 2:
        show(int(a[1]))
    elif a[0] == "cancel" and len(a) == 2:
        cancel(int(a[1]))
    elif a[0] == "strategy-all":
        add("triage", [])
        print("Triage queued; deep-dives on the best projects are queued automatically when it finishes.")
    elif a[0] == "telegram-link":
        c = cfg()
        tok = keyring("telegram-bot-token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not tok:
            sys.exit("No bot token yet. Copy the token BotFather gave you, then type: keys clip telegram-bot-token")
        if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", tok):
            sys.exit("The saved bot token doesn't look right (should be like 123456789:AAH...). Copy it again: keys clip telegram-bot-token")
        try:
            with urllib.request.urlopen(f"{c['telegram_api']}/bot{tok}/getMe", timeout=20) as r:
                me = json.loads(r.read())["result"]
            with urllib.request.urlopen(f"{c['telegram_api']}/bot{tok}/getUpdates", timeout=20) as r:
                ups = json.loads(r.read()).get("result", [])
        except urllib.error.HTTPError as e:
            sys.exit(f"Telegram refused the token (HTTP {e.code}). Copy it again from BotFather: keys clip telegram-bot-token")
        except Exception as e:
            sys.exit(f"Could not reach Telegram: {e}")
        chats = [u["message"]["chat"] for u in ups if u.get("message", {}).get("chat", {}).get("type") == "private"]
        if not chats:
            sys.exit(f"Bot @{me.get('username')} works, but it has no message from you yet.\n"
                     f"On your phone: open t.me/{me.get('username')} , press START, send 'hi', then run this again.")
        chat = chats[-1]
        r = subprocess.run(["secret-tool", "store", f"--label=API key: telegram-chat-id", "service", "apikeys",
                            "name", "telegram-chat-id", "note", f"chat with {chat.get('first_name', '')}",
                            "added", date.today().isoformat()], input=str(chat["id"]), text=True, capture_output=True)
        if r.returncode != 0:
            sys.exit(f"Could not save the chat ID in the keyring: {r.stderr.strip()}")
        tg(c, "sendMessage", {"chat_id": chat["id"], "text": "Relay connected. You'll get short updates here. Send /report or /status anytime."})
        print(f"Linked: bot @{me.get('username')} -> your chat ({chat.get('first_name', '')}). Check your phone.")
    elif a[0] == "report":
        c = cfg(); cx = db(); t = digest(c, cx); print(t); notify(c, t)
    elif a[0] == "brief" and len(a) == 2:
        c = cfg(); cx = db(); j = cx.execute("SELECT * FROM jobs WHERE id=?", (int(a[1]),)).fetchone()
        print(make_brief(c, cx, j) if j else "no such job")
    elif a[0] == "check-claude":
        c = cfg(); k = claude_key(c)
        if not k:
            sys.exit("No Claude key found. Run: keys clip anthropic")
        kind = "ADMIN key (cannot call models)" if k.startswith("sk-ant-admin") else \
               "API key" if k.startswith("sk-ant-api") else "UNKNOWN format"
        print(f"Key: {k[:10]}...{k[-4:]}  length {len(k)}  type: {kind}")
        if any(ch.isspace() for ch in k):
            print("WARNING: the key contains spaces or line breaks; copy it again.")
        body = {"model": c["claude_model_cheap"], "max_tokens": 5, "messages": [{"role": "user", "content": "ping"}]}
        req = urllib.request.Request(c["claude_api_url"], data=json.dumps(body).encode(), method="POST",
                                     headers={"x-api-key": k, "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read()
            set_state(db(), "claude_cooldown_until", "")
            print("Claude answered: the key works. Cooldown cleared; the relay uses Claude again.")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            hint = {401: "Key rejected: copy a fresh key from console.anthropic.com > API Keys (a regular key, not Admin).",
                    403: "Key valid but not allowed: check the workspace/organization permissions.",
                    400: "Request refused: often no credit balance. Check console.anthropic.com > Billing.",
                    404: f"Model {c['claude_model_cheap']} not found for this key: tell Claude to update relay.json.",
                    429: "Rate limited: wait a minute and retry.",
                    529: "Anthropic overloaded: retry later."}.get(e.code, "")
            print(f"Claude said HTTP {e.code}: {detail}")
            if hint:
                print("=> " + hint)
        except Exception as e:
            print(f"Could not reach Claude: {e}")
    elif a[0] in ("pause", "resume"):
        set_state(db(), "paused", "1" if a[0] == "pause" else "0"); print(a[0] + "d")
    elif a[0] == "notify-test":
        tok, chat = tg_conf(cfg())
        if not (tok and chat):
            sys.exit("Telegram not configured: run  keys add telegram-bot-token  and  keys add telegram-chat-id")
        notify(cfg(), "Relay connected. You'll get short updates here. Send /status anytime.")
        print("sent")
    else:
        print(__doc__)
