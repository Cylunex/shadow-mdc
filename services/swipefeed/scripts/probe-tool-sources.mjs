/**
 * probe-tool-sources.mjs — 探 `tool\` 素材区对应站点源的可用性。
 *
 *   node scripts/probe-tool-sources.mjs
 *
 * 只打列表/分类/解析三项的事实结果，不写文件；用于排障与回归。
 */
import { pektino } from '../src/plugins/pektino.js';
import { threego } from '../src/plugins/threego.js';
import { haijiao } from '../src/plugins/haijiao.js';
import { videoshub } from '../src/plugins/videoshub.js';

const line = (label, value) => console.log(`${label.padEnd(22)} ${value}`);

async function probeSource(name, src, cursor = { page: 1 }) {
  console.log(`\n=== ${name} (${src.id}) ===`);
  let page;
  try {
    page = await src.nextPage(cursor);
    line('listing', `HTTP ${page._httpStatus ?? '?'} items=${(page.items || []).length} total=${page.total ?? '-'}`);
    if (page.items?.[0]) line('first card', JSON.stringify(page.items[0]).slice(0, 200));
  } catch (e) {
    line('listing', 'ERR ' + e.message);
    return;
  }
  if (typeof src.categories === 'function') {
    try {
      const cats = await src.categories();
      line('categories', `${cats.length} → ` + cats.slice(0, 5).map((c) => c.displayName || c.id).join(' / '));
    } catch (e) {
      line('categories', 'ERR ' + e.message);
    }
  }
  if (page.items?.length) {
    try {
      const s = await src.resolve(page.items[0]);
      line('resolve', `${s.type} ${String(s.url).slice(0, 110)}${s.preview ? ' (preview)' : ''}`);
    } catch (e) {
      line('resolve', 'ERR ' + e.message);
    }
  }
}

await probeSource('Pektino', pektino);
await probeSource('3GO', threego);
await probeSource('海角社区', haijiao, { page: 1, category: 'latest' });
await probeSource('VideosHub', videoshub);

// VideosHub 额外验一下清单签名（不下载整片）
try {
  const page = await videoshub.nextPage({ page: 1 });
  const card = page.items[0];
  const { url, preview } = await videoshub.resolvePlaylist(card.id, '720p');
  const res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0', Referer: preview } });
  const text = await res.text();
  const segs = text.split('\n').filter((l) => l && !l.startsWith('#')).length;
  line('playlist', `HTTP ${res.status} cors=${res.headers.get('access-control-allow-origin')} segments=${segs} key=${/METHOD=([A-Z0-9-]+)/.exec(text)?.[1] || 'none'}`);
  line('ffmpeg', videoshub.hasFfmpeg() ? 'available (mp4)' : 'missing (serves .ts)');
} catch (e) {
  line('playlist', 'ERR ' + e.message);
}
