# -*- coding: utf-8 -*-
"""0.3.5：「供应商」（模型路由 / 卡片）功能拿掉以后，第一次启动时把它留下的东西收拾干净。

做什么（只动我们自己写过的东西；可以重复跑，第二次什么都不做）：
1. 按启用时留下的记录（~/.cc-sessionsync/route.json 里的 cards_applied / applied）还原：
   - 文件后来没被别人动过 → 按字节还原成启用前的样子（启用前没有这个文件就删掉）；
   - 动过 → 只把「现在还是我们写的值」的那几项放回启用前的值（没有的就删掉），别的键不碰。
2. 扫一遍（没有记录、或还原后还有残留时）：
   - Codex config.toml：model_provider 还指着 cc-route → 放回记录里的原值，没有记录就删掉这个键（回到 Codex 默认的 ChatGPT 登录）；
     删掉 [model_providers.cc-route]、[profiles.cc-route]；model_catalog_json 指着我们生成的 ccss-models-*.json → 删掉这个键。
   - Codex 的 cc-route.config.toml（我们的 profile 文件）→ 删掉。
   - Claude Code settings.json：env.ANTHROPIC_BASE_URL 指着我们的本机代理 → 连同 ANTHROPIC_AUTH_TOKEN 一起删；
     apiKeyHelper 取的是代理口令 route-token / 卡片的 key → 删掉。
3. 删我们生成的模型目录 ccss-models-*.json（这个前缀只有本程序用：0.2 的供应商列表和 0.3.x 的卡片写的）。
4. 停掉后台代理进程、删开机自启 HKCU\\...\\Run「CC Session Sync Route」、删凭据管理器里的本机代理口令 route-token。
每个要改的文件先备份进 ~/.cc-sessionsync/backups（和以前改配置时的备份同一个地方、同一个 index.jsonl）；做了什么记在
~/.cc-sessionsync/legacy-0.3.5.json，界面上提示一次。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import hashlib
import urllib.request
from pathlib import Path

from . import paths

MARK = paths.HOME / "legacy-0.3.5.json"
ROUTE = paths.HOME / "route.json"
BACKUPS = paths.HOME / "backups"
PIDFILE = paths.HOME / "route.pid"
PROFILE = "cc-route"
TOKEN_KEY = "route-token"
RUN_VALUE = "CC Session Sync Route"
CATALOG_RE = re.compile(r"(^|[\\/])ccss-models-[A-Za-z0-9._-]+\.json$")
_ABSENT = {"__absent__": True}


# ---------------- 小工具 ----------------

def _read(p: Path) -> bytes | None:
    try:
        return p.read_bytes()
    except OSError:
        return None


def _sha(b: bytes | None) -> str | None:
    return hashlib.sha256(b).hexdigest() if b is not None else None


def atomic_write(p: Path, data: bytes):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".ccss-{os.getpid()}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    for i in range(5):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.2 * (i + 1))
    os.replace(tmp, p)


def backup(p: Path, label: str) -> str | None:
    """和 0.2–0.3.4 改配置时同一种备份：~/.cc-sessionsync/backups/<id><后缀> + index.jsonl 一行。"""
    if not p.exists():
        return None
    BACKUPS.mkdir(parents=True, exist_ok=True)
    bid = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}-{label}"
    shutil.copy2(p, BACKUPS / (bid + p.suffix))
    with open(BACKUPS / "index.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": bid, "file": str(p), "ext": p.suffix, "at": time.time(), "label": label}, ensure_ascii=False) + "\n")
    return bid


def _backup_bytes(bid: str | None) -> bytes | None:
    if not bid:
        return None
    try:
        rows = [json.loads(x) for x in (BACKUPS / "index.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except OSError:
        return None
    r = next((x for x in rows if x.get("id") == bid), None)
    return _read(BACKUPS / (bid + r["ext"])) if r else None


def _tk():
    import tomlkit
    return tomlkit


def _plain(v):
    if hasattr(v, "unwrap"):
        v = v.unwrap()
    return json.loads(json.dumps(v, default=str))


class _Doc:
    def __init__(self, f: Path, fmt: str):
        self.f, self.fmt = f, fmt
        self.raw = _read(f)
        txt = self.raw.decode("utf-8-sig") if self.raw else ""
        if fmt == "toml":
            self.tk = _tk()
            self.doc = self.tk.parse(txt)
        else:
            self.tk = None
            self.doc = json.loads(txt) if txt.strip() else {}
            if not isinstance(self.doc, dict):
                raise ValueError(f"{f} 不是 JSON 对象")

    def dump(self) -> bytes:
        if self.fmt == "toml":
            out = self.tk.dumps(self.doc)
            from . import _toml
            _toml.loads(out)                          # 写之前确认还是合法的 TOML
            return out.encode("utf-8")
        return (json.dumps(self.doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _walk(doc, path, create=False, tk=None):
    cur = doc
    for k in path[:-1]:
        if k not in cur:
            if not create:
                return None
            cur[k] = tk.table(is_super_table=True) if tk else {}
        cur = cur[k]
        if not hasattr(cur, "keys"):
            return None
    return cur


def _get(doc, path):
    p = _walk(doc, path)
    if p is None or path[-1] not in p:
        return _ABSENT
    return _plain(p[path[-1]])


def _prune(doc, path):
    for i in range(len(path) - 1, 0, -1):
        p = _walk(doc, path[:i])
        if p is not None and hasattr(p, "keys") and not len(p):
            q = _walk(doc, path[:i - 1]) if i > 1 else doc
            if q is not None:
                del q[path[i - 1]]


def _set(doc, path, v, tk=None):
    if v == _ABSENT:
        p = _walk(doc, path)
        if p is not None and path[-1] in p:
            del p[path[-1]]
        _prune(doc, path)
        return
    p = _walk(doc, path, True, tk)
    if tk and isinstance(v, dict):
        t = tk.table()
        for k, x in v.items():
            if isinstance(x, dict):
                sub = tk.table()
                for k2, x2 in x.items():
                    sub[k2] = x2
                t[k] = sub
            else:
                t[k] = x
        v = t
    p[path[-1]] = v


def _restore_whole(rec: dict) -> str:
    """文件没被动过（sha 一致）→ 按字节放回启用前的样子；动过 → "changed"。"""
    p = Path(rec["file"])
    cur = _read(p)
    if cur is None:
        return "gone"
    if _sha(cur) != rec.get("sha"):
        return "changed"
    if rec.get("existed"):
        old = _backup_bytes(rec.get("backup"))
        if old is None:
            return "changed"                          # 备份找不到：退回到只拿掉我们写的
        backup(p, "legacy-before-restore")
        atomic_write(p, old)
    else:
        backup(p, "legacy-before-remove")
        p.unlink()
        return "deleted"
    return "restored"


# ---------------- 1. 按记录还原 ----------------

def _revert_edits(rec: dict) -> str:
    """0.3.1–0.3.4 卡片的记录：{file, fmt, existed, backup, sha, edits:[{path, orig, ours}]}。"""
    r = _restore_whole(rec)
    if r != "changed":
        return r
    f = Path(rec["file"])
    x = _Doc(f, rec.get("fmt") or ("toml" if f.suffix == ".toml" else "json"))
    hit = False
    for e in rec.get("edits") or []:
        if _get(x.doc, e["path"]) == e.get("ours"):
            _set(x.doc, e["path"], e.get("orig", _ABSENT), x.tk)
            hit = True
    if not hit:
        return "untouched"
    backup(f, "legacy-before-revert")
    atomic_write(f, x.dump())
    return "partial"


def _revert_route_codex(rec: dict) -> list:
    """0.3.0「高级 → 模型路由」应用到 Codex 的记录：{files:[{kind: profile|config, file, existed, backup, sha, orig, ours}]}。"""
    out = []
    for x in rec.get("files") or []:
        r = _restore_whole(x)
        if r == "changed" and x.get("kind") == "config":
            p = Path(x["file"])
            d = _Doc(p, "toml")
            mp = d.doc.get("model_providers")
            changed = False
            if mp is not None and PROFILE in mp:
                del mp[PROFILE]
                changed = True
                if not len(mp):
                    del d.doc["model_providers"]
            for k, ours in (x.get("ours") or {}).items():
                if str(d.doc.get(k)) == ours:
                    o = (x.get("orig") or {}).get(k)
                    if o is None:
                        del d.doc[k]
                    else:
                        d.doc[k] = o
                    changed = True
            if changed:
                backup(p, "legacy-before-revert")
                atomic_write(p, d.dump())
            r = "partial" if changed else "untouched"
        elif r == "changed" and x.get("kind") == "profile":
            p = Path(x["file"])
            backup(p, "legacy-before-remove")
            p.unlink()
            r = "removed"
        out.append({"file": x["file"], "result": r})
    return out


def _revert_route_claude(rec: dict) -> dict:
    r = _restore_whole(rec)
    if r == "changed":
        p = Path(rec["file"])
        d = _Doc(p, "json")
        env = d.doc.get("env") if isinstance(d.doc.get("env"), dict) else {}
        changed = False
        for k, v in (rec.get("ours") or {}).items():
            if env.get(k) == v:
                o = (rec.get("orig") or {}).get(k)
                if o is None:
                    env.pop(k, None)
                else:
                    env[k] = o
                changed = True
        if not env and not rec.get("had_env"):
            d.doc.pop("env", None)
        if changed:
            backup(p, "legacy-before-revert")
            atomic_write(p, d.dump())
        r = "partial" if changed else "untouched"
    return {"file": rec["file"], "result": r}


# ---------------- 2. 扫残留 ----------------

def _orig_codex(st: dict) -> dict:
    """记录里启用前的 model_provider / model / model_catalog_json（扫残留时用来放回）。"""
    o = {}
    for rec in ((st.get("cards_applied") or {}).get("codex") or {}).values():
        for e in rec.get("edits") or []:
            if len(e["path"]) == 1 and e["path"][0] in ("model_provider", "model", "model_catalog_json"):
                o.setdefault(e["path"][0], e.get("orig", _ABSENT))
    for x in ((st.get("applied") or {}).get("codex") or {}).get("files") or []:
        for k, v in (x.get("orig") or {}).items():
            o.setdefault(k, _ABSENT if v is None else v)
    return o


def _is_ours_catalog(v) -> bool:
    return isinstance(v, str) and bool(CATALOG_RE.search(v.strip()))


def _sweep_codex(st: dict) -> list:
    f = paths.CODEX_HOME / "config.toml"
    out = []
    if f.exists():
        try:
            d = _Doc(f, "toml")
        except Exception as e:
            return [f"Codex config.toml 读不了，没动：{type(e).__name__}"]
        doc, orig = d.doc, _orig_codex(st)
        if str(doc.get("model_provider") or "") == PROFILE:
            o = orig.get("model_provider", _ABSENT)
            if o in (_ABSENT, None, PROFILE):
                del doc["model_provider"]
                out.append("Codex：model_provider 不再指向 cc-route（没有启用前的记录 → 删掉这个键，回到 Codex 默认的 ChatGPT 登录）")
            else:
                doc["model_provider"] = o
                out.append(f"Codex：model_provider 放回 {o}")
            m = orig.get("model", _ABSENT)
            if m not in (_ABSENT, None):
                doc["model"] = m
            elif "model" in doc and o in (_ABSENT, None, PROFILE):
                del doc["model"]                      # 那是服务商的模型名，ChatGPT 登录下用不了 → 回到 Codex 默认模型
                out.append("Codex：model 是给 cc-route 用的，删掉（回到 Codex 默认模型）")
        mp = doc.get("model_providers")
        if mp is not None and hasattr(mp, "keys") and PROFILE in mp:
            del mp[PROFILE]
            if not len(mp):
                del doc["model_providers"]
            out.append("Codex：删掉 [model_providers.cc-route]")
        pf = doc.get("profiles")
        if pf is not None and hasattr(pf, "keys") and PROFILE in pf:
            del pf[PROFILE]
            if not len(pf):
                del doc["profiles"]
            out.append("Codex：删掉 [profiles.cc-route]")
        if _is_ours_catalog(doc.get("model_catalog_json")):
            o = orig.get("model_catalog_json", _ABSENT)
            if o not in (_ABSENT, None) and not _is_ours_catalog(o):
                doc["model_catalog_json"] = o
            else:
                del doc["model_catalog_json"]
            out.append("Codex：model_catalog_json 不再指向我们生成的模型目录（Codex 改回自己拉模型列表）")
        if out:
            backup(f, "legacy-sweep")
            atomic_write(f, d.dump())
    prof = paths.CODEX_HOME / f"{PROFILE}.config.toml"
    if prof.exists():
        backup(prof, "legacy-before-remove")
        prof.unlink()
        out.append(f"Codex：删掉 profile 文件 {prof.name}")
    return out


def _route_port(st: dict) -> int | None:
    try:
        return int(st.get("port")) if st.get("port") else None
    except (TypeError, ValueError):
        return None


def _ours_helper(v) -> bool:
    """apiKeyHelper 是我们写的取本机代理口令的命令（…key get route-token）。
    直连卡片 / 0.2 供应商写的 key get prov-… / card-… / claude-… 不动：没有记录时删了反而断（base_url 还指着服务商），而取 key 命令保留着照样能用。"""
    return isinstance(v, str) and bool(re.search(r"key\"?\s+\"?get\"?\s+\"?route-token\b", v))


def _sweep_claude(st: dict) -> list:
    f = paths.CLAUDE_HOME / "settings.json"
    if not f.exists():
        return []
    try:
        d = _Doc(f, "json")
    except Exception as e:
        return [f"Claude Code settings.json 读不了，没动：{type(e).__name__}"]
    out, doc = [], d.doc
    env = doc.get("env") if isinstance(doc.get("env"), dict) else None
    port = _route_port(st)
    if env:
        url = str(env.get("ANTHROPIC_BASE_URL") or "")
        m = re.match(r"^https?://(127\.0\.0\.1|localhost):(\d+)/?$", url)
        tok = _token()
        if m and ((port is not None and int(m.group(2)) == port) or (tok and env.get("ANTHROPIC_AUTH_TOKEN") == tok)):
            env.pop("ANTHROPIC_BASE_URL", None)
            env.pop("ANTHROPIC_AUTH_TOKEN", None)
            out.append("Claude Code：删掉指向本机代理的 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN")
        if not env:
            doc.pop("env", None)
    if _ours_helper(doc.get("apiKeyHelper")):
        doc.pop("apiKeyHelper", None)
        out.append("Claude Code：删掉我们写的 apiKeyHelper")
    if out:
        backup(f, "legacy-sweep")
        atomic_write(f, d.dump())
    return out


# ---------------- 3/4. 文件、进程、自启、口令 ----------------

def _remove_catalogs() -> list:
    out = []
    try:
        fs = sorted(paths.CODEX_HOME.glob("ccss-models-*.json"))
    except OSError:
        fs = []
    for p in fs:
        try:
            backup(p, "legacy-catalog")
            p.unlink()
            out.append(f"Codex：删掉我们生成的模型目录 {p.name}")
        except OSError as e:
            out.append(f"删不掉 {p.name}：{e}")
    try:
        (paths.CODEX_HOME / "models_cache.json").unlink()      # Codex 的模型列表缓存：删掉让它马上按恢复后的设置重新拉
    except OSError:
        pass
    return out


def _token() -> str | None:
    try:
        from . import keystore
        return keystore.get(TOKEN_KEY)
    except Exception:
        return None


def _stop_proxy(st: dict) -> list:
    out = []
    port, tok = _route_port(st), _token()
    if port:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        alive = False
        try:
            with op.open(f"http://127.0.0.1:{port}/health", timeout=1.5) as r:
                alive = b"cc-sessionsync-route" in r.read(200)
        except Exception:
            pass
        if alive and tok:
            try:
                rq = urllib.request.Request(f"http://127.0.0.1:{port}/ccss/shutdown", data=b"{}", method="POST",
                                            headers={"authorization": "Bearer " + tok, "content-type": "application/json"})
                op.open(rq, timeout=3).read()
                out.append(f"停掉后台代理（127.0.0.1:{port}）")
                alive = False
            except Exception:
                pass
        if alive:
            pid = None
            try:
                pid = int(PIDFILE.read_text().strip())
            except Exception:
                pass
            if pid and pid != os.getpid():
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=0x08000000)
                    else:
                        os.kill(pid, 15)
                    out.append(f"结束后台代理进程（pid {pid}）")
                except Exception as e:
                    out.append(f"结束后台代理进程失败：{type(e).__name__}")
    try:
        PIDFILE.unlink()
    except OSError:
        pass
    return out


def _remove_run_value() -> list:
    if os.name != "nt" or os.environ.get("CC_SESSIONSYNC_NO_LOGIN_ITEM"):      # 测试时不碰真的注册表
        return []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                            winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
            try:
                winreg.QueryValueEx(k, RUN_VALUE)
            except OSError:
                return []
            winreg.DeleteValue(k, RUN_VALUE)
            return [f"删掉开机自启「{RUN_VALUE}」"]
    except OSError as e:
        return [f"开机自启「{RUN_VALUE}」删不掉：{e}"]


def _remove_token() -> list:
    try:
        from . import keystore
        if keystore.get(TOKEN_KEY):
            keystore.delete(TOKEN_KEY)
            return ["删掉凭据管理器里的本机代理口令"]
    except Exception as e:
        return [f"本机代理口令删不掉：{type(e).__name__}"]
    return []


def _leftover_session_files() -> list:
    out = []
    for p in (paths.HOME / "route" / "claude-settings.json",):
        if p.exists():
            try:
                p.unlink()
                out.append("删掉 claude --settings 用的代理设置文件")
            except OSError:
                pass
    return out


def _still_uses_helper() -> list:
    """0.2 的「供应商列表」切过去的配置还在用 `key get <名字>` 取 key：命令保留着，照样能用，提示一下。"""
    out = []
    try:
        s = json.loads((paths.CLAUDE_HOME / "settings.json").read_text(encoding="utf-8-sig") or "{}")
        if isinstance(s, dict) and re.search(r"key\"?\s+\"?get\b", str(s.get("apiKeyHelper") or "")):
            out.append("Claude Code")
    except Exception:
        pass
    try:
        t = (paths.CODEX_HOME / "config.toml").read_text(encoding="utf-8-sig")
        if re.search(r"keyhelper\.py|CCSessionSync\.exe", t, re.I):
            out.append("Codex")
    except OSError:
        pass
    return out


# ---------------- 入口 ----------------

_SAY = {"restored": "按字节还原成启用前的样子", "partial": "放回启用前的值（只动我们写的那几项）", "gone": "文件已经不在了",
        "removed": "是我们建的，删掉", "deleted": "启用前没有这个文件，删掉"}


def state() -> dict | None:
    try:
        return json.loads(MARK.read_text(encoding="utf-8"))
    except Exception:
        return None


def _codex_open() -> bool:
    try:
        from . import apps
        return bool(apps.running("codex"))
    except Exception:
        return False


def _queue_codex() -> dict | None:
    """Codex 桌面端开着：现在改 config.toml 会在它退出时被盖回去 → 排队，等它完全退出再做（和以前「启用卡片」一样）。"""
    from . import apps
    if any(j.get("op") == "legacy-codex" and j.get("state") == "waiting" for j in apps._load_q()):
        return {"deferred": True}
    return apps.run_or_defer("codex", "legacy-codex", {}, "恢复 Codex 配置（供应商功能已移除）")


def migrate(force: bool = False, codex_now: bool = False) -> dict:
    """第一次启动 0.3.5 时跑一次；Codex 那部分要是排了队，之后每次启动 / 队列执行时再试。每一步都可以重复做（没东西就什么都不做）。"""
    prev = state()
    if prev and not force and not prev.get("codex_pending"):
        return prev
    try:
        st = json.loads(ROUTE.read_text(encoding="utf-8")) if ROUTE.exists() else {}
        st = st if isinstance(st, dict) else {}
    except Exception:
        st = {}
    busy = not codex_now and _codex_open()
    done, errs = [], []
    try:
        from . import watch
        watch.note_self("codex")                      # 我们自己改 Codex 的供应商：别报「账号已切换」
        watch.note_self("claude")
    except Exception:
        pass

    def step(fn, *a):
        try:
            r = fn(*a)
            done.extend(r if isinstance(r, list) else [r])
        except Exception as e:
            errs.append(f"{fn.__name__}: {type(e).__name__}: {e}")

    def records(which):
        out = []
        for cl, recs in (st.get("cards_applied") or {}).items():
            if cl not in which:
                continue
            for name, rec in (recs or {}).items():
                r = _revert_edits(rec)
                if r not in ("untouched", "gone"):
                    out.append(f"{'Codex' if cl == 'codex' else 'Claude Code'}：{Path(rec['file']).name} " + _SAY.get(r, r))
        ap = st.get("applied") or {}
        if ap.get("codex") and "codex" in which:
            for x in _revert_route_codex(ap["codex"]):
                if x["result"] not in ("untouched", "gone"):
                    out.append(f"Codex：{Path(x['file']).name} " + _SAY.get(x["result"], x["result"]))
        if ap.get("claude") and "claude" in which:
            x = _revert_route_claude(ap["claude"])
            if x["result"] not in ("untouched", "gone"):
                out.append(f"Claude Code：{Path(x['file']).name} " + _SAY.get(x["result"], x["result"]))
        return out

    pending = False
    if busy:
        # 先把 Claude Code 恢复了；Codex 的配置、后台代理、口令留到 Codex 退出后（代理先留着，开着的 Codex 还能用）
        step(records, ("claude",))
        step(_sweep_claude, st)
        step(_leftover_session_files)
        try:
            q = _queue_codex()
        except Exception as e:
            q = None
            errs.append(f"queue: {type(e).__name__}: {e}")
        if q and q.get("deferred") is False:          # 刚好退出了：已经整套做完
            return state() or {}
        pending = bool(q)
        if pending and not (prev or {}).get("codex_pending"):
            done.append("Codex 桌面端开着：等它完全退出后再恢复 Codex 配置（已排队，也可以在顶上点「关闭 Codex 并执行」）")
    else:
        step(_stop_proxy, st)                         # 先停代理（它可能还在写 route.json / 用量）
        step(records, ("codex", "claude"))
        step(_sweep_codex, st)
        step(_sweep_claude, st)
        step(_remove_catalogs)
        step(_leftover_session_files)
        step(_remove_run_value)
        step(_remove_token)
        if ROUTE.exists() and not errs:               # 记录用完了：留一份备份，原文件改名（不再被读）
            try:
                backup(ROUTE, "legacy-route-json")
                ROUTE.replace(ROUTE.with_name("route.json.removed-0.3.5"))
            except OSError:
                pass
    now = [x for x in done if x]
    prev = prev or {}
    seen_ = bool(prev.get("seen")) and not now               # 又收拾出新东西 → 再提示一次
    res = {"version": "0.3.5", "at": time.time(), "done": list(prev.get("done") or []) + now, "errors": errs,
           "codex_pending": pending, "helper_still_used": _still_uses_helper(), "seen": seen_,
           "evented": bool(prev.get("evented")) and not now, "runs": int(prev.get("runs") or 0) + 1}
    res["notice"] = bool(res["done"]) and not res["seen"]
    try:
        MARK.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(MARK, json.dumps(res, ensure_ascii=False, indent=1).encode("utf-8"))
        with open(paths.HOME / "legacy-0.3.5.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + json.dumps({k: res[k] for k in ("done", "errors", "codex_pending")} | {"now": now}, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return res


def seen() -> dict:
    r = state() or {}
    r["seen"] = True
    r["notice"] = False
    try:
        atomic_write(MARK, json.dumps(r, ensure_ascii=False, indent=1).encode("utf-8"))
    except OSError:
        pass
    return r
