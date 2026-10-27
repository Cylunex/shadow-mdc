/**
 * views.js — 「查看」面板：按站点展开视频列表（对齐 LumenBox 的 Browse 页）+ tool\ 工具目录。
 *
 * 布局：
 *   · 桌面：左侧站点抽屉（分组 + 搜索 + 状态点，可折叠/搜索框过滤） + 右侧内容区
 *   · 手机：站点抽屉默认收起，用工具栏「☰ 站点」按钮拉出
 *   · 内容区顶部是当前站点标题 + 分类 chip 条 + 布局/抓封面开关
 *   · 卡片：封面 + 时长 + 播放浮层；悬停出「列表 / 源站」；点「详情」按需拉直链与画质
 *   · 触底自动续页；点卡片交给 app.js 的播放路径（同一套解析/预热/熔断/历史）
 */
(function () {
  const BP = (document.querySelector('base')?.getAttribute('href') || '/').replace(/\/$/, '') || '';
  const $ = (s) => document.querySelector(s);

  const panel = $('#viewPanel');
  const titleEl = $('#viewTitle');
  const subEl = $('#viewSub');
  const bodyEl = $('#viewBody');
  const wrapEl = $('#viewBodyWrap');
  const statusEl = $('#viewStatus');
  const moreBtn = $('#viewMore');
  const reloadBtn = $('#viewReload');
  const closeBtn = $('#viewClose');
  const currentEl = $('#viewCurrentSite');
  const siteSearch = $('#viewSiteSearch');
  const sidePanel = $('#viewSiteSidebar');
  const sideSearch = $('#viewSideSearch');
  const sideClose = $('#viewSideClose');
  const sideList = $('#viewSiteList');
  const sideBtn = $('#viewSidebarBtn');
  const sessBtn = $('#viewSessionBtn');
  const sessPanel = $('#viewSessionPanel');
  const sessClose = $('#viewSessionClose');
  const sessHost = $('#viewSessHost');
  const sessCookie = $('#viewSessCookie');
  const sessStorage = $('#viewSessStorage');
  const sessSave = $('#viewSessSave');
  const sessClear = $('#viewSessClear');
  const sessStatus = $('#viewSessStatus');
  const sessList = $('#viewSessList');
  const sessUserscript = $('#viewSessUserscript');
  const sessPaste = $('#viewSessPaste');
  const enrichBox = $('#viewEnrich');
  const modeNote = $('#viewModeNote');
  const catChips = $('#viewCatChips');
  const toolsFilter = $('#viewToolsFilter');
  const toolSearch = $('#viewToolSearch');
  const kindsEl = $('#viewKinds');
  const tabsEl = $('#viewTabs');
  const toolbarEl = $('#viewToolbar');

  if (!panel) return;

  const state = {
    tab: 'browse',
    site: null,
    category: 'all',
    cursor: null,
    items: [],
    loading: false,
    loadingKey: null,
    browsing: false,
    browseMeta: null,
    categoriesBySite: {},
    categories: [],
    layout: 'grid',
    toolSites: null,
    tools: null,
    toolsLoading: false,
    toolKind: 'all',
    toolQuery: '',
    showSupport: false,
    toolId: null,
    siteFilter: '',
    sideOpen: false,
    streams: new Map(), // "site:id" -> stream | 'loading' | {error}
  };

  let playHandler = null;
  try { state.layout = localStorage.getItem('swipefeed:browse-layout') || 'grid'; } catch (_) {}
  // 触屏/无 hover 设备：卡片操作常驻显示
  try {
    const mq = window.matchMedia;
    if (mq && (!mq('(hover: hover) and (pointer: fine)').matches || mq('(max-width: 760px)').matches)) {
      panel.classList.add('touch');
    }
  } catch (_) {}

  async function api(path, opts) {
    const r = await fetch(BP + path, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }

  const esc = (s) => String(s == null ? '' : s);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  /** 封面走本地 /api/img 代理：防盗链站点直接热链会被浏览器拦掉。 */
  function coverUrl(card) {
    if (!card.cover) return null;
    // 带上详情页地址当 Referer，防盗链站点（如海角/3GO 的 CDN）才给图
    const ref = card.pageUrl && /^https?:\/\//i.test(card.pageUrl) ? `&r=${encodeURIComponent(card.pageUrl)}` : '';
    return `${BP}/api/img?u=${encodeURIComponent(card.cover)}${ref}`;
  }

  function siteTitle(id) {
    const s = (state.browseMeta?.sites || []).find((x) => x.id === id || x.sourceId === id);
    if (s) return s.name;
    const src = (state.browseMeta?.serverSources || []).find((x) => x.id === id);
    if (src) return src.displayName || src.name;
    const other = (state.browseMeta?.otherSources || []).find((x) => x.id === id);
    return other ? (other.displayName || other.name) : id;
  }

  function siteEntry(id) {
    return (state.browseMeta?.sites || []).find((s) => s.id === id || s.sourceId === id) || null;
  }

  /* ─────────────── 会话面板（cookie / localStorage 导入） ─────────────── */

  let sessLoaded = false;

  function setSessionPanel(open) {
    if (!sessPanel) return;
    sessPanel.classList.toggle('hidden', !open);
    if (sessBtn) sessBtn.classList.toggle('active', open);
    if (open) loadSessions();
  }

  async function loadSessions() {
    if (!sessList) return;
    sessStatus.textContent = '读取中…';
    try {
      const data = await api('/api/session');
      const rows = data.sessions || [];
      sessList.replaceChildren();
      if (!rows.length) {
        sessList.appendChild(el('div', 'view-empty', '还没有导入任何站点会话'));
      }
      for (const s of rows) {
        const row = el('div', 'view-sess-item' + (s.stale ? ' stale' : ''));
        const info = el('div', 'view-sess-info');
        info.appendChild(el('div', 'view-sess-host', s.host + (s.stale ? ' · 已过期' : '')));
        const bits = [
          s.hasCookie ? `cookie ${s.cookieLen}B` : null,
          s.storageKeys?.length ? `storage ${s.storageKeys.join('/')}` : null,
          s.updatedAt ? s.updatedAt.replace('T', ' ').slice(0, 19) : null,
        ].filter(Boolean);
        info.appendChild(el('div', 'view-sess-meta', bits.join(' · ')));
        row.appendChild(info);
        const del = el('button', 'view-mini', '删除');
        del.type = 'button';
        del.onclick = async () => {
          try {
            await api('/api/session/' + encodeURIComponent(s.host), { method: 'DELETE' });
            loadSessions();
          } catch (e) {
            sessStatus.textContent = '删除失败：' + e.message;
          }
        };
        row.appendChild(del);
        sessList.appendChild(row);
      }
      sessStatus.textContent = rows.length ? `已导入 ${rows.length} 个站点` : '未导入';
    } catch (e) {
      sessStatus.textContent = '读取失败：' + e.message;
    }
  }

  async function saveSessionFromForm() {
    const host = (sessHost?.value || '').trim();
    if (!host) { sessStatus.textContent = '先填域名'; return; }
    const storage = (sessStorage?.value || '')
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const at = line.indexOf('=');
        return at > 0 ? { key: line.slice(0, at).trim(), value: line.slice(at + 1).trim() } : null;
      })
      .filter(Boolean);
    sessStatus.textContent = '保存中…';
    try {
      const r = await api('/api/session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          host,
          cookie: sessCookie?.value || '',
          storage,
          note: '查看页手动导入',
          source: 'manual',
        }),
      });
      sessStatus.textContent = `已保存 ${r.host}（cookie ${r.cookieLen}B，storage ${r.storageKeys.length} 项）`;
      if (sessCookie) sessCookie.value = '';
      if (sessStorage) sessStorage.value = '';
      loadSessions();
    } catch (e) {
      sessStatus.textContent = '保存失败：' + e.message;
    }
  }

  /* ─────────────── 站点抽屉 ─────────────── */

  const SITE_GROUPS = [
    { key: 'direct', label: '可直接查看', hint: '服务端取数，点开即出列表' },
    { key: 'browser', label: '需浏览器会话', hint: '只做索引：入口 + 脚本' },
    { key: 'other', label: '其它站内源', hint: '' },
  ];

  function siteRows() {
    const meta = state.browseMeta || {};
    const sites = meta.sites || [];
    const others = (meta.otherSources || []).filter(
      (s) => !sites.some((m) => m.sourceId === s.id || m.id === s.id),
    );
    return {
      direct: sites.filter((s) => s.mode === 'server').map((s) => ({
        id: s.sourceId || s.id,
        name: s.name,
        host: s.host,
        live: true,
        source: s,
      })),
      browser: sites.filter((s) => s.mode !== 'server').map((s) => ({
        id: s.id,
        name: s.name,
        host: s.host,
        live: false,
        source: s,
      })),
      other: others.map((s) => ({
        id: s.id,
        name: s.displayName || s.name,
        host: '',
        live: true,
        source: null,
      })),
    };
  }

  function renderSiteList() {
    if (!sideList) return;
    const q = (state.siteFilter || '').trim().toLowerCase();
    const groups = siteRows();
    sideList.replaceChildren();

    const hit = (row) => {
      if (!q) return true;
      const src = row.source || {};
      const hay = [row.id, row.name, row.host, (src.tags || []).join(' '), src.notes || '']
        .join(' ').toLowerCase();
      return hay.includes(q);
    };

    let shown = 0;
    for (const g of SITE_GROUPS) {
      const rows = (groups[g.key] || []).filter(hit);
      if (!rows.length) continue;
      const head = el('div', 'view-side-group');
      head.appendChild(el('span', null, g.label));
      head.appendChild(el('span', 'view-side-count', String(rows.length)));
      sideList.appendChild(head);
      for (const row of rows) {
        const b = el('button', 'view-side-item' + (row.id === state.site ? ' active' : '') + (row.live ? '' : ' muted'));
        b.type = 'button';
        const dot = el('span', 'view-side-dot ' + (row.live ? 'ok' : 'warn'));
        const txt = el('span', 'view-side-text');
        txt.appendChild(el('span', 'view-side-name', row.name));
        if (row.host) txt.appendChild(el('span', 'view-side-host', row.host));
        b.appendChild(dot);
        b.appendChild(txt);
        if (!row.live) b.appendChild(el('span', 'view-side-tag', '脚本'));
        b.onclick = () => selectSite(row.id);
        sideList.appendChild(b);
        shown++;
      }
    }

    const mixedRow = el('button', 'view-side-item' + (state.site === 'mixed' ? ' active' : ''));
    mixedRow.type = 'button';
    mixedRow.appendChild(el('span', 'view-side-dot ok'));
    const mt = el('span', 'view-side-text');
    mt.appendChild(el('span', 'view-side-name', '随机混排'));
    mt.appendChild(el('span', 'view-side-host', '多个源交错'));
    mixedRow.appendChild(mt);
    mixedRow.onclick = () => selectSite('mixed');
    sideList.appendChild(mixedRow);
    shown++;

    if (!shown) sideList.appendChild(el('div', 'view-empty', '没有匹配的站点'));
  }

  function setSidebar(open) {
    state.sideOpen = open;
    if (sidePanel) sidePanel.classList.toggle('open', open);
    if (wrapEl) wrapEl.classList.toggle('side-open', open);
    if (sideBtn) sideBtn.classList.toggle('active', open);
  }

  /* ─────────────── 分类 chip 条（动态解析） ─────────────── */

  const CAT_SOURCE_LABEL = {
    'plugin-api': '站点接口',
    'homepage-links': '站点页面',
    'slug-probe': '路径探测',
    none: '无',
    demo: '示例',
  };

  function renderCategoryChips(categories, meta) {
    if (!catChips) return;
    state.categories = categories || [];
    state.categoryMeta = meta || state.categoryMeta;
    catChips.replaceChildren();
    const list = state.categories;
    if (!list.length) {
      catChips.classList.add('hidden');
      return;
    }
    const urls = (meta && meta.urls) || state.categoryUrls || {};
    const mk = (id, label) => {
      const b = el('button', 'chip' + ((state.category || 'all') === id ? ' active' : ''), label);
      b.type = 'button';
      // 悬停显示这个分类实际会请求的地址，方便核对站点形状
      if (urls[id]) b.title = urls[id];
      b.onclick = () => {
        state.category = id;
        for (const x of catChips.querySelectorAll('.chip')) x.classList.toggle('active', x === b);
        loadBrowse();
      };
      catChips.appendChild(b);
    };
    // 第一段：来源说明（分类是动态解析出来的，这里标一下怎么来的）
    const m = state.categoryMeta || {};
    const src = el('span', 'view-cat-source',
      `分类 ${list.length} 个 · ${CAT_SOURCE_LABEL[m.source] || m.source || '—'}${m.pattern ? ' ' + m.pattern : ''}`);
    src.title = m.note || '';
    catChips.appendChild(src);
    // 站点接口本来就返回 all 时别再补一个「全部」
    if (!list.some((c) => c.id === 'all')) mk('all', '全部');
    for (const c of list) mk(c.id, c.id === 'all' ? (c.displayName || '全部') : (c.displayName || c.id));
    const redo = el('button', 'view-cat-redo', '↻ 重探');
    redo.type = 'button';
    redo.title = '重新按站点接口 / 页面链接 / 路径探测解析一遍分类';
    redo.onclick = async () => {
      const site = state.site;
      delete state.categoriesBySite[site];
      try {
        const data = await api('/api/browse/' + encodeURIComponent(site) + '?refresh=1');
        state.categoriesBySite[site] = {
          categories: data.categories || [],
          meta: { source: data.categorySource, pattern: data.categoryPattern, note: data.categoryNote },
        };
        if (state.site === site) renderCategoryChips(data.categories || [], state.categoriesBySite[site].meta);
        statusEl.textContent = `分类已重探：${(data.categories || []).length} 个（${CAT_SOURCE_LABEL[data.categorySource] || data.categorySource}）`;
      } catch (e) {
        statusEl.textContent = '重探失败：' + e.message;
      }
    };
    catChips.appendChild(redo);
    catChips.classList.remove('hidden');
    const active = catChips.querySelector('.chip.active');
    if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest', inline: 'center' });
  }

  async function refreshCategories(siteId) {
    const entry = siteEntry(siteId);
    const staticCats = entry?.categories?.map((c) => (typeof c === 'string' ? { id: c, displayName: c } : c)) || [];
    const cached = state.categoriesBySite[siteId];
    if (cached) {
      state.categoryUrls = cached.meta?.urls || {};
      renderCategoryChips(cached.categories, cached.meta);
      return;
    }
    renderCategoryChips(staticCats, { source: 'loading' });
    try {
      const data = await api('/api/browse/' + encodeURIComponent(siteId));
      const payload = {
        categories: data.categories || [],
        meta: {
          source: data.categorySource,
          pattern: data.categoryPattern,
          note: data.categoryNote,
          urls: data.categoryUrls || {},
        },
      };
      if (payload.categories.length || data.categorySource === 'none') {
        state.categoriesBySite[siteId] = payload;
      }
      if (state.site === siteId) {
        state.categoryUrls = payload.meta.urls;
        renderCategoryChips(payload.categories, payload.meta);
      }
    } catch { /* 保留静态分类 */ }
  }

  function updateModeNote() {
    if (state.tab !== 'browse') { modeNote.classList.add('hidden'); return; }
    const site = siteEntry(state.site);
    if (currentEl) currentEl.textContent = siteTitle(state.site);
    if (!site) { modeNote.classList.add('hidden'); return; }
    const parts = [site.mode === 'server' ? '服务端直连' : '需浏览器会话'];
    if (site.host) parts.push(site.host);
    if (site.tags?.length) parts.push(site.tags.join(' · '));
    if (site.notes) parts.push(site.notes);
    if (site.mode !== 'server') parts.push('列表不在本站取数；点卡片进站点，脚本见「工具目录」');
    modeNote.textContent = parts.join('｜');
    modeNote.classList.remove('hidden');
  }

  /* ─────────────── 卡片 ─────────────── */

  function cardMetaChildren(card, big) {
    const meta = el('div', big ? 'view-list-meta' : 'view-card-meta');
    meta.appendChild(el('div', big ? 'view-list-title' : 'view-card-title', esc(card.title || card.id)));
    const bits = [siteTitle(card.site), card.author, card.views ? card.views + ' 次' : null,
      card.videoType, ...(card.tags || []).slice(0, 3)].filter(Boolean);
    meta.appendChild(el('div', big ? 'view-list-sub' : 'view-card-sub', bits.join(' · ')));
    return meta;
  }

  function cardCover(card, cls) {
    const cov = el('div', cls);
    const cvUrl = coverUrl(card);
    // 站点名占位：死图/无图时不再整块纯黑
    cov.dataset.site = siteTitle(card.site);
    if (cvUrl) {
      // 用 Image 预探：成功才铺背景（避免黑块闪），失败就留占位
      const probe = new Image();
      probe.decoding = 'async';
      probe.onload = () => {
        cov.style.backgroundImage = `url("${cvUrl}")`;
        cov.classList.add('loaded');
      };
      probe.onerror = () => cov.classList.add('cover-fallback');
      probe.src = cvUrl;
    } else {
      cov.classList.add('cover-fallback');
    }
    if (card.duration) {
      cov.appendChild(el('span', 'view-card-dur', card.duration));
    }
    return cov;
  }

  function cardTile(card) {
    const b = el('button', 'view-card');
    b.type = 'button';
    b.appendChild(cardCover(card, 'view-card-cover'));
    b.appendChild(cardMetaChildren(card, false));
    decorateCard(b, card);
    return b;
  }

  function cardRow(card) {
    const b = el('button', 'view-row');
    b.type = 'button';
    b.appendChild(cardCover(card, 'view-row-cover'));
    b.appendChild(cardMetaChildren(card, true));
    decorateCard(b, card);
    return b;
  }

  function decorateCard(node, card) {
    node.title = '点击播放：' + (card.title || card.id);
    node.onclick = () => {
      if (!playHandler) return;
      // 第四个参数带上续页信息，播放页滚到底还能继续拉同一站点的下一页
      playHandler(card, state.items, state.items.indexOf(card), {
        nextCursor: state.cursor,
        category: state.category && state.category !== 'all' ? state.category : null,
        site: state.site,
      });
    };

    // 悬停浮层：详情（直链）/ 下载（有服务端下载能力的源）/ 列表 / 源站
    const actions = el('div', 'view-card-actions');
    const detail = el('button', 'view-mini', '详情');
    detail.type = 'button';
    detail.onclick = (e) => { e.stopPropagation(); openDetail(card); };
    actions.appendChild(detail);
    const dl = downloadLink(card);
    if (dl) actions.appendChild(dl);
    const listBtn = el('button', 'view-mini', '列表');
    listBtn.type = 'button';
    listBtn.onclick = (e) => { e.stopPropagation(); toggleLayoutItem(card); };
    actions.appendChild(listBtn);
    if (card.pageUrl) {
      const a = el('a', 'view-mini', '源站');
      a.href = card.pageUrl;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.onclick = (e) => e.stopPropagation();
      actions.appendChild(a);
    }
    node.appendChild(actions);

    if (card.site) node.appendChild(el('span', 'view-card-badge', card.site));
    node.appendChild(el('span', 'view-play-hint', '▶'));
  }

  /** 支持服务端整片下载的源（tool-sites 里带 download 配置）→ 卡片上加「下载」。 */
  function downloadLink(card) {
    const entry = siteEntry(card.site);
    const dl = entry?.download;
    if (!dl) return null;
    const q = dl.quality || '720p';
    const a = el('a', 'view-mini', '下载');
    a.href = `${BP}/api/download?site=${encodeURIComponent(card.site)}&id=${encodeURIComponent(card.id)}&quality=${encodeURIComponent(q)}`;
    a.download = '';
    a.title = `整片下载（${q}，AES 分片由服务端解密合并）`;
    a.onclick = (e) => e.stopPropagation();
    return a;
  }

  /** 卡片「列表」按钮：把该卡片切到列表布局（记住选择）。 */
  function toggleLayoutItem() {
    state.layout = state.layout === 'grid' ? 'list' : 'grid';
    try { localStorage.setItem('swipefeed:browse-layout', state.layout); } catch (_) {}
    syncLayoutButtons();
    renderBrowse();
  }

  /** 「详情」：按需解析直链/画质，直接展开在卡片上（可复制）。 */
  async function openDetail(card) {
    const key = `${card.site}:${card.id}`;
    const inline = bodyEl.querySelector(`[data-detail="${CSS.escape(key)}"]`);
    if (inline) { inline.remove(); return; }
    const row = el('div', 'view-detail-row');
    row.setAttribute('data-detail', key);
    row.textContent = '解析中…';
    const host = bodyEl.querySelector('.view-grid, .view-list');
    if (host) host.prepend(row);

    let stream = state.streams.get(key);
    if (!stream || stream === 'loading') {
      stream = 'loading';
      state.streams.set(key, stream);
      try {
        stream = await api('/api/resolve', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ site: card.site, id: card.id, pageUrl: card.pageUrl }),
        });
      } catch (e) {
        stream = { error: e.message };
      }
      state.streams.set(key, stream);
    }
    row.replaceChildren();
    if (stream?.error) {
      row.classList.add('error');
      row.appendChild(el('span', null, '解析失败：' + stream.error));
      return;
    }
    if (!stream?.url) {
      row.appendChild(el('span', null, '没有可用地址'));
      return;
    }
    const head = el('div', 'view-detail-head');
    head.appendChild(el('span', 'view-detail-url', stream.url));
    const copy = el('button', 'view-mini', '复制');
    copy.type = 'button';
    copy.onclick = async () => {
      try {
        await navigator.clipboard.writeText(stream.url);
        copy.textContent = '已复制';
        setTimeout(() => { copy.textContent = '复制'; }, 1200);
      } catch { copy.textContent = '复制失败'; }
    };
    head.appendChild(copy);
    const play = el('button', 'view-mini primary', '播放');
    play.type = 'button';
    play.onclick = () => {
      if (playHandler) {
        playHandler(card, state.items, state.items.indexOf(card), {
          nextCursor: state.cursor,
          category: state.category && state.category !== 'all' ? state.category : null,
          site: state.site,
        });
      }
    };
    head.appendChild(play);
    const close = el('button', 'view-mini', '收起');
    close.type = 'button';
    close.onclick = () => row.remove();
    head.appendChild(close);
    row.appendChild(head);
    const quals = (stream.qualities || []).slice(0, 6);
    if (quals.length > 1) {
      const q = el('div', 'view-detail-quals');
      for (const item of quals) {
        const tag = el('span', 'view-tag', `${item.label} · ${String(item.url).split('/').pop()?.slice(0, 24) || ''}`);
        q.appendChild(tag);
      }
      row.appendChild(q);
    }
    const metaBits = [stream.type, stream.preview ? '试看' : null, stream.expired ? '地址可能已过期' : null,
      stream.note].filter(Boolean);
    if (metaBits.length) row.appendChild(el('div', 'view-detail-meta', metaBits.join(' · ')));
  }

  /* ─────────────── 列表渲染 ─────────────── */

  function renderBrowse() {
    if (state.browsing) return;
    state.browsing = true;
    try {
      const scrollTop = bodyEl.scrollTop;
      bodyEl.replaceChildren();
      const entry = siteEntry(state.site);
      // 需浏览器会话的站点：没有服务端列表，直接给入口卡（原站 + 脚本 + 会话）
      if (entry && entry.mode !== 'server') {
        bodyEl.appendChild(browserSiteCard(entry));
        moreBtn.classList.add('hidden');
        if (currentEl) currentEl.textContent = entry.name;
        statusEl.textContent = `${entry.name} · 该站点需要浏览器会话，这里给入口与脚本`;
        return;
      }
      const wrap = el('div', state.layout === 'grid' ? 'view-grid' : 'view-list');
      for (const card of state.items) {
        wrap.appendChild(state.layout === 'grid' ? cardTile(card) : cardRow(card));
      }
      if (!state.items.length) {
        wrap.appendChild(el('div', 'view-empty',
          state.loading ? '加载中…' : '暂无内容（该源可能被拦、被关或为空）'));
      }
      bodyEl.appendChild(wrap);
      if (scrollTop) bodyEl.scrollTop = scrollTop;
      moreBtn.classList.toggle('hidden', !state.cursor);
      if (currentEl) currentEl.textContent = siteTitle(state.site);
      if (!state.loading) {
        statusEl.textContent = `${siteTitle(state.site)} · 已列出 ${state.items.length} 条${state.cursor ? '（继续下拉加载）' : ''}`;
      }
    } finally {
      state.browsing = false;
    }
  }

  /** 需浏览器会话的站点：入口 + 脚本 + 会话 三个动作，别让人对着空网格发呆。 */
  function browserSiteCard(entry) {
    const card = el('div', 'view-browser-card');
    card.appendChild(el('h3', null, entry.name));
    const host = entry.host || '';
    card.appendChild(el('div', 'view-browser-host', host + (entry.tags?.length ? ' · ' + entry.tags.join(' · ') : '')));
    if (entry.notes) card.appendChild(el('p', 'view-browser-note', entry.notes));

    const actions = el('div', 'view-browser-actions');
    const primaryHost = /^https?:\/\//.test(host) ? host
      : host.replace(/^\*\./, '');
    if (primaryHost && !/[（(]/.test(primaryHost)) {
      const open = el('a', 'chip active', '打开原站');
      open.href = /^https?:\/\//.test(primaryHost) ? primaryHost : `https://${primaryHost}`;
      open.target = '_blank';
      open.rel = 'noopener noreferrer';
      actions.appendChild(open);
    }
    for (const m of (entry.mirrors || []).slice(0, 3)) {
      const a = el('a', 'chip', m);
      a.href = /^https?:\/\//.test(m) ? m : `https://${m}`;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      actions.appendChild(a);
    }

    // 对应脚本：工具目录里按 toolHints 匹配，给出下载副本链接
    const tools = (state.tools?.items || []);
    const hints = entry.toolHints || [];
    const script = tools.find((t) => hints.some((h) => t.name.includes(h) || t.file.includes(h)));
    if (script) {
      const row = el('div', 'view-browser-script');
      row.appendChild(el('span', null, '对应脚本：' + script.name));
      if (script.downloadPath) {
        const dl = el('a', 'view-mini', '下载副本');
        dl.href = `${BP}/${String(script.downloadPath).replace(/^\//, '')}`;
        dl.target = '_blank';
        dl.rel = 'noopener noreferrer';
        row.appendChild(dl);
      }
      const gotoTool = el('button', 'view-mini', '看详情');
      gotoTool.type = 'button';
      gotoTool.onclick = () => { state.toolId = script.id; location.hash = '#/tools/' + encodeURIComponent(script.id); switchTab('tools'); };
      row.appendChild(gotoTool);
      card.appendChild(row);
    }

    const sessRow = el('div', 'view-browser-actions');
    const sess = el('button', 'chip', '导入会话');
    sess.type = 'button';
    sess.onclick = () => {
      setSessionPanel(true);
      if (sessHost) sessHost.value = primaryHost.replace(/^\*\./, '');
      if (sessStatus) sessStatus.textContent = `为 ${primaryHost} 粘贴 cookie / localStorage 后保存，服务端取源时会带上`;
    };
    sessRow.appendChild(sess);
    const us = el('button', 'chip', '采集脚本');
    us.type = 'button';
    us.onclick = () => window.open(`${BP}/api/session/userscript?origin=${encodeURIComponent(location.origin)}`, '_blank', 'noopener,noreferrer');
    sessRow.appendChild(us);
    card.appendChild(sessRow);

    if (!state.tools) loadTools();
    return card;
  }

  function browseKey(site) {
    return `${site}|${state.category || 'all'}|${enrichBox && enrichBox.checked ? 1 : 0}`;
  }

  async function loadBrowse({ append = false } = {}) {
    const site = state.site || 'hsex';
    // 需浏览器会话的站点没有服务端列表：直接渲染入口卡，不发 /api/feed
    const entry = siteEntry(site);
    if (entry && entry.mode !== 'server') {
      state.items = [];
      state.cursor = null;
      renderBrowse();
      return;
    }
    const key = browseKey(site);
    if (state.loading && !append && state.loadingKey === key) return;
    if (state.abort) { try { state.abort.abort(); } catch (_) {} }
    const ctrl = new AbortController();
    state.abort = ctrl;
    state.loading = true;
    state.loadingKey = key;
    let failed = false;
    if (!append) { state.items = []; state.cursor = null; }
    renderBrowse();
    const timer = setTimeout(() => { try { ctrl.abort(); } catch (_) {} }, 45000);
    try {
      const cat = state.category && state.category !== 'all' ? `&category=${encodeURIComponent(state.category)}` : '';
      const cur = state.cursor ? `&cursor=${encodeURIComponent(JSON.stringify(state.cursor))}` : '';
      const enr = enrichBox && enrichBox.checked ? '&enrich=1' : '';
      const data = await api(`/api/feed?mode=browse&site=${encodeURIComponent(site)}${cat}${cur}${enr}`, { signal: ctrl.signal });
      if (state.loadingKey !== key) return;
      const seen = new Set(state.items.map((c) => c.site + ':' + c.id));
      const fresh = [];
      for (const c of data.items || []) {
        const k = c.site + ':' + c.id;
        if (seen.has(k)) continue;
        seen.add(k);
        fresh.push(c);
      }
      state.items = state.items.concat(fresh);
      state.cursor = data.nextCursor || null;
      if (append && !fresh.length) state.cursor = null; // 站点忽略分页 → 同批重复，停止续页
    } catch (e) {
      failed = true;
      statusEl.textContent = e.name === 'AbortError' ? '已取消（切换了站点或超时）' : '失败：' + e.message;
    } finally {
      clearTimeout(timer);
      if (state.loadingKey === key) {
        state.loading = false;
        state.loadingKey = null;
        renderBrowse();
        if (!failed) {
          statusEl.textContent = `${siteTitle(site)} · 已列出 ${state.items.length} 条${state.cursor ? '（继续下拉加载）' : ''}`;
        }
      }
    }
  }

  /* ─────────────── 工具目录 ─────────────── */

  function renderKinds() {
    kindsEl.replaceChildren();
    const kinds = [
      ['all', '全部'],
      ['userscript', '油猴脚本'],
      ['text', '文本/规则'],
      ['archive', '压缩包'],
      ['binary', '样本'],
    ];
    for (const [id, label] of kinds) {
      const b = el('button', 'chip' + (state.toolKind === id ? ' active' : ''), label);
      b.type = 'button';
      b.onclick = () => { state.toolKind = id; renderKinds(); renderTools(); };
      kindsEl.appendChild(b);
    }
  }

  function toolMatches(t) {
    if (state.toolKind !== 'all' && t.kind !== state.toolKind) return false;
    const q = state.toolQuery.trim().toLowerCase();
    if (!q) return true;
    const hay = [t.name, t.id, t.file, t.description, (t.hosts || []).join(' '),
      (t.capabilities || []).map((c) => c.label).join(' '), (t.modules || []).join(' ')]
      .filter(Boolean).join(' ').toLowerCase();
    return hay.includes(q);
  }

  function renderToolDetail(tool) {
    bodyEl.replaceChildren();
    const wrap = el('div', 'view-detail');
    const back = el('button', 'chip', '← 返回工具列表');
    back.type = 'button';
    back.onclick = () => { state.toolId = null; location.hash = '#/tools'; renderTools(); };
    wrap.appendChild(back);
    wrap.appendChild(el('h3', null, tool.name));
    if (tool.copies && tool.copies.length) {
      wrap.appendChild(el('div', 'view-sub',
        `同名副本 ${tool.copies.length} 份：` + tool.copies.map((c) => String(c.path || '').split(/[\\/]/).slice(-2).join('/')).join('，')));
    }

    const rows = [
      ['文件', tool.file],
      ['类型', tool.kind],
      ['来源目录', tool.origin === 'tool-dir' ? 'tool\\（素材区）' : '解包工作区（.nas-tmp）'],
      ['体积', (tool.bytes / 1024).toFixed(1) + ' KB'],
      ['版本', tool.version || '—'],
      ['作者', tool.author || '—'],
      ['运行时机', tool.runAt || '—'],
      ['匹配域名', (tool.hosts || []).join(', ') || '（正则通配，见脚本头）'],
      ['能力', (tool.capabilities || []).map((c) => c.label + (c.mode === 'server' ? '(可服务端)' : '(需浏览器)')).join('、')],
      ['子模块', (tool.modules || []).join(' / ') || '—'],
      ['状态', tool.status],
    ];
    const dl = el('div', 'view-kv');
    for (const [k, v] of rows) {
      const r = el('div', 'view-kv-row');
      r.appendChild(el('span', 'view-kv-key', k));
      r.appendChild(el('span', 'view-kv-val', v));
      dl.appendChild(r);
    }
    wrap.appendChild(dl);

    if (tool.description) wrap.appendChild(el('p', 'view-detail-desc', tool.description));

    const actions = el('div', 'view-detail-actions');
    if (tool.downloadPath) {
      const a = el('a', 'chip', '下载脚本副本');
      a.href = `${BP}/${String(tool.downloadPath).replace(/^\//, '')}`;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      actions.appendChild(a);
    }
    actions.appendChild(el('code', null, tool.path || ''));
    wrap.appendChild(actions);

    const site = (state.toolSites?.sites || []).find((s) =>
      (s.toolHints || []).some((h) => tool.name.includes(h) || tool.file.includes(h)));
    if (site) {
      const link = el('button', 'chip active', '查看站点：' + site.name);
      link.type = 'button';
      link.onclick = () => openBrowseForSite(site);
      wrap.appendChild(link);
    }
    bodyEl.appendChild(wrap);
  }

  function renderTools() {
    bodyEl.replaceChildren();
    if (state.toolId) {
      const tool = (state.tools?.items || []).find((t) => t.id === state.toolId);
      if (tool) { renderToolDetail(tool); return; }
    }
    const all = state.tools?.items || [];
    const supportCount = all.filter((t) => t.support).length;
    const items = all.filter(toolMatches).filter((t) => state.showSupport || !t.support);

    // 附属数据文件（油猴的 options.json / _storage.json）默认折叠，给一个开关
    if (supportCount) {
      const bar = el('div', 'view-tools-bar');
      const btn = el('button', 'chip' + (state.showSupport ? ' active' : ''),
        `${state.showSupport ? '隐藏' : '显示'}附属文件 ${supportCount}`);
      btn.type = 'button';
      btn.onclick = () => { state.showSupport = !state.showSupport; renderTools(); };
      bar.appendChild(btn);
      bodyEl.appendChild(bar);
    }

    const grid = el('div', 'view-tools');
    for (const t of items) {
      const b = el('button', 'view-tool' + (t.id === state.toolId ? ' active' : ''));
      b.type = 'button';
      b.appendChild(el('div', 'view-tool-name', (t.copies && t.copies.length) ? `${t.name} +${t.copies.length}` : t.name));
      b.appendChild(el('div', 'view-tool-desc', t.description || t.status || ''));
      const tags = el('div', 'view-tool-tags');
      for (const c of (t.capabilities || []).slice(0, 4)) {
        tags.appendChild(el('span', 'view-tag ' + (c.mode === 'server' ? 'ok' : 'warn'), c.label));
      }
      b.appendChild(tags);
      b.appendChild(el('div', 'view-tool-hosts', (t.hosts || []).slice(0, 4).join(' · ')));
      b.onclick = () => {
        state.toolId = t.id;
        location.hash = '#/tools/' + encodeURIComponent(t.id);
        renderTools();
      };
      grid.appendChild(b);
    }
    if (!items.length) grid.appendChild(el('div', 'view-empty', '没有匹配的工具'));
    bodyEl.appendChild(grid);

    if (state.toolSites) {
      const sect = el('div', 'view-section');
      sect.appendChild(el('h4', null,
        `tool 站点对照（可直连 ${state.toolSites.summary?.server?.length || 0} 个 / 需浏览器 ${state.toolSites.summary?.browser?.length || 0} 个）`));
      const list = el('div', 'view-sites');
      const q = state.toolQuery.trim().toLowerCase();
      for (const s of state.toolSites.sites || []) {
        if (q) {
          const hay = [s.name, s.host, (s.tags || []).join(' '), s.notes || ''].join(' ').toLowerCase();
          if (!hay.includes(q)) continue;
        }
        const row = el('button', 'view-site-row' + (s.mode === 'server' ? ' ok' : ''));
        row.type = 'button';
        row.appendChild(el('div', 'view-site-name', s.name + (s.mode === 'server' ? ' · 可直连' : ' · 需浏览器')));
        row.appendChild(el('div', 'view-site-host', s.host + (s.tags?.length ? ' · ' + s.tags.join(' · ') : '')));
        row.onclick = () => openBrowseForSite(s);
        list.appendChild(row);
      }
      sect.appendChild(list);
      bodyEl.appendChild(sect);
    }
  }

  async function loadTools({ force = false } = {}) {
    if (state.toolsLoading) return;
    if (state.tools && !force) {
      if (state.tab === 'tools') renderTools();
      return;
    }
    state.toolsLoading = true;
    statusEl.textContent = '加载工具目录…';
    try {
      const [tools, sites] = await Promise.all([api('/api/tools'), api('/api/tools/sites')]);
      state.tools = tools;
      state.toolSites = sites;
      statusEl.textContent = `${tools.total} 个条目 · 扫描 ${String(tools.scannedAt).replace('T', ' ').slice(0, 19)}`;
      // 在浏览页后台加载工具（「需浏览器会话」卡片要引用脚本）时不要抢掉当前内容
      if (state.tab === 'tools') renderTools();
      else renderBrowse();
    } catch (e) {
      statusEl.textContent = '工具目录加载失败：' + e.message;
    } finally {
      state.toolsLoading = false;
    }
  }

  function openBrowseForSite(site) {
    const id = site.sourceId || site.id;
    if ((state.browseMeta?.sites || []).some((s) => (s.sourceId || s.id) === id) || id === 'mixed') {
      location.hash = '#/browse/' + id;
      selectSite(id);
    } else if (site.host) {
      // 没有服务端源的站点：直接开站点入口
      window.open(/^https?:/.test(site.host) ? site.host : `https://${site.host}`, '_blank', 'noopener,noreferrer');
    }
  }

  /* ─────────────── 布局切换（网格 / 列表） ─────────────── */

  function syncLayoutButtons() {
    const box = $('#viewLayout');
    if (!box) return;
    for (const b of box.querySelectorAll('[data-layout]')) {
      b.classList.toggle('active', b.getAttribute('data-layout') === state.layout);
    }
  }

  function initLayoutButtons() {
    const box = $('#viewLayout');
    if (!box) return;
    for (const b of box.querySelectorAll('[data-layout]')) {
      b.onclick = () => {
        state.layout = b.getAttribute('data-layout');
        try { localStorage.setItem('swipefeed:browse-layout', state.layout); } catch (_) {}
        syncLayoutButtons();
        renderBrowse();
      };
    }
    syncLayoutButtons();
  }

  /* ─────────────── 面板开关 / 路由 ─────────────── */

  function selectSite(id) {
    state.site = id;
    state.category = 'all';
    state.cursor = null;
    state.items = [];
    state.streams.delete(`__${id}`); // 只清站点级占位（卡片详情缓存保留）
    if (currentEl) currentEl.textContent = siteTitle(id);
    if (enrichBox) enrichBox.checked = ['pektino', 'haijiao'].includes(id);
    renderSiteList();
    updateModeNote();
    renderCategoryChips([]);
    refreshCategories(id);
    loadBrowse();
  }

  function switchTab(tab) {
    state.tab = tab;
    for (const b of tabsEl.querySelectorAll('[data-view-tab]')) {
      b.classList.toggle('active', b.getAttribute('data-view-tab') === tab);
    }
    const isBrowse = tab === 'browse';
    toolbarEl.classList.toggle('hidden', !isBrowse);
    toolsFilter.classList.toggle('hidden', isBrowse);
    moreBtn.classList.toggle('hidden', !isBrowse || !state.cursor);
    if (sideBtn) sideBtn.classList.toggle('hidden', !isBrowse);
    if (!isBrowse) setSidebar(false);
    syncLayoutButtons();
    if (isBrowse) {
      titleEl.textContent = '站点查看';
      subEl.textContent = '左侧选站点 · 右侧列表 · 点卡片即播';
      if (currentEl) currentEl.textContent = siteTitle(state.site);
      updateModeNote();
      renderCategoryChips(state.categories || [], state.categoryMeta);
      renderBrowse();
    } else {
      titleEl.textContent = '工具目录';
      subEl.textContent = 'tool\\ 素材区整合总览';
      modeNote.classList.add('hidden');
      if (catChips) catChips.classList.add('hidden');
      loadTools();
    }
  }

  async function initBrowseMeta() {
    const meta = await api('/api/browse');
    try {
      const all = await api('/api/sites');
      meta.otherSources = all.sites || [];
      meta.categories = all.categories || meta.categories;
    } catch { meta.otherSources = []; }
    state.browseMeta = meta;
    renderSiteList();
  }

  async function openBrowse(siteId) {
    panel.classList.remove('hidden');
    panel.setAttribute('aria-hidden', 'false');
    if (!state.browseMeta) { try { await initBrowseMeta(); } catch { /* 离线时只报状态 */ } }
    if (siteId) state.site = siteId;
    if (!state.site) state.site = 'hsex';
    if (currentEl) currentEl.textContent = siteTitle(state.site);
    if (enrichBox) enrichBox.checked = ['pektino', 'haijiao'].includes(state.site);
    renderSiteList();
    switchTab('browse');
    updateModeNote();
    refreshCategories(state.site);
    await loadBrowse();
  }

  function closePanel() {
    panel.classList.add('hidden');
    panel.setAttribute('aria-hidden', 'true');
    setSidebar(false);
    if (location.hash.startsWith('#/')) history.replaceState(null, '', location.pathname + location.search);
  }

  /** hash 路由：#/browse/<site>、#/tools、#/tools/<toolId> */
  function applyRoute() {
    const h = location.hash || '';
    if (!h.startsWith('#/')) return false;
    const parts = h.slice(2).split('/').map(decodeURIComponent);
    if (parts[0] === 'browse') {
      openBrowse(parts[1] || state.site || 'hsex');
      return true;
    }
    if (parts[0] === 'tools') {
      state.toolId = parts[1] || null;
      panel.classList.remove('hidden');
      panel.setAttribute('aria-hidden', 'false');
      if (!state.browseMeta) initBrowseMeta().catch(() => {});
      switchTab('tools');
      loadTools();
      return true;
    }
    return false;
  }

  /* ─────────────── 事件 ─────────────── */

  for (const b of tabsEl.querySelectorAll('[data-view-tab]')) {
    b.onclick = () => {
      const tab = b.getAttribute('data-view-tab');
      if (tab === 'tools') { state.toolId = null; location.hash = '#/tools'; }
      else location.hash = '#/browse/' + (state.site || 'hsex');
      switchTab(tab);
    };
  }

  if (sideBtn) sideBtn.onclick = () => setSidebar(!state.sideOpen);
  if (sideClose) sideClose.onclick = () => setSidebar(false);
  if (sessBtn) sessBtn.onclick = () => setSessionPanel(sessPanel?.classList.contains('hidden'));
  if (sessClose) sessClose.onclick = () => setSessionPanel(false);
  if (sessSave) sessSave.onclick = () => saveSessionFromForm();
  if (sessPanel) {
    // 面板里的写入（含配套脚本 POST 进来的）刷新一次列表
    sessPanel.addEventListener('submit', () => loadSessions());
  }
  if (sessClear) {
    sessClear.onclick = async () => {
      try {
        await api('/api/session/all', { method: 'DELETE' });
        sessStatus.textContent = '已清空';
        loadSessions();
      } catch (e) {
        sessStatus.textContent = '清空失败：' + e.message;
      }
    };
  }
  if (sessPaste) {
    sessPaste.onclick = () => {
      const entry = siteEntry(state.site);
      const host = entry?.host && !/[/*（]/.test(entry.host)
        ? entry.host.replace(/^\*\./, '')
        : (entry?.mirrors?.[0] || '');
      if (sessHost) sessHost.value = host || '';
      sessStatus.textContent = host ? `已填 ${host}` : '这个站点没有固定域名，手动填吧';
    };
  }
  if (sessUserscript) {
    sessUserscript.onclick = () => {
      const url = `${BP}/api/session/userscript?origin=${encodeURIComponent(location.origin)}`;
      window.open(url, '_blank', 'noopener,noreferrer');
      sessStatus.textContent = '脚本已打开：另存为 .user.js 后拖进 Tampermonkey';
    };
  }
  if (sideSearch) {
    sideSearch.addEventListener('input', () => {
      state.siteFilter = sideSearch.value;
      renderSiteList();
    });
  }
  if (siteSearch) {
    // 工具栏搜索框：以站点名开头匹配，回车/输入即过滤抽屉并自动展开
    siteSearch.addEventListener('input', () => {
      state.siteFilter = siteSearch.value;
      if (siteSearch.value) setSidebar(true);
      renderSiteList();
    });
    siteSearch.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      const first = sideList?.querySelector('.view-side-item');
      if (first) first.click();
    });
  }

  if (enrichBox) enrichBox.onchange = () => loadBrowse();
  moreBtn.onclick = () => loadBrowse({ append: true });
  reloadBtn.onclick = () => {
    if (state.tab === 'tools') loadTools({ force: true });
    else loadBrowse();
  };
  closeBtn.onclick = () => closePanel();
  panel.addEventListener('click', (e) => { if (e.target === panel) closePanel(); });
  if (toolSearch) {
    toolSearch.addEventListener('input', () => {
      state.toolQuery = toolSearch.value;
      renderTools();
    });
  }

  // 主体滚动到底自动续页（对齐 LumenBox 的接近末尾 loadMore）
  bodyEl.addEventListener('scroll', () => {
    if (state.tab !== 'browse' || state.loading || !state.cursor) return;
    const remain = bodyEl.scrollHeight - bodyEl.scrollTop - bodyEl.clientHeight;
    if (remain < 480) loadBrowse({ append: true });
  });

  window.addEventListener('hashchange', () => { if (!applyRoute()) closePanel(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !panel.classList.contains('hidden')) {
      if (state.sideOpen) setSidebar(false);
      else closePanel();
    }
    if (e.key === 'g' && !panel.classList.contains('hidden') && state.tab === 'browse') {
      toggleLayoutItem();
    }
    if (e.key === '/' && !panel.classList.contains('hidden') && state.tab === 'browse') {
      const tag = (e.target && e.target.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA') return;
      e.preventDefault();
      siteSearch?.focus();
    }
  });

  window.Views = {
    open: (tab) => {
      if (tab === 'tools') { location.hash = '#/tools'; switchTab('tools'); loadTools(); return; }
      openBrowse(state.site);
    },
    openBrowse,
    close: closePanel,
    setPlayHandler: (fn) => { playHandler = fn; },
    isOpen: () => !panel.classList.contains('hidden'),
  };

  renderKinds();
  initLayoutButtons();
  // 宽屏默认展开站点抽屉。用「实际布局宽度」判断：手机 UA 首次布局可能是 980px，
  // 只靠 matchMedia 会误判成宽屏，抽屉直接把内容挡住。跨断点时自动收起/展开。
  const wideQuery = window.matchMedia('(min-width: 900px)');
  setSidebar(window.innerWidth >= 900 && wideQuery.matches);
  const onWideChange = (e) => setSidebar(e.matches);
  if (wideQuery.addEventListener) wideQuery.addEventListener('change', onWideChange);
  else if (wideQuery.addListener) wideQuery.addListener(onWideChange);
  applyRoute();
})();
