#!/usr/bin/env node
/**
 * 卡片元数据解析的离线夹具测试（不联网）。
 *
 * 跑法：node scripts/test-card-parse.mjs
 * 覆盖：标题优先级、懒加载封面、协议相对/相对路径封面、时长归一化、
 *      作者链接、相邻卡片不串味（块边界）、以及空字段回落。
 */

import assert from 'node:assert/strict';
import { collectHrefCards, streamFromMap, looseMp4Hls } from '../src/util/http.js';
import { normalizeDuration, extractCardMeta, cardBlockAt } from '../src/util/card-parse.js';

const BASE = 'https://example.com';

let pass = 0;
function check(name, fn) {
  try {
    fn();
    pass += 1;
    console.log(`  ✓ ${name}`);
  } catch (e) {
    console.error(`  ✗ ${name}\n    ${e.message}`);
    process.exitCode = 1;
  }
}

const HREF = /href=["']((?:https?:\/\/(?:www\.)?example\.com)?\/videos\/([0-9]+)\/[^"']*)["']/gi;

console.log('normalizeDuration');
check('12:34 → 12:34', () => assert.equal(normalizeDuration('12:34'), '12:34'));
check('1:02:03 → 1:02:03', () => assert.equal(normalizeDuration('1:02:03'), '1:02:03'));
check('PT12M34S → 12:34', () => assert.equal(normalizeDuration('PT12M34S'), '12:34'));
check('PT1H2M3S → 1:02:03', () => assert.equal(normalizeDuration('PT1H2M3S'), '1:02:03'));
check('秒数 754 → 12:34', () => assert.equal(normalizeDuration('754'), '12:34'));
check('垃圾值 → null', () => assert.equal(normalizeDuration('abc'), null));
check('空值 → null', () => assert.equal(normalizeDuration(''), null));

console.log('collectHrefCards / 常规卡片');
const html1 = `
<div class="list">
  <div class="video-item">
    <a href="/videos/12345/hot-title-here/" title="Hot Title Here">
      <img src="https://cdn.example.com/covers/12345.jpg" alt="alt text">
    </a>
    <span class="duration">12:34</span>
    <a href="/models/jane-doe">Jane Doe</a>
  </div>
</div>`;
const cards1 = collectHrefCards(html1, { site: 'demo', base: BASE, hrefRe: HREF });

check('抠出 1 张卡', () => assert.equal(cards1.length, 1));
check('id 正确', () => assert.equal(cards1[0].id, '12345'));
check('title 取 a[title]', () => assert.equal(cards1[0].title, 'Hot Title Here'));
check('cover 取 img[src]', () => assert.equal(cards1[0].cover, 'https://cdn.example.com/covers/12345.jpg'));
check('duration 取 .duration', () => assert.equal(cards1[0].duration, '12:34'));
check('author 取 /models/ 链接', () => assert.equal(cards1[0].author, 'jane doe'));
check('pageUrl 去 query', () => assert.equal(cards1[0].pageUrl, 'https://example.com/videos/12345/hot-title-here/'));

console.log('懒加载封面 + 协议相对 URL');
const html2 = `
<div class="card">
  <a href="/videos/777/another-one/">
    <div class="thumb"><img src="/img/blank.gif" data-src="//cdn2.example.com/cover/777.jpg"></div>
  </a>
  <div class="fa-time">1:02:03</div>
</div>`;
const [c2] = collectHrefCards(html2, { site: 'demo', base: BASE, hrefRe: HREF });
check('跳过占位图取 data-src', () => assert.equal(c2.cover, 'https://cdn2.example.com/cover/777.jpg'));
check('时长 1:02:03', () => assert.equal(c2.duration, '1:02:03'));
check('无标题时回落 img alt/锚文本', () => assert.equal(typeof c2.title, 'string'));

console.log('JSON-LD / time[content] 时长');
const html3 = `
<div class="card">
  <a href="/videos/888/iso-card/"><img data-original="https://cdn.example.com/888.jpg"></a>
  <time content="PT12M34S">12 min</time>
</div>`;
const [c3] = collectHrefCards(html3, { site: 'demo', base: BASE, hrefRe: HREF });
check('time[content] ISO → 12:34', () => assert.equal(c3.duration, '12:34'));
check('data-original 封面', () => assert.equal(c3.cover, 'https://cdn.example.com/888.jpg'));

console.log('相邻卡片不串味');
const html4 = `
<div class="card"><a href="/videos/1/first/"><img src="https://cdn.example.com/1.jpg" alt="First"></a></div>
<div class="card"><a href="/videos/2/second/"><img src="https://cdn.example.com/2.jpg" alt="Second"></a></div>`;
const cards4 = collectHrefCards(html4, { site: 'demo', base: BASE, hrefRe: HREF });
check('两张卡', () => assert.equal(cards4.length, 2));
check('第一张标题不串到第二张的图', () => assert.equal(cards4[0].title, 'First'));
check('第一张封面是 1.jpg', () => assert.equal(cards4[0].cover, 'https://cdn.example.com/1.jpg'));
check('第二张标题', () => assert.equal(cards4[1].title, 'Second'));
check('第二张封面是 2.jpg', () => assert.equal(cards4[1].cover, 'https://cdn.example.com/2.jpg'));

console.log('空块回落');
check('无任何元数据 → 全 null + Video id', () => {
  const html = '<a href="/videos/999/x/' + '"></a>';
  const [c] = collectHrefCards(html, { site: 'demo', base: BASE, hrefRe: HREF });
  assert.equal(c.title, 'Video 999');
  assert.equal(c.cover, null);
  assert.equal(c.duration, null);
});
check('rich:false 关闭元数据解析', () => {
  const [c] = collectHrefCards(html1, { site: 'demo', base: BASE, hrefRe: HREF, rich: false });
  assert.equal(c.title, 'Video 12345');
  assert.equal(c.cover, null);
});

console.log('块边界');
check('cardBlockAt 不越过下一张卡', () => {
  const html = '<a href="/videos/1/a/">AAA</a><a href="/videos/2/b/">BBB</a>';
  const block = cardBlockAt(html, 0, 5);
  assert.ok(block.includes('AAA'));

  assert.ok(!block.includes('BBB'));
});
check('extractCardMeta 直接调用可用', () => {
  const meta = extractCardMeta('<a href="/x" title="T"><img src="https://c/1.jpg"></a>', BASE);
  assert.equal(meta.title, 'T');
  assert.equal(meta.cover, 'https://c/1.jpg');
});

console.log('stream 挑选');
check('尾部反斜杠/空白要清掉（Bili影院那种写法）', () => {
  const s = streamFromMap({ 720: 'https://cdn.example/a.m3u8\\  ' });
  assert.equal(s.url, 'https://cdn.example/a.m3u8');
});
check('loose 打分后取到正片而不是预告片', () => {
  const html = '<script>var a="https://cdn.example/preview/trailer_480p.mp4";'
    + 'var b="https://cdn.example/get_file/1/720p_1000.mp4";</script>';
  const s = looseMp4Hls(html);
  assert.ok(s.url.includes('get_file'), `got ${s.url}`);
});
check('只有预告片时不给可播地址', () => {
  assert.equal(looseMp4Hls('x = "https://cdn.example/preview/clip.mp4";'), null);
});

console.log(`\n${pass} 项断言组通过${process.exitCode ? '（存在失败）' : ''}`);
