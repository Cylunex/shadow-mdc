/**
 * Probe every source: nextPage listing + one resolve if cards > 0.
 * Prints JSON summary and writes TEST_RESULTS.md
 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { ALL_SOURCES, getSource } from '../src/plugins/registry.js';
import { detectBlockNotes } from '../src/util/http.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(__dirname, '..');

async function probeOne(src) {
  const row = {
    site: src.id,
    listingStatus: null,
    cards: 0,
    resolve: 'skip',
    notes: [],
    sampleId: null,
  };
  let page;
  try {
    page = await src.nextPage(src.id === 'xvideos' || src.id === 'beeg' ? { page: 0 } : { page: 1 });
    row.listingStatus = page._httpStatus ?? 200;
    row.cards = (page.items || []).length;
    if (page._raw) {
      row.notes.push(...detectBlockNotes(row.listingStatus, page._raw));
    }
    if (row.cards === 0) {
      row.notes.push('empty');
    } else {
      row.sampleId = page.items[0].id;
      row.sampleTitle = (page.items[0].title || '').slice(0, 60);
    }
  } catch (e) {
    row.listingStatus = e.status ?? 'error';
    row.notes.push(e.message || String(e));
    if (e.body) row.notes.push(...detectBlockNotes(e.status || 0, e.body));
    return row;
  }

  if (row.cards > 0) {
    const card = page.items[0];
    try {
      const stream = await src.resolve(card);
      if (stream?.url) {
        row.resolve = 'ok';
        row.streamType = stream.type;
        row.streamHost = (() => {
          try { return new URL(stream.url).host; } catch { return '?'; }
        })();
      } else {
        row.resolve = 'fail';
        row.notes.push('resolve-empty');
      }
    } catch (e) {
      row.resolve = 'fail';
      row.notes.push('resolve: ' + (e.message || String(e)).slice(0, 120));
    }
  }
  row.notes = [...new Set(row.notes.filter(Boolean))];
  return row;
}

async function main() {
  console.log('Probing', ALL_SOURCES.length, 'sources…');
  const results = [];
  // sequential to be polite / avoid burst blocks
  for (const src of ALL_SOURCES) {
    process.stdout.write(`  → ${src.id} … `);
    const row = await probeOne(src);
    results.push(row);
    console.log(`HTTP ${row.listingStatus} cards=${row.cards} resolve=${row.resolve}`);
  }

  const summary = {
    probedAt: new Date().toISOString(),
    total: results.length,
    withCards: results.filter((r) => r.cards > 0).map((r) => r.site),
    withoutCards: results.filter((r) => r.cards === 0).map((r) => r.site),
    resolveOk: results.filter((r) => r.resolve === 'ok').map((r) => r.site),
    results,
  };

  console.log('\n=== JSON SUMMARY ===');
  console.log(JSON.stringify(summary, null, 2));

  const md = [];
  md.push('# Swipefeed Web — TEST_RESULTS');
  md.push('');
  md.push(`Probed at: **${summary.probedAt}** (from this box, Mobile Chrome UA).`);
  md.push('');
  md.push('## Summary');
  md.push('');
  md.push(`- Sites with cards > 0: **${summary.withCards.length}** — ${summary.withCards.join(', ') || '(none)'}`);
  md.push(`- Sites with 0 cards: **${summary.withoutCards.length}** — ${summary.withoutCards.join(', ') || '(none)'}`);
  md.push(`- Resolve OK: **${summary.resolveOk.length}** — ${summary.resolveOk.join(', ') || '(none)'}`);
  md.push('');
  md.push('## Table');
  md.push('');
  md.push('| site | listing HTTP | cards | resolve | notes |');
  md.push('|------|-------------:|------:|---------|-------|');
  for (const r of results) {
    const notes = (r.notes || []).join('; ').replace(/\|/g, '/');
    md.push(`| ${r.site} | ${r.listingStatus} | ${r.cards} | ${r.resolve} | ${notes || '—'} |`);
  }
  md.push('');
  md.push('## Honesty note');
  md.push('');
  md.push('A site is only marked working when `cards > 0` from `nextPage` on this box.');
  md.push('Resolve is attempted only when listing returned cards.');
  md.push('Demo source uses public Big Buck Bunny / Google sample MP4s and should always work.');
  md.push('');

  const out = path.join(root, 'TEST_RESULTS.md');
  fs.writeFileSync(out, md.join('\n'));
  console.log('\nWrote', out);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
