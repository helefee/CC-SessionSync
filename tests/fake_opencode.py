# -*- coding: utf-8 -*-
"""假的 opencode 命令行（只给场景测试用；真的 OpenCode 在 tools/verify_opencode.py 里验）：
  --version → 2.1.1（FAKE_OC_MAJOR=1 → 1.19.3）
  session import --standalone [--directory d] <file>   照 2.x 的规矩检查（info.projectID、id 前缀），写进 session_v2 / session_message
  import <file>                                         1.x：写进 session / message / part
  session delete [--standalone] <id>"""
import json, os, sqlite3, sys
from pathlib import Path

a = sys.argv[1:]
major = os.environ.get("FAKE_OC_MAJOR", "2")
if a[:1] == ["--version"]:
    print("2.1.1" if major == "2" else "1.19.3"); sys.exit(0)
db = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "opencode" / "opencode.db"
log = os.environ.get("FAKE_OC_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": a, "cwd": os.getcwd()}) + "\n")
c = sqlite3.connect(str(db))
if a[:2] == ["session", "import"]:
    d = json.loads(Path(a[-1]).read_text(encoding="utf-8"))
    info = d["info"]
    if "projectID" not in info or not info.get("id", "").startswith("ses_"):
        print("Bad Request: Missing key at info.projectID", file=sys.stderr); sys.exit(1)
    dr = a[a.index("--directory") + 1] if "--directory" in a else os.getcwd()
    c.execute("insert into session_v2 (id, project_id, slug, directory, title, version, time_created, time_updated) values (?, ?, 's', ?, ?, '2.1.1', ?, ?)",
              (info["id"], info["projectID"], dr, info.get("title"), info["time"]["created"], info["time"]["updated"]))
    for i, m in enumerate(d["messages"]):
        assert m["id"].startswith("msg_") and m["type"] in ("user", "assistant")
        data = {k: v for k, v in m.items() if k not in ("id", "type")}
        c.execute("insert into session_message values (?, ?, ?, ?, ?, ?, ?)", (m["id"], info["id"], m["type"], i, m["time"]["created"], m["time"]["created"], json.dumps(data)))
    c.commit(); print("Imported session: " + info["id"]); sys.exit(0)
if a[:1] == ["import"]:
    d = json.loads(Path(a[-1]).read_text(encoding="utf-8"))
    info = d["info"]
    c.execute("insert into session (id, project_id, slug, directory, title, version, time_created, time_updated) values (?, ?, ?, ?, ?, '1.19.3', ?, ?)",
              (info["id"], info["projectID"], info["slug"], info["directory"], info["title"], info["time"]["created"], info["time"]["updated"]))
    for m in d["messages"]:
        mi = m["info"]
        c.execute("insert into message values (?, ?, ?, ?, ?)", (mi["id"], info["id"], mi["time"]["created"], mi["time"]["created"],
                                                                 json.dumps({k: v for k, v in mi.items() if k not in ("id", "sessionID")})))
        for p in m["parts"]:
            c.execute("insert into part values (?, ?, ?, ?, ?, ?)", (p["id"], mi["id"], info["id"], 0, 0,
                                                                     json.dumps({k: v for k, v in p.items() if k not in ("id", "sessionID", "messageID")})))
    c.commit(); print("Imported " + info["id"]); sys.exit(0)
if a[:2] == ["session", "delete"]:
    sid = a[-1]
    for t, k in (("session_message", "session_id"), ("session_v2", "id")):
        try: c.execute(f"delete from {t} where {k}=?", (sid,))
        except sqlite3.OperationalError: pass
    for t, k in (("part", "session_id"), ("message", "session_id"), ("session", "id")):
        try: c.execute(f"delete from {t} where {k}=?", (sid,))
        except sqlite3.OperationalError: pass
    c.commit(); sys.exit(0)
print("unknown", a, file=sys.stderr); sys.exit(2)
