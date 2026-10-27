/**
 * 含羞草（hanxiucao）—— 实验性源：**必须带你自己浏览器的登录态**（Auth token）。
 *
 * 协议来自 tool 目录新脚本 `hanxiucao-pure-unlock2.user.txt`：
 *   · 请求体 `{ endata, ents }`：
 *       endata = base64(AES-256-CBC(JSON(参数), key=固定串, iv=key[0:16]))
 *       ents   = base64(AES-256-CBC(String(本地UTC秒), 同上))
 *   · 头：`Did` / `source: 1` / `isShortChain: ''`，登录后还要 `Auth: <token>`
 *   · 响应是明文 JSON；`/videos/getUrl` 的 `data.url|preUrl` 就是播放地址
 *
 * 为什么不能“跳过登录”：站上没有游客/设备登录接口（试过 8 个常见路径，404 或域名 TLS 重置），
 * 列表接口在无 `Auth` 时恒返回 `{"count":0,"list":[]}`；脚本本身也是在已登录页面里改写响应。
 *
 * 用法：查看页 →「会话」面板，host 填 API 域名（如 a64d.vd9h4.com），
 *       headers 里加 `Auth: <浏览器 localStorage 里的 token>`，再在设置里打开本源。
 */
import crypto from 'node:crypto';
import { fetchText } from '../util/http.js';
import { sessionFor } from '../util/session-store.js';

const KEY = Buffer.from('B77A9FF7F323B5404902102257503C2F', 'utf8');
const IV = KEY.subarray(0, 16);
const REF = 'https://www.hanxiucao.com/';

/** 轮换 API 域名：优先会话里登记的域名。 */
const API_BASES = [
  'https://a64d.vd9h4.com',
  'https://a59e.f3de7.com',
];

function enc(plain) {
  const c = crypto.createCipheriv('aes-256-cbc', KEY, IV);
  return Buffer.concat([c.update(String(plain), 'utf8'), c.final()]).toString('base64');
}

function utcTs() {
  const d = new Date();
  return Math.floor(d.getTime() / 1000) + d.getTimezoneOffset() * 60;
}

function encryptBody(obj) {
  const clean = Object.fromEntries(Object.entries(obj || {}).filter(([, v]) => v !== '' && v != null));
  return JSON.stringify({ endata: enc(JSON.stringify(clean)), ents: enc(String(utcTs())) });
}

/** 从会话里找 Auth token（headers.Auth 或 storage 里 token/auth 键）。 */
function authFor(base) {
  let host = '';
  try { host = new URL(base).host; } catch { return null; }
  const s = sessionFor(host);
  if (!s) return null;
  const h = s.headers || {};
  const direct = h.Auth || h.auth || h.Authorization;
  if (direct) return String(direct).replace(/^Bearer\s+/i, '');
  for (const row of s.storage || []) {
    if (/token|auth/i.test(row.key) && row.value && row.value.length < 600) {
      return String(row.value).replace(/^"|"$/g, '');
    }
  }
  return null;
}

function candidateBases() {
  const list = [];
  const env = String(process.env.HXC_API_BASE || '').trim();
  if (env) list.push(env.replace(/\/$/, ''));
  list.push(...API_BASES);
  return [...new Set(list)];
}

async function post(base, path, body, token) {
  const headers = {
    accept: 'application/json, text/plain, */*',
    'content-type': 'application/json;charset=UTF-8;',
    Did: '1',
    source: '1',
    isShortChain: '',
    Auth: token,
  };
  const { status, body: text } = await fetchText(base + path, {
    method: 'POST',
    referer: REF,
    headers,
    body: encryptBody(body),
    timeout: 20000,
  });
  if (status >= 400) throw Object.assign(new Error(`hanxiucao HTTP ${status}`), { status });
  return JSON.parse(text);
}

function toCard(v) {
  const id = String(v.id ?? v.videoId ?? v.vid ?? '');
  if (!id) return null;
  const cover = v.cover || v.img || v.image || v.thumb || v.pic || null;
  const title = String(v.title || v.name || v.desc || `含羞草 ${id}`).trim();
  return {
    site: 'hanxiucao',
    id,
    title,
    cover: cover && /^https?:\/\//i.test(cover) ? cover : null,
    duration: v.duration ? `${Math.floor(Number(v.duration) / 60)}:${String(Math.floor(Number(v.duration) % 60)).padStart(2, '0')}` : null,
    author: v.author || v.nickname || null,
    pageUrl: `${REF}play/video/${id}`,
    aspect: v.width && v.height && v.height > v.width ? 'portrait' : 'unknown',
    extra: { vip: !!(v.isVip || v.isBuy) },
  };
}

async function listOnce(page, token) {
  // 各站点版本对参数名不一致，按可能性依次试，命中即返回
  const shapes = [
    { page, size: 24, cid: 0 },
    { page, size: 24 },
    { page, pageSize: 24, type: 0 },
    { page, limit: 24 },
  ];
  for (const base of candidateBases()) {
    for (const shape of shapes) {
      try {
        const json = await post(base, '/videos/getList', shape, token);
        const rows = json?.data?.list || json?.data?.rows || [];
        if (Array.isArray(rows) && rows.length) {
          return { base, items: rows.map(toCard).filter(Boolean) };
        }
      } catch { /* 换下一个形状/域名 */ }
    }
  }
  return null;
}

export const hanxiucao = {
  id: 'hanxiucao',
  name: 'Hanxiucao',
  displayName: '含羞草（需登录态）',
  async nextPage(cursor) {
    const page = Math.max(1, cursor?.page ?? 1);
    const token = candidateBases().map(authFor).find(Boolean);
    if (!token) {
      throw new Error('含羞草需要登录态：请在「会话」面板为 API 域名导入 Auth token 后再开本源');
    }
    const got = await listOnce(page, token);
    if (!got) return { items: [], nextCursor: null, hasMore: false };
    return {
      items: got.items,
      nextCursor: got.items.length ? { page: page + 1 } : null,
      hasMore: got.items.length > 0,
    };
  },
  async search(q, cursor) {
    // 站点没有公开搜索：拉一页后本地过滤标题
    const page = await this.nextPage(cursor);
    const key = String(q || '').trim().toLowerCase();
    if (!key) return page;
    return { ...page, items: page.items.filter((c) => c.title.toLowerCase().includes(key)) };
  },
  async resolve(card) {
    const token = candidateBases().map(authFor).find(Boolean);
    if (!token) throw new Error('含羞草需要登录态（Auth token）才能取播放地址');
    for (const base of candidateBases()) {
      for (const path of ['/videos/v2/getUrl', '/videos/getUrl']) {
        try {
          const json = await post(base, path, { videoId: Number(card.id) || card.id, id: card.id }, token);
          const d = json?.data || {};
          const url = d.url || d.info?.url || d.preUrl || d.info?.preUrl || '';
          if (url && /^https?:\/\//i.test(url)) {
            return {
              url,
              type: /\.m3u8/i.test(url) ? 'hls' : 'mp4',
              qualities: [{ label: '默认', url }],
              referer: card.pageUrl,
            };
          }
        } catch { /* 换下一个 */ }
      }
    }
    throw new Error('含羞草未取到播放地址（token 失效或该片需要更高权限）');
  },
};
