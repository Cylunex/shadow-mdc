/**
 * 3go.fun（media.3go.fun）— MediaCMS 实例
 *
 * 来源：F:\Freedom\tool 的 NSFW 六合一脚本（模块 1/6）。
 * 脚本在浏览器里用 /api/v1/media/<token> 取原画直链；这里走站点公开的 MediaCMS REST 接口
 * （/api/v1/media、/api/v1/categories、/api/v1/search），服务端直接出列表与播放源。
 */
import { fetchText, unescape, clean } from '../util/http.js';

const HOSTS = ['https://media.3go.fun', 'https://tube.3go.fun'];
const PER_PAGE = 36;
const UA_HEADERS = { Accept: 'application/json' };

async function json(path, host = HOSTS[0]) {
  const url = host + path;
  const { status, body } = await fetchText(url, {
    referer: host + '/',
    desktop: true,
    headers: UA_HEADERS,
  });
  if (status >= 400) throw Object.assign(new Error(`HTTP ${status}`), { status, body });
  try {
    return { status, body, data: JSON.parse(body) };
  } catch {
    throw Object.assign(new Error('MediaCMS 返回非 JSON'), { status, raw: body });
  }
}

function secToDur(sec) {
  const n = Number(sec);
  if (!Number.isFinite(n) || n <= 0) return null;
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = Math.floor(n % 60);
  return h
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

/** 原画路径里带中文用户名，必须逐段百分号编码，否则 Node fetch 直接报 invalid URL。 */
function encodePath(u) {
  if (!u) return u;
  if (/^https?:\/\//i.test(u)) {
    try {
      const url = new URL(u);
      url.pathname = url.pathname.split('/').map((seg) => encodeURIComponent(decodeURIComponent(seg))).join('/');
      return url.toString();
    } catch {
      return u;
    }
  }
  const p = u.startsWith('/') ? u : `/${u}`;
  return p.split('/').map((seg) => (seg ? encodeURIComponent(seg) : '')).join('/');
}

function absMedia(u) {
  if (!u) return null;
  return /^https?:\/\//i.test(u) ? encodePath(u) : encodePath(`https://media.3go.fun${u}`);
}

function toCard(v) {
  const token = v.friendly_token;
  if (!token) return null;
  const thumb = v.thumbnail_url || null;
  return {
    site: 'threego',
    id: token,
    title: clean(v.title) || `Video ${token}`,
    cover: thumb ? absMedia(thumb) : null,
    duration: secToDur(v.duration),
    author: v.user || null,
    views: v.views || null,
    aspect: 'landscape',
    pageUrl: `https://media.3go.fun/view?m=${encodeURIComponent(token)}`,
  };
}

function pageOf(data, page) {
  const items = (data.results || []).map(toCard).filter(Boolean);
  const hasNext = !!data.next;
  return {
    items,
    nextCursor: hasNext ? { page: page + 1 } : null,
    hasMore: hasNext,
    total: data.count,
  };
}

export const threego = {
  id: 'threego',
  name: '3GO',
  displayName: '3GO 国产自拍',

  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    const { data, body, status } = await json(`/api/v1/media?page=${page}&ordering=-add_date`);
    return { ...pageOf(data, page), _httpStatus: status, _raw: body };
  },

  async search(q, cursor) {
    const page = cursor?.page ?? 1;
    const query = q.trim();
    const r = await json(`/api/v1/search?q=${encodeURIComponent(query)}&page=${page}`);
    // MediaCMS 搜索返回 results（含 media / playlists）
    const data = r.data;
    if (Array.isArray(data?.results)) return { ...pageOf(data, page), _httpStatus: r.status, _raw: r.body };
    // 退化为带 q 的 media 列表
    const alt = await json(`/api/v1/media?q=${encodeURIComponent(query)}&page=${page}`);
    return { ...pageOf(alt.data, page), _httpStatus: alt.status, _raw: alt.body };
  },

  async categoryPage(category, cursor) {
    const page = cursor?.page ?? 1;
    const slug = String(category || '').trim();
    if (!slug) return this.nextPage(cursor);
    const { data, body, status } = await json(
      `/api/v1/media?category=${encodeURIComponent(slug)}&page=${page}`,
    );
    return { ...pageOf(data, page), _httpStatus: status, _raw: body };
  },

  /** 分类列表（uid → 名称），查看页用它出筛选项。 */
  async categories() {
    try {
      const { data } = await json('/api/v1/categories');
      return (data || []).map((c) => ({
        id: c.title,
        uid: c.uid,
        displayName: c.title,
        count: c.media_count,
      }));
    } catch {
      return [];
    }
  },

  async resolve(card) {
    const token = card.id;
    const { data } = await json(`/api/v1/media/${encodeURIComponent(token)}`);
    const single = data?.encodings_info?.hls?.single_rate?.hls_file
      || data?.hls_info?.hls_file
      || data?.original_media_url
      || null;
    const encodings = Array.isArray(data?.encodings) ? data.encodings : [];
    const qualities = [];
    const main = absMedia(single);
    if (main) qualities.push({ label: /\.m3u8/i.test(main) ? 'hls' : '原画', url: main });
    for (const u of encodings) {
      if (typeof u === 'string' && /^https?:/.test(u)) {
        const abs = absMedia(u);
        if (abs && !qualities.some((q) => q.url === abs)) qualities.push({ label: 'enc', url: abs });
      }
    }
    const url = qualities.find((q) => q.label === '原画')?.url || qualities[0]?.url;
    if (!url) throw new Error('MediaCMS 未返回播放源: ' + token);
    return {
      url,
      type: /\.m3u8/i.test(url) ? 'hls' : 'mp4',
      qualities,
      cover: card.cover || absMedia(data?.thumbnail_url) || null,
      duration: secToDur(data?.duration) || card.duration || null,
      expiresHintMs: Date.now() + 45 * 60_000,
    };
  },
};
