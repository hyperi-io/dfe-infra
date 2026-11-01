/*
 * Project:      dfe-infra
 * File:         docs-site/src/components/landing/Faq.js
 * Purpose:      The landing page's short FAQ, one question a row, each answer pointing at the page that backs it.
 * Language:     JavaScript
 *
 * License:      BUSL-1.1
 * Copyright:    (c) 2026 HYPERI PTY LIMITED
 */

import React from 'react';
import Link from '@docusaurus/Link';
import styles from './landing.module.css';

const QUESTIONS = [
  {
    key: 'what',
    q: 'What is DFE?',
    a: 'A data pipeline you run yourself. Agents send to dfe-receiver, dfe-loader writes to ClickHouse, and dfe-engine holds the sources, rules and hunts that decide where each event lands and what to look for in it.',
    link: {to: '/overview/', label: 'Overview'},
  },
  {
    key: 'agents',
    q: 'What can send to it?',
    a: 'Anything that speaks one of the receiver\'s 11 protocols: JSON over HTTP, gRPC, OTLP, Beats, Splunk HEC, syslog over UDP, TCP or TLS, GELF, Fluent Forward, Prometheus remote write, webhooks, and NetFlow and sFlow (experimental).',
    link: {to: '/dfe-receiver/architecture/', label: 'dfe-receiver'},
  },
  {
    key: 'kafka',
    q: 'Do I need Kafka?',
    a: 'No. The leaner profiles send from the receiver to the loader over direct gRPC. Kafka goes in the middle when you want a buffer, with at-least-once delivery from it.',
    link: {to: '/dfe-docker/profiles/', label: 'Profiles'},
  },
  {
    key: 'where',
    q: 'Where does it run?',
    a: 'Docker Compose on one host. Kubernetes 1.35 or later on a cluster you run, with RKE2 the reference distribution. Amazon EKS on Graviton, built by OpenTofu.',
    link: {to: '/quickstart/', label: 'Quickstarts'},
  },
  {
    key: 'source',
    q: 'Can I read the source?',
    a: 'Yes. Every DFE repository is public on GitHub, and each one\'s LICENSE file names its licence.',
    link: {href: 'https://github.com/hyperi-io', label: 'hyperi-io on GitHub'},
  },
];

export default function Faq() {
  return (
    <div className={styles.faq}>
      {QUESTIONS.map(({key, q, a, link}) => (
        <details key={key} className={styles.faqItem}>
          <summary className={styles.faqQuestion}>{q}</summary>
          <div className={styles.faqAnswer}>
            <p>{a}</p>
            {link.href ? (
              <a className={styles.textLink} href={link.href}>
                {link.label}
              </a>
            ) : (
              <Link className={styles.textLink} to={link.to}>
                {link.label}
              </Link>
            )}
          </div>
        </details>
      ))}
    </div>
  );
}
