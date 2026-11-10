/* VC-R-01 本地界面前端
 *
 * 三条纪律，和项目铁律一致：
 * 1. 这里**不算规则**。能不能退、能不能流转，全部由服务端调 src/main.py 的 CLI 决定，
 *    页面只负责把请求发出去、把 CLI 的原话显示出来。
 * 2. 每个动作都显示「等价命令 + 终端原样输出」，点按钮 = 敲那条命令。
 * 3. 数字口径不混用：去重前 1,067,371 / 去重后 1,044,848 分开摆，切片数据单独标注。
 */
'use strict';

/* ---------------------------------------------------------------- 基础工具 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const esc = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

const fmt = (n) => (n === null || n === undefined || n === '' ? '—' : Number(n).toLocaleString('zh-CN'));

const clamp = (n) => Math.max(0, Math.min(100, n));

const state = {
  meta: null,
  route: 'overview',
  invoice: null,
  invoiceQuery: '',
  requests: null,
  statusFilter: '',
  rules: null,
  evidence: null,
  demo: null,
  lastCli: null,
};

/* ---------------------------------------------------------------- 请求 */

async function api(path, { method = 'GET', body = null } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error(`响应不是 JSON（HTTP ${res.status}）：${text.slice(0, 180)}`);
  }
  if (!res.ok) throw new Error(payload.error || `HTTP ${res.status}`);
  return payload;
}

const apiGet = async (path) => {
  const payload = await api(path);
  if (!payload.ok) throw new Error(payload.error || '接口返回失败');
  return payload.data;
};

const apiPost = (path, body) => api(path, { method: 'POST', body });

/* ---------------------------------------------------------------- 提示条 */

function toast(message, tone = '') {
  const box = document.createElement('div');
  box.className = `toast ${tone}`;
  box.textContent = message;
  $('#toast').appendChild(box);
  setTimeout(() => box.remove(), 3200);
}

/* ---------------------------------------------------------------- 弹窗 */

function openModal({ title, sub = '', body = '', footer = '', wide = false, onMount }) {
  const root = $('#modal-root');
  root.innerHTML = `<div class="modal-overlay">
    <div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true">
      <div class="modal-head">
        <div>
          <h3>${esc(title)}</h3>
          ${sub ? `<div class="modal-sub">${sub}</div>` : ''}
        </div>
        <button class="modal-close" data-close aria-label="关闭">&times;</button>
      </div>
      <div class="modal-body" id="modal-body">${body}</div>
      ${footer ? `<div class="modal-foot" id="modal-foot">${footer}</div>` : ''}
    </div>
  </div>`;

  const overlay = $('.modal-overlay', root);
  const close = () => { root.innerHTML = ''; document.removeEventListener('keydown', onKey); };
  const onKey = (event) => { if (event.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  overlay.addEventListener('click', (event) => { if (event.target === overlay) close(); });
  $$('[data-close]', root).forEach((btn) => btn.addEventListener('click', close));

  const handle = { close, root, body: $('#modal-body'), foot: $('#modal-foot') };
  if (onMount) onMount(handle);
  return handle;
}

/* ---------------------------------------------------------------- 片段 */

const dotClass = (tone) => (tone === 'ok' ? 'ok' : tone === 'danger' ? 'danger' : tone === 'warn' ? 'warn' : '');

function badge(status, label, tone) {
  return `<span class="badge ${tone || 'muted'}">${esc(label || status)}</span>`;
}

/** 命令行实录：界面上每个动作都能看到"我到底跑了哪条命令、终端说了什么"。 */
function cliBlock(cli) {
  if (!cli) return '';
  const tone = cli.exitCode === 0 ? 'ok' : cli.exitCode === 3 ? 'warn' : 'danger';
  const lines = cli.lines.map((line) => {
    if (line.startsWith('RESULT=')) {
      return `<span class="${cli.exitCode === 0 ? 'ok' : 'bad'}">${esc(line)}</span>`;
    }
    if (line.startsWith('错误')) return `<span class="bad">${esc(line)}</span>`;
    if (line.startsWith('  （')) return `<span class="dim">${esc(line)}</span>`;
    return esc(line);
  }).join('\n');
  return `<div class="cli-wrap">
    <div class="cli-head">
      <span class="dot ${tone}"></span>
      <span>命令行实录（界面调的就是这条命令）</span>
      <span class="cli-verdict">${esc(cli.verdict)} · exit ${cli.exitCode}</span>
    </div>
    <pre class="cli"><span class="cmd">$ ${esc(cli.command)}</span>${lines}</pre>
  </div>`;
}

/** 一句话判决：把 CLI 输出里最像"结论"的那行提出来，错误码单独一行。
 *  exit 3（E_MANUAL）既不是成功也不是失败，是"转人工核验"，所以单给一个 manualText。 */
function verdictBlock(cli, { okText, manualText, failText } = {}) {
  if (!cli) return '';
  const tone = cli.exitCode === 0 ? 'ok' : cli.exitCode === 3 ? 'warn' : 'danger';
  const noise = (line) => line.startsWith('RESULT=') || line.startsWith('  （');
  const message = cli.lines.filter((line) => !noise(line)).join('\n') || cli.verdict;
  const head = cli.exitCode === 0
    ? (okText || '已通过')
    : cli.exitCode === 3
      ? (manualText || cli.verdict)
      : (failText || cli.verdict);
  return `<div class="verdict ${tone}">
    <div class="verdict-icon">${cli.exitCode === 0 ? '✓' : cli.exitCode === 3 ? '!' : '✕'}</div>
    <div class="verdict-body">
      <div class="verdict-code">${esc(cli.result || '—')} · ${esc(head)}</div>
      <div class="verdict-msg">${esc(message)}</div>
    </div>
  </div>`;
}

/** 额度条：原始可退 / 历史已退 / 已占用 / 剩余，一眼看出"为什么只能退这么多"。 */
function quotaBlock(lineState) {
  const s = lineState;
  if (!s) return '';
  const raw = Number(s.returnable_qty);
  if (raw <= 0) {
    return `<div class="quota">
      <div class="quota-head">
        <span class="quota-label">可退数量</span>
        <span class="quota-value">0</span>
      </div>
      <div class="small muted">这一行原始可退数量为 ${esc(raw)}，没有可退额度。</div>
    </div>`;
  }
  const hist = clamp((s.history_returned / raw) * 100);
  const used = clamp((s.active_qty / raw) * 100);
  const free = Math.max(0, 100 - hist - used);
  return `<div class="quota">
    <div class="quota-head">
      <span class="quota-label">现在还能退</span>
      <span class="quota-value">${fmt(s.retirable)} <small class="muted">件</small></span>
    </div>
    <div class="quota-track">
      <div class="quota-seg hist" style="width:${hist}%"></div>
      <div class="quota-seg used" style="width:${used}%"></div>
      <div class="quota-seg free" style="width:${free}%"></div>
    </div>
    <div class="quota-legend">
      <span><i style="background:#b9c0cf"></i>历史已退 <b>${fmt(s.history_returned)}</b></span>
      <span><i style="background:#f0b429"></i>系统内占用 <b>${fmt(s.active_qty)}</b></span>
      <span><i style="background:#0a8f5b"></i>剩余可退 <b>${fmt(s.retirable)}</b></span>
      <span class="muted">原始 ${fmt(raw)} − 历史 ${fmt(s.history_returned)} − 占用 ${fmt(s.active_qty)} = ${fmt(s.retirable)}</span>
    </div>
  </div>`;
}

/* ---------------------------------------------------------------- 侧栏与顶栏 */

const ROUTE_META = {
  overview: { title: '概览', sub: '这一库现在是什么状态' },
  invoice: { title: '发票查询', sub: '查一条明细还能退几件' },
  requests: { title: '申请单', sub: '推动状态机、看流水' },
  demo: { title: '演示剧本', sub: '一条龙：查询 → 申请 → 受理 → 跳级被拒 → 收货 → 结案' },
  rules: { title: '规则说明', sub: 'R-01 ~ R-09 与代码落点' },
  evidence: { title: '证据与自检', sub: '13 项不变量、去重口径、拒绝计数' },
};

function renderChrome() {
  const meta = state.meta;
  $$('#nav a').forEach((link) => {
    link.classList.toggle('active', link.dataset.route === state.route);
  });

  const info = ROUTE_META[state.route] || ROUTE_META.overview;
  $('#crumb').innerHTML = `<strong>${esc(info.title)}</strong>
    <span class="crumb-sub">${esc(info.sub)}</span>`;

  const mode = meta
    ? `<span class="badge ${meta.mode === '全量' ? 'ok' : meta.mode === '切片' ? 'warn' : 'info'} no-dot">${esc(meta.mode)}数据</span>`
    : '';
  $('#topbar-right').innerHTML = `${mode}
    <button class="btn btn-ghost btn-sm" id="btn-import-sample">导入切片</button>
    <button class="btn btn-ghost btn-sm" id="btn-reload">刷新</button>`;

  const chip = $('#db-chip');
  if (meta) {
    chip.innerHTML = `<b>${esc(meta.mode)} · ${meta.dbExists ? '已连接' : '库不存在'}</b>
      <code>${esc(meta.db)}</code>
      <div style="margin-top:4px">${esc(meta.note || '')}</div>`;
  }

  $('#btn-reload').addEventListener('click', () => { render({ refresh: true }); toast('已刷新'); });
  $('#btn-import-sample').addEventListener('click', () => importData('sample'));
}

/* ---------------------------------------------------------------- 数据导入 */

async function importData(source) {
  const label = source === 'sample' ? '包内 2,000 行切片' : '全量 order_line.csv';
  const handle = openModal({
    title: `导入数据：${label}`,
    sub: '走的是 <code>python src/main.py import</code> 这条命令，结果原样显示在下面',
    body: `<div class="notice info"><span class="ico">i</span><div>
      导入是**整体重建**明细表（<code>--rebuild</code>），申请记录会保留。
      切片数据不与全量基线比较（<code>--baseline report</code>）；全量 CSV 才用
      <code>strict</code>，任何一项不变量不符就整体回滚、一行都不进。
    </div></div>
    <div id="import-result"><div class="muted small"><span class="spinner"></span> 正在导入，请稍候…</div></div>`,
    footer: '<button class="btn" data-close>关闭</button>',
  });
  try {
    const res = await apiPost('/api/import', { source, rebuild: true });
    const box = $('#import-result', handle.root);
    if (res.error) {
      box.innerHTML = `<div class="verdict danger"><div class="verdict-icon">✕</div>
        <div class="verdict-body"><div class="verdict-code">导入未执行</div>
        <div class="verdict-msg">${esc(res.error)}</div></div></div>`;
      return;
    }
    box.innerHTML = verdictBlock(res.cli, { okText: '导入完成', failText: '导入失败' }) + cliBlock(res.cli);
    if (res.ok) {
      toast('导入完成', 'ok');
      await loadMeta();
      renderChrome();
      render({ refresh: true });
    } else {
      toast('导入失败，看命令输出', 'bad');
    }
  } catch (err) {
    $('#import-result', handle.root).innerHTML = `<div class="verdict danger">
      <div class="verdict-icon">✕</div><div class="verdict-body">
      <div class="verdict-code">请求失败</div><div class="verdict-msg">${esc(err.message)}</div></div></div>`;
  }
}

/* ---------------------------------------------------------------- 概览 */

function emptyDbHTML(data) {
  return `<div class="card"><div class="empty">
    <strong>库里还没有明细</strong>
    先用包内 2,000 行切片建一个演示库，或者把全量 order_line.csv 导进来。
    <div style="margin-top:14px; display:flex; gap:8px; justify-content:center">
      <button class="btn btn-primary" onclick="importData('sample')">用包内切片建库</button>
      ${data.hasOrderCsv ? `<button class="btn" onclick="importData('csv')">导入全量 CSV</button>` : ''}
    </div>
    ${data.hasOrderCsv ? '' : '<p class="small muted" style="margin-top:10px">全量 CSV 按规范不进提交包；要它请先在项目根跑 <code>python src/prepare.py --no-db</code>。</p>'}
  </div></div>`;
}

function statusChipsHTML(byStatus, total) {
  const counts = byStatus || {};
  const order = ['APPLIED', 'ACCEPTED', 'RECEIVED', 'CLOSED', 'REJECTED', 'PENDING_MANUAL'];
  const labels = {};
  (state.meta?.statuses || []).forEach((s) => { labels[s.status] = s.label; });
  const chips = [`<button class="chip ${state.statusFilter ? '' : 'on'}" data-status="">全部 <span class="n">${fmt(total)}</span></button>`];
  order.forEach((status) => {
    const count = counts[status] || 0;
    if (!count && state.statusFilter !== status) return;
    chips.push(`<button class="chip ${state.statusFilter === status ? 'on' : ''}" data-status="${status}">
      ${esc(labels[status] || status)} <span class="n">${fmt(count)}</span></button>`);
  });
  return `<div class="chips">${chips.join('')}</div>`;
}

async function viewOverview() {
  const data = await apiGet('/api/overview');
  if (data.empty) return emptyDbHTML(data);
  const n = data.numbers;
  const rejects = Object.entries(data.rejects || {});
  const rejectTotal = rejects.reduce((sum, [, count]) => sum + Number(count), 0);
  const prov = state.meta?.provenance || {};

  return `<h1 class="page-title">概览</h1>
  <p class="page-lede">这一屏的数字全部从库里现算，不背稿子。界面上每一个动作都会调
  <code>src/main.py</code> 的同一条 CLI，所以「界面说什么」和「终端说什么」永远一致。</p>

  <div class="grid cols-4">
    <div class="stat">
      <div class="stat-label">明细行数</div>
      <div class="stat-value">${fmt(n.lines)}</div>
      <div class="stat-hint">其中可退明细 ${fmt(n.retirable_rows)} 行</div>
    </div>
    <div class="stat accent">
      <div class="stat-label">可退数量合计</div>
      <div class="stat-value">${fmt(n.retirable_sum)} <small>件</small></div>
      <div class="stat-hint">历史上已退 ${fmt(n.history_sum)} 件</div>
    </div>
    <div class="stat">
      <div class="stat-label">申请单</div>
      <div class="stat-value">${fmt(n.requests)}</div>
      <div class="stat-hint">生效中 ${fmt(n.active_requests)} 条 · 占用 ${fmt(n.active_qty)} 件</div>
    </div>
    <div class="stat">
      <div class="stat-label">校验拒绝累计</div>
      <div class="stat-value">${fmt(rejectTotal)}</div>
      <div class="stat-hint">每次被拒/转人工都计数，落 reject_stat.txt</div>
    </div>
  </div>

  <div class="grid cols-2" style="margin-top:16px">
    <div class="card">
      <div class="card-head"><h2>明细分类</h2><span class="sub">R-03 / R-05 口径</span></div>
      <div class="card-body">
        <dl class="kv">
          <dt>取消行 C 开头</dt><dd class="mono">${fmt(n.cancel_rows)}</dd>
          <dt>坏账调整 A</dt><dd class="mono">${fmt(n.bad_debt)}</dd>
          <dt>非商品行</dt><dd class="mono">${fmt(n.non_product)}</dd>
          <dt>缺客户号</dt><dd class="mono">${fmt(n.missing_cust)}</dd>
          <dt>历史已退过的行</dt><dd class="mono">${fmt(n.history_hit_rows)}</dd>
        </dl>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h2>校验拒绝计数</h2><span class="sub">累计，未手改</span></div>
      <div class="card-body">
        ${rejects.length
          ? `<table class="table"><thead><tr><th>错误码</th><th class="right">次数</th></tr></thead><tbody>
              ${rejects.map(([code, count]) => `<tr><td class="mono">${esc(code)}</td>
                <td class="num">${fmt(count)}</td></tr>`).join('')}
            </tbody></table>`
          : '<div class="muted small">还没有被拒或转人工的申请。去发票查询里故意退超量，这里就会涨。</div>'}
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-head"><h2>去重口径</h2><span class="sub">报告与界面都不许混用这两个数</span></div>
    <div class="card-body">
      <div class="grid cols-3">
        <div class="stat" style="box-shadow:none">
          <div class="stat-label">去重前（原始件）</div>
          <div class="stat-value">${fmt(prov['去重前行数'])}</div>
          <div class="stat-hint">两年两个 sheet 直接拼起来</div>
        </div>
        <div class="stat" style="box-shadow:none">
          <div class="stat-label">重叠发票丢弃</div>
          <div class="stat-value">${fmt(prov['去重丢弃行数'])}</div>
          <div class="stat-hint">${fmt(prov['重叠发票数'])} 张发票在两表里都出现</div>
        </div>
        <div class="stat" style="box-shadow:none">
          <div class="stat-label">去重后（本系统唯一数据源）</div>
          <div class="stat-value">${fmt(prov['去重后行数'])}</div>
          <div class="stat-hint">${fmt(prov['去重前行数'])} − ${fmt(prov['去重丢弃行数'])} = ${fmt(prov['去重后行数'])}</div>
        </div>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-head"><h2>最近申请</h2><a href="#/requests" class="small">全部申请单 →</a></div>
    <div class="card-body tight">
      ${data.recent.length ? `<div class="table-wrap"><table class="table">
        <thead><tr><th>申请号</th><th>明细</th><th>商品</th><th class="right">数量</th><th>状态</th><th>提交时间</th></tr></thead>
        <tbody>${data.recent.map((row) => `<tr class="is-clickable" data-req="${esc(row.req_no)}">
          <td class="mono">${esc(row.req_no)}</td>
          <td><span class="line-key">${esc(row.line_key)}</span></td>
          <td class="desc">${esc(row.description || '—')}</td>
          <td class="num">${fmt(row.qty)}</td>
          <td>${badge(row.status, row.statusLabel, row.tone)}</td>
          <td class="mono small muted">${esc(row.created_at)}</td>
        </tr>`).join('')}</tbody></table></div>`
        : '<div class="empty">还没有申请单。去 <a href="#/invoice">发票查询</a> 挑一条明细申请退货。</div>'}
    </div>
  </div>`;
}

/* ---------------------------------------------------------------- 发票查询 */

async function viewInvoice() {
  const query = state.invoiceQuery.trim();
  let result = '';
  if (state.invoice) {
    const data = state.invoice;
    if (!data.found) {
      result = `<div class="card"><div class="empty">
        <strong>发票 ${esc(data.invoice)} 下没有明细</strong>
        核对一下发票号；取消单是 C 开头（例如 C536379），不要漏掉那个 C。
        ${(data.suggestions || []).length ? `<div class="suggest" style="max-width:420px;margin:14px auto 0;text-align:left">
          ${data.suggestions.map((item) => `<button data-invoice="${esc(item.invoice)}">
            <span class="k">${esc(item.invoice)}</span>
            <span class="muted">明细 ${fmt(item.lines)} 行 · 可退 ${fmt(item.retirable_sum)} 件</span>
          </button>`).join('')}
        </div>` : ''}
      </div></div>`;
    } else {
      const h = data.header;
      result = `<div class="card">
        <div class="card-head">
          <div>
            <h2>发票 <span class="mono">${esc(h.invoice)}</span></h2>
            <div class="sub">客户 ${esc(h.cust_id || '（缺 Customer ID）')} · ${esc(h.country)} · 开票 ${esc(h.invoice_date)}</div>
          </div>
          <div style="text-align:right">
            <div class="stat-value" style="font-size:20px">${fmt(data.totals.retirable)} <small>件可退</small></div>
            <div class="small muted">${fmt(h.lines)} 行明细 · ${fmt(data.totals.applicable)} 行可申请</div>
          </div>
        </div>
        <div class="card-body tight">
          <div class="table-wrap"><table class="table">
            <thead><tr>
              <th>序号</th><th>商品编码</th><th>描述</th>
              <th class="right">数量</th><th class="right">历史已退</th><th class="right">已占用</th>
              <th class="right">可退</th><th>状态</th><th></th>
            </tr></thead>
            <tbody>${data.lines.map((line) => `<tr>
              <td class="mono">${esc(line.seq)}</td>
              <td class="mono">${esc(line.stock_code)}</td>
              <td class="desc" title="${esc(line.description)}">${esc(line.description || '—')}</td>
              <td class="num">${fmt(line.quantity)}</td>
              <td class="num">${fmt(line.history_returned)}</td>
              <td class="num">${fmt(line.active_qty)}</td>
              <td class="num"><b>${fmt(line.retirable)}</b></td>
              <td>${badge(line.code, line.text, line.tone)}</td>
              <td class="right">${line.can_apply
                ? (line.needs_manual
                  ? `<button class="btn btn-sm btn-warn" data-apply="${esc(line.line_key)}" title="${esc(line.reason)}">申请（转人工）</button>`
                  : `<button class="btn btn-sm btn-primary" data-apply="${esc(line.line_key)}">申请退货</button>`)
                : `<button class="btn btn-sm" disabled title="${esc(line.reason || '这一行不可退')}">申请退货</button>`}</td>
            </tr>`).join('')}</tbody>
          </table></div>
        </div>
      </div>
      <div class="notice info" style="margin-top:16px"><span class="ico">i</span><div>
        「可退」是展示值，算法 <code>原始 − 历史已退 − 已占用</code>，与
        <code>rules.compute_retirable</code> 逐字一致。真正点下去时，CLI 会用
        <code>rules.decide</code> 再算一遍——表格算错也不会真放行。
      </div></div>`;
    }
  }

  return `<h1 class="page-title">发票查询</h1>
  <p class="page-lede">输入发票号（支持前缀），从库里捞出这张发票的全部明细：
  每条还能退几件、被谁占了、申请到哪一步。等价命令
  <code>python src/main.py query --invoice …</code>。</p>

  <div class="card">
    <div class="card-body">
      <div class="search-row">
        <input id="invoice-input" type="text" placeholder="发票号，例如 489434 / 536809 / C536379"
               value="${esc(query)}" autocomplete="off" spellcheck="false">
        <button class="btn btn-primary" id="btn-query">查询</button>
      </div>
      <div class="suggest" id="suggest"></div>
      ${(state.meta?.mode === '切片') ? `<div class="small muted" style="margin-top:8px">
        提示：当前是<b>切片数据</b>（包内 2,000 行），库里没有 <code>536809</code> 是正常的，换 <code>489434</code> 试试。
        想看 1820 那组数字，用 <code>--db data/build/return_system.db</code> 启动。
      </div>` : ''}
    </div>
  </div>
  <div style="margin-top:16px" id="invoice-result">${result}</div>`;
}

async function searchInvoices(term) {
  const box = $('#suggest');
  if (!box) return;
  if (!term) { box.innerHTML = ''; return; }
  try {
    const data = await apiGet(`/api/invoices?q=${encodeURIComponent(term)}`);
    box.innerHTML = data.items.slice(0, 6).map((item) => `<button data-invoice="${esc(item.invoice)}">
      <span class="k">${esc(item.invoice)}</span>
      <span class="muted">明细 ${fmt(item.lines)} 行 · 可退 ${fmt(item.retirable_sum)} 件 · ${esc(item.invoice_date || '')}</span>
    </button>`).join('');
  } catch {
    box.innerHTML = '';
  }
}

function bindInvoice() {
  $$('[data-apply]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.apply;
      const line = (state.invoice?.lines || []).find((row) => row.line_key === key);
      if (line) openApplyModal(line);
    });
  });
}

/* ---------------------------------------------------------------- 申请弹窗 */

function openApplyModal(line) {
  const defaultQty = Math.min(1, line.retirable) || 1;
  const handle = openModal({
    title: '提交退货申请',
    sub: `明细 <span class="line-key">${esc(line.line_key)}</span> · ${esc(line.stock_code)}`,
    wide: true,
    body: `
      <dl class="kv">
        <dt>商品</dt><dd>${esc(line.description || '—')}</dd>
        <dt>发票 / 序号</dt><dd class="mono">${esc(line.invoice)} / ${esc(line.seq)}</dd>
        <dt>原单数量</dt><dd class="mono">${fmt(line.quantity)}</dd>
        <dt>开票时间</dt><dd class="mono">${esc(line.invoice_date)}</dd>
        <dt>客户号</dt><dd class="mono">${esc(line.cust_id || '（缺）')}</dd>
      </dl>
      ${quotaBlock(line)}
      <div class="grid cols-2">
        <div class="field">
          <label for="qty-input">申请数量</label>
          <input id="qty-input" class="num-input" type="number" min="1" step="1"
                 max="${line.retirable}" value="${defaultQty}">
          <span class="hint" id="qty-hint">最多 ${fmt(line.retirable)} 件</span>
        </div>
        <div class="field">
          <label for="operator-input">操作人</label>
          <input id="operator-input" type="text" value="${esc(state.meta?.operator || '售后-张')}">
          <span class="hint">写进状态流水</span>
        </div>
      </div>
      <div class="field">
        <label for="reason-input">退货原因（可空）</label>
        <input id="reason-input" type="text" placeholder="例如：尺寸不符">
        <span class="hint">数据集没有原因字段，这里只做人工备注，不出图表</span>
      </div>
      <div id="apply-result"></div>`,
    footer: `<button class="btn" data-close>取消</button>
      <button class="btn btn-primary" id="btn-apply">提交申请</button>`,
    onMount: ({ root }) => {
      const qty = $('#qty-input', root);
      const hint = $('#qty-hint', root);
      qty.addEventListener('input', () => {
        const value = Number(qty.value);
        if (!Number.isFinite(value) || value <= 0) { hint.textContent = '必须是正整数'; return; }
        const left = line.retirable - value;
        hint.innerHTML = left < 0
          ? `<span style="color:var(--danger)">超过可退 ${fmt(line.retirable)}，会被 E_EXCEED 拒绝</span>`
          : `提交后剩余 ${fmt(left)} 件`;
      });

      $('#btn-apply', root).addEventListener('click', async () => {
        const button = $('#btn-apply', root);
        button.disabled = true;
        button.textContent = '提交中…';
        try {
          const res = await apiPost('/api/apply', {
            invoice: line.invoice,
            seq: line.seq,
            qty: Number(qty.value),
            reason: $('#reason-input', root).value.trim(),
            operator: $('#operator-input', root).value.trim(),
          });
          $('#apply-result', root).innerHTML =
            verdictBlock(res.cli, { okText: '已受理', manualText: '已转人工核验', failText: '未通过' })
            + cliBlock(res.cli);
          state.lastCli = res.cli;
          const reqNo = res.reqNo || (res.cli.lines.join('\n').match(/申请号 (R-\d+)/) || [])[1] || '';
          $('#modal-foot', root).innerHTML =
            `<button class="btn" data-close>关闭</button>
             ${reqNo ? '<button class="btn btn-primary" id="btn-after">查看结果</button>' : ''}`;
          $$('[data-close]', root).forEach((el) => el.addEventListener('click', handle.close));
          if (reqNo) {
            $('#btn-after', root).addEventListener('click', async () => {
              handle.close();
              state.invoice = null;
              await render({ refresh: true });
              openRequestDrawer(reqNo);
            });
          }
          // exit 3 = E_MANUAL：申请已落库为 PENDING_MANUAL，不是"被拒"，别用红色报错口吻
          if (res.manual) toast(`已转人工核验 ${reqNo}`.trim(), 'warn');
          else toast(res.ok ? '申请已提交' : `被拒：${res.cli.result}`, res.ok ? 'ok' : 'bad');
        } catch (err) {
          $('#apply-result', root).innerHTML = `<div class="verdict danger">
            <div class="verdict-icon">✕</div><div class="verdict-body">
            <div class="verdict-code">请求失败</div>
            <div class="verdict-msg">${esc(err.message)}</div></div></div>`;
        } finally {
          button.disabled = false;
          button.textContent = '提交申请';
        }
      });
    },
  });
  return handle;
}

/* ---------------------------------------------------------------- 申请单 */

async function viewRequests() {
  const query = state.statusFilter ? `?status=${encodeURIComponent(state.statusFilter)}` : '';
  const data = await apiGet(`/api/requests${query}`);
  const total = Object.values(data.byStatus || {}).reduce((sum, n) => sum + Number(n), 0);
  const rows = data.items;

  return `<h1 class="page-title">申请单</h1>
  <p class="page-lede">状态机 <code>APPLIED → ACCEPTED → RECEIVED → CLOSED</code>，外加人工
  <code>REJECTED</code>。合法边写死在 <code>src/rules.py</code> 的 <code>TRANSITIONS</code> 表里，
  点一下就走 CLI 的同一张表——跳级一定被拒。</p>

  <div class="card" style="margin-bottom:16px">
    <div class="card-body">${statusChipsHTML(data.byStatus, total)}</div>
  </div>

  <div class="card">
    <div class="card-head">
      <h2>${state.statusFilter ? esc(statusLabel(state.statusFilter)) : '全部申请'}</h2>
      <span class="sub">共 ${fmt(rows.length)} 条${rows.length >= 200 ? '（最多显示 200 条）' : ''}</span>
    </div>
    <div class="card-body tight">
      ${rows.length ? `<div class="table-wrap"><table class="table">
        <thead><tr>
          <th>申请号</th><th>明细</th><th>商品</th><th class="right">申请</th>
          <th class="right">原始可退</th><th>状态</th><th>最近更新</th><th></th>
        </tr></thead>
        <tbody>${rows.map((row) => `<tr class="is-clickable" data-req="${esc(row.req_no)}">
          <td class="mono">${esc(row.req_no)}</td>
          <td><span class="line-key">${esc(row.line_key)}</span></td>
          <td class="desc" title="${esc(row.description || '')}">${esc(row.description || '—')}</td>
          <td class="num">${fmt(row.qty)}</td>
          <td class="num">${fmt(row.returnable_qty)}</td>
          <td>${badge(row.status, row.statusLabel, row.tone)}</td>
          <td class="mono small muted">${esc(row.updated_at)}</td>
          <td class="right"><button class="btn btn-sm">查看</button></td>
        </tr>`).join('')}</tbody></table></div>`
        : '<div class="empty"><strong>这个筛选下没有申请单</strong>换个状态看看，或者去发票查询提一条。</div>'}
    </div>
  </div>`;
}

function statusLabel(status) {
  const found = (state.meta?.statuses || []).find((item) => item.status === status);
  return found ? found.label : status;
}

async function openRequestDrawer(reqNo) {
  let detail;
  try {
    detail = await apiGet(`/api/request?req=${encodeURIComponent(reqNo)}`);
  } catch (err) {
    toast(`查不到 ${reqNo}：${err.message}`, 'bad');
    return;
  }
  if (!detail.found) { toast(`没有申请号 ${reqNo}`, 'bad'); return; }

  const handle = openModal({
    title: `申请 ${detail.req_no}`,
    sub: `<span class="line-key">${esc(detail.line_key)}</span> · 数量 ${fmt(detail.qty)} 件`,
    wide: true,
    body: requestBodyHTML(detail),
    footer: `<button class="btn" data-close>关闭</button>
      <button class="btn" id="btn-refresh-req">刷新</button>`,
    onMount: ({ root }) => {
      bindRequestActions(root, detail, handle);
      $('#btn-refresh-req', root).addEventListener('click', async () => {
        const fresh = await apiGet(`/api/request?req=${encodeURIComponent(reqNo)}`);
        handle.body.innerHTML = requestBodyHTML(fresh);
        bindRequestActions(root, fresh, handle);
      });
    },
  });
  return handle;
}

function requestBodyHTML(detail) {
  const line = detail.line;
  const targets = detail.targets || [];
  const legal = targets.filter((t) => t.legal);
  const illegal = targets.filter((t) => !t.legal);
  const meta = state.meta || {};
  const labels = meta.statuses || [];
  const transitions = meta.transitions || {};
  const chain = ['APPLIED', 'ACCEPTED', 'RECEIVED', 'CLOSED'];

  const pipeline = chain.map((status, index) => {
    const info = labels.find((item) => item.status === status) || { label: status };
    const isCurrent = detail.status === status;
    const isLegal = (transitions[detail.status] || []).includes(status);
    return `${index ? '<span class="pipe-arrow">→</span>' : ''}
      <div class="pipe-node ${isCurrent ? 'current' : isLegal ? 'legal' : ''}">
        <span class="code">${esc(status)}</span>
        <span>${esc(info.label)}</span>
      </div>`;
  }).join('');

  const actions = targets.map((target) => `
    <button class="btn ${target.legal ? 'btn-primary' : 'btn-danger'}"
            data-command="${esc(target.command)}" data-to="${esc(target.to)}"
            title="${target.legal ? '合法流转' : '会被拒：E_BAD_TRANSITION'}">
      <span class="t">${target.legal ? target.verb : `试一下：${target.verb}`}</span>
      <span class="s">→ ${esc(target.to)}${target.legal ? '' : '（应被拒）'}</span>
    </button>`).join('');

  const log = (detail.log || []).map((entry) => {
    const tone = entry.to_status === 'REJECTED' ? 'danger'
      : entry.to_status === 'CLOSED' ? 'ok'
      : entry.note && entry.note.startsWith('AUTO_RELEASED') ? 'warn' : 'info';
    return `<div class="timeline-item">
      <div class="timeline-dot ${tone}"></div>
      <div class="timeline-body">
        <div class="timeline-title">
          ${esc(entry.from_status ? rulesLabel(entry.from_status) : '—')}
          → ${esc(rulesLabel(entry.to_status))}
        </div>
        <div class="timeline-meta">${esc(entry.at)} · by ${esc(entry.operator)}</div>
        ${entry.note ? `<div class="timeline-note">${esc(entry.note)}</div>` : ''}
      </div>
    </div>`;
  }).join('');

  return `
  <div class="grid cols-2">
    <div>
      <dl class="kv">
        <dt>申请号</dt><dd class="mono">${esc(detail.req_no)}</dd>
        <dt>状态</dt><dd>${badge(detail.status, detail.statusLabel, detail.tone)}</dd>
        <dt>申请数量</dt><dd class="mono">${fmt(detail.qty)} 件</dd>
        <dt>明细键</dt><dd class="mono"><span class="line-key">${esc(detail.line_key)}</span></dd>
        <dt>发票 / 序号</dt><dd class="mono">${esc(detail.invoice || '—')} / ${esc(detail.seq ?? '—')}</dd>
        <dt>商品</dt><dd>${esc(detail.description || '—')} <span class="muted mono">${esc(detail.stock_code || '')}</span></dd>
        <dt>原因备注</dt><dd>${esc(detail.reason || '—')}</dd>
        <dt>发起人</dt><dd>${esc(detail.operator)}</dd>
        <dt>提交时间</dt><dd class="mono small">${esc(detail.created_at)}</dd>
        ${detail.anomaly_kind ? `<dt>转人工原因</dt><dd class="mono">${esc(detail.anomaly_kind)}</dd>` : ''}
      </dl>
    </div>
    <div>
      ${detail.lineState ? quotaBlock(detail.lineState) : '<div class="muted small">明细已不在库中（可能重跑过导入）。</div>'}
    </div>
  </div>

  <div>
    <div class="small muted" style="margin-bottom:8px">状态机位置</div>
    <div class="pipeline">${pipeline}</div>
  </div>

  <div>
    <div class="small muted" style="margin-bottom:8px">
      推动状态 —— 每个按钮都会调 <code>python src/main.py &lt;动词&gt;</code>，由 TRANSITIONS 表裁决
    </div>
    <div class="transition-grid">${actions}</div>
    ${illegal.length ? `<div class="small muted" style="margin-top:8px">
      红色按钮是<strong>故意的</strong>：现在只能去 ${legal.map((t) => esc(t.to)).join(' / ') || '无（已是终态）'}，
      点红色会拿到 <code>E_BAD_TRANSITION</code>，并告诉你下一步只能是哪几个。
    </div>` : ''}
  </div>

  <div id="req-action-result"></div>

  <div>
    <div class="small muted" style="margin-bottom:10px">状态流水（谁、什么时候、为什么）</div>
    <div class="timeline">${log || '<div class="muted small">没有流水记录。</div>'}</div>
  </div>`;
}

function rulesLabel(status) {
  const found = (state.meta?.statuses || []).find((item) => item.status === status);
  return found ? `${found.label}` : status;
}

function bindRequestActions(root, detail, handle) {
  $$('[data-command]', root).forEach((btn) => {
    btn.addEventListener('click', async () => {
      const command = btn.dataset.command;
      const box = $('#req-action-result', root);
      box.innerHTML = '<div class="muted small"><span class="spinner"></span> 正在执行…</div>';
      try {
        const res = await apiPost('/api/transition', {
          req: detail.req_no,
          command,
          operator: state.meta?.operator || '售后-张',
          note: command === 'reject' ? '人工拒绝（界面操作）' : '界面操作',
        });
        const okText = { accept: '已受理', receive: '已收货', close: '已结案', reject: '已拒绝' }[command];
        box.innerHTML = verdictBlock(res.cli, { okText, failText: '被拒绝' }) + cliBlock(res.cli);
        state.lastCli = res.cli;
        toast(res.ok ? `${okText}` : `被拒：${res.cli.result}`, res.ok ? 'ok' : 'bad');
        if (res.ok && res.request?.found) {
          handle.body.innerHTML = requestBodyHTML(res.request);
          bindRequestActions(root, res.request, handle);
          const fresh = $('#req-action-result', root);
          if (fresh) fresh.innerHTML = verdictBlock(res.cli, { okText, failText: '被拒绝' }) + cliBlock(res.cli);
        }
      } catch (err) {
        box.innerHTML = `<div class="verdict danger"><div class="verdict-icon">✕</div>
          <div class="verdict-body"><div class="verdict-code">请求失败</div>
          <div class="verdict-msg">${esc(err.message)}</div></div></div>`;
      }
    });
  });
}

/* ---------------------------------------------------------------- 演示剧本 */

const DEMO_STEPS = [
  { id: 'reset', title: '复位演示沙盒', desc: '有全量库就复制，没有就用包内 2,000 行切片建库' },
  { id: 'query', title: '查一张能心算的发票', desc: '先看清楚：可退 = 原始 − 历史已退' },
  { id: 'apply', title: '提交申请', desc: '落库并拿到申请号，可退当场变少' },
  { id: 'accept', title: '受理', desc: '主管受理；注意额度还占着，不能直接结案' },
  { id: 'illegal', title: '故意跳级结案', desc: '预期被拒 —— 全场最值钱的一条' },
  { id: 'receive', title: '收货', desc: '质检通过，下一步终于能结案' },
  { id: 'close', title: '结案', desc: '额度在这一刻才真正释放' },
  { id: 'log', title: '回查流水', desc: '四步流转都在流水里，谁做的、为什么都查得到' },
];

function demoState() {
  if (!state.demo) {
    state.demo = { plan: null, results: {}, running: false, trajectory: [] };
  }
  return state.demo;
}

async function viewDemo() {
  const demo = demoState();
  let plan = demo.plan;
  if (!plan) {
    try { plan = await apiGet('/api/demo/plan'); demo.plan = plan; } catch { plan = { found: false }; }
  }
  const target = plan.found
    ? `目标明细 <span class="line-key">${esc(plan.line_key)}</span> ·
       原始 ${fmt(plan.returnable_qty)} / 历史 ${fmt(plan.history_returned)} /
       可退 <b>${fmt(plan.retirable)}</b> · 本次申请 ${fmt(plan.qty)} 件 · 预计申请号 ${esc(plan.nextReqNo)}`
    : (plan.reason || '还没有可演示的明细');

  return `<h1 class="page-title">演示剧本</h1>
  <p class="page-lede">这里没有"演示专用逻辑"：每一步都是界面调 <code>src/main.py</code> 的同一条命令，
  预期写在左边，实际结果写在右边。对不上就是红的。</p>

  <div class="card">
    <div class="card-head">
      <div>
        <h2>本次要演的明细</h2>
        <div class="sub">${target}</div>
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn btn-primary" id="btn-demo-run" ${plan.found ? '' : 'disabled'}>
          ${Object.keys(demo.results).length ? '继续演完全部' : '开始演完全部'}
        </button>
        <button class="btn" id="btn-demo-step" ${plan.found ? '' : 'disabled'}>单步</button>
        <button class="btn btn-ghost" id="btn-demo-clear">重来</button>
      </div>
    </div>
    <div class="card-body">
      <div class="notice ${plan.mode === '切片' ? 'warn' : 'info'}"><span class="ico">i</span><div>
        ${plan.mode === '切片'
          ? '当前是<b>包内 2,000 行切片</b>，数字与讲稿里的 1820 不同——这是正常的。被问到就照实说，全量数字在「证据与自检」页。'
          : `当前是<b>全量数据</b>，演示明细应当是可退 ${fmt(plan.retirable)} 件。`}
        时间一律由 CLI 取真实当前时间（不传 <code>--now</code>），
        所以 R-09 的 7 天惰性释放按真实时钟判断，不会因为"演示时钟"把申请提前放掉。
      </div></div>
      <div class="steps" style="margin:16px -20px -18px">
        ${DEMO_STEPS.map((step, index) => demoStepHTML(step, index + 1, demo)).join('')}
      </div>
    </div>
  </div>

  ${demoTrajectoryHTML(demo)}
  `;
}

function demoTrajectoryHTML(demo) {
  const points = demo.trajectory.filter(Boolean);
  if (!points.length) return '';
  return `<div class="card" style="margin-top:16px">
    <div class="card-head"><h2>额度轨迹</h2><span class="sub">每一步之后都从库里重算，不是背的</span></div>
    <div class="card-body">
      <table class="table"><tbody>
        ${points.map((point) => `<tr>
          <td>${esc(point.label)}</td>
          <td class="num">${fmt(point.value)}</td>
          <td class="small muted">${esc(point.note || '')}</td>
        </tr>`).join('')}
      </tbody></table>
      ${points.length >= 3 ? demoTrajectoryVerdict(demo, points) : ''}
    </div>
  </div>`;
}

function demoTrajectoryVerdict(demo, points) {
  const start = points[0].value;
  const middle = points[1].value;
  const end = points[points.length - 1].value;
  const closedLoop = start === end;
  return `<div class="notice ${closedLoop ? 'info' : 'warn'}" style="margin-top:12px"><span class="ico">${closedLoop ? '✓' : '!'}</span>
    <div>${closedLoop
      ? `闭环成立：${fmt(start)} → ${fmt(middle)} → ${fmt(end)}。额度跟着状态走，结案那一刻才释放，所以同一条明细可以被退多次，但同一时间只能占一次。`
      : `没回到起点（${fmt(start)} → ${fmt(end)}），检查一下中间哪步没走完。`}</div></div>`;
}

function demoStepHTML(step, no, demo) {
  const result = demo.results[step.id];
  const pending = Boolean(result && result.pending);
  const cls = pending ? 'running' : result ? (result.ok ? 'ok' : 'bad') : '';
  const mark = pending ? '<span class="spinner"></span>' : result ? (result.ok ? '✓' : '✗') : no;
  return `<div class="step ${cls}">
    <div class="step-no">${mark}</div>
    <div class="step-main">
      <div class="step-title">${esc(step.title)}</div>
      <div class="step-cmd">${esc(step.desc)}</div>
      ${result && !pending ? `<div class="step-result">
        <div class="step-row exp"><span class="tag">预期</span><span class="val">${esc(result.expected)}</span></div>
        <div class="step-row act ${result.ok ? 'ok' : 'bad'}"><span class="tag">实际</span>
          <span class="val">${esc(result.actual)}</span></div>
        ${result.cli ? `<div style="margin-top:8px">${cliBlock(result.cli)}</div>` : ''}
      </div>` : ''}
    </div>
  </div>`;
}

/** 跑一步；返回 {expected, actual, ok, cli} */
async function runDemoStep(id, demo) {
  const plan = demo.plan;
  const operator = state.meta?.operator || '售后-张';

  if (id === 'reset') {
    const res = await apiPost('/api/demo/reset', {});
    if (res.error) return { expected: '演示库重建完成', actual: res.error, ok: false };
    const fresh = await apiGet('/api/demo/plan');
    demo.plan = fresh;
    demo.results = {};
    demo.trajectory = [];
    await loadMeta();
    renderChrome();
    const cli = res.cli || null;
    const actual = res.mode === '全量'
      ? '演示库已由全量库复制（全量模式）'
      : `演示库已用 2,000 行切片建好（${cli ? cli.result : 'OK'}）`;
    return { expected: '演示库重建完成，重挑一条可退明细', actual, ok: true, cli };
  }

  if (!plan?.found) return { expected: '—', actual: '没有可演示的明细，先复位', ok: false };

  if (id === 'query') {
    const invoice = await apiGet(`/api/invoice?q=${encodeURIComponent(plan.invoice)}`);
    const line = (invoice.lines || []).find((row) => row.seq === plan.seq);
    const actual = `可退 ${fmt(line?.retirable)} 件（原始 ${fmt(line?.returnable_qty)} − 历史已退 ${fmt(line?.history_returned)}）`;
    demo.trajectory = [{ label: '申请前', value: line?.retirable ?? 0, note: '从库里现算' }];
    return {
      expected: `可退 ${fmt(plan.retirable)} 件`,
      actual,
      ok: (line?.retirable ?? -1) === plan.retirable,
    };
  }

  if (id === 'apply') {
    const res = await apiPost('/api/apply', {
      invoice: plan.invoice, seq: plan.seq, qty: plan.qty,
      reason: '尺寸不符', operator,
    });
    const match = (res.cli.lines.join('\n').match(/申请号 (R-\d+)/) || [])[1] || '';
    demo.reqNo = match;
    const line = await apiGet(`/api/line?invoice=${encodeURIComponent(plan.invoice)}&seq=${plan.seq}`);
    const current = line.line?.retirable;
    demo.trajectory[1] = { label: `申请 ${plan.qty} 件占用后`, value: current, note: '额度被这条申请占住' };
    return {
      expected: `申请号 ${plan.nextReqNo}，可退 ${fmt(plan.retirable)} → ${fmt(plan.after_apply)}`,
      actual: `${match || '没有拿到申请号'}，可退 ${fmt(plan.retirable)} → ${fmt(current)}`,
      ok: res.ok && current === plan.after_apply,
      cli: res.cli,
    };
  }

  if (id === 'accept') {
    const res = await apiPost('/api/transition', {
      req: demo.reqNo, command: 'accept', operator: '客服主管-李', note: '电话核实后受理',
    });
    return {
      expected: 'ACCEPTED（已受理），额度仍占着',
      actual: res.ok ? `已受理，当前状态 ${res.request?.status}` : `未成功：${res.cli.result}`,
      ok: res.ok && res.request?.status === 'ACCEPTED',
      cli: res.cli,
    };
  }

  if (id === 'illegal') {
    const res = await apiPost('/api/transition', {
      req: demo.reqNo, command: 'close', operator: '客服主管-李',
    });
    const isReject = res.cli.exitCode === 2 && res.cli.result === 'E_BAD_TRANSITION';
    return {
      expected: '被拒：E_BAD_TRANSITION，退出码 2（下一步只能是 RECEIVED）',
      actual: `退出码 ${res.cli.exitCode} · ${res.cli.result}${isReject ? '（正是预期：跳级被拦）' : ''}`,
      ok: isReject,
      cli: res.cli,
    };
  }

  if (id === 'receive') {
    const res = await apiPost('/api/transition', {
      req: demo.reqNo, command: 'receive', operator: '仓库-王', note: '质检通过',
    });
    return {
      expected: 'RECEIVED（已收货）',
      actual: res.ok ? `已收货，当前状态 ${res.request?.status}` : `未成功：${res.cli.result}`,
      ok: res.ok && res.request?.status === 'RECEIVED',
      cli: res.cli,
    };
  }

  if (id === 'close') {
    const res = await apiPost('/api/transition', {
      req: demo.reqNo, command: 'close', operator: '客服主管-李', note: '退款已处理',
    });
    const line = await apiGet(`/api/line?invoice=${encodeURIComponent(plan.invoice)}&seq=${plan.seq}`);
    const current = line.line?.retirable;
    demo.trajectory[2] = { label: '结案后', value: current, note: '额度真正释放，回到起点' };
    return {
      expected: `CLOSED（已结案），可退回到 ${fmt(plan.retirable)}`,
      actual: res.ok ? `已结案，可退回到 ${fmt(current)}` : `未成功：${res.cli.result}`,
      ok: res.ok && current === plan.retirable,
      cli: res.cli,
    };
  }

  if (id === 'log') {
    const detail = await apiGet(`/api/request?req=${encodeURIComponent(demo.reqNo)}`);
    const count = (detail.log || []).length;
    const path = (detail.log || []).map((entry) => entry.to_status).join(' → ');
    return {
      expected: '四步流转都在流水里（至少 4 条，含首次提交）',
      actual: `${count} 条流水：${path}`,
      ok: count >= 4,
    };
  }

  return { expected: '—', actual: '未知步骤', ok: false };
}

async function runDemoAll(ids) {
  const demo = demoState();
  if (demo.running) return;
  demo.running = true;
  const buttons = ['#btn-demo-run', '#btn-demo-step'];
  buttons.forEach((sel) => { const b = $(sel); if (b) b.disabled = true; });

  for (const id of ids) {
    try {
      demo.results[id] = { expected: '执行中…', actual: '…', ok: true, pending: true };
      await render({ refresh: false });
      demo.results[id] = await runDemoStep(id, demo);
    } catch (err) {
      demo.results[id] = { expected: '—', actual: `出错：${err.message}`, ok: false };
    }
    await render({ refresh: false });
    const fail = demo.results[id] && !demo.results[id].ok;
    if (fail && id !== 'illegal') break;
    await new Promise((resolve) => setTimeout(resolve, 120));
  }

  demo.running = false;
  await render({ refresh: false });
}

function bindDemo() {
  const demo = demoState();
  $('#btn-demo-run')?.addEventListener('click', async () => {
    const remaining = DEMO_STEPS.map((s) => s.id).filter((id) => !demo.results[id]);
    const ids = remaining.includes('reset') ? remaining : remaining;
    if (!ids.length) { toast('已经演完了，按「重来」'); return; }
    await runDemoAll(ids);
  });
  $('#btn-demo-step')?.addEventListener('click', async () => {
    const next = DEMO_STEPS.find((step) => !demo.results[step.id]);
    if (!next) { toast('已经演完了，按「重来」'); return; }
    await runDemoAll([next.id]);
  });
  $('#btn-demo-clear')?.addEventListener('click', async () => {
    state.demo = { plan: null, results: {}, running: false, trajectory: [] };
    await render({ refresh: false });
  });
}

/* ---------------------------------------------------------------- 规则说明 */

async function viewRules() {
  const data = state.rules || await apiGet('/api/rules');
  state.rules = data;
  const labels = data.labels || {};
  const order = ['APPLIED', 'ACCEPTED', 'RECEIVED', 'CLOSED', 'REJECTED', 'PENDING_MANUAL'];

  const transitionTable = order.map((from) => {
    const allowed = data.transitions[from] || [];
    return `<tr>
      <td class="mono">${esc(from)}</td>
      <td>${esc(labels[from] || '')}</td>
      <td>${allowed.length
        ? allowed.map((to) => `<span class="badge info no-dot">${esc(labels[to] || to)}</span>`).join(' ')
        : '<span class="muted small">终态，不能再改</span>'}</td>
    </tr>`;
  }).join('');

  return `<h1 class="page-title">规则说明</h1>
  <p class="page-lede">本页只做说明，不参与判断。真正的裁决在
  <code>src/rules.py</code>；<a href="#/invoice">发票查询</a> 里的每次点击都会回到那里。</p>

  <div class="card">
    <div class="card-head"><h2>九条规则与代码落点</h2><span class="sub">R-01 ~ R-09 一一对应</span></div>
    <div class="card-body tight">
      ${data.table.map((row) => `<div class="rule-row">
        <div class="rule-no">${esc(row.no)}</div>
        <div class="rule-main">
          <div class="rule-title">${esc(row.title)}</div>
          <div class="rule-logic">${esc(row.logic)}</div>
          <div class="rule-where">${esc(row.where)}</div>
        </div>
      </div>`).join('')}
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-head"><h2>校验顺序</h2><span class="sub">越底层的错误越先报，避免误导性提示</span></div>
    <div class="card-body">
      <div class="pipeline">
        ${data.order.map((code, index) => `${index ? '<span class="pipe-arrow">→</span>' : ''}
          <div class="pipe-node ${code === 'OK' ? 'legal' : ''}"><span>${esc(code)}</span></div>`).join('')}
      </div>
      <p class="small muted" style="margin:12px 0 0">
        先报 <code>E_NOT_FOUND</code> 是因为"退 999 件"如果实际连明细都找不到，
        回一句 <code>E_DUPLICATE</code> 只会让人更糊涂。
      </p>
    </div>
  </div>

  <div class="grid cols-2" style="margin-top:16px">
    <div class="card">
      <div class="card-head"><h2>错误码</h2></div>
      <div class="card-body tight">
        <div class="table-wrap"><table class="table">
          <thead><tr><th>错误码</th><th>含义</th><th>提示</th></tr></thead>
          <tbody>${data.codes.map((item) => `<tr>
            <td class="mono">${esc(item.code)}</td>
            <td>${esc(item.meaning)}</td>
            <td class="small muted">${esc(item.hint)}</td>
          </tr>`).join('')}</tbody>
        </table></div>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h2>状态流转表</h2><span class="sub">R-09，写死在 rules.TRANSITIONS</span></div>
      <div class="card-body tight">
        <div class="table-wrap"><table class="table">
          <thead><tr><th>当前状态</th><th>中文</th><th>能去哪</th></tr></thead>
          <tbody>${transitionTable}</tbody>
        </table></div>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-head"><h2>额度语义</h2></div>
    <div class="card-body">
      <div class="grid cols-3">
        <div><div class="small muted">占额度</div>
          <div style="margin-top:6px">${['APPLIED', 'ACCEPTED', 'RECEIVED'].map((s) => `<span class="badge info">${esc(labels[s])}</span>`).join(' ')}</div>
          <div class="small muted" style="margin-top:6px">这三态期间，同一条明细的额度被这条申请拿走</div>
        </div>
        <div><div class="small muted">释放额度</div>
          <div style="margin-top:6px">${['CLOSED', 'REJECTED'].map((s) => `<span class="badge ok">${esc(labels[s])}</span>`).join(' ')}</div>
          <div class="small muted" style="margin-top:6px">结案或拒绝后额度立刻回来，同一明细可以再申请</div>
        </div>
        <div><div class="small muted">不占额度</div>
          <div style="margin-top:6px">${['PENDING_MANUAL'].map((s) => `<span class="badge warn">${esc(labels[s])}</span>`).join(' ')}</div>
          <div class="small muted" style="margin-top:6px">转人工的单子不占额度；人工放行前会再核一次额度，防超卖</div>
        </div>
      </div>
    </div>
  </div>`;
}

/* ---------------------------------------------------------------- 证据 */

async function viewEvidence() {
  const data = await apiGet('/api/evidence');
  if (!data.found) {
    return `<h1 class="page-title">证据与自检</h1>
    <div class="card"><div class="empty">
      <strong>找不到 data/build/import_invariants.txt</strong>
      它是导入时由程序写出的。先在概览页导一次数据，或者回到项目根跑
      <code>python tools/make_evidence.py</code>。
    </div></div>`;
  }
  const prov = data.provenance;
  const items = data.items || [];
  const matched = items.filter((item) => item.match === true).length;
  const mismatched = items.filter((item) => item.match === false).length;

  return `<h1 class="page-title">证据与自检</h1>
  <p class="page-lede">这一页读的是 <code>data/build/import_invariants.txt</code> ——
  导入时由程序自己写出、没有手改的文件。所以不是"我说它跑了一百万行"，是程序当时数的账。</p>

  <div class="grid cols-4">
    <div class="stat">
      <div class="stat-label">读取行数</div>
      <div class="stat-value">${fmt(data.rows)}</div>
      <div class="stat-hint">导入时间 ${esc(data.importedAt)}</div>
    </div>
    <div class="stat">
      <div class="stat-label">13 项不变量</div>
      <div class="stat-value">${matched}/${items.length}</div>
      <div class="stat-hint">${mismatched ? `<span style="color:var(--danger)">${mismatched} 项不符</span>` : '全部与基线吻合'}</div>
    </div>
    <div class="stat">
      <div class="stat-label">切片模式</div>
      <div class="stat-value" style="font-size:19px">${esc(data.slice || '—')}</div>
      <div class="stat-hint">切片不与全量基线比较</div>
    </div>
    <div class="stat">
      <div class="stat-label">去重口径自洽</div>
      <div class="stat-value" style="font-size:19px">${data.provenanceOk ? '通过' : '不通过'}</div>
      <div class="stat-hint">${fmt(prov['去重前行数'])} − ${fmt(prov['去重丢弃行数'])} = ${fmt(prov['去重后行数'])}</div>
    </div>
  </div>

  <div class="card" style="margin-top:16px">
    <div class="card-head">
      <h2>13 项不变量逐项对照</h2>
      <span class="sub">✓ = 与冻结基线 BASELINE 一致</span>
    </div>
    <div class="card-body tight">
      <div class="table-wrap"><table class="table">
        <thead><tr><th>不变量</th><th class="right">实测</th><th class="right">基线</th><th class="center">结果</th></tr></thead>
        <tbody>${items.map((item) => `<tr>
          <td class="mono">${esc(item.name)}</td>
          <td class="num">${fmt(item.value)}</td>
          <td class="num muted">${fmt(item.expected)}</td>
          <td class="center">${item.match === true
            ? '<span class="badge ok no-dot">✓ 吻合</span>'
            : item.match === false
              ? '<span class="badge danger no-dot">✗ 不符</span>'
              : '<span class="muted">—</span>'}</td>
        </tr>`).join('')}</tbody>
      </table></div>
    </div>
  </div>

  <div class="grid cols-2" style="margin-top:16px">
    <div class="card">
      <div class="card-head"><h2>文件来源</h2></div>
      <div class="card-body">
        <dl class="kv">
          <dt>数据源</dt><dd class="mono small">${esc(data.source || '—')}</dd>
          <dt>目标库</dt><dd class="mono small">${esc(data.target || '—')}</dd>
          <dt>导入时间</dt><dd class="mono small">${esc(data.importedAt || '—')}</dd>
          <dt>✓ 标记</dt><dd class="mono">${fmt(data.ticks)}</dd>
          <dt>✗ 标记</dt><dd class="mono">${fmt(data.crosses)}</dd>
        </dl>
        <div class="notice info" style="margin-top:14px"><span class="ico">i</span><div>
          这份文件只能由 <code>tools/make_evidence.py</code> 重新生成，不许手改。
          数字对不上就查代码，不改数字。
        </div></div>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h2>拒绝计数</h2><span class="sub">reject_stat.txt</span></div>
      <div class="card-body">
        <pre class="cli" style="border-radius:8px">${esc(data.rejectStat || '（暂无拒绝记录）')}</pre>
      </div>
    </div>
  </div>`;
}

/* ---------------------------------------------------------------- 路由与渲染 */

const RENDERERS = {
  overview: viewOverview,
  invoice: viewInvoice,
  requests: viewRequests,
  demo: viewDemo,
  rules: viewRules,
  evidence: viewEvidence,
};

function currentRoute() {
  const hash = location.hash.replace(/^#\/?/, '');
  const name = (hash.split('/')[0] || 'overview').trim();
  return RENDERERS[name] ? name : 'overview';
}

function parseHashPayload() {
  const hash = location.hash.replace(/^#\/?/, '');
  return hash.split('/').slice(1);
}

let renderSeq = 0;

async function render({ refresh = false } = {}) {
  const seq = ++renderSeq;
  const route = currentRoute();
  state.route = route;
  renderChrome();
  const view = $('#view');
  if (!view) return;

  try {
    if (route === 'invoice') {
      const args = parseHashPayload();
      if (args[0]) state.invoiceQuery = decodeURIComponent(args[0]);
      if (state.invoiceQuery && (refresh || !state.invoice || state.invoice.invoice !== state.invoiceQuery)) {
        try {
          state.invoice = await apiGet(`/api/invoice?q=${encodeURIComponent(state.invoiceQuery)}`);
        } catch { /* 留空，页面会显示未找到 */ }
      }
      if (seq !== renderSeq) return;
      view.innerHTML = await viewInvoice();
      bindInvoice();
      bindInvoiceSearch();
      return;
    }

    const html = await RENDERERS[route]();
    if (seq !== renderSeq) return;
    view.innerHTML = html;
    bindCommon();
    if (route === 'demo') bindDemo();
  } catch (err) {
    if (seq !== renderSeq) return;
    view.innerHTML = `<div class="card"><div class="empty">
      <strong>页面渲染失败</strong>${esc(err.message)}
      <div style="margin-top:12px"><button class="btn" onclick="location.reload()">重新加载</button></div>
    </div></div>`;
  }
}

function bindInvoiceSearch() {
  const input = $('#invoice-input');
  const button = $('#btn-query');
  if (!input || !button) return;
  let timer = null;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => searchInvoices(input.value.trim()), 180);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      commitInvoiceSearch(input.value.trim());
    }
  });
  button.addEventListener('click', () => commitInvoiceSearch(input.value.trim()));
}

// 搜索建议是后来才渲染出来的，用事件委托，避免每次都要重新绑定
document.addEventListener('click', (event) => {
  const btn = event.target.closest('#suggest [data-invoice], #invoice-result [data-invoice]');
  if (!btn) return;
  event.preventDefault();
  commitInvoiceSearch(btn.dataset.invoice);
});

async function commitInvoiceSearch(invoice) {
  if (!invoice) { toast('先输入一个发票号'); return; }
  const next = `#/invoice/${encodeURIComponent(invoice)}`;
  if (location.hash === next) {
    // 同一个发票再查一次：hash 不变不会触发 hashchange，这里手动重画
    state.invoiceQuery = invoice;
    state.invoice = null;
    await render({ refresh: true });
    return;
  }
  location.hash = next; // 变了就交给 hashchange 去渲染，避免重复画两次
}

function bindCommon() {
  $$('[data-req]').forEach((row) => {
    row.addEventListener('click', (event) => {
      if (event.target.closest('[data-apply]')) return;
      openRequestDrawer(row.dataset.req);
    });
  });
  $$('[data-status]').forEach((chip) => {
    chip.addEventListener('click', async () => {
      state.statusFilter = chip.dataset.status;
      await render({ refresh: true });
    });
  });
}

async function loadMeta() {
  try {
    state.meta = await apiGet('/api/meta');
  } catch (err) {
    state.meta = null;
    const chip = $('#db-chip');
    if (chip) chip.innerHTML = `<b style="color:var(--danger)">连不上服务</b><code>${esc(err.message)}</code>`;
  }
  return state.meta;
}

window.addEventListener('hashchange', () => render({ refresh: true }));

(async function boot() {
  await loadMeta();
  await render({ refresh: true });
})();

// 供 inline onclick 使用（空库页的两个按钮）
window.importData = importData;
