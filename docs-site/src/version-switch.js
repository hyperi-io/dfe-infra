/*
 * Project:      dfe-infra
 * File:         docs-site/src/version-switch.js
 * Purpose:      Keep the reader on the same page when the version menu switches
 *               builds, falling back to the target version's landing page.
 * Language:     JavaScript
 *
 * License:      BUSL-1.1
 * Copyright:    (c) 2026 HYPERI PTY LIMITED
 */

import siteConfig from '@generated/docusaurus.config';

const VERSION_LINK = 'a.dfe-version-link';

async function switchVersion(event) {
  const link = event.target instanceof Element ? event.target.closest(VERSION_LINK) : null;
  if (!link) {
    return;
  }
  event.preventDefault();
  const targetRoot = new URL(link.getAttribute('href'), window.location.origin).pathname;
  const here = window.location.pathname;
  const page = here.startsWith(siteConfig.baseUrl) ? here.slice(siteConfig.baseUrl.length) : '';
  let destination = targetRoot;
  if (page) {
    try {
      const response = await fetch(targetRoot + page, {method: 'HEAD'});
      if (response.ok) {
        destination = targetRoot + page + window.location.hash;
      }
    } catch {
      destination = targetRoot;
    }
  }
  window.location.assign(destination);
}

if (typeof document !== 'undefined') {
  document.addEventListener('click', switchVersion, true);
}
