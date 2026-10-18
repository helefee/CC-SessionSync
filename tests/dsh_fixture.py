# -*- coding: utf-8 -*-
"""造 DeepSeek Harness（dsh）第 4 版会话（测试用，全是假内容）：头一帧 + 每批事件一帧，zstd 带校验和，和 dsh 自己写的一样。
带：系统提示、运行时上下文、用户文字 + 图片、思考、工具调用 / 结果、压缩（surfaceOp replace）、用量。"""
import hashlib
import json
from pathlib import Path

import zstandard

FORMAT = 4


def seg(s: str) -> str:
    from cc_sessionsync import dsh
    return dsh.encode_segment(s)


def frames(chunks, compression="zstd") -> bytes:
    if compression == "none":
        return "".join(chunks).encode()
    c = zstandard.ZstdCompressor(level=3, write_checksum=True)
    return b"".join(c.compress(x.encode()) for x in chunks)


def line(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":")) + "\n"


class Sess:
    def __init__(self, sid, cwd, t0=1_760_000_000_000, version=FORMAT):
        self.sid, self.cwd, self.t, self.ev = sid, cwd, t0, []
        self.header = {"type": "session", "version": version, "id": sid, "createdAt": t0, "cwd": cwd, "isSeeded": False, "delegationDepth": 0}
        self.batches = [[]]

    def add(self, typ, data, surf=None):
        self.t += 1000
        e = {"type": typ, "seq": len(self.ev), "time": self.t, "data": data}
        if surf is not None:
            e["surfaceOp"] = surf
        self.ev.append(e)
        self.batches[-1].append(e)
        return e["seq"]

    def flush(self):
        self.batches.append([])

    def user(self, text, kind="user", images=()):
        content = [{"type": "text", "text": text}] + [{"type": "image", "attachment": a} for a in images]
        return self.add("user/message", {"role": "user", "id": f"u{len(self.ev)}", "content": content, "source": {"kind": kind}}, "append")

    def assistant(self, blocks, model="deepseek-flash", provider="deepseek-account", usage=None):
        d = {"message": {"role": "assistant", "id": f"m{len(self.ev)}", "content": blocks, "source": {"kind": "model", "model": model, "provider": provider}},
             "stream": [], "turn": 1, "step": 1}
        if usage:
            d["usage"] = usage
        return self.add("assistant/message", d, "append")

    def bytes(self, compression="zstd") -> bytes:
        chunks = [line(self.header)] + ["".join(line(e) for e in b) for b in self.batches if b]
        return frames(chunks, compression)

    def write(self, root: Path, compression="zstd", project=None, name=None):
        from cc_sessionsync import dsh
        d = root / (project or dsh.project_key(self.cwd)) / seg(self.sid)
        d.mkdir(parents=True, exist_ok=True)
        f = d / (name or ("session.v4.jsonl" + (".zstd" if compression == "zstd" else "")))
        f.write_bytes(self.bytes(compression))
        return f


def attach(home: Path, data: bytes, mt="image/png"):
    h = hashlib.sha256(data).hexdigest()
    f = home / "attachments/v1/objects" / h[:2] / h
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return {"attachmentId": "sha256:" + h, "mediaType": mt, "name": "shot.png", "width": 2, "height": 2, "bytes": len(data)}


PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def rich(sid, cwd, home: Path) -> Sess:
    """一个「什么都有」的会话：第 1 轮聊天被压缩替换成摘要，第 2 轮有思考 + 工具 + 图片。"""
    s = Sess(sid, cwd)
    s.add("turn/start", {})
    s.add("step/start", {})
    s.add("system/message", {"message": {"role": "system", "id": "sys0", "content": [{"type": "text", "text": "SYSTEM PROMPT secret-ish"}], "source": {"kind": "system-prompt"}}}, "append")
    s.user("RUNTIME CONTEXT <env>", kind="runtime-context")
    a = s.user("OLD QUESTION before compaction")
    b = s.assistant([{"type": "text", "text": "OLD ANSWER before compaction"}],
                    usage={"inputTokens": 1000, "outputTokens": 200, "cacheReadTokens": 5000, "cacheWriteTokens": 0, "reasoningTokens": 0, "totalTokens": 6200})
    s.add("session/title", {"title": "修登录 bug"})
    s.add("step/end", {})
    s.add("turn/end", {"reason": {"kind": "completed"}})
    s.flush()
    s.add("compaction/start", {})
    seq = s.add("user/message", {"role": "user", "id": "sum1", "content": [{"type": "text", "text": "SUMMARY of earlier talk"}], "source": {"kind": "user"}},
                {"op": "replace", "startSeq": a, "endSeq": b})
    s.ev[seq]["sourceEventSeqs"] = list(range(a, b + 1))
    s.add("compaction/end", {})
    s.flush()
    s.add("turn/start", {})
    s.add("step/start", {})
    s.user("NEW QUESTION look at this", images=[attach(home, PNG)])
    s.assistant([{"type": "reasoning", "text": "THINKING hidden"}, {"type": "text", "text": "Let me check."},
                 {"type": "tool-call", "id": "c1", "name": "bash", "arguments": json.dumps({"command": "ls -la"})}],
                usage={"inputTokens": 300, "outputTokens": 50, "cacheReadTokens": 7000, "cacheWriteTokens": 0, "reasoningTokens": 20, "totalTokens": 7350})
    s.add("tool/result", {"message": {"role": "tool", "id": "r1", "source": {"kind": "tool"}, "toolCallId": "c1", "content": [{"type": "text", "text": "file-a\nfile-b"}], "isError": False}}, "append")
    s.assistant([{"type": "text", "text": "FINAL ANSWER two files"}], model="mystery-model-x",
                usage={"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 0, "cacheWriteTokens": 0, "reasoningTokens": 0, "totalTokens": 15})
    s.add("step/end", {})
    s.add("turn/end", {"reason": {"kind": "completed"}})
    return s
