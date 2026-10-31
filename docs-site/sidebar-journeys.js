/*
 * Project:      dfe-infra
 * File:         docs-site/sidebar-journeys.js
 * Purpose:      Regroup the generated sidebar into the reader journeys journeys.yaml
 *               defines, leaving every page at its own URL.
 * Language:     JavaScript
 *
 * License:      BUSL-1.1
 * Copyright:    (c) 2026 HYPERI PTY LIMITED
 */

// Every page in tree order. A category's own page comes first, and the generated API
// category stays whole, keyed by its directory.
function flatten(items, out = []) {
  for (const item of items) {
    if (item.type === 'doc') {
      out.push({key: item.id, item});
    } else if (item.type === 'category' && item.customProps && item.customProps.apiDir) {
      out.push({key: `${item.customProps.apiDir}/`, item});
    } else if (item.type === 'category') {
      if (item.link && item.link.type === 'doc') {
        out.push({key: item.link.id, item: {type: 'doc', id: item.link.id}});
      }
      flatten(item.items, out);
    } else {
      out.push({key: '', item});
    }
  }
  return out;
}

// The longest placement key that is the doc id, or a directory prefix of it. Case is
// ignored, so one lowercase key places both the renamed page and the upper-case file a
// frozen stack tag still carries.
function placeOf(docId, placements) {
  const key = docId.toLowerCase();
  let best = null;
  for (const candidate of Object.keys(placements)) {
    const folded = candidate.toLowerCase();
    const fits = folded === key || (folded.endsWith('/') && key.startsWith(folded)) || folded === '';
    if (fits && (best === null || folded.length > best.folded.length)) {
      best = {candidate, folded};
    }
  }
  if (best === null) {
    throw new Error(`journeys.yaml places no page at ${docId}`);
  }
  return placements[best.candidate];
}

/**
 * The sidebar as top-level pages, then each journey with its non-empty sections.
 *
 * @param {object[]} items The generated sidebar, API slice already spliced in.
 * @param {{label: string, sections: string[]}[]} journeys The journeys, in order.
 * @param {Object<string, string>} placements Doc id or prefix -> "Journey / Section".
 * @returns {object[]} The sidebar items.
 */
function journeySidebar(items, journeys, placements) {
  const top = [];
  const bySection = new Map();
  for (const {key, item} of flatten(items)) {
    const place = placeOf(key, placements);
    if (place === '') {
      top.push(item);
    } else {
      bySection.set(place, [...(bySection.get(place) || []), item]);
    }
  }
  const journeyItems = journeys
    .map(({label, sections}) => ({
      type: 'category',
      label,
      collapsed: false,
      items: sections
        .filter((section) => bySection.has(`${label} / ${section}`))
        .map((section) => ({
          type: 'category',
          label: section,
          collapsed: true,
          items: bySection.get(`${label} / ${section}`),
        })),
    }))
    .filter((journey) => journey.items.length > 0);
  return [...top, ...journeyItems];
}

module.exports = {journeySidebar, placeOf};
