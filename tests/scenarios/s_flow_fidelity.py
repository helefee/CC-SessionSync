# -*- coding: utf-8 -*-
"""0.5.2：两个 Token 流转图「画出来的量 = 真实用量」（用 node 跑页面里的函数，不开浏览器）
- 模型品牌：claude-* / sonnet / opus / haiku → Claude；gpt-* / o1 / o3 / o4-mini / codex → OpenAI；deepseek → DeepSeek；其他 → 通用图标；图标是内联 SVG
- 会话流转：真实比例 k = 1、输入输出同一个单位；放大时输出带子高度 = 输出 × 比例尺 × k；顺序 输入 → 输出（先思考）→ 工具 → 回路；粒子个数 = cnt
- 用量页流转：带子高度 = 真实合计 × 比例尺（输出 × k）；粒子计划每条路径之和 = floor(合计 / 单位)，没用量就没有粒子；实时新增按差值；放大倍数和画布大小无关"""
import sys, pathlib, json, shutil, subprocess, tempfile, re, random
ROOT = pathlib.Path(__file__).resolve().parents[2]
FAILS = []
def ok(c, m): print(("PASS " if c else "FAIL ") + m); c or FAILS.append(m)
page = (ROOT / "cc_sessionsync" / "index.html").read_text(encoding="utf-8")

def grab(name):
    """取出顶层的 `function name(` / `const name = `，到下一条顶层语句为止（页面里顶层语句都从行首开始）"""
    m = re.search(r"^(function %s\(|const %s = )" % (re.escape(name), re.escape(name)), page, re.M)
    assert m, name
    e = re.compile(r"^(function |const |let |var |async function |//|/\*|\S+\s*=|[A-Za-z_$][\w$.]*\()", re.M).search(page, m.end())
    return page[m.start():e.start() if e else len(page)]

ok('viewBox="0 0 24 24"' in page and ("function mic(" in page or "const mic = " in page) and "data:image/svg+xml" in page, "brand icons are inline SVG (DOM + canvas via SVG data URL)")
ok("Simple Icons" in page and "CC0" in page, "icon source / licence noted")
ok("cg = c, g = tfGeo" in page and "tfLerp" not in page, "session flow: band widths are the current call's real numbers (no tween)")
ok("ufPick" not in page and "ufSpawn" not in page and "Math.random() * " not in grab("ufEmit"), "usage flow: no random sampling of how many particles / which path")
ok('id="tfSc"' in page or 'fxSeg("tfSc")' in page, "session flow: 放大输出 | 真实比例 toggle")
ok('fxSeg("ufSc")' in page and 'id="${id}"' in page, "usage flow: same toggle")

node = shutil.which("node")
if not node:
    print("SKIP node not found"); sys.exit(1 if FAILS else 0)

names = ["TF_PH", "tfRand", "tfNice", "tfModel", "tfParts", "tfGeo", "modelBrand", "UF_CAT", "ufModel", "ufK", "ufPaths", "ufUnit", "ufPlan", "fmtN", "ufGeo", "ufPathTot", "ufDelta", "fxSpread"]
src = "\n".join(grab(n) for n in names if (n != "fxSpread" or "function fxSpread(" in page))
stub = """
let FXT = "0"; const store = {get: k => k === "ccss.fxtrue" ? FXT : null, set(){}}; const fxTrue = () => FXT === "1";
const tr = s => s, tfShort = s => String(s), UF_APP = {}, ufMod = s => s, ufApp = a => a, ufCat = k => k;
const CTX = {font: "", measureText: s => ({width: String(s).length * 7})};
"""
TF_DATA = {"calls": [{"inp": 3 + i % 7, "cr": 1000 * i + 17, "cw": (i * 37) % 900, "out": 50 + (i * 13) % 400, "reasoning": (i * 7) % 50, "tot": 0, "cost": None,
                      "ntools": i % 3, "turn": i // 10, "model": "claude-sonnet-4-5"} for i in range(60)]}
rnd = random.Random(7)
apps, models = ["claude-code", "codex", "dsh"], ["claude-opus-4-1", "gpt-5-codex", "deepseek-chat", "\x00other"]
B = []
for i in range(48):
    cells = []
    if i % 5 != 3:                                     # 有几个桶完全没用量
        for a in range(3):
            for m in range(4):
                if rnd.random() < .45:
                    out = rnd.randint(0, 40000); cells.append([a, m, rnd.randint(1, 30), rnd.randint(0, 90000), rnd.randint(0, 3000000), rnd.randint(0, 200000), out, rnd.randint(0, out), 0, 0])
    B.append({"t": i, "cells": cells})
UFD = {"apps": apps, "models": models, "buckets": B}
UFD2 = json.loads(json.dumps(UFD)); UFD2["buckets"][-1]["cells"].append([0, 0, 3, 5000, 800000, 0, 12000, 3000, 0, 0])   # 实时：最后一个桶多了一些
BR = ["claude-sonnet-4-5-20250929", "anthropic/claude-3.5-haiku", "opus", "sonnet-4", "gpt-5", "gpt-4o-mini", "openai/gpt-4.1", "o3", "o4-mini", "o1-preview",
      "gpt-5-codex", "codex-mini-latest", "deepseek-chat", "deepseek/deepseek-reasoner", "\x00other", "", "glm-4.6", "kimi-k2", "qwen3-coder", "gemini-2.5-pro", "proto3"]
js = stub + src + "\nconst TFD = " + json.dumps(TF_DATA) + ", UFD = " + json.dumps(UFD) + ", UFD2 = " + json.dumps(UFD2) + ", BR = " + json.dumps(BR) + ";\n" + r"""
const R = {brand: Object.fromEntries(BR.map(s => [s, modelBrand(s)])), ph: TF_PH};
// 会话
for (const real of [false, true]) {
  const m = tfModel(TFD, {real}), r = {k: m.k, uIn: m.uIn, uOut: m.uOut, parts: [], geo: []};
  for (const i of [0, 7, 33, 59]) { const P = tfParts(m, i), n = m.cnt[i], c = TFD.calls[i], cnt = {cr: 0, cw: 0, inp: 0, out: 0, tool: 0, rs: 0}; let firstNonRs = 1e9, lastRs = -1;
    P.forEach((p, j) => { cnt[p.s] = (cnt[p.s] || 0) + 1; if (p.s === "out") { if (p.rs) { cnt.rs++; lastRs = Math.max(lastRs, p.e); } else firstNonRs = Math.min(firstNonRs, p.e); } });
    const maxIn = Math.max(-1, ...P.filter(p => ["cr", "cw", "inp"].includes(p.s)).map(p => p.e)), minOut = Math.min(1e9, ...P.filter(p => p.s === "out").map(p => p.e)),
          maxOut = Math.max(-1, ...P.filter(p => p.s === "out").map(p => p.e)), minTool = Math.min(1e9, ...P.filter(p => p.s === "tool").map(p => p.e));
    r.parts.push({n, cnt, c, ntools: c.ntools, order: maxIn + TF_PH.inT <= minOut + 1e-9 && (minTool === 1e9 || maxOut + TF_PH.outT <= minTool + 1e-9), rsFirst: lastRs <= firstNonRs + 1e-9});
    const g = tfGeo(900, 420, m, c); r.geo.push({s: g.s, h: {cr: c.cr * g.s, cw: c.cw * g.s, inp: c.inp * g.s}, mi: {cr: g.mi.cr.h, cw: g.mi.cw.h, inp: g.mi.inp.h}, ho: g.ho, out: c.out, k: m.k}); }
  R[real ? "tfReal" : "tfMag"] = r;
}
// 用量页
for (const real of [false, true]) { FXT = real ? "1" : "0";
  const m = ufModel(UFD), k = ufK(m), u = ufUnit(m, k), plan = ufPlan(m, u, k), tot = {}, cnt = {}, outp = {};
  plan.forEach(row => row.forEach(p => { cnt[p.key] = (cnt[p.key] || 0) + p.n; outp[p.key] = p.out; }));
  ufPaths(m).forEach(row => row.forEach(p => { tot[p.key] = (tot[p.key] || 0) + p.v; outp[p.key] = p.out; }));
  const emptyB = m.d.buckets.map((b, i) => b.cells.length ? null : (plan[i] || []).length).filter(x => x !== null);
  const perB = plan.map(row => row.reduce((a, p) => a + p.n, 0));
  const geos = [[900, 420], [1400, 520], [520, 360]].map(([W, H]) => { const g = ufGeo(W, H, m, CTX, 12, "x");
    return {k: g.k, s: g.s, chk: m.L.map((l, j) => { const G = g.LG[j]; if (!G) return null; const f = m.full[j] * g.s * (l.kind === "cd" ? g.k : 1); return Math.abs(G.h1 - f) < 1e-6 && Math.abs(G.h2 - f) < 1e-6; }),
            // 0.5.3：卡片在各自一列里不重叠、不出画布；卡片高度 ≥ 真实高度（太小的按最小高度当标签画）；带子从卡片边上真实高度的色条接出；时间轴不在这张画布里
            lay: ["a", "c", "m", "o"].every(c => g.N[c].every((n, i) => n.y >= g.top - 1e-6 && n.y + n.h <= g.top + g.Hf + 1e-6 && n.h >= n.th - 1e-9 && n.h >= Math.min(g.mc, n.h) && n.w > 0 &&
                   (i === 0 || n.y >= g.N[c][i - 1].y + g.N[c][i - 1].h - 1e-6))) &&
                 g.xA + g.w.a < g.xB && g.xB + g.w.c < g.xC && g.xC + g.w.m < g.xD && g.xD + g.w.o <= W &&
                 m.L.every((l, j) => { const G = g.LG[j]; if (!G) return true; const x1 = {ab: g.xA + g.w.a, bc: g.xB + g.w.c, cd: g.xC + g.w.m}[l.kind], x2 = {ab: g.xB, bc: g.xC, cd: g.xD}[l.kind];
                   const tn = {ab: g.N.c[UF_CAT.indexOf(l.t)], bc: g.N.m[l.t], cd: g.N.o[UF_OUT.indexOf(l.t)]}[l.kind];
                   return Math.abs(G.x1 - x1) < 1e-6 && Math.abs(G.x2 - x2) < 1e-6 && G.y2 >= tn.ty - 1e-6 && G.y2 + G.h2 <= tn.ty + tn.th + 1e-6; }) &&
                 g.sy === undefined && g.sh === undefined}; });
  const d = ufDelta(m, ufModel(UFD2), u, k), m2 = ufModel(UFD2), t2 = {}; ufPaths(m2).forEach(row => row.forEach(p => { t2[p.key] = (t2[p.key] || 0) + p.v; }));
  R[real ? "ufReal" : "ufMag"] = {k, u, tot, cnt, outp, emptyB, maxB: Math.max(...perB), geos, delta: d.map(p => [p.key, p.n, p.out]), t2, T: m.T};
}
console.log(JSON.stringify(R));
"""
T = pathlib.Path(tempfile.mkdtemp(prefix="ccss-ff-")); (T / "ff.js").write_text(js, encoding="utf-8")
p = subprocess.run([node, str(T / "ff.js")], capture_output=True, text=True, encoding="utf-8")
ok(p.returncode == 0, "node runs the page's flow functions " + p.stderr[-600:])
R = json.loads(p.stdout or "{}")
if R:
    exp = {"claude": BR[:4], "openai": BR[4:12], "deepseek": BR[12:14], "other": BR[14:]}
    for b, L in exp.items():
        for s in L: ok(R["brand"][s] == b, f"modelBrand({s!r}) = {b} (got {R['brand'][s]})")
    ph = R["ph"]
    ok(ph["inE"] + ph["inT"] <= ph["outS"] + 1e-9 and ph["outE"] + ph["outT"] <= ph["tlS"] + 1e-9 and ph["tlE"] + ph["tlT"] + ph["lpT"] <= 1.08 + 1e-9, f"phases: input arrives before output starts, tools only after the last output arrives, loop ends by the next call ({ph})")
    fl = lambda v, u: int(v / u + 1e-7)
    for key in ("tfMag", "tfReal"):
        r = R[key]
        if key == "tfReal": ok(r["k"] == 1 and abs(r["uOut"] - r["uIn"]) < 1e-9, f"session true proportions: ×1, one unit ({r['k']}, {r['uIn']}, {r['uOut']})")
        else: ok(r["k"] > 1, f"session magnified mode actually magnifies here (×{r['k']})")
        for q in r["parts"]:
            n, c = q["n"], q["cnt"]
            ok(all(c[s] == n[s] for s in ("cr", "cw", "inp", "out")) and c["tool"] == min(24, q["ntools"]), f"{key}: particles drawn = planned counts ({c} vs {n})")
            ok(q["order"] and q["rsFirst"], f"{key}: input → output (reasoning first) → tools")
            ok(abs(c["rs"] - round(n["out"] * q["c"]["reasoning"] / q["c"]["out"])) <= 1 if q["c"]["out"] else c["rs"] == 0, f"{key}: reasoning share of output particles ≈ real share")
        for g in r["geo"]:
            ok(all(abs(g["mi"][s] - max(g["h"][s], .6)) < 1e-6 for s in g["h"]) and abs(g["ho"] - max(2, g["out"] * g["s"] * g["k"])) < 1e-6, f"{key}: band heights = tokens × scale (output × k)")
    for key in ("ufMag", "ufReal"):
        r = R[key]; k, u = r["k"], r["u"]
        ok(all(r["cnt"].get(p, 0) == fl(v, u / k if r["outp"][p] else u) for p, v in r["tot"].items()), f"{key}: every path's particles = floor(total / unit) (unit {u}, output unit {u / k})")
        ok(set(r["cnt"]) <= {p for p, v in r["tot"].items() if v > 0}, f"{key}: no particles on paths with no usage")
        ok(all(x == 0 for x in r["emptyB"]) and len(r["emptyB"]) > 0, f"{key}: buckets with no usage emit nothing ({r['emptyB']})")
        ok(r["maxB"] <= 60 * 1.05 + 30, f"{key}: busiest bucket ≈ 60 particles ({r['maxB']})")
        ok(all(g["k"] == k for g in r["geos"]) and all(all(x is None or x for x in g["chk"]) for g in r["geos"]), f"{key}: band heights = real totals × scale (output × {k}), same k at every canvas size")
        ok(all(g["lay"] for g in r["geos"]), f"{key}: cards don't overlap / stay on the canvas / are ≥ their true height; bands attach to the true-height port bars; no timeline strip inside the flow canvas")
        T = r["T"]
        if key == "ufReal": ok(k == 1, "usage true proportions: ×1")
        else: ok(k > 1 and T["out"] * k <= .5 * (T["inp"] + T["cr"] + T["cw"]) + 1e-6, f"usage magnified: ×{k}, output stays ≤ half the input")
        dd = {p: (n, o) for p, n, o in r["delta"]}
        ok(all(dd.get(p, (0, 0))[0] == fl(v, u / k if r["outp"].get(p, p.endswith(("|rp", "|rs"))) else u) - fl(r["tot"].get(p, 0), u / k if r["outp"].get(p, p.endswith(("|rp", "|rs"))) else u)
               for p, v in r["t2"].items()) and len(dd) > 0, f"{key}: live burst = floor(new/unit) − floor(old/unit) per path ({len(dd)} paths)")
print("\n%d FAIL" % len(FAILS) if FAILS else "\nALL PASS"); sys.exit(1 if FAILS else 0)
