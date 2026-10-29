# -*- coding: utf-8 -*-
"""造大号的假 Claude Code 会话 jsonl（全是假数据）：指定字节数，中英混排、工具调用、偶尔很长的工具结果，
最后带一条 custom-title。测大文件 / 批量导入用。"""
import json, random, uuid
from pathlib import Path

WORDS = ["重构", "测试", "数据库", "索引", "缓存", "接口", "页面", "性能", "内存", "日志", "fix", "build", "deploy",
         "render", "stream", "parser", "token", "commit", "branch", "async", "导入", "导出", "会话", "迁移"]


def _text(rnd: random.Random, n: int) -> str:
    return " ".join(rnd.choice(WORDS) for _ in range(n))


def write(path, size: int, seed: int = 1, cwd: str = "/home/demo/proj-big", title: str | None = None) -> dict:
    """写到 path，大小 ≈ size 字节（略超一点）。→ {path, size, messages(用户+助手文字条数), sid, title}"""
    rnd = random.Random(seed)
    sid = str(uuid.UUID(int=rnd.getrandbits(128)))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    parent, n, msgs, i = None, 0, 0, 0
    title = title or f"大会话 {seed}：{_text(rnd, 3)}"
    filler = _text(rnd, 4000)                                  # 长工具结果复用一段，生成快
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        def put(rec):
            nonlocal n
            s = json.dumps(rec, ensure_ascii=False) + "\n"
            fh.write(s)
            n += len(s.encode("utf-8"))
        while n < size - 400 or i % 4 != 3:
            i += 1
            u = str(uuid.UUID(int=rnd.getrandbits(128)))
            ts = f"2026-10-{1 + i % 27:02d}T{i % 24:02d}:{i % 60:02d}:00.000Z"
            base = {"parentUuid": parent, "isSidechain": False, "userType": "external", "cwd": cwd, "sessionId": sid,
                    "version": "2.0.0", "gitBranch": "main", "uuid": u, "timestamp": ts}
            k = i % 4
            if k == 0:
                put(dict(base, type="user", message={"role": "user", "content": f"第 {i} 步：请{_text(rnd, 12)}"}))
                msgs += 1
            elif k == 1:
                tid = "toolu_" + uuid.UUID(int=rnd.getrandbits(128)).hex[:20]
                put(dict(base, type="assistant", message={"id": "msg_" + u[:8], "type": "message", "role": "assistant", "model": "claude-sonnet-4-5",
                         "content": [{"type": "text", "text": f"好的，{_text(rnd, 30)}"},
                                     {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": f"echo step {i}"}}],
                         "stop_reason": "tool_use", "usage": {"input_tokens": 100 + i % 50, "output_tokens": 20 + i % 9}}))
                msgs += 1
                last_tool = tid
            elif k == 2:
                big = filler * (1 + (i % 7 == 2) * 6)                # 偶尔一行好几百 KB
                put(dict(base, type="user", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": last_tool,
                         "content": f"step {i}\n" + big}]}, toolUseResult={"stdout": f"step {i}", "stderr": ""}))
            else:
                put(dict(base, type="assistant", message={"id": "msg_" + u[:8], "type": "message", "role": "assistant", "model": "claude-sonnet-4-5",
                         "content": [{"type": "text", "text": f"第 {i} 步做完了。{_text(rnd, 40)}"}], "stop_reason": "end_turn",
                         "usage": {"input_tokens": 200, "output_tokens": 60}}))
                msgs += 1
            parent = u
        put({"type": "custom-title", "customTitle": title, "sessionId": sid})
    return {"path": str(p), "size": n, "messages": msgs, "sid": sid, "title": title}


# 用户那一批的大小（MB），17 个约 988 MB
USER_SET_MB = [228.7, 210.2, 181.3, 96.4, 70.7, 63.3, 48.7, 20.8, 12.9, 10.9, 10, 8, 7.8, 6.4, 5.4, 3.9, 2.7]


def write_set(folder, scale: float = 1.0) -> list[dict]:
    out = []
    for j, mb in enumerate(USER_SET_MB):
        name = f"claude-{uuid.UUID(int=(j + 1) * 7919).hex[-8:]}-demo-20261029-{70000 + j * 37:06d}.jsonl"
        out.append(write(Path(folder) / name, int(mb * scale * 1024 * 1024), seed=j + 1))
    return out


if __name__ == "__main__":
    import sys, time
    t = time.time()
    r = write_set(sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 1.0)
    print(len(r), "files", sum(x["size"] for x in r) / 2**20, "MB", round(time.time() - t, 1), "s")
