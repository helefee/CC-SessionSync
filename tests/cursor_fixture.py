# -*- coding: utf-8 -*-
"""造 Cursor 的数据（测试用，全是假内容），结构和本机 Cursor 3.25 的一样（只照着键名 / 字段名）：
编辑器的 state.vscdb（composerData / bubbleId / composerHeaders）、workspaceStorage、agent 记录（agent-transcripts）、cursor-agent 的 store.db。"""
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path


def ws_storage(user: Path, folder: str, wid: str | None = None) -> str:
    wid = wid or uuid.uuid4().hex
    d = user / "workspaceStorage" / wid
    d.mkdir(parents=True, exist_ok=True)
    uri = "file://" + ("/" + folder.replace("\\", "/") if ":" in folder[:3] else folder)
    (d / "workspace.json").write_text(json.dumps({"folder": uri}))
    return wid


def _db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("create table if not exists ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    c.execute("create table if not exists cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)")
    c.execute("create table if not exists composerHeaders (composerId TEXT PRIMARY KEY, workspaceId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER,"
              " isArchived INTEGER, isSubagent INTEGER, recency INTEGER, checkpointAt INTEGER, value TEXT, subagentTypeName TEXT)")
    return c


class Composer:
    """一个编辑器里的对话。bubble 按顺序加。"""
    def __init__(self, cid=None, name="修首页白屏", cwd=None, wid=None, t0=1_760_000_000_000, mode="agent", model="claude-4.5-sonnet",
                 parent=None, archived=False):
        self.cid = cid or str(uuid.uuid4())
        self.name, self.cwd, self.wid, self.t, self.mode, self.model, self.parent, self.archived = name, cwd, wid, t0, mode, model, parent, archived
        self.t0, self.bubbles = t0, []

    def _b(self, typ, **kw):
        self.t += 1000
        from datetime import datetime, timezone
        b = {"_v": 3, "type": typ, "bubbleId": str(uuid.uuid4()), "createdAt": datetime.fromtimestamp(self.t / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
             "text": "", "images": [], "codeBlocks": [], "tokenCount": {"inputTokens": 0, "outputTokens": 0}, "capabilities": [], "toolResults": []}
        b.update(kw)
        self.bubbles.append(b)
        return b

    def user(self, text, images=0):
        return self._b(1, text=text, images=[{"path": f"img{i}.png", "dimension": {"width": 10, "height": 10}} for i in range(images)])

    def thinking(self, text):
        return self._b(2, thinking={"text": text, "signature": "sig"}, capabilityType=30)

    def tool(self, name, params, result, status="completed", error=None):
        tf = {"tool": 15, "toolIndex": 0, "toolCallId": "call_" + uuid.uuid4().hex[:8], "modelCallId": "m", "name": name,
              "params": json.dumps(params), "rawArgs": json.dumps(params), "status": status, "toolCallBinary": ""}
        if result is not None:
            tf["result"] = json.dumps(result)
        if error:
            tf["error"] = error
        return self._b(2, toolFormerData=tf, capabilityType=15)

    def say(self, text, tokens=(1200, 300)):
        return self._b(2, text=text, modelInfo={"modelName": self.model}, tokenCount={"inputTokens": tokens[0], "outputTokens": tokens[1]})

    def write(self, user_dir: Path, headers=True):
        c = _db(user_dir / "globalStorage" / "state.vscdb")
        d = {"_v": 18, "composerId": self.cid, "name": self.name, "createdAt": self.t0, "lastUpdatedAt": self.t, "unifiedMode": self.mode,
             "forceMode": "edit", "modelConfig": {"modelName": self.model, "maxMode": False}, "isDraft": False, "richText": "", "text": "",
             "conversationState": "~AAAA", "blobEncryptionKey": "not-a-real-key", "fullConversationHeadersOnly":
                 [{"bubbleId": b["bubbleId"], "type": b["type"]} for b in self.bubbles] if headers else [],
             "context": {}, "codeBlockData": {}, "originalFileStates": {}, "conversationMap": {}, "status": "completed"}
        if self.cwd:
            d["workspaceIdentifier"] = {"id": self.wid or "w", "uri": {"$mid": 1, "fsPath": self.cwd, "external": "file:///x", "path": "/x", "scheme": "file"}}
        elif self.wid:
            d["workspaceIdentifier"] = {"id": self.wid}
        if self.parent:
            d["subagentInfo"] = {"parentComposerId": self.parent, "subagentType": 1, "subagentTypeName": "explore", "conversationLengthAtSpawn": 3}
        c.execute("insert into cursorDiskKV values(?, ?)", (f"composerData:{self.cid}", json.dumps(d, ensure_ascii=False)))
        for b in self.bubbles:
            c.execute("insert into cursorDiskKV values(?, ?)", (f"bubbleId:{self.cid}:{b['bubbleId']}", json.dumps(b, ensure_ascii=False)))
        c.execute("insert or replace into composerHeaders(composerId, workspaceId, createdAt, lastUpdatedAt, isArchived, isSubagent, value) values(?,?,?,?,?,?,?)",
                  (self.cid, self.wid or "empty-window", self.t0, self.t, int(self.archived), int(bool(self.parent)), json.dumps({"composerId": self.cid})))
        c.execute("insert or replace into cursorDiskKV values(?, ?)", ("agentKv:blob:" + hashlib.sha256(self.cid.encode()).hexdigest(), b"\n\x20" + b"x" * 32))
        c.commit()
        c.close()
        return self


def rich(user_dir: Path, cwd: str, wid: str | None = None, marker: str = "CURSOR-MARK") -> Composer:
    """一个什么都有的对话：你说的（带图）、思考、读文件、跑命令（出错一次）、改文件、回复。"""
    c = Composer(name="修首页白屏", cwd=cwd, wid=wid)
    c.user(f"{marker} 首页白屏了，帮我看看", images=1)
    c.thinking("THINKING hidden: 先看路由")
    c.tool("read_file_v2", {"targetFile": "src/App.tsx", "limit": 200}, {"contents": "export default function App(){}\n" * 3})
    c.tool("run_terminal_command_v2", {"command": "npm test", "cwd": cwd}, {"output": "1 failing\nTypeError: x is undefined"}, status="error", error="exit 1")
    c.tool("edit_file_v2", {"relativeWorkspacePath": "src/App.tsx"}, {"diff": "+ guard"})
    c.say("找到了：App 里没判空。已经加了判断，测试过了。FINAL ANSWER")
    c.user("再把 README 补一句")
    c.say("README 加了一句安装说明。")
    return c


def transcript(home: Path, project: str, cid: str, turns, sub_of: str | None = None) -> Path:
    """~/.cursor/projects/<项目>/agent-transcripts/<对话号>/<对话号>.jsonl；turns=[(role, text | ('tool', name, input))]"""
    d = home / "projects" / project / "agent-transcripts" / (sub_of or cid)
    f = (d / "subagents" / f"{cid}.jsonl") if sub_of else (d / f"{cid}.jsonl")
    f.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for role, x in turns:
        if isinstance(x, tuple):
            content = [{"type": "tool_use", "name": x[1], "input": x[2]}]
        elif role == "user":
            content = [{"type": "text", "text": "<user_info>OS: test</user_info>"}, {"type": "text", "text": f"<user_query>\n{x}\n</user_query>"}]
        else:
            content = [{"type": "text", "text": x}]
        lines.append(json.dumps({"role": role, "message": {"content": content}}, ensure_ascii=False))
        if role == "assistant":
            lines.append(json.dumps({"type": "turn_ended", "status": "completed"}))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def _pb_bytes(field: int, b: bytes) -> bytes:
    out, n = bytearray([(field << 3) | 2]), len(b)
    while True:
        x = n & 0x7F
        n >>= 7
        out.append(x | (0x80 if n else 0))
        if not n:
            break
    return bytes(out) + b


def cli_store(home: Path, cwd: str, msgs: list[dict], name="New Agent", agent_id=None) -> Path:
    """cursor-agent 的 ~/.cursor/chats/<md5(cwd)>/<对话号>/store.db"""
    aid = agent_id or str(uuid.uuid4())
    d = home / "chats" / hashlib.md5(cwd.encode()).hexdigest() / aid
    d.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(d / "store.db")
    c.execute("create table blobs (id TEXT PRIMARY KEY, data BLOB)")
    c.execute("create table meta (key TEXT PRIMARY KEY, value TEXT)")
    ids = []
    for m in msgs:
        b = json.dumps(m, ensure_ascii=False).encode()
        h = hashlib.sha256(b).digest()
        c.execute("insert or ignore into blobs values(?, ?)", (h.hex(), b))
        ids.append(h)
    root = b"".join(_pb_bytes(1, h) for h in ids) + _pb_bytes(9, (Path(cwd).as_uri() if Path(cwd).is_absolute() else "file://" + cwd).encode())
    rid = hashlib.sha256(root).hexdigest()
    c.execute("insert into blobs values(?, ?)", (rid, root))
    meta = {"agentId": aid, "latestRootBlobId": rid, "name": name, "mode": "auto-run", "createdAt": 1_760_000_500_000, "lastUsedModel": "gpt-5"}
    c.execute("insert into meta values('0', ?)", (json.dumps(meta).encode().hex(),))
    c.commit()
    c.close()
    return d / "store.db"
