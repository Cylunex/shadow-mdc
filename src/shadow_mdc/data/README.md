# Bundled reference data

## actress_name_map.json.gz

Normalized actress alias → Japanese display name for GFriends matching and
identity alias expansion.

Built from (open-source peers; regenerated offline — not scraped at runtime):

- Javinizer `jvThumbs.csv` (MIT) — romaji FullName / alias ↔ JapaneseName
- JavSP `data/actress_alias.json` (GPL-3.0) — CJK / stage-name variants

Do not commit regenerated maps that pull private catalog data.

## dmm_content_id_prefixes.json.gz

Series letters → known DMM content-id prefixes (e.g. `nps` → `h_021`, `abf` → `118`),
used by `dmm_ids.content_id_candidates` to rank FANZA lookups. Generated offline from
javinizer-go `internal/scraper/dmm/content_id_prefixes.go` (MIT); ~24k series. Pure
public catalog structure, no private data.

## Vocabulary seed data

`tag-dictionary.seed.json` contains the public first-run vocabulary, aliases, hidden-label rules,
filter priorities and featured-category order. `classifications.seed.json` contains the initial
seven dimensions and their terms. These files replace Python vocabulary literals.

The tag seed is imported once into the runtime data directory; classification terms are seeded
into their existing database table without overwriting edited rows. Maintain real library changes
through the web/skill APIs, never by editing these defaults or committing runtime dictionaries.
