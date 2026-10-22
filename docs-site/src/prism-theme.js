/*
 * Project:      dfe-infra
 * File:         docs-site/src/prism-theme.js
 * Purpose:      One syntax theme for both colour modes, coloured from the brand roles in custom.css.
 * Language:     JavaScript
 *
 * License:      BUSL-1.1
 * Copyright:    (c) 2026 HYPERI PTY LIMITED
 */

// Prism writes these as inline styles, so each var() follows the mode and any navy band.
module.exports = {
  plain: {color: 'var(--role-body)', backgroundColor: 'var(--role-raised)'},
  styles: [
    {types: ['comment', 'prolog', 'doctype', 'cdata'], style: {color: 'var(--role-muted)', fontStyle: 'italic'}},
    {types: ['punctuation', 'operator'], style: {color: 'var(--role-muted)'}},
    {
      types: ['keyword', 'builtin', 'function', 'class-name', 'tag', 'selector', 'atrule', 'important'],
      style: {color: 'var(--role-link)'},
    },
    {types: ['string', 'char', 'attr-value', 'regex', 'url', 'inserted'], style: {color: 'var(--role-accent-ink)'}},
    {types: ['number', 'boolean', 'constant', 'symbol', 'variable', 'property', 'attr-name'], style: {color: 'var(--role-ink)'}},
    {types: ['deleted'], style: {color: 'var(--role-muted)', textDecorationLine: 'line-through'}},
  ],
};
