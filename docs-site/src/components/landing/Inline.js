/*
 * Project:      dfe-infra
 * File:         docs-site/src/components/landing/Inline.js
 * Purpose:      Render one line of copy/ text, turning `code` and [text](link) into elements.
 * Language:     JavaScript
 *
 * License:      BUSL-1.1
 * Copyright:    (c) 2026 HYPERI PTY LIMITED
 */

import React from 'react';
import Link from '@docusaurus/Link';

const MARKUP = /`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)/g;

export default function Inline({text}) {
  const parts = [];
  let last = 0;
  for (const match of text.matchAll(MARKUP)) {
    if (match.index > last) {
      parts.push(text.slice(last, match.index));
    }
    const [, code, label, target] = match;
    const key = match.index;
    if (code !== undefined) {
      parts.push(<code key={key}>{code}</code>);
    } else if (target.startsWith('/')) {
      parts.push(<Link key={key} to={target}>{label}</Link>);
    } else {
      parts.push(<a key={key} href={target}>{label}</a>);
    }
    last = match.index + match[0].length;
  }
  parts.push(text.slice(last));
  return <>{parts}</>;
}
