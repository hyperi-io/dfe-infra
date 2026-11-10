/*
 * Project:      dfe-infra
 * File:         docs-site/src/components/landing/Screenshot.js
 * Purpose:      The hero's product screenshot, framed as a browser window, with an empty frame until the image exists.
 * Language:     JavaScript
 *
 * License:      BUSL-1.1
 * Copyright:    (c) 2026 HYPERI PTY LIMITED
 */

import React from 'react';
import useBaseUrl from '@docusaurus/useBaseUrl';
import styles from './landing.module.css';

// Set to a path under static/, e.g. '/img/console-search.png', once a console screenshot exists. Null draws the empty frame.
const SCREENSHOT = null;
const ALT = 'The DFE console, searching the main source in Observe';

export default function Screenshot() {
  const src = useBaseUrl(SCREENSHOT || '/');
  return (
    <figure className={styles.shot}>
      <div className={styles.shotBar} aria-hidden="true">
        <span className={styles.shotDot} />
        <span className={styles.shotDot} />
        <span className={styles.shotDot} />
        <span className={styles.shotAddress}>localhost:3000</span>
      </div>
      {SCREENSHOT ? (
        <img className={styles.shotImage} src={src} alt={ALT} width="1440" height="900" />
      ) : (
        <div className={styles.shotEmpty} role="img" aria-label="Console screenshot to come">
          <span className={styles.shotEmptyTitle}>Console screenshot</span>
          <span className={styles.shotEmptyText}>Observe, Search, on the main source</span>
        </div>
      )}
    </figure>
  );
}
