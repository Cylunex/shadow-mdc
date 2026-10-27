// 演示源：只用本机网络实测能取到字节的样片（Google 那个 bucket 在这里是 403，
// 之前首页一直卡在「加载中…」就是因为它：代理 403 → 播放失败 → 自动下一条 → 循环）。
const CATALOG = [
  {
    id: 'flower',
    title: 'Flower（MDN 样片）',
    author: 'MDN CC0',
    duration: '0:07',
    cover: null,
    url: 'https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4',
    aspect: 'horizontal',
  },
  {
    id: 'bbb-360',
    title: 'Big Buck Bunny 360p',
    author: 'Blender Foundation',
    duration: '0:10',
    cover: null,
    url: 'https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4',
    aspect: 'horizontal',
  },
  {
    id: 'sintel',
    title: 'Sintel Trailer',
    author: 'Blender Foundation',
    duration: '0:52',
    cover: 'https://media.w3.org/2010/05/sintel/poster.png',
    url: 'https://media.w3.org/2010/05/sintel/trailer.mp4',
    aspect: 'horizontal',
  },
];

function toCard(item) {
  return {
    site: 'demo',
    id: item.id,
    title: item.title,
    cover: item.cover,
    duration: item.duration,
    author: item.author,
    aspect: item.aspect,
    pageUrl: item.url,
  };
}

export const demo = {
  id: 'demo',
  name: 'Demo',
  displayName: 'Demo',
  async nextPage(cursor) {
    const page = cursor?.page ?? 1;
    if (page > 1) return { items: [], nextCursor: null, hasMore: false };
    return { items: CATALOG.map(toCard), nextCursor: null, hasMore: false };
  },
  async search(q, cursor) {
    if ((cursor?.page ?? 1) > 1) return { items: [], nextCursor: null, hasMore: false };
    const query = (q || '').trim().toLowerCase();
    const items = CATALOG.filter(
      (i) => !query || i.title.toLowerCase().includes(query) || i.author.toLowerCase().includes(query),
    ).map(toCard);
    return { items, nextCursor: null, hasMore: false };
  },
  async categoryPage() {
    return this.nextPage();
  },
  async resolve(card) {
    const item = CATALOG.find((c) => c.id === card.id);
    if (!item) throw new Error('Unknown demo id: ' + card.id);
    return {
      url: item.url,
      type: 'mp4',
      qualities: [{ label: '默认', url: item.url }],
    };
  },
};
