(() => {
  const BASE_PATH = (document.querySelector('base')?.getAttribute('href') || '/').replace(/\/$/, '') || '';
  const $ = (s) => document.querySelector(s);
  const player = $('#player');
  const coverEl = $('#cover');
  const titleEl = $('#title');
  const metaEl = $('#meta');
  const loading = $('#loading');
  const toast = $('#toast');
  const siteRow = $('#siteRow');
  const catRow = $('#catRow');
  const searchRow = $('#searchRow');
  const topbar = $('#topbar');
  const hud = $('#hud');
  const controls = $('#controls');
  const progressEl = $('#progress');
  const progressFill = $('#progressFill');
  const progressBuffered = $('#progressBuffered');
  const progressKnob = $('#progressKnob');
  const progressTime = $('#progressTime');
  const seekBufEl = $('#seekBuf');
  const copyStreamBtn = $('#copyStreamBtn');
  const hudActions = $('#hudActions');
  const warmPlayer = $('#warmPlayer');
  const favBtn = $('#favBtn');
  const muteBtn = $('#muteBtn');
  const pipBtn = $('#pipBtn');
  const ttffHint = $('#ttffHint');
  const historyBtn = $('#historyBtn');
  const viewBtn = $('#viewBtn');
  const historyPanel = $('#historyPanel');
  const historyList = $('#historyList');
  const historyClose = $('#historyClose');
  const historyClear = $('#historyClear');

  let mode = 'single';
  let site = 'demo';
  let category = 'asian';
  let browseCategory = null; // 从查看面板进来时的分类上下文
  let cursor = null;
  let items = [];
  let index = 0;
  let playGen = 0;
  let objectFitCover = false;
  let sites = [];
  let categories = [];
  let pagingPromise = null;
  let hlsInstance = null;
  let hlsLibPromise = null;
  let currentStreamUrl = null; // last resolved public playable URL for copy
  let currentPageUrl = null;
  let hlsFatalRecovered = false; // one auto-recover per item
  let pendingNativeHls = null; // 原生 HLS 播放中的地址，用于出错后回退 hls.js
  let scrollDir = 1; // 1=next / -1=prev — drives directional warm
  let warmHls = null;
  let warmKey = null; // site:id currently warmed
  let warmGen = 0;
  let lastTtffMs = null;
  let ttffStart = 0;
  let autoNextTimer = null;
  let reducedMotion = false;
  try {
    reducedMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  } catch (_) {}

  // resolve cache + concurrency
  const resolveCache = new Map(); // key -> { stream, expires, inflight?, abort? }
  const RESOLVE_TTL_MS = 15 * 60 * 1000;
  const MAX_PARALLEL_RESOLVE = 2;
  let resolveActive = 0;
  const resolveWaiters = [];

  // progress / scrub (rAF-debounced)
  let scrubbing = false;
  let scrubRatio = 0;
  let progressRaf = 0;
  let pendingProgress = null; // { ratio, current, duration } | 'sync'
  let seekToken = 0;
  let seekInFlight = false;
  let seekBufferTimer = null;
  let seekSafetyTimer = null;
  let wasPlayingBeforeScrub = false;
  let seekListeners = null; // { onSeeked, onCanPlay, onWaiting }

  // chrome auto-hide
  let chromeIdleTimer = null;
  const CHROME_IDLE_MS = 2800;

  // long-press 2x
  let longPressTimer = null;
  let speedBoost = false;
  const LONG_PRESS_MS = 300;
  const MOVE_CANCEL_PX = 12;

  player.setAttribute('playsinline', '');
  player.setAttribute('webkit-playsinline', '');
  player.playsInline = true;

  function siteDisplayName(siteId) {
    const s = sites.find((x) => x.id === siteId);
    return (s && (s.displayName || s.name)) || siteId || '';
  }

  function isHttpUrl(u) {
    return typeof u === 'string' && /^https?:\/\//i.test(u);
  }

  /** Prefer the public upstream URL our API already resolved; never forge new hosts. */
  function copyableStreamUrl(stream) {
    // 服务端会把播放地址换成本地代理（/api/hls、/api/stream、/api/png-hls），
    // 上游真实地址放在 directUrl 里，优先用它。
    if (isHttpUrl(stream?.directUrl)) return stream.directUrl;
    const directQuality = (stream?.qualities || []).find((q) => isHttpUrl(q?.directUrl));
    if (directQuality) return directQuality.directUrl;
    const picked = pickPlayableUrl(stream);
    if (!picked?.url) return null;
    let url = picked.url;
    // 本地代理把上游地址包在 ?u= 里（png-hls / hls / stream 三种都认）
    if (/\/api\/(?:png-hls|hls|stream)\?/i.test(url)) {
      try {
        const abs = url.startsWith('http') ? url : (location.origin + (url.startsWith('/') ? url : ('/' + url)));
        const u = new URL(abs);
        const upstream = u.searchParams.get('u');
        if (isHttpUrl(upstream)) return upstream;
      } catch (_) {}
    }
    if (isHttpUrl(url)) return url;
    // Relative same-origin playable (e.g. /swipefeed/api/png-hls without parseable u)
    if (url.startsWith('/')) return location.origin + url;
    return null;
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.cssText = 'position:fixed;left:-9999px;top:0';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
      } catch (_) {
        return false;
      }
    }
  }

  function updateSourceChrome(card) {
    currentPageUrl = (card && card.pageUrl && isHttpUrl(card.pageUrl)) ? card.pageUrl : null;
    if (hudActions) {
      hudActions.replaceChildren();
      if (currentPageUrl) {
        const a = document.createElement('a');
        a.className = 'source-link';
        a.href = currentPageUrl;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.textContent = '源站';
        a.addEventListener('click', (e) => e.stopPropagation());
        hudActions.appendChild(a);
      }
    }
  }

  function showToast(msg, ms = 1200) {
    toast.textContent = msg;
    toast.classList.add('show');
    clearTimeout(showToast._t);
    const hideMs = reducedMotion ? Math.min(ms, 600) : ms;
    showToast._t = setTimeout(() => toast.classList.remove('show'), hideMs);
  }

  function netProfile() {
    const c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    const saveData = !!(c && c.saveData);
    const et = (c && c.effectiveType) || '';
    const slow = /^(?:slow-)?2g$|^3g$/i.test(et);
    return { saveData, slow, effectiveType: et };
  }

  /** 这些源解析要跑无头 Chrome（10–20 秒），不参与预取/预热，免得一屏起好几次浏览器。 */
  const SLOW_SITES = new Set(['txxx', 'hqporner', 'beeg']);

  function preloadDepth() {
    const n = netProfile();
    if (n.saveData || n.slow) return 1;
    return 2;
  }

  function hlsMainConfig() {
    const n = netProfile();
    const save = n.saveData || n.slow;
    return {
      // 关掉 hls.js 的 Web Worker：本地代理（/api/hj-hls 这类）在 worker 里加载清单会静默失败，
      // 表现为 currentTime 一直 0、buffered 为空。单机自用，主线程解析完全够。
      enableWorker: false,
      lowLatencyMode: false,
      maxBufferLength: save ? 12 : 30,
      maxMaxBufferLength: save ? 20 : 60,
      maxBufferHole: 0.8,
      nudgeOffset: 0.1,
      nudgeMaxRetry: 5,
      startFragPrefetch: true,
      capLevelToPlayerSize: false,
      startLevel: save ? 0 : 0,
      abrEwmaDefaultEstimate: save ? 300000 : 500000,
    };
  }

  function hlsWarmConfig() {
    const n = netProfile();
    return {
      // 关掉 hls.js 的 Web Worker：本地代理（/api/hj-hls 这类）在 worker 里加载清单会静默失败，
      // 表现为 currentTime 一直 0、buffered 为空。单机自用，主线程解析完全够。
      enableWorker: false,
      lowLatencyMode: false,
      maxBufferLength: 4,
      maxMaxBufferLength: n.saveData || n.slow ? 4 : 6,
      maxBufferHole: 0.8,
      startFragPrefetch: false,
      capLevelToPlayerSize: true,
      startLevel: 0,
      autoStartLoad: true,
    };
  }

  // —— Local favorites / history (localStorage only) ——
  const FAV_KEY = 'swipefeed:favorites';
  const HIST_KEY = 'swipefeed:history';
  const MAX_FAV = 100;
  const MAX_HIST = 200;

  function lsRead(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return fallback;
      const j = JSON.parse(raw);
      return Array.isArray(j) ? j : fallback;
    } catch (_) {
      return fallback;
    }
  }
  function lsWrite(key, arr) {
    try { localStorage.setItem(key, JSON.stringify(arr)); } catch (_) {}
  }
  function cardLocalKey(card) {
    return (card.site || '') + ':' + (card.id || '');
  }
  function toLocalEntry(card) {
    return {
      site: card.site,
      id: card.id,
      title: card.title || card.id || '',
      cover: card.cover || null,
      pageUrl: card.pageUrl || '',
      duration: card.duration || null,
      author: card.author || null,
      ts: Date.now(),
    };
  }
  function getFavorites() { return lsRead(FAV_KEY, []); }
  function getHistory() { return lsRead(HIST_KEY, []); }
  function isFavorite(card) {
    if (!card) return false;
    const k = cardLocalKey(card);
    return getFavorites().some((x) => cardLocalKey(x) === k);
  }
  function toggleFavorite(card) {
    if (!card || !card.site || !card.id) return false;
    const k = cardLocalKey(card);
    let list = getFavorites();
    const idx = list.findIndex((x) => cardLocalKey(x) === k);
    if (idx >= 0) {
      list.splice(idx, 1);
      lsWrite(FAV_KEY, list);
      return false;
    }
    list.unshift(toLocalEntry(card));
    if (list.length > MAX_FAV) list = list.slice(0, MAX_FAV);
    lsWrite(FAV_KEY, list);
    return true;
  }
  function pushHistory(card) {
    if (!card || !card.site || !card.id) return;
    const k = cardLocalKey(card);
    let list = getHistory().filter((x) => cardLocalKey(x) !== k);
    list.unshift(toLocalEntry(card));
    if (list.length > MAX_HIST) list = list.slice(0, MAX_HIST);
    lsWrite(HIST_KEY, list);
  }
  function updateFavChrome(card) {
    if (!favBtn) return;
    const on = isFavorite(card);
    favBtn.textContent = on ? '★' : '☆';
    favBtn.classList.toggle('fav-on', on);
    favBtn.title = on ? '取消收藏' : '收藏 (本地)';
  }

  // 加载态：正常显示转圈；超过 12 秒还没起来就换成「超时 + 重试」，
  // 免得像以前那样一直停在「加载中…」看不出是死是活。
  let loadingTimer = null;
  function setLoading(on) {
    loading.classList.toggle('hide', !on);
    clearTimeout(loadingTimer);
    loading.classList.remove('stalled');
    if (!on) return;
    loadingTimer = setTimeout(() => {
      if (loading.classList.contains('hide')) return;
      loading.classList.add('stalled');
      loading.innerHTML = '<span class="stalled-text">加载超时</span><button type="button" class="chip" id="loadingRetry">重试</button>';
      const retry = loading.querySelector('#loadingRetry');
      if (retry) {
        retry.onclick = (e) => {
          e.stopPropagation();
          setLoading(false);
          resetAndLoad();
        };
      }
    }, 12000);
  }

  function formatTime(sec) {
    if (!isFinite(sec) || sec < 0) return '0:00';
    const s = Math.floor(sec);
    const m = Math.floor(s / 60);
    const r = s % 60;
    const h = Math.floor(m / 60);
    if (h > 0) return h + ':' + String(m % 60).padStart(2, '0') + ':' + String(r).padStart(2, '0');
    return m + ':' + String(r).padStart(2, '0');
  }

  function bumpChrome() {
    topbar.classList.remove('chrome-hidden', 'chrome-peek');
    hud.classList.remove('chrome-hidden');
    controls.classList.remove('chrome-hidden');
    clearTimeout(chromeIdleTimer);
    chromeIdleTimer = setTimeout(() => {
      if (scrubbing || speedBoost) return;
      topbar.classList.add('chrome-hidden');
      hud.classList.add('chrome-hidden');
      controls.classList.add('chrome-hidden');
    }, CHROME_IDLE_MS);
  }

  function peekTopbar() {
    if (!topbar.classList.contains('chrome-hidden')) return;
    topbar.classList.remove('chrome-hidden');
    topbar.classList.add('chrome-peek');
    clearTimeout(chromeIdleTimer);
    chromeIdleTimer = setTimeout(() => {
      topbar.classList.remove('chrome-peek');
      topbar.classList.add('chrome-hidden');
    }, 1600);
  }

  function applyProgressUI(ratio, current, duration) {
    const pct = Math.max(0, Math.min(1, ratio)) * 100;
    progressFill.style.width = pct + '%';
    progressKnob.style.left = pct + '%';
    if (duration > 0) {
      progressTime.textContent = formatTime(current) + ' / ' + formatTime(duration);
    } else if (progressTime.textContent) {
      progressTime.textContent = '';
    }
  }

  function setProgressUI(ratio, current, duration) {
    pendingProgress = { ratio, current, duration };
    if (progressRaf) return;
    progressRaf = requestAnimationFrame(() => {
      progressRaf = 0;
      const p = pendingProgress;
      pendingProgress = null;
      if (!p || p === 'sync') {
        if (scrubbing || seekInFlight) return;
        const d = player.duration;
        if (!d || !isFinite(d)) {
          applyProgressUI(0, 0, 0);
          return;
        }
        applyProgressUI(player.currentTime / d, player.currentTime, d);
        updateBuffered();
        return;
      }
      applyProgressUI(p.ratio, p.current, p.duration);
    });
  }

  function updateBuffered() {
    try {
      if (!player.duration || !player.buffered || !player.buffered.length) {
        progressBuffered.style.width = '0%';
        return;
      }
      const end = player.buffered.end(player.buffered.length - 1);
      progressBuffered.style.width = Math.min(100, (end / player.duration) * 100) + '%';
    } catch (_) {
      progressBuffered.style.width = '0%';
    }
  }

  function syncProgressFromPlayer() {
    if (scrubbing || seekInFlight) return;
    pendingProgress = 'sync';
    if (progressRaf) return;
    progressRaf = requestAnimationFrame(() => {
      progressRaf = 0;
      const p = pendingProgress;
      pendingProgress = null;
      if (scrubbing || seekInFlight) return;
      if (p && p !== 'sync') {
        applyProgressUI(p.ratio, p.current, p.duration);
        return;
      }
      const d = player.duration;
      if (!d || !isFinite(d)) {
        applyProgressUI(0, 0, 0);
        return;
      }
      applyProgressUI(player.currentTime / d, player.currentTime, d);
      updateBuffered();
    });
  }

  function ratioFromClientX(clientX) {
    const track = progressEl.querySelector('.progress-track');
    const rect = track.getBoundingClientRect();
    if (rect.width <= 0) return 0;
    return Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  }


  function timeInBuffered(t) {
    try {
      const b = player.buffered;
      for (let i = 0; i < b.length; i++) {
        if (t >= b.start(i) - 0.05 && t <= b.end(i) + 0.05) return true;
      }
    } catch (_) {}
    return false;
  }

  /** If target is just outside a buffered range, snap in for instant seek; else keep target. */
  function preferBufferedSeekTarget(t) {
    if (timeInBuffered(t)) return t;
    try {
      const b = player.buffered;
      let best = null;
      let bestDist = Infinity;
      for (let i = 0; i < b.length; i++) {
        const start = b.start(i);
        const end = b.end(i);
        // snap into range if within 1.25s of an edge
        if (t < start && start - t <= 1.25) {
          const dist = start - t;
          if (dist < bestDist) { bestDist = dist; best = start + 0.05; }
        } else if (t > end && t - end <= 1.25) {
          const dist = t - end;
          if (dist < bestDist) { bestDist = dist; best = Math.max(start, end - 0.15); }
        }
      }
      if (best != null) return best;
    } catch (_) {}
    return t;
  }

  function clearSeekListeners() {
    if (!seekListeners) return;
    player.removeEventListener('seeked', seekListeners.onSeeked);
    player.removeEventListener('canplay', seekListeners.onCanPlay);
    player.removeEventListener('waiting', seekListeners.onWaiting);
    seekListeners = null;
  }

  function hideSeekBuf() {
    clearTimeout(seekBufferTimer);
    seekBufferTimer = null;
    if (seekBufEl) seekBufEl.classList.add('hide');
  }

  function armSeekBufHint() {
    hideSeekBuf();
    seekBufferTimer = setTimeout(() => {
      seekBufferTimer = null;
      if (seekInFlight && seekBufEl) seekBufEl.classList.remove('hide');
    }, 200);
  }

  function cancelSeek() {
    seekToken++;
    seekInFlight = false;
    clearSeekListeners();
    hideSeekBuf();
    clearTimeout(seekSafetyTimer);
    seekSafetyTimer = null;
  }

  /**
   * Instant seek on scrub end / key jump. Never re-resolves or reloads the stream.
   * Progress stays pinned to target until seeked/canplay; buffer chip only after 200ms.
   */
  function seekTo(t, { resume } = {}) {
    const duration = player.duration;
    if (!duration || !isFinite(duration)) return;
    const token = ++seekToken;
    clearSeekListeners();
    hideSeekBuf();
    clearTimeout(seekSafetyTimer);

    let target = Math.max(0, Math.min(duration - 0.05, t));
    // HLS: prefer already-buffered nearby range for snappy scrub; otherwise seek exact target
    if (hlsInstance) {
      target = preferBufferedSeekTarget(target);
    }

    seekInFlight = true;
    applyProgressUI(target / duration, target, duration);
    updateBuffered();

    const shouldPlay = resume === true || (resume !== false && wasPlayingBeforeScrub);

    const finish = () => {
      if (token !== seekToken) return;
      seekInFlight = false;
      clearSeekListeners();
      hideSeekBuf();
      clearTimeout(seekSafetyTimer);
      seekSafetyTimer = null;
      syncProgressFromPlayer();
      if (shouldPlay) {
        player.play().catch(() => {});
      }
    };

    const maybeFinish = () => {
      if (token !== seekToken) return;
      // readyState >= HAVE_FUTURE_DATA (3) is enough to resume; HAVE_CURRENT_DATA (2) often OK for MP4
      if (player.readyState >= 2) finish();
    };

    const onSeeked = () => {
      if (token !== seekToken) return;
      if (player.readyState >= 3 || timeInBuffered(player.currentTime)) finish();
      // else wait for canplay
    };
    const onCanPlay = () => {
      if (token !== seekToken) return;
      finish();
    };
    const onWaiting = () => {
      if (token !== seekToken || !seekInFlight) return;
      armSeekBufHint();
    };

    seekListeners = { onSeeked, onCanPlay, onWaiting };
    player.addEventListener('seeked', onSeeked);
    player.addEventListener('canplay', onCanPlay);
    player.addEventListener('waiting', onWaiting);

    if (!timeInBuffered(target)) armSeekBufHint();

    try {
      player.currentTime = target;
    } catch (_) {
      finish();
      return;
    }

    // If browser already at target / sync seek
    if (Math.abs(player.currentTime - target) < 0.125 && player.readyState >= 3) {
      finish();
      return;
    }

    seekSafetyTimer = setTimeout(() => {
      if (token === seekToken) finish();
    }, 10000);
  }

  function beginScrub(clientX) {
    if (!player.duration || !isFinite(player.duration)) return false;
    // Cancel any in-flight seek from a previous scrub; keep UI live
    cancelSeek();
    wasPlayingBeforeScrub = !player.paused;
    if (wasPlayingBeforeScrub) {
      try { player.pause(); } catch (_) {}
    }
    scrubbing = true;
    progressEl.classList.add('scrubbing');
    progressFill.classList.add('scrub-paint');
    scrubRatio = ratioFromClientX(clientX);
    const t = scrubRatio * player.duration;
    applyProgressUI(scrubRatio, t, player.duration);
    bumpChrome();
    clearLongPress();
    return true;
  }

  function moveScrub(clientX) {
    if (!scrubbing) return;
    scrubRatio = ratioFromClientX(clientX);
    const t = scrubRatio * player.duration;
    // Keep knob/time responsive even if previous media seek is slow
    setProgressUI(scrubRatio, t, player.duration);
  }

  function endScrub() {
    if (!scrubbing) return;
    scrubbing = false;
    progressEl.classList.remove('scrubbing');
    progressFill.classList.remove('scrub-paint');
    bumpChrome();
    if (!player.duration || !isFinite(player.duration)) return;
    const t = scrubRatio * player.duration;
    // Seek ASAP on pointerup — no re-resolve, no video.load()
    seekTo(t, { resume: wasPlayingBeforeScrub });
  }

  progressEl.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    e.stopPropagation();
    progressEl.setPointerCapture?.(e.pointerId);
    beginScrub(e.clientX);
  });
  progressEl.addEventListener('pointermove', (e) => {
    if (!scrubbing) return;
    e.preventDefault();
    moveScrub(e.clientX);
  });
  const endScrubEvt = (e) => {
    if (!scrubbing) return;
    e.preventDefault();
    e.stopPropagation();
    endScrub();
  };
  progressEl.addEventListener('pointerup', endScrubEvt);
  progressEl.addEventListener('pointercancel', endScrubEvt);

  player.addEventListener('timeupdate', syncProgressFromPlayer);
  player.addEventListener('progress', () => {
    if (!progressRaf) {
      progressRaf = requestAnimationFrame(() => {
        progressRaf = 0;
        updateBuffered();
      });
    }
  });
  player.addEventListener('loadedmetadata', syncProgressFromPlayer);
  player.addEventListener('seeked', syncProgressFromPlayer);
  player.addEventListener('durationchange', syncProgressFromPlayer);

  function clearLongPress() {
    if (longPressTimer) {
      clearTimeout(longPressTimer);
      longPressTimer = null;
    }
  }

  function startSpeedBoost() {
    if (speedBoost || player.paused) return;
    speedBoost = true;
    player.playbackRate = 2;
    showToast('2x', 800);
    toast.classList.add('speed-badge');
    bumpChrome();
  }

  function endSpeedBoost() {
    clearLongPress();
    if (!speedBoost) return;
    speedBoost = false;
    player.playbackRate = 1;
    toast.classList.remove('speed-badge');
    toast.classList.remove('show');
    bumpChrome();
  }

  async function api(path, opts = {}) {
    const r = await fetch(BASE_PATH + path, opts);
    if (opts.signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  }

  function cacheKey(card) {
    return card.site + ':' + card.id;
  }

  function getCachedStream(card) {
    if (card._stream && card._stream.url) return card._stream;
    const hit = resolveCache.get(cacheKey(card));
    if (!hit) return null;
    if (hit.expires && Date.now() > hit.expires) {
      resolveCache.delete(cacheKey(card));
      return null;
    }
    if (hit.stream) {
      card._stream = hit.stream;
      return hit.stream;
    }
    return null;
  }

  function putCachedStream(card, stream) {
    card._stream = stream;
    resolveCache.set(cacheKey(card), {
      stream,
      expires: Date.now() + RESOLVE_TTL_MS,
    });
  }

  function acquireResolveSlot() {
    if (resolveActive < MAX_PARALLEL_RESOLVE) {
      resolveActive++;
      return Promise.resolve();
    }
    return new Promise((resolve) => resolveWaiters.push(resolve)).then(() => {
      resolveActive++;
    });
  }

  function releaseResolveSlot() {
    resolveActive = Math.max(0, resolveActive - 1);
    const next = resolveWaiters.shift();
    if (next) next();
  }

  async function resolveCard(card, { signal } = {}) {
    const cached = getCachedStream(card);
    if (cached) return cached;

    const key = cacheKey(card);
    const existing = resolveCache.get(key);
    if (existing?.inflight) {
      return existing.inflight;
    }

    const ctrl = new AbortController();
    const onAbort = () => ctrl.abort();
    if (signal) {
      if (signal.aborted) throw new DOMException('Aborted', 'AbortError');
      signal.addEventListener('abort', onAbort, { once: true });
    }

    const inflight = (async () => {
      await acquireResolveSlot();
      try {
        if (ctrl.signal.aborted) throw new DOMException('Aborted', 'AbortError');
        const r = await api('/api/resolve', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ site: card.site, id: card.id, pageUrl: card.pageUrl }),
          signal: ctrl.signal,
        });
        putCachedStream(card, r);
        return r;
      } finally {
        releaseResolveSlot();
        const cur = resolveCache.get(key);
        if (cur && cur.inflight) {
          if (cur.stream) {
            resolveCache.set(key, { stream: cur.stream, expires: cur.expires || Date.now() + RESOLVE_TTL_MS });
          } else {
            resolveCache.delete(key);
          }
        }
        if (signal) signal.removeEventListener('abort', onAbort);
      }
    })();

    resolveCache.set(key, { inflight, abort: ctrl });
    return inflight;
  }

  function cancelStalePrefetch(exceptKeys) {
    const keep = new Set(exceptKeys || []);
    for (const [key, entry] of resolveCache) {
      if (entry.stream) continue;
      if (entry.inflight && entry.abort && !keep.has(key)) {
        try { entry.abort.abort(); } catch (_) {}
      }
    }
  }

  async function loadSites() {
    const data = await api('/api/sites');
    sites = data.sites || [];
    categories = data.categories || [];
    renderChips();
  }

  function renderChips() {
    // Only rebuild when site/category lists or selection change — not on swipe
    siteRow.replaceChildren();
    for (const s of sites) {
      const b = document.createElement('button');
      b.className = 'chip' + (s.id === site ? ' active' : '');
      b.textContent = s.displayName || s.name;
      b.onclick = () => {
        if (site === s.id) return;
        site = s.id;
        renderChips();
        bumpChrome();
        if (mode === 'single' || mode === 'category') resetAndLoad();
      };
      siteRow.appendChild(b);
    }
    catRow.replaceChildren();
    for (const c of categories) {
      const b = document.createElement('button');
      b.className = 'chip' + (c.id === category ? ' active' : '');
      b.textContent = c.displayName;
      b.onclick = () => {
        if (category === c.id) return;
        category = c.id;
        renderChips();
        bumpChrome();
        if (mode === 'category') resetAndLoad();
      };
      catRow.appendChild(b);
    }
  }

  document.querySelectorAll('.modes .chip[data-mode]').forEach((btn) => {
    btn.onclick = () => {
      mode = btn.dataset.mode;
      document.querySelectorAll('.modes .chip[data-mode]').forEach((b) => b.classList.toggle('active', b === btn));
      catRow.classList.toggle('hidden', mode !== 'category');
      searchRow.classList.toggle('hidden', mode !== 'search');
      siteRow.classList.toggle('hidden', mode === 'random' || mode === 'search');
      bumpChrome();
      if (mode !== 'search') resetAndLoad();
    };
  });

  $('#searchBtn').onclick = () => doSearch();
  $('#searchInput').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') doSearch();
  });
  topbar.addEventListener('pointerdown', () => bumpChrome());

  $('#fitBtn').onclick = () => {
    objectFitCover = !objectFitCover;
    player.classList.toggle('cover-fit', objectFitCover);
    $('#fitBtn').textContent = objectFitCover ? '裁切' : '适应';
    showToast(objectFitCover ? '封面裁切 (cover)' : '完整显示 (contain)');
    bumpChrome();
  };

  if (copyStreamBtn) {
    copyStreamBtn.onclick = async () => {
      bumpChrome();
      const url = currentStreamUrl;
      if (!url) {
        showToast('无直链');
        return;
      }
      const ok = await copyText(url);
      showToast(ok ? '已复制' : '复制失败');
    };
  }
  // 源站入口只保留 HUD 里那一个（控制列已经很挤了，避免两个「源站」）

  function destroyHls() {
    if (hlsInstance) {
      try { hlsInstance.destroy(); } catch (_) {}
      hlsInstance = null;
    }
  }

  function destroyWarm() {
    warmGen++;
    if (warmHls) {
      try { warmHls.destroy(); } catch (_) {}
      warmHls = null;
    }
    warmKey = null;
    if (warmPlayer) {
      try { warmPlayer.pause(); } catch (_) {}
      warmPlayer.removeAttribute('src');
      try { warmPlayer.load(); } catch (_) {}
    }
  }

  function canNativeHls() {
    return player.canPlayType('application/vnd.apple.mpegurl') !== '';
  }

  /**
   * 原生 HLS 播放失败时的兜底：改用 hls.js 播同一个地址。
   * 少数浏览器 canPlayType 返回 "maybe" 但实际不支持，这里把它救回来而不是直接下一条。
   */
  async function fallbackToHlsJs(url) {
    try {
      const Hls = await loadHlsLib();
      if (!Hls || !Hls.isSupported()) return false;
      destroyHls();
      hlsInstance = new Hls(hlsMainConfig());
      const ok = await new Promise((resolve) => {
        const done = (v) => { cleanup(); resolve(v); };
        const cleanup = () => {
          hlsInstance?.off(Hls.Events.MANIFEST_PARSED, onReady);
          hlsInstance?.off(Hls.Events.ERROR, onErr);
        };
        const onReady = () => done(true);
        const onErr = (_e, data) => { if (data?.fatal) done(false); };
        hlsInstance.on(Hls.Events.MANIFEST_PARSED, onReady);
        hlsInstance.on(Hls.Events.ERROR, onErr);
        hlsInstance.loadSource(url);
        hlsInstance.attachMedia(player);
        setTimeout(() => done(false), 12000);
      });
      if (!ok) { destroyHls(); return false; }
      player.play().catch(() => {});
      showToast('已切 hls.js 播放', 900);
      return true;
    } catch (_) {
      return false;
    }
  }

  function loadHlsLib() {
    if (window.Hls) return Promise.resolve(window.Hls);
    if (hlsLibPromise) return hlsLibPromise;
    hlsLibPromise = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = BASE_PATH + '/vendor/hls.min.js';
      s.async = true;
      s.onload = () => (window.Hls ? resolve(window.Hls) : reject(new Error('Hls missing')));
      s.onerror = () => reject(new Error('hls.js load failed'));
      document.head.appendChild(s);
    }).catch((e) => {
      hlsLibPromise = null;
      throw e;
    });
    return hlsLibPromise;
  }

  function pickPlayableUrl(stream) {
    if (!stream) return null;
    const qualities = Array.isArray(stream.qualities) ? stream.qualities : [];
    const mp4 = qualities.find((q) => q.url && /\.mp4(\?|$)/i.test(q.url))
      || (stream.type === 'mp4' || (stream.url && /\.mp4(\?|$)/i.test(stream.url)) ? stream : null);
    if (mp4?.url) return { url: mp4.url, type: 'mp4' };
    if (stream.url && /\.mp4(\?|$)/i.test(stream.url)) return { url: stream.url, type: 'mp4' };
    const hls = qualities.find((q) => q.url && /\.m3u8/i.test(q.url))
      || (stream.type === 'hls' || (stream.url && /\.m3u8/i.test(stream.url)) ? stream : null);
    if (hls?.url) return { url: hls.url, type: 'hls' };
    if (stream.url) return { url: stream.url, type: stream.type || (/\.m3u8/i.test(stream.url) ? 'hls' : 'mp4') };
    return null;
  }

  async function attachStream(stream, gen) {
    const picked = pickPlayableUrl(stream);
    if (!picked) throw new Error('无可用播放地址');
    destroyHls();
    hlsFatalRecovered = false;
    currentStreamUrl = copyableStreamUrl(stream);
    // 经本地代理的 HLS（/api/hls、/api/png-hls）一律交给 hls.js：
    //   · 同源，不存在跨域/ORB 问题
    //   · AES-128 清单（海角等）原生播放器容易静默失败，hls.js 能正常解密
    const isLocalProxyHls = /\/api\/(?:hls|hj-hls|png-hls)\?/i.test(picked.url || '');
    const useHlsJs = picked.type === 'hls' && (isLocalProxyHls || !canNativeHls());
    pendingNativeHls = (picked.type === 'hls' && !useHlsJs && canNativeHls()) ? picked.url : null;

    // Prefer direct MP4 when qualities include it (pickPlayableUrl already prefers .mp4).
    // Keep cover visible; only swap src when ready. Avoid gratuitous load() churn.
    if (useHlsJs) {
      const Hls = await loadHlsLib();
      if (gen !== playGen) return;
      if (!Hls.isSupported()) throw new Error('此浏览器无法播放 HLS');
      hlsInstance = new Hls(hlsMainConfig());
      hlsInstance.loadSource(picked.url);
      hlsInstance.attachMedia(player);
      await new Promise((resolve, reject) => {
        const onReady = () => {
          cleanup();
          resolve();
        };
        const onErr = (_e, data) => {
          if (data?.fatal) {
            cleanup();
            reject(new Error(data?.details || 'HLS error'));
          }
        };
        const cleanup = () => {
          hlsInstance?.off(Hls.Events.MANIFEST_PARSED, onReady);
          hlsInstance?.off(Hls.Events.ERROR, onErr);
        };
        hlsInstance.on(Hls.Events.MANIFEST_PARSED, onReady);
        hlsInstance.on(Hls.Events.ERROR, onErr);
      });
      if (gen !== playGen) return;
      // One automatic fatal recover / reload per item (network or media).
      hlsInstance.on(Hls.Events.ERROR, (_e, data) => {
        if (!data?.fatal || gen !== playGen || !hlsInstance) return;
        if (hlsFatalRecovered) {
          showToast('播放失败', 1200);
          scheduleAutoNext(gen);
          return;
        }
        hlsFatalRecovered = true;
        try {
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
            hlsInstance.startLoad();
          } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            hlsInstance.recoverMediaError();
          } else {
            // Last resort once: reload same already-resolved source
            hlsInstance.loadSource(picked.url);
            hlsInstance.startLoad();
          }
        } catch (_) {
          showToast('播放失败', 1200);
          scheduleAutoNext(gen);
        }
      });
    } else {
      if (player.src !== picked.url) {
        player.src = picked.url;
      }
    }
    if (gen !== playGen) return;
    player.playbackRate = 1;
    player.play().catch(() => {});
  }

  function stopPlayerSoft() {
    endSpeedBoost();
    destroyWarm();
    try { player.pause(); } catch (_) {}
    destroyHls();
    // Do not call video.load() here — causes blank flash; cover stays up until next play
    player.removeAttribute('src');
    try { player.load(); } catch (_) {}
  }

  async function resetAndLoad() {
    playGen++;
    items = [];
    index = 0;
    cursor = null;
    pagingPromise = null;
    cancelStalePrefetch([]);
    stopPlayerSoft();
    applyProgressUI(0, 0, 0);
    progressBuffered.style.width = '0%';
    coverEl.classList.remove('hide');
    await loadMore({ showLoading: true });
    if (items.length) await playAt(0);
    else {
      titleEl.textContent = '暂无内容';
      metaEl.textContent = '该源可能被拦截或为空';
      currentStreamUrl = null;
      updateSourceChrome(null);
      coverEl.style.backgroundImage = '';
      coverEl.classList.remove('hide');
      setLoading(false);
    }
  }

  async function doSearch() {
    const q = $('#searchInput').value.trim();
    if (!q) return showToast('请输入关键词');
    mode = 'search';
    playGen++;
    items = [];
    index = 0;
    cursor = null;
    setLoading(true);
    try {
      const data = await api('/api/search?q=' + encodeURIComponent(q) + (site ? '&site=' + encodeURIComponent(site) : ''));
      items = data.items || [];
      cursor = data.nextCursor || null;
      if (items.length) await playAt(0);
      else showToast('无结果');
    } catch (e) {
      if (e.name !== 'AbortError') showToast(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function loadMore({ showLoading = false } = {}) {
    if (pagingPromise) return pagingPromise;
    if (showLoading) setLoading(true);
    pagingPromise = (async () => {
      try {
        let url;
        const cur = cursor ? '&cursor=' + encodeURIComponent(JSON.stringify(cursor)) : '';
        if (mode === 'search') {
          const q = $('#searchInput').value.trim();
          if (!q) return;
          url = `/api/search?q=${encodeURIComponent(q)}${cur}`;
        } else if (mode === 'random') {
          url = `/api/feed?mode=random${randomSitesQuery()}${cur}`;
        } else if (mode === 'category') {
          url = `/api/feed?mode=category&category=${encodeURIComponent(category)}&site=${encodeURIComponent(site)}${cur}`;
        } else if (mode === 'browse') {
          // 从查看面板进来的续页：沿用面板的站点/分类
          const bcat = browseCategory ? `&category=${encodeURIComponent(browseCategory)}` : '';
          url = `/api/feed?mode=browse&site=${encodeURIComponent(site)}${bcat}${cur}`;
        } else {
          url = `/api/feed?mode=single&site=${encodeURIComponent(site)}${cur}`;
        }
        const data = await api(url);
        const fresh = data.items || [];
        const seen = new Set(items.map((i) => i.site + ':' + i.id));
        for (const c of fresh) {
          const k = c.site + ':' + c.id;
          if (!seen.has(k)) {
            seen.add(k);
            items.push(c);
          }
        }
        cursor = data.nextCursor || null;
      } catch (e) {
        showToast('列表失败: ' + e.message, 2000);
      } finally {
        if (showLoading) setLoading(false);
        pagingPromise = null;
      }
    })();
    return pagingPromise;
  }

  function maybePrefetchPage() {
    if (!cursor) return;
    if (index >= items.length - 3) {
      loadMore({ showLoading: false });
    }
  }

  function scheduleAutoNext(gen) {
    clearTimeout(autoNextTimer);
    autoNextTimer = setTimeout(() => {
      autoNextTimer = null;
      if (gen !== playGen) return;
      goNext();
    }, 450);
  }

  function markTtffStart() {
    ttffStart = performance.now();
  }

  function markTtffEnd() {
    if (!ttffStart) return;
    lastTtffMs = Math.round(performance.now() - ttffStart);
    ttffStart = 0;
    if (ttffHint) {
      ttffHint.textContent = 'TTFF ' + lastTtffMs + 'ms';
      ttffHint.classList.remove('hide');
      clearTimeout(markTtffEnd._t);
      markTtffEnd._t = setTimeout(() => ttffHint.classList.add('hide'), 2200);
    }
  }

  async function playAt(i) {
    if (i < 0 || i >= items.length) return;
    const gen = ++playGen;
    index = i;
    const card = items[i];
    clearTimeout(autoNextTimer);
    cancelSeek();
    endSpeedBoost();
    markTtffStart();
    pushHistory(card);
    updateFavChrome(card);
    updateMediaSession(card);
    titleEl.textContent = card.title || card.id;
    metaEl.textContent = [siteDisplayName(card.site), card.duration, card.author].filter(Boolean).join(' · ');
    currentStreamUrl = null;
    updateSourceChrome(card);
    if (card.cover) {
      coverEl.style.backgroundImage = `url("${card.cover}")`;
    } else {
      coverEl.style.backgroundImage = '';
    }
    coverEl.classList.remove('hide');
    applyProgressUI(0, 0, 0);
    progressBuffered.style.width = '0%';
    setLoading(true);
    bumpChrome();

    // Abort resolves that are no longer near the viewport (directional)
    const keep = [];
    const depth = preloadDepth();
    keep.push(cacheKey(card));
    for (let d = 1; d <= depth; d++) {
      const c = items[i + scrollDir * d];
      if (c) keep.push(cacheKey(c));
    }
    // also keep the opposite neighbor one step in case user reverses
    const opp = items[i - scrollDir];
    if (opp) keep.push(cacheKey(opp));
    cancelStalePrefetch(keep);
    // Drop warm media if current item is the warmed one (main takes over)
    if (warmKey === cacheKey(card)) destroyWarm();

    const playCtrl = new AbortController();
    // Tie abort to generation change
    const watchGen = setInterval(() => {
      if (gen !== playGen) {
        playCtrl.abort();
        clearInterval(watchGen);
      }
    }, 50);

    try {
      destroyHls();
      // Soft clear previous src so cover shows without flash of old frame
      try { player.pause(); } catch (_) {}
      if (player.src) {
        player.removeAttribute('src');
        try { player.load(); } catch (_) {}
      }

      const stream = await resolveCard(card, { signal: playCtrl.signal });
      if (gen !== playGen) return;
      await attachStream(stream, gen);
      if (gen !== playGen) return;
      prefetchAround(i);
      maybePrefetchPage();
    } catch (e) {
      if (e.name === 'AbortError' || gen !== playGen) return;
      showToast('解析失败，下一条…', 1200);
      setLoading(false);
      scheduleAutoNext(gen);
    } finally {
      clearInterval(watchGen);
    }
  }

  function prefetchAround(fromIndex) {
    const depth = preloadDepth();
    const targets = [];
    for (let d = 1; d <= depth; d++) {
      const c = items[fromIndex + scrollDir * d];
      if (c) targets.push(c);
    }
    for (const card of targets) {
      if (getCachedStream(card)) continue;
      if (SLOW_SITES.has(card.site)) continue;
      resolveCard(card).catch(() => {});
    }
    // Directional shallow media warm for the immediate next card
    const warmTarget = items[fromIndex + scrollDir];
    if (warmTarget && !SLOW_SITES.has(warmTarget.site)) warmMedia(warmTarget).catch(() => {});
    else destroyWarm();
  }

  async function warmMedia(card) {
    if (!warmPlayer || !card) return;
    const key = cacheKey(card);
    if (warmKey === key) return;
    // On direction change / new target: destroy out-of-window warm
    destroyWarm();
    const myGen = warmGen;
    const n = netProfile();
    // Skip media warm entirely on save-data (resolve prefetch still runs)
    if (n.saveData) return;
    let stream;
    try {
      stream = await resolveCard(card);
    } catch (_) {
      return;
    }
    if (myGen !== warmGen) return;
    const picked = pickPlayableUrl(stream);
    if (!picked?.url) return;
    warmKey = key;
    try {
      if (picked.type === 'hls' && !canNativeHls()) {
        const Hls = await loadHlsLib();
        if (myGen !== warmGen) return;
        if (!Hls.isSupported()) return;
        warmHls = new Hls(hlsWarmConfig());
        warmHls.loadSource(picked.url);
        warmHls.attachMedia(warmPlayer);
        // Short-lived: stop loading after a shallow buffer window
        setTimeout(() => {
          if (myGen !== warmGen || !warmHls) return;
          try { warmHls.stopLoad(); } catch (_) {}
        }, 8000);
      } else {
        warmPlayer.src = picked.url;
        try { warmPlayer.load(); } catch (_) {}
        // Nudge a tiny buffer without audible playback
        warmPlayer.muted = true;
        const p = warmPlayer.play();
        if (p && p.then) {
          p.then(() => {
            try { warmPlayer.pause(); } catch (_) {}
          }).catch(() => {});
        }
      }
    } catch (_) {
      destroyWarm();
    }
  }

  function hideCoverWhenReady() {
    coverEl.classList.add('hide');
    setLoading(false);
  }

  // Prefer first painted frame over loadeddata alone (reduces black flash)
  player.addEventListener('loadeddata', () => {
    if (player.readyState >= 2) hideCoverWhenReady();
  });
  player.addEventListener('playing', () => {
    hideCoverWhenReady();
    markTtffEnd();
  });
  player.addEventListener('pause', () => {
    endSpeedBoost();
    bumpChrome();
  });
  player.addEventListener('error', () => {
    if (!player.src && !hlsInstance) return;
    // 部分桌面浏览器 canPlayType 对 HLS 返回 "maybe" 但实际放不了：先退回 hls.js 再放弃本条
    const src = player.src || player.currentSrc || '';
    if (!hlsInstance && pendingNativeHls && /\.m3u8/i.test(src)) {
      pendingNativeHls = null;
      fallbackToHlsJs(src).then((ok) => {
        if (!ok) {
          showToast('播放失败，下一条…', 1200);
          scheduleAutoNext(playGen);
        }
      });
      return;
    }
    showToast('播放失败，下一条…', 1200);
    scheduleAutoNext(playGen);
  });
  player.addEventListener('volumechange', () => {
    syncMuteChrome();
  });

  // gesture: tap pause/play, swipe next/prev, long-press 2x
  let touchStartY = 0;
  let touchStartX = 0;
  let touchStartT = 0;
  let gestureMoved = false;
  let gestureActive = false;
  let suppressedTap = false;
  const stage = $('#stage');

  stage.addEventListener('pointerdown', (e) => {
    if (e.target.closest('button,input,.chip,#topbar,#progress,#hudActions,a.source-link')) return;
    gestureActive = true;
    gestureMoved = false;
    suppressedTap = false;
    touchStartY = e.clientY;
    touchStartX = e.clientX;
    touchStartT = Date.now();
    bumpChrome();
    clearLongPress();
    if (!player.paused && (player.src || hlsInstance)) {
      longPressTimer = setTimeout(() => {
        longPressTimer = null;
        if (!gestureActive || gestureMoved || scrubbing) return;
        suppressedTap = true;
        startSpeedBoost();
      }, LONG_PRESS_MS);
    }
  });

  stage.addEventListener('pointermove', (e) => {
    if (!gestureActive) return;
    const dy = e.clientY - touchStartY;
    const dx = e.clientX - touchStartX;
    if (Math.abs(dy) > MOVE_CANCEL_PX || Math.abs(dx) > MOVE_CANCEL_PX) {
      gestureMoved = true;
      clearLongPress();
      if (speedBoost) endSpeedBoost();
    }
  }, { passive: true });

  stage.addEventListener('pointerup', (e) => {
    if (!gestureActive) return;
    gestureActive = false;
    const wasBoost = speedBoost;
    clearLongPress();
    endSpeedBoost();

    if (e.target.closest('button,input,.chip,#topbar,#progress,#hudActions,a.source-link')) return;

    const dy = e.clientY - touchStartY;
    const dx = e.clientX - touchStartX;
    const dt = Date.now() - touchStartT;

    if (wasBoost || suppressedTap) {
      suppressedTap = false;
      return;
    }

    if (Math.abs(dy) < MOVE_CANCEL_PX && Math.abs(dx) < MOVE_CANCEL_PX && dt < 350) {
      // Gesture zones: left 20% = prev, right 20% = next, center = pause/play
      const rect = stage.getBoundingClientRect();
      const ratio = rect.width > 0 ? (e.clientX - rect.left) / rect.width : 0.5;
      if (ratio < 0.2) {
        goPrev();
        return;
      }
      if (ratio > 0.8) {
        goNext();
        return;
      }
      if (player.paused) player.play().catch(() => {});
      else player.pause();
      showToast(player.paused ? '已暂停' : '播放中', 600);
      bumpChrome();
      return;
    }
    if (Math.abs(dy) > 50 && Math.abs(dy) > Math.abs(dx)) {
      if (dy < 0) goNext();
      else goPrev();
    }
  });

  stage.addEventListener('pointercancel', () => {
    gestureActive = false;
    clearLongPress();
    endSpeedBoost();
  });

  stage.addEventListener('pointerdown', (e) => {
    if (e.clientY < 48) peekTopbar();
  }, true);

  async function goNext() {
    endSpeedBoost();
    if (scrollDir !== 1) {
      scrollDir = 1;
      destroyWarm(); // direction change → drop out-of-window warm
    }
    // Never block swipe on page load — kick off prefetch and proceed if we have items
    maybePrefetchPage();
    if (index < items.length - 1) {
      playAt(index + 1);
      return;
    }
    // At end: try load once without freezing forever
    if (cursor || pagingPromise) {
      await loadMore({ showLoading: false });
      if (index < items.length - 1) playAt(index + 1);
      else showToast('没有更多了');
    } else {
      showToast('没有更多了');
    }
  }
  async function goPrev() {
    endSpeedBoost();
    if (scrollDir !== -1) {
      scrollDir = -1;
      destroyWarm();
    }
    if (index > 0) playAt(index - 1);
  }

  let wheelLock = false;
  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (wheelLock) return;
    wheelLock = true;
    setTimeout(() => { wheelLock = false; }, 420);
    if (e.deltaY > 20) goNext();
    else if (e.deltaY < -20) goPrev();
  }, { passive: false });

  window.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    if (e.key === 'ArrowDown' || e.key === 'j' || e.key === 'PageDown') { e.preventDefault(); goNext(); }
    if (e.key === 'ArrowUp' || e.key === 'k' || e.key === 'PageUp') { e.preventDefault(); goPrev(); }
    if (e.key === ' ' || e.key === 'p') {
      e.preventDefault();
      if (player.paused) player.play().catch(() => {});
      else player.pause();
      bumpChrome();
    }
    if (e.key === 'm' || e.key === 'M') {
      e.preventDefault();
      toggleMute();
    }
    if (e.key === 'f' || e.key === 'F') {
      e.preventDefault();
      const card = items[index];
      if (card) {
        const on = toggleFavorite(card);
        updateFavChrome(card);
        showToast(on ? '已收藏' : '已取消收藏');
      }
    }
    if (e.key === 'ArrowLeft') {
      if (player.duration) {
        wasPlayingBeforeScrub = !player.paused;
        seekTo(Math.max(0, player.currentTime - 5), { resume: wasPlayingBeforeScrub || !player.paused });
      }
    }
    if (e.key === 'ArrowRight') {
      if (player.duration) {
        wasPlayingBeforeScrub = !player.paused;
        seekTo(Math.min(player.duration, player.currentTime + 5), { resume: wasPlayingBeforeScrub || !player.paused });
      }
    }
  });

  function syncMuteChrome() {
    if (!muteBtn) return;
    const muted = !!player.muted || player.volume === 0;
    muteBtn.textContent = muted ? '🔇' : '🔊';
    muteBtn.classList.toggle('muted', muted);
    muteBtn.title = muted ? '取消静音 (M)' : '静音 (M)';
  }
  function toggleMute() {
    player.muted = !player.muted;
    syncMuteChrome();
    showToast(player.muted ? '已静音' : '声音开', 600);
    bumpChrome();
  }
  if (muteBtn) muteBtn.onclick = () => { toggleMute(); };
  syncMuteChrome();

  if (favBtn) {
    favBtn.onclick = () => {
      bumpChrome();
      const card = items[index];
      if (!card) { showToast('无内容'); return; }
      const on = toggleFavorite(card);
      updateFavChrome(card);
      showToast(on ? '已收藏' : '已取消收藏');
    };
  }

  // Optional PiP
  if (pipBtn) {
    const pipOk = !!(document.pictureInPictureEnabled && player.requestPictureInPicture);
    pipBtn.classList.toggle('hidden', !pipOk);
    if (pipOk) {
      pipBtn.onclick = async () => {
        bumpChrome();
        try {
          if (document.pictureInPictureElement) await document.exitPictureInPicture();
          else await player.requestPictureInPicture();
        } catch (_) {
          showToast('PiP 不可用');
        }
      };
    }
  }

  function updateMediaSession(card) {
    if (!('mediaSession' in navigator) || !card) return;
    try {
      const artwork = card.cover ? [{ src: card.cover, sizes: '512x512', type: 'image/jpeg' }] : [];
      navigator.mediaSession.metadata = new MediaMetadata({
        title: card.title || card.id || 'Swipefeed',
        artist: siteDisplayName(card.site) || 'Swipefeed',
        album: 'Swipefeed',
        artwork,
      });
      navigator.mediaSession.setActionHandler('nexttrack', () => { goNext(); });
      navigator.mediaSession.setActionHandler('previoustrack', () => { goPrev(); });
      navigator.mediaSession.setActionHandler('play', () => { player.play().catch(() => {}); });
      navigator.mediaSession.setActionHandler('pause', () => { player.pause(); });
    } catch (_) {}
  }
  try {
    if ('mediaSession' in navigator) {
      navigator.mediaSession.setActionHandler('nexttrack', () => { goNext(); });
      navigator.mediaSession.setActionHandler('previoustrack', () => { goPrev(); });
    }
  } catch (_) {}

  // —— History / favorites panel ——
  let histTab = 'history';
  function openHistory() {
    if (!historyPanel) return;
    historyPanel.classList.remove('hidden');
    historyPanel.setAttribute('aria-hidden', 'false');
    renderHistoryList();
  }
  function closeHistory() {
    if (!historyPanel) return;
    historyPanel.classList.add('hidden');
    historyPanel.setAttribute('aria-hidden', 'true');
  }
  function renderHistoryList() {
    if (!historyList) return;
    historyList.replaceChildren();
    const list = histTab === 'favorites' ? getFavorites() : getHistory();
    if (!list.length) {
      const empty = document.createElement('div');
      empty.className = 'settings-status';
      empty.style.padding = '16px 8px';
      empty.textContent = histTab === 'favorites' ? '暂无收藏' : '暂无历史';
      historyList.appendChild(empty);
      return;
    }
    for (const entry of list) {
      const row = document.createElement('div');
      row.className = 'hist-row';
      const cov = document.createElement('div');
      cov.className = 'hist-cover';
      if (entry.cover) cov.style.backgroundImage = 'url("' + entry.cover.replace(/"/g, '') + '")';
      const meta = document.createElement('div');
      meta.className = 'hist-meta';
      const t = document.createElement('div');
      t.className = 'hist-title';
      t.textContent = entry.title || entry.id;
      const s = document.createElement('div');
      s.className = 'hist-sub';
      const when = entry.ts ? new Date(entry.ts).toLocaleString() : '';
      s.textContent = [siteDisplayName(entry.site) || entry.site, when].filter(Boolean).join(' · ');
      meta.appendChild(t);
      meta.appendChild(s);
      row.appendChild(cov);
      row.appendChild(meta);
      row.onclick = () => {
        closeHistory();
        playLocalEntry(entry);
      };
      historyList.appendChild(row);
    }
  }
  async function playLocalEntry(entry) {
    if (!entry || !entry.site || !entry.id) return;
    // Inject at front of feed and play via existing resolve path
    const card = {
      site: entry.site,
      id: entry.id,
      title: entry.title || entry.id,
      cover: entry.cover || null,
      pageUrl: entry.pageUrl || '',
      duration: entry.duration || null,
      author: entry.author || null,
    };
    const k = cardLocalKey(card);
    const existing = items.findIndex((x) => cardLocalKey(x) === k);
    if (existing >= 0) {
      await playAt(existing);
      return;
    }
    items.unshift(card);
    await playAt(0);
  }
  if (historyBtn) historyBtn.onclick = (e) => { e.stopPropagation(); openHistory(); bumpChrome(); };
  // 查看面板（views.js）：站点卡片墙 + 工具目录
  if (viewBtn) {
    viewBtn.onclick = (e) => {
      e.stopPropagation();
      bumpChrome();
      if (!window.Views) {
        showToast('查看面板未加载');
        return;
      }
      // 只走一次 openBrowse：先 Views.open 再 openBrowse 会并发两次 loadBrowse，后者被静默丢弃
      const target = (sites.some((s) => s.id === site) ? site : null) || null;
      window.Views.openBrowse(target);
    };
  }
  if (window.Views) {
    // 查看面板点卡片 → 交给同一个播放路径（保持预热 / 熔断 / 历史行为一致）
    window.Views.setPlayHandler((card, list, idx, page) => {
      window.Views.close();
      if (Array.isArray(list) && list.length) {
        items = list.slice();
        index = Math.max(0, idx || 0);
        // 网格列表自带分页：沿用它的 cursor，并把浏览上下文交给 loadMore 续页
        cursor = (page && page.nextCursor) || null;
        mode = 'browse';
        browseCategory = (page && page.category) || null;
        site = (page && page.site) || card.site || site;
        renderChips();
      } else {
        const at = items.findIndex((x) => cardLocalKey(x) === cardLocalKey(card));
        if (at >= 0) index = at;
        else { items.unshift(card); index = 0; }
      }
      playAt(index);
    });
  }
  if (historyClose) historyClose.onclick = () => closeHistory();
  if (historyPanel) {
    historyPanel.addEventListener('click', (e) => {
      if (e.target === historyPanel) closeHistory();
    });
    historyPanel.querySelectorAll('[data-hist-tab]').forEach((btn) => {
      btn.onclick = () => {
        histTab = btn.getAttribute('data-hist-tab') || 'history';
        historyPanel.querySelectorAll('[data-hist-tab]').forEach((b) => {
          b.classList.toggle('active', b === btn);
        });
        renderHistoryList();
      };
    });
  }
  if (historyClear) {
    historyClear.onclick = () => {
      if (histTab === 'favorites') lsWrite(FAV_KEY, []);
      else lsWrite(HIST_KEY, []);
      renderHistoryList();
      showToast('已清空');
      if (items[index]) updateFavChrome(items[index]);
    };
  }

  // —— Site settings admin panel ——
  const settingsBtn = $('#settingsBtn');
  const settingsPanel = $('#settingsPanel');
  const settingsList = $('#settingsList');
  const settingsClose = $('#settingsClose');
  const settingsSave = $('#settingsSave');
  const settingsStatus = $('#settingsStatus');
  let settingsDraft = []; // {id, enabled, note, displayName, inRegistry, health}
  const circuitResetAll = $('#circuitResetAll');

  function openSettings() {
    if (!settingsPanel) return;
    settingsPanel.classList.remove('hidden');
    settingsPanel.setAttribute('aria-hidden', 'false');
    loadSettingsPanel();
  }
  function closeSettings() {
    if (!settingsPanel) return;
    settingsPanel.classList.add('hidden');
    settingsPanel.setAttribute('aria-hidden', 'true');
  }
  async function loadSettingsPanel() {
    if (!settingsList) return;
    settingsStatus.textContent = '加载中…';
    try {
      const data = await api('/api/site-settings');
      settingsDraft = (data.sites || []).map((s) => ({
        id: s.id,
        enabled: !!s.enabled,
        note: s.note || '',
        displayName: s.displayName || s.name || s.id,
        inRegistry: s.inRegistry !== false,
        health: s.health || null,
      }));
      renderSettingsList();
      renderRandomSrc();
      settingsStatus.textContent = data.updatedAt
        ? ('已同步 ' + String(data.updatedAt).replace('T', ' ').slice(0, 19))
        : '默认配置';
    } catch (e) {
      settingsStatus.textContent = '加载失败';
      showToast('设置加载失败');
    }
  }
  function renderSettingsList() {
    settingsList.replaceChildren();
    for (const row of settingsDraft) {
      const el = document.createElement('div');
      el.className = 'settings-row' + (row.enabled ? '' : ' off') + (row.inRegistry ? '' : ' settings-orphan');
      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'settings-toggle' + (row.enabled ? ' on' : '');
      toggle.setAttribute('aria-pressed', row.enabled ? 'true' : 'false');
      toggle.title = row.enabled ? '已启用' : '已关闭';
      toggle.onclick = () => {
        row.enabled = !row.enabled;
        renderSettingsList();
      };
      const meta = document.createElement('div');
      meta.className = 'meta';
      const name = document.createElement('div');
      name.className = 'name';
      const dot = document.createElement('span');
      const color = (row.health && row.health.color) || 'gray';
      dot.className = 'health-dot ' + color;
      dot.title = '健康: ' + color;
      name.appendChild(dot);
      name.appendChild(document.createTextNode(row.displayName));
      if (SLOW_SITES.has(row.id)) {
        const slow = document.createElement('span');
        slow.className = 'circuit-badge';
        slow.textContent = '慢';
        slow.title = '解析要跑一次无头 Chrome（10–20 秒）';
        name.appendChild(slow);
      }
      if (row.health && row.health.autoBroken) {
        const badge = document.createElement('span');
        badge.className = 'circuit-badge';
        const rem = row.health.coolDownRemainingMs
          ? Math.ceil(row.health.coolDownRemainingMs / 1000) + 's'
          : '';
        badge.textContent = '自动熔断' + (rem ? ' ' + rem : '');
        name.appendChild(badge);
      }
      const idEl = document.createElement('div');
      idEl.className = 'id';
      idEl.textContent = row.id + (row.inRegistry ? '' : ' · 未注册(仅记录)');
      meta.appendChild(name);
      meta.appendChild(idEl);
      if (row.health) {
        const hl = document.createElement('div');
        hl.className = 'health-line';
        const parts = [
          '状态 ' + (row.health.state || '?'),
          '分 ' + (row.health.healthScore != null ? row.health.healthScore : '-'),
          row.health.avgLatencyMs != null ? ('延迟 ' + row.health.avgLatencyMs + 'ms') : null,
          '✓' + (row.health.success || 0) + ' ✗' + (row.health.fail || 0),
        ].filter(Boolean);
        hl.textContent = parts.join(' · ');
        meta.appendChild(hl);
        if (row.health.autoBroken || row.health.state === 'open' || row.health.state === 'half-open') {
          const rb = document.createElement('button');
          rb.type = 'button';
          rb.className = 'chip circuit-reset-btn';
          rb.textContent = '解除熔断';
          rb.onclick = async (ev) => {
            ev.stopPropagation();
            try {
              await api('/api/circuit-breaker/reset', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ site: row.id }),
              });
              showToast('已解除 ' + row.id);
              loadSettingsPanel();
            } catch (_) {
              showToast('解除失败');
            }
          };
          meta.appendChild(rb);
        }
      }
      if (row.note) {
        const note = document.createElement('div');
        note.className = 'note';
        note.textContent = row.note;
        meta.appendChild(note);
      }
      el.appendChild(toggle);
      el.appendChild(meta);
      settingsList.appendChild(el);
    }
  }
  async function saveSettings() {
    if (!settingsSave) return;
    settingsSave.disabled = true;
    settingsStatus.textContent = '保存中…';
    try {
      const body = {
        sites: settingsDraft.map((s) => ({
          id: s.id,
          enabled: s.enabled,
          note: s.note || '',
        })),
      };
      const data = await api('/api/site-settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      settingsDraft = (data.sites || []).map((s) => ({
        id: s.id,
        enabled: !!s.enabled,
        note: s.note || '',
        displayName: s.displayName || s.name || s.id,
        inRegistry: s.inRegistry !== false,
        health: s.health || null,
      }));
      renderSettingsList();
      settingsStatus.textContent = '已保存';
      showToast('站点开关已保存');
      // Refresh chip list (disabled removed) and reload feed if current site gone
      await loadSites();
      if (!sites.some((s) => s.id === site)) {
        site = (sites[0] && sites[0].id) || 'demo';
        renderChips();
      }
      if (mode === 'single' || mode === 'category' || mode === 'random') resetAndLoad();
    } catch (e) {
      settingsStatus.textContent = '保存失败';
      showToast('保存失败');
    } finally {
      settingsSave.disabled = false;
    }
  }
  if (settingsBtn) settingsBtn.onclick = (e) => { e.stopPropagation(); openSettings(); bumpChrome(); };
  if (settingsClose) settingsClose.onclick = () => closeSettings();
  if (settingsSave) settingsSave.onclick = () => saveSettings();

  // —— 随机 / 刷流源选择（本机记住，只影响 random 混排） ——
  const RANDOM_SRC_KEY = 'swipefeed:randomSites';
  const randomSrcList = $('#randomSrcList');
  const randomSrcStatus = $('#randomSrcStatus');

  function randomSrcSelected() {
    try {
      const raw = JSON.parse(localStorage.getItem(RANDOM_SRC_KEY) || 'null');
      return Array.isArray(raw) ? raw.filter((x) => typeof x === 'string') : null;
    } catch (_) {
      return null;
    }
  }

  function saveRandomSrc(ids) {
    try { localStorage.setItem(RANDOM_SRC_KEY, JSON.stringify(ids)); } catch (_) {}
    renderRandomSrc();
  }

  /** 选出参与混排的源；null = 全部启用源（默认） */
  function activeRandomSites() {
    const enabled = (settingsDraft.length ? settingsDraft : sites.map((s) => ({ id: s.id, enabled: true })))
      .filter((r) => r.enabled)
      .map((r) => r.id);
    const picked = randomSrcSelected();
    if (!picked || !picked.length) return enabled;
    const keep = picked.filter((id) => enabled.includes(id));
    return keep.length ? keep : enabled;
  }

  function randomSitesQuery() {
    const enabled = (settingsDraft.length ? settingsDraft : sites.map((s) => ({ id: s.id, enabled: true })))
      .filter((r) => r.enabled)
      .map((r) => r.id);
    const picked = activeRandomSites();
    if (!picked.length || picked.length === enabled.length) return '';
    return '&sites=' + encodeURIComponent(picked.join(','));
  }

  function renderRandomSrc() {
    if (!randomSrcList) return;
    const rows = (settingsDraft.length ? settingsDraft : sites.map((s) => ({ id: s.id, displayName: s.displayName || s.name || s.id, enabled: true, health: null })))
      .filter((r) => r.enabled);
    const selected = new Set(activeRandomSites());
    randomSrcList.replaceChildren();
    for (const row of rows) {
      const on = selected.has(row.id);
      const color = (row.health && row.health.color) || '';
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'random-src' + (on ? ' on' : '') + (color === 'red' ? ' bad' : '');
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.title = `${row.id}${color ? ' · 健康 ' + color : ''}`;
      const tick = document.createElement('span');
      tick.className = 'tick';
      btn.appendChild(tick);
      btn.appendChild(document.createTextNode(row.displayName || row.id));
      btn.onclick = () => {
        const cur = new Set(activeRandomSites());
        if (cur.has(row.id)) cur.delete(row.id); else cur.add(row.id);
        // 全选状态回退成默认（不写 localStorage）
        if (cur.size === rows.length) {
          try { localStorage.removeItem(RANDOM_SRC_KEY); } catch (_) {}
          renderRandomSrc();
        } else {
          saveRandomSrc([...cur]);
        }
      };
      randomSrcList.appendChild(btn);
    }
    if (randomSrcStatus) {
      randomSrcStatus.textContent = `${selected.size}/${rows.length} 个源参与随机`;
    }
    const summary = document.getElementById('randomSrcSummary');
    if (summary) summary.textContent = `${selected.size}/${rows.length} 个源参与 · 点开勾选`;
    const block = document.getElementById('randomSrcBlock');
    if (block && !block.dataset.touched) block.open = false;
  }

  const randomSrcBlock = document.getElementById('randomSrcBlock');
  if (randomSrcBlock) {
    randomSrcBlock.addEventListener('toggle', () => { randomSrcBlock.dataset.touched = '1'; });
  }

  const randomSrcAllBtn = $('#randomSrcAll');
  const randomSrcNoneBtn = $('#randomSrcNone');
  const randomSrcOkBtn = $('#randomSrcOk');
  if (randomSrcAllBtn) randomSrcAllBtn.onclick = () => {
    try { localStorage.removeItem(RANDOM_SRC_KEY); } catch (_) {}
    renderRandomSrc();
  };
  if (randomSrcNoneBtn) randomSrcNoneBtn.onclick = () => {
    // 至少留一个，否则随机没得刷
    const rows = (settingsDraft.length ? settingsDraft : sites.map((s) => ({ id: s.id }))).filter((r) => r.enabled);
    const first = rows[0]?.id;
    saveRandomSrc(first ? [first] : []);
  };
  if (randomSrcOkBtn) randomSrcOkBtn.onclick = () => {
    const rows = (settingsDraft.length ? settingsDraft : []).filter((r) => r.enabled);
    const ok = rows.filter((r) => (r.health && r.health.color) === 'green').map((r) => r.id);
    saveRandomSrc(ok.length ? ok : rows.map((r) => r.id));
  };

  if (circuitResetAll) {
    circuitResetAll.onclick = async () => {
      try {
        await api('/api/circuit-breaker/reset', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ all: true }),
        });
        showToast('已清除全部熔断');
        loadSettingsPanel();
      } catch (_) {
        showToast('清除失败');
      }
    };
  }
  if (settingsPanel) {
    settingsPanel.addEventListener('click', (e) => {
      if (e.target === settingsPanel) closeSettings();
    });
  }

  bumpChrome();
  loadSites().then(resetAndLoad);
})();

