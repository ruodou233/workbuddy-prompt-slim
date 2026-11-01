// 用 nunjucks 试编译一批模板文本。由 slim.py verify 调用，输入走 stdin（JSON），结果走 stdout（JSON）。
// 输入: { distDir, nunjucksDir?, items: [{ id, text }] }
// 引擎优先级: nunjucksDir（外部安装的 nunjucks）> 应用自带（cli/dist/lazy-lite-wb 里的 rspack 分块）。
import fs from 'node:fs';
import path from 'node:path';
import events from 'node:events';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const input = JSON.parse(fs.readFileSync(0, 'utf8'));

function loadExternal(dir) {
  const req = createRequire(path.join(path.resolve(dir), 'noop.js'));
  for (const spec of ['nunjucks', path.resolve(dir)]) {
    try { return req(spec); } catch (e) { /* 试下一个 */ }
  }
  throw new Error('在 ' + dir + ' 下找不到 nunjucks');
}

async function loadBundled(distDir) {
  const lazy = path.join(distDir, 'lazy-lite-wb');
  const files = fs.readdirSync(lazy).filter((f) => f.endsWith('.mjs'));
  const mods = {};
  for (const f of files) {
    try {
      const m = await import(pathToFileURL(path.join(lazy, f)).href);
      Object.assign(mods, m.__webpack_modules__ || {});
    } catch (e) { /* 个别分块导入失败不影响 */ }
  }
  // 应用分块里被外置的 node 内置模块（编号随版本可能变化；找不到的当空对象，nunjucks 的可选依赖都有 try/catch）
  const ext = { 79896: fs, 16928: path, 24434: events };
  const cache = {};
  function req(id) {
    if (cache[id]) return cache[id].exports;
    if (ext[id]) return ext[id];
    const fn = mods[id];
    if (!fn) return {};
    const mod = (cache[id] = { exports: {} });
    fn(mod, mod.exports, req);
    return mod.exports;
  }
  req.n = (x) => (x && x.__esModule ? () => x.default : () => x);
  req.d = (o, d) => { for (const k in d) Object.defineProperty(o, k, { enumerable: true, get: d[k] }); };
  req.r = (o) => Object.defineProperty(o, '__esModule', { value: true });
  req.o = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
  req.g = globalThis;
  req.t = (v) => v;
  // 入口模块 = nunjucks 的 index（同时出现 Environment / Template / FileSystemLoader / WebLoader）
  const entries = Object.keys(mods).filter((id) => {
    const s = String(mods[id]);
    return s.includes('FileSystemLoader') && s.includes('WebLoader') && s.includes('Environment');
  });
  for (const id of entries) {
    try {
      const nj = req(id);
      if (nj && typeof nj.Environment === 'function' && typeof nj.Template === 'function') return nj;
    } catch (e) { /* 试下一个 */ }
  }
  throw new Error('应用包里没找到可加载的 nunjucks 分块');
}

let nj, source;
try {
  if (input.nunjucksDir) { nj = loadExternal(input.nunjucksDir); source = '外部 nunjucks（' + input.nunjucksDir + '）'; }
  else { nj = await loadBundled(input.distDir); source = '应用自带 nunjucks（cli/dist/lazy-lite-wb 分块）'; }
  // 自检：能编译合法模板、能拒绝残缺模板，否则引擎不可信
  const env0 = new nj.Environment(null, { autoescape: false });
  const ok = new nj.Template('{% if a %}x{{ a }}{% endif %}', env0, 'smoke', true);
  let rejected = false;
  try { new nj.Template('{% if a %}', env0, 'smoke2', true); } catch (e) { rejected = true; }
  if (!ok || !rejected) throw new Error('自检未通过');
} catch (e) {
  process.stdout.write(JSON.stringify({ engine: null, error: String(e.message || e) }));
  process.exit(0);
}

const env = new nj.Environment(null, { autoescape: false });
const results = [];
for (const it of input.items) {
  try {
    new nj.Template(it.text, env, it.id, true);
    results.push({ id: it.id, ok: true });
  } catch (e) {
    results.push({ id: it.id, ok: false, error: String(e.message || e).split('\n').filter(Boolean).slice(0, 3).join(' / ') });
  }
}
process.stdout.write(JSON.stringify({ engine: source, results }));
