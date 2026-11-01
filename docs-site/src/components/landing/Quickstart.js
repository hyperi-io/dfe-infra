/*
 * Project:      dfe-infra
 * File:         docs-site/src/components/landing/Quickstart.js
 * Purpose:      The landing page's tabbed quickstart, laid out from copy/landing.yaml.
 * Language:     JavaScript
 *
 * License:      BUSL-1.1
 * Copyright:    (c) 2026 HYPERI PTY LIMITED
 */

import React from 'react';
import Link from '@docusaurus/Link';
import Tabs from '@theme/Tabs';
import TabItem from '@theme/TabItem';
import CodeBlock from '@theme/CodeBlock';
import Inline from './Inline';
import {IconArrow} from './icons';
import styles from './landing.module.css';

function Block({block}) {
  if (block.code !== undefined) {
    return (
      <CodeBlock language="bash" title="bash">
        {block.code}
      </CodeBlock>
    );
  }
  return (
    <p className={styles.quickstartLead}>
      <Inline text={block.text} />
    </p>
  );
}

export default function Quickstart({copy}) {
  return (
    <section className={styles.quickstart} aria-labelledby="quickstart-title">
      <p className={styles.eyebrow}>{copy.eyebrow}</p>
      <h2 id="quickstart-title" className={styles.quickstartTitle}>
        {copy.title}
      </h2>
      <Tabs groupId="dfe-quickstart" className={styles.quickstartTabs}>
        {copy.tabs.map((tab, index) => (
          <TabItem key={tab.value} value={tab.value} label={tab.label} default={index === 0}>
            {tab.blocks.map((block, position) => (
              <Block key={position} block={block} />
            ))}
            <Link className={styles.textLink} to={tab.guide.to}>
              {tab.guide.label}
              <IconArrow />
            </Link>
          </TabItem>
        ))}
      </Tabs>
    </section>
  );
}
