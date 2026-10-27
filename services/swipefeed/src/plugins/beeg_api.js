/**
 * Beeg —— 站点是 Vue SPA，静态 HTML 里没有列表也没有播放地址。
 * 协议（从 bundle + 实际抓包得到）：
 *   列表：GET https://store.externulls.com/facts/tag?slug=<tag>&limit=24&offset=N
 *   文件：GET https://store.externulls.com/video/play_url/<fileId>
 *         → "<key=...,end=...,limit=10>/data=<hash>/media=hls4A/multi=<levels>"
 *   播放：https://video.beeg.com/<上面整段>/_TPL_/<fileId>.mp4.m3u8
 *        （key 是按路径签名的，路径必须逐字一致；子清单在 .../media=hls4A/<quality>/<fileId>.mp4.m3u8）
 */
import { fetchText } from '../util/http.js';
import { capturePage } from '../util/browser-fetch.js';

const STORE = 'https://store.externulls.com';
const CDN = 'https://video.beeg.com';
const REF = 'https://beeg.com/';

/** 默认按这组 tag 轮换，翻页时换下一个。 */
const TAGS = ['amateur', 'teen', 'milf', 'anal', 'big-tits', 'asian', 'lesbian', 'bbw'];

function pickTag(page) {
  return TAGS[(Math.max(1, page) - 1) % TAGS.length];
}

function dur(sec) {
  const total = Math.round(Number(sec) || 0);
  if (!total) return null;
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function toCard(fact) {
  const file = fact?.file || {};
  const id = String(file.id ?? '');
  if (!id) return null;
  const titleRow = (file.data || []).find((d) => d && d.cd_column === 'sf_name');
  const tags = (fact.tags || []).map((t) => t?.tg_name).filter(Boolean);
  return {
    site: 'beeg',
    id,
    title: titleRow?.cd_value || tags[0] || `Beeg ${id}`,
    cover: null,
    duration: dur(file.fl_duration),
    author: tags[0] || null,
    pageUrl: `https://beeg.com/${id}`,
    aspect: file.fl_width && file.fl_height
      ? (file.fl_height > file.fl_width ? 'portrait' : 'landscape')
      : 'unknown',
    extra: { w: file.fl_width || 0, h: file.fl_height || 0 },
  };
}

async function getJson(url) {
  const { status, body } = await fetchText(url, {
    referer: REF,
    headers: { Accept: 'application/json' },
  });
  if (status >= 400) throw Object.assign(new Error(`beeg HTTP ${status}`), { status });
  return JSON.parse(body);
}

export const beegApi = {
  id: 'beeg', name: 'Beeg', displayName: 'Beeg',
  async nextPage(cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const slug = pickTag(page);
    const offset = (page - 1) * 24;
    const arr = await getJson(`${STORE}/facts/tag?slug=${encodeURIComponent(slug)}&limit=24&offset=${offset}`);
    const items = (Array.isArray(arr) ? arr : []).map(toCard).filter(Boolean);
    return {
      items,
      nextCursor: items.length ? { page: page + 1 } : null,
      hasMore: items.length > 0,
    };
  },
  async search(q, cursor) {
    // 没有公开搜索接口：用 tag 近似匹配后本地过滤
    const page = Math.max(1, cursor?.page ?? 1);
    const wanted = String(q || '').trim().toLowerCase();
    if (!wanted) return { items: [], nextCursor: null, hasMore: false };
    const slug = TAGS.find((t) => t.includes(wanted) || wanted.includes(t)) || TAGS[0];
    const arr = await getJson(`${STORE}/facts/tag?slug=${encodeURIComponent(slug)}&limit=24&offset=0`);
    const items = (Array.isArray(arr) ? arr : []).map(toCard).filter(Boolean)
      .filter((c) => c.title.toLowerCase().includes(wanted));
    return { items, nextCursor: null, hasMore: false };
  },
  async categoryPage(cat, cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const slug = String(cat || '').trim() || TAGS[0];
    const offset = (page - 1) * 24;
    const arr = await getJson(`${STORE}/facts/tag?slug=${encodeURIComponent(slug)}&limit=24&offset=${offset}`);
    const items = (Array.isArray(arr) ? arr : []).map(toCard).filter(Boolean);
    return { items, nextCursor: items.length ? { page: page + 1 } : null, hasMore: items.length > 0 };
  },
  async resolve(card) {
    // master 清单的 key 是按完整路径签名的，只有播放器自己那一份能对上，
    // 所以直接用 CDP 打开详情页，拿它请求到的主清单（约 7 秒，有缓存）。
    const pageUrl = card.pageUrl && /^https?:/i.test(card.pageUrl)
      ? card.pageUrl
      : `https://beeg.com/${card.id}`;
    const shot = await capturePage(pageUrl, { timeoutMs: 30000, waitMs: 14000 });
    const masters = (shot?.media || []).filter((u) => /\.m3u8(\?|$)/i.test(u));
    const master = masters.find((u) => /_TPL_/i.test(u)) || masters[0];
    if (master) {
      return {
        url: master,
        type: 'hls',
        qualities: [{ label: '自适应', url: master }],
        referer: REF,
        expiresHintMs: Date.now() + 15 * 60_000,
      };
    }
    // 兜底：直接按 play_url 拼（能取到就用，取不到就明确报错）
    const play = await fetchText(`${STORE}/video/play_url/${encodeURIComponent(card.id)}`, {
      referer: REF,
      headers: { Accept: 'application/json' },
    });
    if (play.status >= 400 || !play.body) throw new Error(`beeg play_url HTTP ${play.status}`);
    const token = String(play.body).trim();
    const masterUrl = `${CDN}/${token}/_TPL_/${card.id}.mp4.m3u8`;
    const check = await fetchText(masterUrl, { referer: REF, headers: { Accept: '*/*' } });
    if (check.status >= 400) throw new Error(`beeg master HTTP ${check.status}`);
    return {
      url: masterUrl,
      type: 'hls',
      qualities: [{ label: '自适应', url: masterUrl }],
      referer: REF,
      expiresHintMs: Date.now() + 15 * 60_000,
    };
  },
};
