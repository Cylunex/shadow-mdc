export const CATEGORIES = [
  { id: 'asian', displayName: '亚洲' },
  { id: 'japanese', displayName: '日本' },
  { id: 'amateur', displayName: '业余' },
  { id: 'milf', displayName: 'MILF' },
  { id: 'lesbian', displayName: '女同' },
  { id: 'gay', displayName: '男同' },
];

/** 站点基础地址（分类探测要用它拼候选 URL）。 */
export const SITE_BASE = {
  pornhub: 'https://www.pornhub.com',
  xvideos: 'https://www.xvideos.com',
  xhamster: 'https://xhamster.com',
  xnxx: 'https://www.xnxx.com',
  spankbang: 'https://spankbang.com',
  eporner: 'https://www.eporner.com',
  youporn: 'https://www.youporn.com',
  beeg: 'https://beeg.com',
  youjizz: 'https://www.youjizz.com',
  redtube: 'https://www.redtube.com',
  tube8: 'https://www.tube8.com',
  txxx: 'https://txxx.com',
  drtuber: 'https://www.drtuber.com',
  tnaflix: 'https://www.tnaflix.com',
  empflix: 'https://www.empflix.com',
  porntube: 'https://www.porntube.com',
  thisvid: 'https://thisvid.com',
  hellporno: 'https://hellporno.com',
  hqporner: 'https://hqporner.com',
  porntrex: 'https://www.porntrex.com',
  threemovs: 'https://www.3movs.com',
  pornone: 'https://pornone.com',
  anysex: 'https://www.anysex.com',
  pornhat: 'https://www.pornhat.com',
  okxxx: 'https://ok.xxx',
  porngo: 'https://www.porngo.com',
  zbporn: 'https://zbporn.com',
  rule34video: 'https://rule34video.com',
  perfectgirls: 'https://www.perfectgirls.xxx',
  anyporn: 'https://anyporn.com',
  superporn: 'https://www.superporn.com',
  sexvid: 'https://www.sexvid.xxx',
  bigfuck: 'https://bigfuck.tv',
  porndoe: 'https://porndoe.com',
};

/**
 * 探测用候选 slug：都是各家站点真实存在的常见分类/标签词。
 * 只在“站点分类 API / 首页链接”都拿不到时才用，靠逐个请求校验。
 */
export const SLUG_CANDIDATES = [
  'asian', 'japanese', 'amateur', 'teen', 'milf', 'mature', 'lesbian', 'gay',
  'anal', 'big-tits', 'blonde', 'brunette', 'ebony', 'interracial', 'creampie',
  'threesome', 'bdsm', 'fetish', 'hentai', 'massage', 'voyeur', 'homemade',
];

/** 常见分类 URL 形状，按站点逐个试（{slug} 占位）。 */
export const CATEGORY_URL_PATTERNS = {
  thisvid: '/categories/{slug}/',
  porntrex: '/categories/{slug}/',
  porntop: '/categories/{slug}/',
  anysex: '/categories/{slug}/',
  okxxx: '/categories/{slug}/',
  '3movs': '/categories/{slug}/',
  zbporn: '/categories/{slug}/',
  rule34video: '/categories/{slug}/',
  hqporner: '/category/{slug}',
  sexvid: '/c/{slug}/',

  porngo: '/categories/{slug}/',
  porndoe: '/videos/category/{slug}',
  bigfuck: '/category/{slug}/',
  pornhub: '/video?c={slug}',
  xvideos: '/tags/{slug}',
  xhamster: '/categories/{slug}',
  xnxx: '/tags/{slug}',
  eporner: '/cat/{slug}/',      // eporner 的真实分类前缀是 /cat/，与 plugins/core_rest.js 一致
  hellporno: '/{slug}/',
  perfectgirls: '/{slug}/',
  redtube: '/redtube/{slug}',
  beeg: '/tag/{slug}',
  sunporno: '/channels/{slug}/',
  porntube: '/tags/{slug}',
  anyporn: '/{slug}/',
  superporn: '/{slug}',
  pornhat: '/{slug}/',
  peekvids: '/{slug}',
  tnaflix: '/{slug}',
  empflix: '/{slug}',
  '4tube': '/videos/{slug}',

  drtuber: '/tags/{slug}',
  txxx: '/categories/{slug}/',
};

export const CAT_PATHS = {  pornhub: {
    asian: 'c=7', japanese: 'c=111', amateur: 'c=3', milf: 'c=29', lesbian: 'c=27', gay: 'c=105',
  },
  xvideos: {
    asian: 'Asian', japanese: 'Japanese', amateur: 'Amateur', milf: 'MILF', lesbian: 'Lesbian', gay: 'Gay',
  },
  xhamster: {
    asian: 'asian', japanese: 'japanese', amateur: 'amateur', milf: 'milf', lesbian: 'lesbian', gay: 'gay',
  },
  xnxx: {
    asian: 'best/asian', japanese: 'search/japanese', amateur: 'best/amateur', milf: 'best/milf', lesbian: 'best/lesbian', gay: 'best/gay',
  },
  spankbang: {
    asian: 'asian', japanese: 'japanese', amateur: 'amateur', milf: 'milf', lesbian: 'lesbian', gay: 'gay',
  },
  eporner: {
    asian: 'asian', japanese: 'japanese', amateur: 'amateur', milf: 'milf', lesbian: 'lesbian', gay: 'gay',
  },
  youporn: {
    asian: 'asian', japanese: 'japanese', amateur: 'amateur', milf: 'milf', lesbian: 'lesbian', gay: 'gay',
  },
  beeg: {
    asian: 'asian', japanese: 'japanese', amateur: 'amateur', milf: 'milf', lesbian: 'lesbian', gay: 'gay',
  },
  youjizz: {
    asian: 'asian', japanese: 'japanese', amateur: 'amateur', milf: 'milf', lesbian: 'lesbian', gay: 'gay',
  },
  redtube: {
    asian: 'asian', japanese: 'japanese', amateur: 'amateur', milf: 'milf', lesbian: 'lesbian', gay: 'gay',
  },
  tube8: {
    asian: 'asian', japanese: 'japanese', amateur: 'amateur', milf: 'milf', lesbian: 'lesbian', gay: 'gay',
  },
};

/**
 * 每个站真实的分类 URL 形状（{slug} 占位）。
 * 手工实现的插件（pornhub / xvideos / xhamster …）用它来：
 *   · 告诉分类动态探测“该站分类长什么样”，探测出来的 slug 才是真的能出列表的；
 *   · 让任意发现到的 slug 都能走同一条 URL，而不是只认写死映射表里那几个词。
 *
 * 1.8.0 起与 `CATEGORY_URL_PATTERNS` 合一：之前两张表各写一份、形状还对不上
 * （eporner 写成 /category/、3movs 的 key 写成 threemovs），探测结果与过滤 URL
 * 会指向两个不同地址。现在只有一个真值来源。
 *
 * 没有登记形状的站（youjizz / porntube / anysex / peekvids …）交给动态探测决定，
 * 探测不到就不显示分类，而不是给一堆点了 404 的固定标签。
 */
export const CATEGORY_TEMPLATES = { ...CATEGORY_URL_PATTERNS };
