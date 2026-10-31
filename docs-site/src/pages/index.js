/*
 * Project:      dfe-infra
 * File:         docs-site/src/pages/index.js
 * Purpose:      The landing page of each docs build: what DFE is, how it works, how to stand it up, where to read next.
 * Language:     JavaScript
 *
 * License:      BUSL-1.1
 * Copyright:    (c) 2026 HYPERI PTY LIMITED
 */

import React from 'react';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Faq from '@site/src/components/landing/Faq';
import HowItWorks from '@site/src/components/landing/HowItWorks';
import Quickstart from '@site/src/components/landing/Quickstart';
import Screenshot from '@site/src/components/landing/Screenshot';
import {
  IconArrow,
  IconExternal,
  IconIngest,
  IconObservability,
  IconStorage,
} from '@site/src/components/landing/icons';
import styles from '@site/src/components/landing/landing.module.css';

// Every claim below is checkable on the page it links to.
const BADGES = [
  {label: 'BUSL-1.1 and Apache-2.0 source', href: 'https://github.com/hyperi-io'},
  {label: 'Kubernetes and Docker Compose', to: '/quickstart/'},
  {label: 'ClickHouse via Kafka or gRPC', to: '/overview/'},
];

// Three claims, each with a mechanism or a number the linked page backs.
const VALUES = [
  {
    key: 'ingest',
    Icon: IconIngest,
    title: '11 protocols, one receiver',
    text: 'dfe-receiver takes each agent in its own protocol and hands everything on as JSON. The source it came from decides where it lands.',
    chips: ['Beats', 'OTLP', 'Syslog', 'Splunk HEC', 'GELF', 'Webhooks'],
    to: '/dfe-receiver/architecture/',
  },
  {
    key: 'storage',
    Icon: IconStorage,
    title: 'Kafka or gRPC, into ClickHouse',
    text: 'Kafka when you want the buffer, with at-least-once delivery from it. Direct gRPC from receiver to loader when you do not. dfe-loader writes RowBinary batches either way.',
    chips: ['Kafka', 'gRPC', 'RowBinary'],
    to: '/dfe-loader/',
  },
  {
    key: 'checked',
    Icon: IconObservability,
    title: 'Two checks on every deploy',
    text: 'Every deploy ends by sending an event through to ClickHouse, then checking the stack\'s own logs and metrics reached HyperDX. Both run before you send anything.',
    chips: ['OpenTelemetry', 'HyperDX', '/livez /readyz /metrics'],
    to: '/dfe-docker/observability/',
  },
];

function whatsNew({newest, releaseNotes, apiPath}) {
  const cards = [];
  if (releaseNotes) {
    cards.push({
      key: 'release',
      eyebrow: 'Latest release',
      title: `DFE ${newest}`,
      text: 'What changed, from the dfe-infra release that certifies this stack.',
      action: 'Release notes',
      href: releaseNotes,
    });
  }
  cards.push({
    key: 'aws',
    eyebrow: 'Deploy',
    title: 'DFE on AWS',
    text: 'OpenTofu builds EKS on Graviton, with Kafka from Amazon MSK, Confluent Cloud or Redpanda Cloud. Then the same bootstrap as every other cluster.',
    action: 'Read the guide',
    to: '/dfe-infra/deployment/aws/',
  });
  if (apiPath) {
    cards.push({
      key: 'api',
      eyebrow: 'Reference',
      title: 'HTTP API reference',
      text: 'Every dfe-engine endpoint, generated from the OpenAPI spec this release pins.',
      action: 'Browse the API',
      to: apiPath,
    });
  }
  return cards;
}

function SectionHeader({id, eyebrow, title, lead, link}) {
  return (
    <div className={styles.sectionHeader}>
      <div>
        <p className={styles.eyebrow}>{eyebrow}</p>
        <h2 id={id} className={styles.sectionTitle}>
          {title}
        </h2>
        {lead && <p className={styles.sectionLead}>{lead}</p>}
      </div>
      {link}
    </div>
  );
}

function CardLink({to, href, className, children, ...rest}) {
  return href ? (
    <a className={className} href={href} {...rest}>
      {children}
    </a>
  ) : (
    <Link className={className} to={to} {...rest}>
      {children}
    </Link>
  );
}

function Hero({versionLabel}) {
  return (
    <section className={styles.hero} aria-labelledby="hero-title">
      <div className={`${styles.container} ${styles.heroGrid}`}>
        <div className={styles.heroCopy}>
          <p className={styles.eyebrow}>Data Fusion Engine {versionLabel}</p>
          <h1 id="hero-title" className={styles.heroTitle}>
            Every agent in. <span className={styles.heroAccent}>ClickHouse out.</span>
          </h1>
          <p className={styles.tagline}>
            A data pipeline you run yourself. It takes logs and events from your agents, routes them by source, lands
            them in ClickHouse and runs your SQL detections on a schedule.
          </p>
          <div className={styles.actions}>
            <Link className={`${styles.button} ${styles.buttonPrimary}`} to="/quickstart/docker/">
              Get started
            </Link>
            <Link className={`${styles.button} ${styles.buttonSecondary}`} to="/overview/">
              Read the docs
            </Link>
          </div>
          <ul className={styles.badges} aria-label="Facts">
            {BADGES.map((badge) => (
              <li key={badge.label}>
                <CardLink className={styles.badge} to={badge.to} href={badge.href}>
                  {badge.label}
                </CardLink>
              </li>
            ))}
          </ul>
        </div>
        <Screenshot />
      </div>
    </section>
  );
}

export default function Home() {
  const {siteConfig} = useDocusaurusContext();
  const {stack, versionLabel, newest, releaseNotes, apiPath} = siteConfig.customFields;
  const news = whatsNew({newest, releaseNotes, apiPath});
  return (
    <Layout description="The Data Fusion Engine documentation: quickstarts, deployment guides and references for every suite repository.">
      <main>
        <Hero versionLabel={versionLabel} />

        <section className={styles.section} aria-labelledby="values">
          <div className={styles.container}>
            <SectionHeader id="values" eyebrow="Why DFE" title="One pipeline, agent to table" />
            <ul className={styles.gridThree}>
              {VALUES.map(({key, Icon, title, text, chips, to}) => (
                <li key={key}>
                  <Link className={`${styles.card} ${styles.linkCard}`} to={to}>
                    <span className={styles.iconTile}>
                      <Icon />
                    </span>
                    <h3 className={styles.cardTitle}>{title}</h3>
                    <p className={styles.cardText}>{text}</p>
                    <ul className={styles.chips} aria-label={`${title} topics`}>
                      {chips.map((chip) => (
                        <li key={chip} className={styles.chip}>
                          {chip}
                        </li>
                      ))}
                    </ul>
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section className={`${styles.section} ${styles.sectionRaised}`} aria-labelledby="how-it-works">
          <div className={styles.container}>
            <SectionHeader
              id="how-it-works"
              eyebrow="How it works"
              title="Agent to table"
              lead="The receiver takes it in, Kafka or gRPC carries it, the loader writes it. The engine and the console sit beside the path, not in it."
              link={
                <Link className={styles.textLink} to="/overview/">
                  The architecture
                  <IconArrow />
                </Link>
              }
            />
            <HowItWorks />
          </div>
        </section>

        <section className={styles.section} aria-label="Quickstart">
          <div className={`${styles.container} ${styles.quickstartWrap}`}>
            <Quickstart stack={stack} />
          </div>
        </section>

        <section className={`${styles.section} ${styles.sectionRaised}`} aria-labelledby="whats-new">
          <div className={styles.container}>
            <SectionHeader
              id="whats-new"
              eyebrow="Updates"
              title="What's new"
              link={
                <a className={styles.textLink} href="https://github.com/hyperi-io/dfe-infra/releases">
                  All releases
                  <IconExternal />
                </a>
              }
            />
            <ul className={styles.gridThree}>
              {news.map((card) => (
                <li key={card.key}>
                  <CardLink className={`${styles.card} ${styles.linkCard}`} to={card.to} href={card.href}>
                    <p className={styles.eyebrow}>{card.eyebrow}</p>
                    <h3 className={styles.cardTitle}>{card.title}</h3>
                    <p className={styles.cardText}>{card.text}</p>
                    <span className={styles.cardAction}>
                      {card.action}
                      {card.href ? <IconExternal /> : <IconArrow />}
                    </span>
                  </CardLink>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <section className={styles.section} aria-labelledby="faq">
          <div className={styles.container}>
            <SectionHeader id="faq" eyebrow="FAQ" title="Questions" />
            <Faq />
          </div>
        </section>

        <section className={styles.closing} aria-labelledby="closing">
          <div className={`${styles.container} ${styles.closingInner}`}>
            <h2 id="closing" className={styles.sectionTitle}>
              Start with the demo
            </h2>
            <p className={styles.sectionLead}>
              One host with Docker. Live firewall syslog, parsed into one ClickHouse table per vendor.
            </p>
            <div className={styles.actions}>
              <Link className={`${styles.button} ${styles.buttonPrimary}`} to="/quickstart/demo/">
                Run the demo
              </Link>
              <a className={`${styles.button} ${styles.buttonSecondary}`} href="https://github.com/hyperi-io">
                Source on GitHub
                <IconExternal />
              </a>
            </div>
          </div>
        </section>
      </main>
    </Layout>
  );
}
