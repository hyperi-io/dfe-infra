/*
 * Project:      dfe-infra
 * File:         docs-site/src/components/landing/icons.js
 * Purpose:      The landing page's line icons: 24-unit grid, currentColor stroke, 1em square.
 * Language:     JavaScript
 *
 * License:      BUSL-1.1
 * Copyright:    (c) 2026 HYPERI PTY LIMITED
 */

import React from 'react';

function Icon({children, label}) {
  const a11y = label ? {role: 'img', 'aria-label': label} : {'aria-hidden': true};
  return (
    <svg
      viewBox="0 0 24 24"
      width="1em"
      height="1em"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      focusable="false"
      {...a11y}>
      {children}
    </svg>
  );
}

export function IconArrow(props) {
  return (
    <Icon {...props}>
      <path d="M5 12h14" />
      <path d="M13 6l6 6-6 6" />
    </Icon>
  );
}

export function IconExternal(props) {
  return (
    <Icon {...props}>
      <path d="M14 4h6v6" />
      <path d="M20 4l-9 9" />
      <path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5" />
    </Icon>
  );
}

export function IconIngest(props) {
  return (
    <Icon {...props}>
      <path d="M3 12h11" />
      <path d="M10 8l4 4-4 4" />
      <path d="M15 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4" />
    </Icon>
  );
}

export function IconStorage(props) {
  return (
    <Icon {...props}>
      <ellipse cx="12" cy="5" rx="7" ry="3" />
      <path d="M5 5v14c0 1.7 3.1 3 7 3s7-1.3 7-3V5" />
      <path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3" />
    </Icon>
  );
}

export function IconObservability(props) {
  return (
    <Icon {...props}>
      <path d="M3 12h4l3-7 4 14 3-7h4" />
    </Icon>
  );
}

