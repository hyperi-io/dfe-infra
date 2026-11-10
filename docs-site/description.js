/*
 * Project:      dfe-infra
 * File:         docs-site/description.js
 * Purpose:      The meta description for a page that sets none: its first real
 *               paragraph, unwrapped and cut at a word to fit a search snippet.
 * Language:     JavaScript
 *
 * License:      BUSL-1.1
 * Copyright:    (c) 2026 HYPERI PTY LIMITED
 */

// Search engines show about 155-160 characters of a description.
const MAX_LENGTH = 160;
const MIN_WORDS = 6;
const FENCE = /^ {0,3}(`{3,}|~{3,})/;
// A field line at the top of a design page, which describes the page, not its subject.
const FIELD_LINE = /^\**(status|owner|owners|date|updated|last updated|author|authors|audience|scope)\**\s*:/i;
const SKIPPED = [/^#/, /^:::/, /^\|/, /^[-*+] /, /^\d+[.)] /, /^<!--/, /^</, /^!\[/];

function plain(text) {
  return text
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/<[^>]+>/g, '')
    // A tag split around another (<scr<b>ipt>) leaves a bracket after one pass.
    .replace(/[<>]/g, '')
    .replace(/`([^`]*)`/g, '$1')
    .replace(/(\*\*|__)(.*?)\1/g, '$2')
    .replace(/(^|\W)[*_]([^*_]+)[*_](?=\W|$)/g, '$1$2')
    .replace(/\s+/g, ' ')
    .trim();
}

function fit(text) {
  if (text.length <= MAX_LENGTH) {
    return text;
  }
  const cut = text.slice(0, MAX_LENGTH - 3);
  const space = cut.lastIndexOf(' ');
  return `${(space > 0 ? cut.slice(0, space) : cut).replace(/[\s,;:.-]+$/, '')}...`;
}

/**
 * The first paragraph of prose in a page's markdown body, as a meta description.
 *
 * Headings, code blocks, comments, admonitions, tables, lists, images and field
 * lines such as "Status: Adopted" are passed over, and so is a paragraph under six
 * words that does not end a sentence ("Operations", "March 2026").
 *
 * @param {string} body The markdown, front matter already removed.
 * @returns {string} The description, at most 160 characters, or '' when none fits.
 */
function describe(body) {
  let fence = '';
  let comment = false;
  let paragraph = [];
  const lines = `${body}\n`.split(/\r?\n/);
  for (const raw of lines) {
    const line = raw.trim();
    const opener = raw.match(FENCE);
    if (fence) {
      if (opener && opener[1][0] === fence[0] && opener[1].length >= fence.length) {
        fence = '';
      }
      continue;
    }
    if (comment) {
      comment = !line.includes('-->');
      continue;
    }
    if (opener) {
      fence = opener[1];
      paragraph = [];
      continue;
    }
    if (line.startsWith('<!--')) {
      comment = !line.includes('-->');
      continue;
    }
    const text = line.replace(/^>\s?/, '').trim();
    if (text === '') {
      const candidate = plain(paragraph.join(' '));
      const words = candidate.split(' ').filter(Boolean).length;
      if (words >= MIN_WORDS || (words > 0 && /[.!?]$/.test(candidate))) {
        return fit(candidate);
      }
      paragraph = [];
      continue;
    }
    if (paragraph.length === 0 && (SKIPPED.some((rule) => rule.test(text)) || FIELD_LINE.test(text))) {
      continue;
    }
    paragraph.push(text);
  }
  return '';
}

module.exports = {describe, MAX_LENGTH};
