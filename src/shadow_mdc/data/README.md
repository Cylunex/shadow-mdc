# Bundled reference data

## actress_name_map.json.gz

Normalized actress alias → Japanese display name for GFriends matching and
identity alias expansion.

Built from (open-source peers; regenerated offline — not scraped at runtime):

- Javinizer `jvThumbs.csv` (MIT) — romaji FullName / alias ↔ JapaneseName
- JavSP `data/actress_alias.json` (GPL-3.0) — CJK / stage-name variants

Do not commit regenerated maps that pull private catalog data.
