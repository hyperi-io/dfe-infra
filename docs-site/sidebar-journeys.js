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

// The longest placement key that is the doc id, or a directory prefix of it.
function placeOf(key, placements) {
  let best = null;
  for (const [candidate, place] of Object.entries(placements)) {
    const fits = candidate === key || (candidate.endsWith('/') && key.startsWith(candidate)) || candidate === '';
    if (fits && (best === null || candidate.length > best.length)) {
      best = candidate;
    }
  }
  if (best === null) {
    throw new Error(`journeys.yaml places no page at ${key}`);
  }
  return placements[best];
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

module.exports = {journeySidebar};
