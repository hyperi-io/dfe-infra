/*
 * Project:      dfe-infra
 * File:         docs-site/src/components/landing/HowItWorks.js
 * Purpose:      The landing page's data path diagram: agents to ClickHouse, with the engine and console beside it.
 * Language:     JavaScript
 *
 * License:      BUSL-1.1
 * Copyright:    (c) 2026 HYPERI PTY LIMITED
 */

import React from 'react';
import Mermaid from '@theme/Mermaid';
import styles from './landing.module.css';

// Nodes and edges follow the default deploy path in content-root/README.md. The classDefs are the house ones from the hyperi-ai diagrams standard.
const CHART = `flowchart LR
    agents["Your agents<br/>Beats, OTel, syslog, HTTP"]
    receiver["dfe-receiver"]
    kafka[("Kafka")]
    loader["dfe-loader"]
    ch[("ClickHouse")]
    engine["dfe-engine<br/>sources, rules, hunts"]
    console["Console<br/>with HyperDX search"]

    agents --> receiver
    receiver --> kafka --> loader
    receiver -- "or direct gRPC" --> loader
    loader --> ch
    ch -- "search" --> console
    engine -.-> receiver
    engine -.-> loader
    engine <-.- console

    classDef store fill:#2EA4F6,stroke:#2EA4F6,stroke-width:2px,color:#000647
    classDef ctrl fill:#000647,stroke:#2EA4F6,stroke-width:3px,color:#FFFFFF
    class kafka,ch store
    class engine ctrl
`;

export default function HowItWorks() {
  return (
    <figure className={styles.diagram}>
      <div className={styles.diagramCanvas}>
        <Mermaid value={CHART} />
      </div>
      <figcaption className={styles.diagramCaption}>
        Solid lines carry data. Dotted lines carry configuration. Blue cylinders are the stores DFE writes and reads.
      </figcaption>
    </figure>
  );
}
