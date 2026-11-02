#!/usr/bin/env python3
"""WorkBuddy 内置提示词精简工具：scan / capture / apply / verify / restore。

只用 Python 3 标准库（macOS 自带 /usr/bin/python3 3.9 可跑）。
所有根路径都能用参数覆盖，方便在临时副本上测试：
  --bundle  应用包解包目录，默认 /Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked
  --home    WorkBuddy 数据目录，默认 ~/.workbuddy
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BUNDLE = "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked"
DEFAULT_HOME = "~/.workbuddy"

TEXT_EXTS = {".tpl", ".md"}
PLUGIN_SUBTREES = ("welcomemode", "prompt-common", "interactionmode")  # 应用包 / 市场目录下的三棵树
CACHE_PLUGIN_PATTERNS = (re.compile(r"^interactionmode-.+"), re.compile(r"^prompt-common$"), re.compile(r"^welcomemode-.+"))
PERSONAL_FILES = ("USER.md", "SOUL.md", "IDENTITY.md")  # capture 只收 USER.md；另两个由用户手工放进配置目录
DEFAULT_MARKERS = ["You are CodeBuddy Code", "<content_policy>", "You're not a chatbot"]


# --------------------------------------------------------------------------- 基础工具

def sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def read_text(p: Path):
    try:
        return p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def write_text(p: Path, text: str) -> None:
    """原子替换写入，保留权限。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".slim-tmp")
    tmp.write_bytes(text.encode("utf-8"))
    if p.exists():
        shutil.copymode(p, tmp)
    os.replace(tmp, p)


def dump_like(original: str, obj) -> str:
    """按原文件的风格（紧凑/缩进、ensure_ascii、末尾换行）重新序列化。"""
    body = original.rstrip("\n")
    nl = "\n" if original.endswith("\n") else ""
    cands = []
    if "\n" not in body:
        cands = [dict(separators=(",", ":")), dict(separators=(", ", ": "))]
    else:
        m = re.search(r"\n(\t+|\s+)\"", original)
        ind = m.group(1) if m else "  "
        indent = ind if ind.startswith("\t") else len(ind)
        cands = [dict(indent=indent)]
    for kw in cands:
        for ea in (False, True):
            out = json.dumps(obj, ensure_ascii=ea, **kw)
            if out == body:
                return out + nl
    # 无法逐字复现原格式时，退回第一种风格
    return json.dumps(obj, ensure_ascii=False, **cands[0]) + nl


def short(p) -> str:
    s = str(p)
    h = str(Path.home())
    return "~" + s[len(h):] if s.startswith(h + "/") else s


# --------------------------------------------------------------------------- 上下文：各处路径

class Ctx:
    def __init__(self, args):
        self.bundle = Path(os.path.expanduser(args.bundle)).resolve()
        self.home = Path(os.path.expanduser(args.home)).resolve()
        br = getattr(args, "backup_root", None)
        self.backup_root = Path(os.path.expanduser(br)) if br else self.home.parent / ".workbuddy-slim" / "backups"
        self.plugin_root = self.bundle / "resources" / "plugins" / "workbuddy-builtin"
        self.market_root = self.home / "plugins" / "marketplaces" / "workbuddy-builtin"
        self.cache_root = self.home / "plugins" / "cache" / "workbuddy-builtin"
        self.product_json = self.bundle / "cli" / "product.json"
        self.cache_dir = self.home / "cache"
        self.app_version = self._app_version()
        self._vdir = {}

    def _app_version(self):
        plist = self.bundle.parent.parent / "Info.plist"
        try:
            with open(plist, "rb") as f:
                d = plistlib.load(f)
            return d.get("CFBundleShortVersionString") or d.get("CFBundleVersion")
        except Exception:
            return None

    # 缓存里每个插件可能有多个版本目录：优先与应用版本号匹配的，否则取最新
    def cache_version_dir(self, plugin: str):
        if plugin in self._vdir:
            return self._vdir[plugin]
        base = self.cache_root / plugin
        res = None
        if base.is_dir():
            dirs = [d for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")]
            ver = self.app_version
            match = [d for d in dirs if ver and (d.name == ver or d.name.startswith(ver + "-"))]
            pool = match or dirs
            if pool:
                res = max(pool, key=lambda d: d.stat().st_mtime)
                if not match:
                    warn("插件 %s 没有与应用版本 %s 匹配的缓存目录，改用最新的 %s" % (plugin, ver, res.name))
        self._vdir[plugin] = res
        return res

    def resolve_key(self, key: str):
        kind, _, rest = key.partition("/")
        if kind == "bundle":
            return self.bundle / rest
        if kind == "market":
            return self.market_root / rest
        if kind == "cache":
            plugin, _, rel = rest.partition("/")
            vd = self.cache_version_dir(plugin)
            return (vd / rel) if vd else None
        return None

    # ---- 文本文件目标：{key: Path}
    def file_targets(self) -> dict:
        out = {}

        def walk(root: Path, prefix: str, exts=TEXT_EXTS):
            if not root.is_dir():
                return
            for p in sorted(root.rglob("*")):
                if not p.is_file() or p.suffix not in exts:
                    continue
                rel = p.relative_to(root)
                if any(part.startswith(".") for part in rel.parts):
                    continue
                out[prefix + "/" + rel.as_posix() if prefix else rel.as_posix()] = p

        # 1. 应用包
        walk(self.bundle / "resources" / "templates", "bundle/resources/templates", {".tpl"})
        for sub in PLUGIN_SUBTREES:
            walk(self.plugin_root / sub, "bundle/resources/plugins/workbuddy-builtin/" + sub)
        # 2. 插件缓存（只处理当前版本目录）
        if self.cache_root.is_dir():
            for pd in sorted(self.cache_root.iterdir()):
                if pd.is_dir() and any(r.match(pd.name) for r in CACHE_PLUGIN_PATTERNS):
                    vd = self.cache_version_dir(pd.name)
                    if vd:
                        walk(vd, "cache/" + pd.name)
        # 3. 市场目录
        for sub in PLUGIN_SUBTREES:
            walk(self.market_root / sub, "market/" + sub)
        return out

    # ---- 带 prompts 的 json：[(label, Path)]
    def json_targets(self) -> list:
        res = []
        if self.product_json.is_file():
            res.append(("product.json", self.product_json))
        for p in sorted(self.cache_dir.glob("acc-product-config-v3*.json")):
            res.append(("cache/" + p.name, p))
        sp = self.cache_dir / "conversation-product-spill"
        if sp.is_dir():
            for p in sorted(sp.glob("*.json")):
                res.append(("spill/" + p.name, p))
        return res

    def personal_dirs(self) -> list:
        return sorted(d for d in self.home.glob("user-*-personal") if d.is_dir())

    def settings_path(self) -> Path:
        return self.home / "settings.json"


def warn(msg: str) -> None:
    print("  [注意] " + msg, file=sys.stderr)


def load_prompts(p: Path):
    """返回 (原文, 解析后对象)；没有 prompts 列表时返回 (原文, None)。"""
    text = read_text(p)
    if text is None:
        return None, None
    try:
        d = json.loads(text)
    except ValueError:
        return text, None
    if not isinstance(d, dict) or not isinstance(d.get("prompts"), list):
        return text, None
    return text, d


# --------------------------------------------------------------------------- 配置目录

class Profile:
    def __init__(self, path):
        self.dir = Path(os.path.expanduser(path))
        self.files = {}  # key -> text
        for kind in ("bundle", "cache", "market"):
            root = self.dir / kind
            if root.is_dir():
                for p in sorted(root.rglob("*")):
                    if p.is_file():
                        t = read_text(p)
                        if t is not None:
                            self.files[p.relative_to(self.dir).as_posix()] = t
        self.prompts = self._json("prompts.json", {})
        self.skill_overrides = self._json("skill-overrides.json", {})
        self.kept = set(self._lines("kept.txt"))
        self.required = self._lines("required.txt")
        self.markers = self._lines("markers.txt")
        self.personal = {n: read_text(self.dir / n) for n in PERSONAL_FILES if (self.dir / n).is_file()}

    def _json(self, name, default):
        p = self.dir / name
        if not p.is_file():
            return default
        try:
            return json.loads(p.read_text("utf-8"))
        except ValueError:
            sys.exit("配置文件不是合法 JSON: %s" % p)

    def _lines(self, name):
        p = self.dir / name
        if not p.is_file():
            return []
        out = []
        for ln in p.read_text("utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append(ln)
        return out

    def knows_file(self, key):
        return key in self.files or ("file:" + key) in self.kept

    def knows_prompt(self, name):
        return name in self.prompts or ("prompt:" + name) in self.kept


# --------------------------------------------------------------------------- 变量收集（required.txt、verify ②）

_KW = set("""if elif else endif for endfor in is not and or true false none null True False None loop set endset include import
from as with ignore missing block endblock extends macro endmacro call endcall filter endfilter raw endraw verbatim
endverbatim self super caller varargs kwargs without context asyncEach endeach asyncAll endall""".split())
_TAG = re.compile(r"\{\{-?(.*?)-?\}\}|\{%-?\s*(\w+)(.*?)-?%\}", re.S)
_STR = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'")
_IDENT = re.compile(r"(?<![\w.|$])([A-Za-z_]\w*)")
_XML_TAG = re.compile(r"<([A-Za-z_][\w:-]*)>")
_HTML = set("br p div span b i a ul ol li code pre hr em strong h1 h2 h3 h4 table tr td th img details summary".split())


def extract_vars(text: str) -> set:
    loopvars, found = set(), set()
    parts = []
    for m in _TAG.finditer(text):
        if m.group(1) is not None:
            parts.append(("out", _STR.sub('""', m.group(1))))
        else:
            parts.append((m.group(2), _STR.sub('""', m.group(3))))
    for kind, body in parts:
        if kind == "for":
            lhs, _, _ = body.partition(" in ")
            loopvars.update(re.findall(r"[A-Za-z_]\w*", lhs))
        elif kind == "set":
            lhs, _, _ = body.partition("=")
            loopvars.update(re.findall(r"[A-Za-z_]\w*", lhs))
    for kind, body in parts:
        if kind in ("out", "if", "elif", "for", "set"):
            if kind == "for":
                body = body.partition(" in ")[2]
            elif kind == "set":
                body = body.partition("=")[2]
            for m in _IDENT.finditer(body):
                n = m.group(1)
                if n not in _KW and n not in loopvars:
                    found.add(n)
    return found


def extract_tags(text: str) -> set:
    return {t for t in _XML_TAG.findall(text) if t.lower() not in _HTML}


# --------------------------------------------------------------------------- scan

def collect_prompt_rows(ctx: Ctx):
    """product.json 的每个 prompt: [(name, chars)]"""
    _, d = load_prompts(ctx.product_json)
    if d is None:
        return []
    return [(p.get("name"), len(p.get("template") or "")) for p in d["prompts"] if p.get("name")]


def cmd_scan(args):
    ctx = Ctx(args)
    prof = Profile(args.profile) if args.profile else None
    print("应用包: %s\n数据目录: %s\n应用版本: %s" % (short(ctx.bundle), short(ctx.home), ctx.app_version))
    rows = []
    for key, p in ctx.file_targets().items():
        t = read_text(p)
        if t is None:
            continue
        st = ""
        if prof:
            if key in prof.files:
                st = "一致" if prof.files[key] == t else "待重放"
            elif ("file:" + key) in prof.kept:
                st = "保留原样"
            else:
                st = "未审"
        rows.append((len(t), key, st))
    pcur = {}
    _, d = load_prompts(ctx.product_json)
    if d:
        for p in d["prompts"]:
            if p.get("name"):
                pcur[p["name"]] = p.get("template") or ""
    for name, t in pcur.items():
        st = ""
        if prof:
            if name in prof.prompts:
                st = "一致" if prof.prompts[name] == t else "待重放"
            elif ("prompt:" + name) in prof.kept:
                st = "保留原样"
            else:
                st = "未审"
        rows.append((len(t), "prompt:" + name, st))
    rows.sort(key=lambda r: (-r[0], r[1]))
    tot = sum(r[0] for r in rows)
    print("\n%8s  %-8s %s" % ("字符数", "状态" if prof else "", "路径或名称"))
    for n, k, st in rows:
        print("%8d  %-8s %s" % (n, st, k))
    print("\n共 %d 项，合计 %d 字符" % (len(rows), tot))
    # 产品配置缓存（嵌着 prompts 副本）
    js = ctx.json_targets()
    cache_files = [x for x in js if x[0].startswith("cache/")]
    spill = [x for x in js if x[0].startswith("spill/")]
    print("\n产品配置缓存（同名 prompt 会被 apply 同步替换）:")
    for label, p in cache_files:
        _, dd = load_prompts(p)
        print("  %s  %d 个 prompt" % (label, len(dd["prompts"]) if dd else 0))
    if spill:
        diff = 0
        npr = 0
        for _, p in spill:
            _, dd = load_prompts(p)
            if dd is None:
                continue
            npr += 1
            if any(pcur.get(q.get("name")) not in (None, q.get("template")) for q in dd["prompts"]):
                diff += 1
        print("  conversation-product-spill/*.json  %d 个文件有 prompts，其中 %d 个与应用包 product.json 不一致" % (npr, diff))
    if prof:
        miss = [k for _, k, st in rows if st == "未审"]
        print("\n配置目录 %s: 未审 %d 项，待重放 %d 项" % (short(prof.dir), len(miss), sum(1 for r in rows if r[2] == "待重放")))
    return 0


# --------------------------------------------------------------------------- capture

def baseline_lookup(baselines, path: Path):
    for b in baselines:
        q = b / str(path).lstrip("/")
        if q.is_file():
            return read_text(q)
    return None


def cmd_capture(args):
    ctx = Ctx(args)
    prof_dir = Path(os.path.expanduser(args.profile))
    baselines = [Path(os.path.expanduser(b)) for b in (args.baseline or [])]
    for b in baselines:
        if not b.is_dir():
            sys.exit("baseline 不是目录: %s" % b)
    prof_dir.mkdir(parents=True, exist_ok=True)
    for kind in ("bundle", "cache", "market"):
        shutil.rmtree(prof_dir / kind, ignore_errors=True)  # 重新捕获时清掉旧文件，避免残留
    kept, nfiles = [], 0
    targets = ctx.file_targets()
    all_text = {}
    for key, p in targets.items():
        t = read_text(p)
        if t is None:
            continue
        all_text[key] = t
        if baselines:
            old = baseline_lookup(baselines, p)
            if old is None or old == t:
                kept.append("file:" + key)  # 备份里没有 = 没动过；相同 = 没改
                continue
        write_text(prof_dir / key, t)
        nfiles += 1
    # prompts
    _, d = load_prompts(ctx.product_json)
    prompts, nkept_p = {}, 0
    if d:
        oldmap = None
        if baselines:
            otext = baseline_lookup(baselines, ctx.product_json)
            if otext is not None:
                try:
                    oldmap = {x.get("name"): x.get("template") for x in json.loads(otext).get("prompts", [])}
                except ValueError:
                    oldmap = None
            else:
                oldmap = {}
        for x in d["prompts"]:
            n, t = x.get("name"), x.get("template")
            if not n or not isinstance(t, str):
                continue
            all_text["prompt:" + n] = t
            if baselines and (oldmap is None or n not in oldmap or oldmap[n] == t):
                kept.append("prompt:" + n)
                nkept_p += 1
                continue
            prompts[n] = t
    (prof_dir / "prompts.json").write_text(json.dumps(prompts, ensure_ascii=False, indent=1) + "\n", "utf-8")
    # skill overrides
    so = {}
    sp = ctx.settings_path()
    if sp.is_file():
        try:
            so = json.loads(sp.read_text("utf-8")).get("skillOverrides", {}) or {}
        except ValueError:
            warn("settings.json 解析失败，skill-overrides.json 留空")
    (prof_dir / "skill-overrides.json").write_text(json.dumps(so, ensure_ascii=False, indent=2) + "\n", "utf-8")
    # USER.md
    got_user = False
    for pd in ctx.personal_dirs():
        t = read_text(pd / "USER.md")
        if t is not None:
            (prof_dir / "USER.md").write_text(t, "utf-8")
            got_user = True
            break
    # kept.txt
    kp = prof_dir / "kept.txt"
    if kept:
        kp.write_text("# 审过、决定保留原样的项（capture --baseline 自动生成）。每次升级后仍用新版原文。\n" + "\n".join(sorted(kept)) + "\n", "utf-8")
    elif kp.exists():
        kp.unlink()
    # required.txt：已存在则尊重用户手改
    rq = prof_dir / "required.txt"
    if rq.exists():
        print("required.txt 已存在，保留不动")
    else:
        vars_, tags = set(), set()
        for key, t in all_text.items():
            if key.startswith("prompt:") or not key.endswith(".tpl"):
                continue  # 初始值只取 .tpl 模板；片段里的内容需要时再手工补
            vars_ |= extract_vars(t)
            tags |= extract_tags(t)
        lines = ["# 必须仍出现在某个模板/片段里的变量名或 <标签>，一行一项；# 开头为注释。可手改。",
                 "# verify 会检查每项至少在一处模板、片段或 product.json 的 prompt 里出现。"]
        lines += sorted(vars_) + sorted("<%s>" % t for t in tags)
        rq.write_text("\n".join(lines) + "\n", "utf-8")
        print("required.txt 初始值: %d 个变量, %d 个标签" % (len(vars_), len(tags)))
    meta = {"captured_at": datetime.datetime.now().isoformat(timespec="seconds"), "app_version": ctx.app_version}
    (prof_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", "utf-8")
    print("已捕获到 %s" % short(prof_dir))
    print("  文件 %d 个, prompt %d 个, 保留原样 %d 项, skillOverrides %d 项, USER.md %s"
          % (nfiles, len(prompts), len(kept), len(so), "有" if got_user else "无"))
    return 0


# --------------------------------------------------------------------------- apply

class Change:
    def __init__(self, kind, path: Path, new_text: str, old_text, note=""):
        self.kind, self.path, self.new, self.old, self.note = kind, path, new_text, old_text, note


def plan_apply(ctx: Ctx, prof: Profile):
    changes, same, missing = [], 0, []
    # 文本文件
    for key, new in prof.files.items():
        p = ctx.resolve_key(key)
        if p is None or not p.is_file():
            missing.append(key)
            continue
        old = read_text(p)
        if old == new:
            same += 1
        else:
            changes.append(Change("文件", p, new, old, key))
    # prompts：product.json + 各缓存
    bundle_names = set()
    _, d0 = load_prompts(ctx.product_json)
    if d0:
        bundle_names = {x.get("name") for x in d0["prompts"]}
    for name in prof.prompts:
        if name not in bundle_names:
            missing.append("prompt:" + name)
    fmt_warn = 0
    for label, p in ctx.json_targets():
        text, d = load_prompts(p)
        if d is None:
            continue
        n = 0
        for x in d["prompts"]:
            nm = x.get("name")
            if nm in prof.prompts and x.get("template") != prof.prompts[nm]:
                x["template"] = prof.prompts[nm]
                n += 1
        if n:
            out = dump_like(text, d)
            ch = Change("prompts", p, out, text, "%s 中 %d 个 prompt" % (label, n))
            changes.append(ch)
    # skillOverrides
    sp = ctx.settings_path()
    if prof.skill_overrides and sp.is_file():
        text = read_text(sp)
        try:
            s = json.loads(text)
        except (ValueError, TypeError):
            s = None
        if isinstance(s, dict):
            cur = s.get("skillOverrides") or {}
            merged = dict(cur)
            merged.update(prof.skill_overrides)
            if merged != cur:
                s["skillOverrides"] = merged
                changes.append(Change("设置", sp, dump_like(text, s), text, "skillOverrides 合并 %d 项" % len(prof.skill_overrides)))
    # 个人工作区文件
    for pd in ctx.personal_dirs():
        for name, new in prof.personal.items():
            if new is None or not new.strip():
                warn("配置里的 %s 为空，已跳过（内容为空会被应用重建成默认模板）" % name)
                continue
            p = pd / name
            old = read_text(p) if p.exists() else None
            if old != new:
                changes.append(Change("个人", p, new, old, "%s/%s" % (pd.name, name)))
    return changes, same, missing


def find_unreviewed(ctx: Ctx, prof: Profile):
    out = []
    for key, p in ctx.file_targets().items():
        if not prof.knows_file(key):
            t = read_text(p)
            out.append((len(t) if t else 0, key))
    _, d = load_prompts(ctx.product_json)
    if d:
        for x in d["prompts"]:
            n = x.get("name")
            if n and not prof.knows_prompt(n):
                out.append((len(x.get("template") or ""), "prompt:" + n))
    out.sort(key=lambda r: (-r[0], r[1]))
    return out


def cmd_apply(args):
    ctx = Ctx(args)
    prof = Profile(args.profile)
    changes, same, missing = plan_apply(ctx, prof)
    denied = []
    unrev = find_unreviewed(ctx, prof)
    print("应用版本 %s | 配置: 文件 %d, prompt %d, 保留原样 %d" % (ctx.app_version, len(prof.files), len(prof.prompts), len(prof.kept)))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bdir = ctx.backup_root / stamp
    if args.dry_run:
        print("\n[dry-run] 不会写入任何文件")
    elif changes:
        n = 1
        while bdir.exists():  # 同一秒内重复运行
            n += 1
            bdir = ctx.backup_root / ("%s-%d" % (stamp, n))
        entries = []
        # 先整批备份，再写入
        for ch in changes:
            e = {"path": str(ch.path), "kind": ch.kind, "existed": ch.old is not None}
            if ch.old is not None:
                dst = bdir / str(ch.path).lstrip("/")
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ch.path, dst)
                e["backup"] = str(dst.relative_to(bdir))
                e["before_sha1"] = sha1(ch.old.encode("utf-8"))
            e["after_sha1"] = sha1(ch.new.encode("utf-8"))
            entries.append(e)
        manifest = {"created": datetime.datetime.now().isoformat(timespec="seconds"), "app_version": ctx.app_version,
                    "bundle": str(ctx.bundle), "home": str(ctx.home), "entries": entries}
        (bdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
        for ch in changes:
            try:
                write_text(ch.path, ch.new)
            except PermissionError:
                # macOS「App 管理」不允许后台进程改 /Applications 里的应用；跳过，继续写其他位置
                denied.append(ch)
                tmp = ch.path.with_name(ch.path.name + ".slim-tmp")
                if tmp.exists():
                    tmp.unlink()
        changes = [ch for ch in changes if ch not in denied]
    print("\n== 已改%s: %d 项（另有 %d 项内容相同，跳过）" % ("（预演）" if args.dry_run else "", len(changes), same))
    spill = [ch for ch in changes if ch.note.startswith("spill/")]
    for ch in changes:
        if ch in spill:
            continue
        old = len(ch.old) if ch.old is not None else 0
        print("  [%s] %s  (%d -> %d 字符)" % (ch.kind, ch.note, old, len(ch.new)))
    if spill:
        print("  [prompts] conversation-product-spill/*.json 共 %d 个文件（同名 prompt 同步替换）" % len(spill))
    if changes and not args.dry_run:
        print("  备份: %s" % short(bdir))
    if denied:
        print("\n== 没有写入权限: %d 项（备份已做，原文未动）" % len(denied))
        for ch in denied:
            print("  - " + ch.note)
        print("  macOS 不允许当前进程修改 /Applications 里的应用。在有「App 管理」权限的终端或 Agent 会话里重跑 apply 即可。")
    print("\n== 配置里有、当前版本找不到目标: %d 项（被改名或删除？）" % len(missing))
    for k in missing:
        print("  - " + k)
    print("\n== 当前版本有、配置里没有（未审）: %d 项" % len(unrev))
    for n, k in unrev[:60]:
        print("  - %6d  %s" % (n, k))
    if len(unrev) > 60:
        print("  ... 另有 %d 项，用 scan --profile 查看全部" % (len(unrev) - 60))
    if changes and not args.dry_run:
        print("\n提示: product.json 只在启动时读入内存，请完全退出并重启 WorkBuddy 生效。")
    print("SUMMARY %s=%d missing=%d unreviewed=%d denied=%d" % ("would_change" if args.dry_run else "changed", len(changes), len(missing), len(unrev), len(denied)))
    return 3 if denied else 0


# --------------------------------------------------------------------------- verify

def tag_pair_check(text: str):
    """退化检查：{% %} 标签是否配对，{{ }} / {# #} 是否闭合。出错返回描述，正常返回 None。"""
    s = re.sub(r"\{#.*?#\}", "", text, flags=re.S)
    pos = 0
    stack = []
    openers = {"if": "endif", "for": "endfor", "macro": "endmacro", "block": "endblock", "call": "endcall",
               "filter": "endfilter", "raw": "endraw", "verbatim": "endverbatim", "asyncEach": "endeach", "asyncAll": "endall"}
    closers = {v: k for k, v in openers.items()}
    tagre = re.compile(r"\{%-?\s*(\w+)(.*?)-?%\}", re.S)
    while True:
        m = tagre.search(s, pos)
        if not m:
            break
        name, rest = m.group(1), m.group(2)
        pos = m.end()
        if name in ("raw", "verbatim"):
            end = re.search(r"\{%-?\s*end%s\s*-?%\}" % name, s[pos:])
            if not end:
                return "未闭合的 {% " + name + " %}"
            pos += end.end()
            continue
        if name == "set" and "=" not in rest:
            stack.append("set")
        elif name == "endset":
            if not stack or stack.pop() != "set":
                return "多余的 {% endset %}"
        elif name in openers:
            stack.append(name)
        elif name in closers:
            if not stack or stack.pop() != closers[name]:
                return "{% " + name + " %} 与前面的开标签不配对"
        elif name in ("elif", "else"):
            if not stack or stack[-1] not in ("if", "for", "asyncEach", "asyncAll"):
                return "{% " + name + " %} 不在 if/for 内"
    if stack:
        return "未闭合的 {% " + stack[-1] + " %}"
    rest = re.sub(r"\{%.*?%\}", "", s, flags=re.S)
    if rest.count("{{") != rest.count("}}"):
        return "{{ }} 数量不配对"
    return None


def find_node(ctx: Ctx):
    n = shutil.which("node")
    if n:
        return n
    cands = sorted(ctx.home.glob("binaries/node/versions/*/bin/node"), reverse=True)
    cands += [Path(x) for x in ("/opt/homebrew/bin/node", "/usr/local/bin/node", os.path.expanduser("~/.local/bin/node"))]
    for c in cands:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def compile_all(ctx: Ctx, items: dict, nunjucks_dir):
    """items: {id: text}。返回 (引擎描述, {id: 错误文本})。"""
    node = find_node(ctx)
    note = ""
    if node and ((ctx.bundle / "cli" / "dist").is_dir() or nunjucks_dir):
        payload = {"distDir": str(ctx.bundle / "cli" / "dist"), "nunjucksDir": nunjucks_dir,
                   "items": [{"id": k, "text": v} for k, v in items.items()]}
        try:
            r = subprocess.run([node, str(HERE / "nunjucks_check.mjs")], input=json.dumps(payload), capture_output=True,
                               text=True, timeout=300, cwd=str(Path.home()))
            out = json.loads(r.stdout) if r.stdout.strip() else {}
            if out.get("engine"):
                return out["engine"], {x["id"]: x["error"] for x in out["results"] if not x["ok"]}
            note = out.get("error") or (r.stderr.strip()[:200] if r.stderr else "node 无输出")
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            note = str(e)
    else:
        note = "找不到 node" if not node else "找不到 cli/dist"
    errs = {}
    for k, v in items.items():
        e = tag_pair_check(v)
        if e:
            errs[k] = e
    return "退化为标签配对检查（原因: %s；只能发现标签不配对，发现不了语法错误）" % note, errs


def include_exists(ctx: Ctx, rel: str) -> bool:
    if (ctx.plugin_root / rel).is_file() or (ctx.market_root / rel).is_file() or (ctx.bundle / "resources" / "templates" / rel).is_file():
        return True
    seg, _, rest = rel.partition("/")
    for pd in ctx.cache_root.glob(seg + "*") if ctx.cache_root.is_dir() else []:
        vd = ctx.cache_version_dir(pd.name)
        if vd and (vd / rest).is_file():
            return True
    return False


_INC = re.compile(r"\{%-?\s*(?:include|import|extends|from)\s+[\"']([^\"']+)[\"']")


def trace_files(ctx: Ctx):
    ps = list(ctx.home.glob("traces/*/*.jsonl"))
    ps.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return ps


def parse_messages(ti: str):
    """toolInput 是 JSON 数组字符串，可能被截断：逐个解析完整的元素。"""
    msgs, dec = [], json.JSONDecoder()
    i = ti.find("[") + 1
    n = len(ti)
    complete = False
    while i < n:
        while i < n and ti[i] in ", \n\r\t":
            i += 1
        if i >= n:
            break
        if ti[i] == "]":
            complete = True
            break
        try:
            obj, j = dec.raw_decode(ti, i)
        except ValueError:
            break
        msgs.append(obj)
        i = j
    return msgs, complete


def content_text(c) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


def latest_generation(ctx: Ctx, max_files=40):
    """在最近的 trace 文件里找最后一次 generation 的输入。返回 dict 或 None。"""
    for p in trace_files(ctx)[:max_files]:
        last = None
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                for ln in f:
                    if '"generation"' not in ln or '"toolInput"' not in ln:
                        continue
                    try:
                        d = json.loads(ln)
                    except ValueError:
                        continue
                    sp = d.get("span") or {}
                    if sp.get("name") == "generation" and sp.get("toolInput"):
                        last = sp
        except OSError:
            continue
        if last:
            ti = last["toolInput"]
            if not isinstance(ti, str):
                ti = json.dumps(ti, ensure_ascii=False)
            return {"file": p, "span": last, "input": ti}
    return None


def cmd_verify(args):
    ctx = Ctx(args)
    prof = Profile(args.profile)
    fails = 0
    # ---- 收集文本池
    pool = {}  # id -> text
    for key, p in ctx.file_targets().items():
        t = read_text(p)
        if t is not None:
            pool["file:" + key] = t
    seen = {}
    for label, p in ctx.json_targets():
        _, d = load_prompts(p)
        if not d:
            continue
        for x in d["prompts"]:
            t = x.get("template")
            if isinstance(t, str):
                h = sha1(t.encode("utf-8"))
                if h not in seen:
                    seen[h] = "prompt:%s@%s" % (x.get("name"), label)
                    pool[seen[h]] = t
    # ---- ① 试编译
    engine, errs = compile_all(ctx, pool, args.nunjucks_dir)
    print("[①] 模板试编译  引擎: %s" % engine)
    print("    共 %d 段（文件 %d + 去重后的 prompt %d），失败 %d" % (len(pool), sum(1 for k in pool if k.startswith("file:")), len(seen), len(errs)))
    for k, e in list(errs.items())[:20]:
        print("    ✗ %s: %s" % (k, e))
    # 引用存在性
    badinc = []
    for k, t in pool.items():
        for m in _INC.finditer(t):
            if not include_exists(ctx, m.group(1)):
                badinc.append("%s -> %s" % (k, m.group(1)))
    for b in badinc[:20]:
        print("    ✗ 引用的片段不存在: " + b)
    if errs or badinc:
        fails += 1
    # ---- ② required
    allvars, alltxt = set(), []
    for t in pool.values():
        allvars |= extract_vars(t)
        alltxt.append(t)
    blob = "\n".join(alltxt)
    lost = []
    for item in prof.required:
        it = item.strip()
        m = re.fullmatch(r"\{\{\s*(\w+)\s*\}\}", it)
        if m:
            it = m.group(1)
        ok = (it in blob) if it.startswith("<") else (it in allvars)
        if not ok:
            lost.append(item)
    print("[②] required.txt  共 %d 项，缺失 %d" % (len(prof.required), len(lost)))
    for x in lost:
        print("    ✗ 缺失: " + x)
    if lost:
        fails += 1
    # ---- ④ 配置是否已套用（升级后没跑 apply，或只跑了一半，都会在这里露出来）
    changes, _same, missing = plan_apply(ctx, prof)
    unrev = find_unreviewed(ctx, prof)
    print("[③] 配置是否已套用  待重放 %d 项；配置里有但找不到目标 %d 项；未审 %d 项" % (len(changes), len(missing), len(unrev)))
    for ch in changes[:10]:
        print("    ✗ 与配置不一致: " + ch.note)
    if len(changes) > 10:
        print("    ... 另有 %d 项" % (len(changes) - 10))
    if changes:
        fails += 1
    # ---- ③ trace
    g = latest_generation(ctx)
    markers = DEFAULT_MARKERS + [m for m in prof.markers if m not in DEFAULT_MARKERS]
    if not g:
        print("[④] 最近一次生成: 没找到 traces/*/*.jsonl 里带输入的 generation（跳过）")
    else:
        sp, ti = g["span"], g["input"]
        msgs, complete = parse_messages(ti)
        sysmsgs = [m for m in msgs if isinstance(m, dict) and m.get("role") == "system"]
        systext = "\n".join(content_text(m.get("content")) for m in sysmsgs)
        trunc = bool((sp.get("attributes") or {}).get("tracePayloadTruncated"))
        if sysmsgs:
            scope = systext
            desc = "系统消息 %d 条，共 %d 字符" % (len(sysmsgs), len(systext))
        else:
            scope = ti
            desc = "未能解析出系统消息（trace 输入被截断？），改为扫描整段输入 %d 字符" % len(ti)
        started = sp.get("startedAt", "")
        print("[④] 最近一次生成: %s  开始于 %s" % (short(g["file"]), started))
        print("    %s%s" % (desc, "；trace 输入被截断，字符数为下限" if trunc and not sysmsgs else ""))
        stale = False
        try:
            t0 = datetime.datetime.strptime(started[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
            newest = max((p.stat().st_mtime for k, p in ctx.file_targets().items()), default=0)
            newest = max(newest, ctx.product_json.stat().st_mtime if ctx.product_json.exists() else 0)
            stale = t0 < newest
        except (ValueError, OSError):
            pass
        hits = [m for m in markers if m in scope]
        if hits:
            if stale:
                print("    ! 发现原版特征串 %s，但这次生成早于最近一次文件修改，尚未重启验证，不计失败" % hits)
            else:
                print("    ✗ 发现原版特征串: %s" % hits)
                fails += 1
        else:
            print("    原版特征串: 无（检查了 %d 个）%s" % (len(markers), "；注意这次生成早于最近的文件修改" if stale else ""))
    print("\n结果: %s" % ("全部通过" if not fails else "有 %d 项未通过" % fails))
    return 1 if fails else 0


# --------------------------------------------------------------------------- restore

def cmd_restore(args):
    bdir = Path(os.path.expanduser(args.backup_dir))
    mf = bdir / "manifest.json"
    if not mf.is_file():
        sys.exit("没有 manifest.json: %s" % bdir)
    m = json.loads(mf.read_text("utf-8"))
    n_back = n_del = n_changed = 0
    for e in m["entries"]:
        p = Path(e["path"])
        cur = read_text(p) if p.exists() else None
        if cur is not None and sha1(cur.encode("utf-8")) != e.get("after_sha1"):
            n_changed += 1
        if e.get("existed"):
            src = bdir / e["backup"]
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".slim-tmp")
            shutil.copy2(src, tmp)
            os.replace(tmp, p)
            n_back += 1
        elif p.exists():
            p.unlink()  # 当时是 apply 新建的文件
            n_del += 1
    print("已还原 %d 个文件，删除 apply 新建的 %d 个文件" % (n_back, n_del))
    if n_changed:
        print("注意: 其中 %d 个文件在 apply 之后又被改过，已被备份内容覆盖" % n_changed)
    print("请完全退出并重启 WorkBuddy。")
    return 0


# --------------------------------------------------------------------------- 入口

def main(argv=None):
    ap = argparse.ArgumentParser(prog="slim.py", description="WorkBuddy 内置提示词精简：可重放")
    ap.add_argument("--bundle", default=DEFAULT_BUNDLE, help="应用包解包目录（app.asar.unpacked）")
    ap.add_argument("--home", default=DEFAULT_HOME, help="WorkBuddy 数据目录（默认 ~/.workbuddy）")
    ap.add_argument("--backup-root", default=None, help="备份根目录（默认 <home 的上级>/.workbuddy-slim/backups）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="列出所有承载提示词的文件和 prompt")
    s.add_argument("--profile")
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("capture", help="把当前状态存成配置目录")
    s.add_argument("--profile", required=True)
    s.add_argument("--baseline", action="append", help="原版备份目录（按绝对路径镜像，可多次给）；只存与之不同的项，其余记入 kept.txt")
    s.set_defaults(fn=cmd_capture)
    s = sub.add_parser("apply", help="把配置套到当前安装上")
    s.add_argument("--profile", required=True)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_apply)
    s = sub.add_parser("verify", help="试编译、检查必留项、检查最近一次生成")
    s.add_argument("--profile", required=True)
    s.add_argument("--nunjucks-dir", default=None, help="外部 nunjucks 所在目录（默认用应用自带的）")
    s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("restore", help="按 manifest 还原备份")
    s.add_argument("backup_dir")
    s.set_defaults(fn=cmd_restore)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
