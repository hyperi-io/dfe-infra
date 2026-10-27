/*
 * Project:      dfe-infra
 * File:         docs-site/src/pages/index.js
 * Purpose:      The landing page of each docs build: what DFE is, how to stand it up, where to read next.
 * Language:     JavaScript
 *
 * License:      BUSL-1.1
 * Copyright:    (c) 2026 HYPERI PTY LIMITED
 */

import React from 'react';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import useDocusaurusContext from '@docusaurus/useDocusaurusContext';
import Quickstart from '@site/src/components/landing/Quickstart';
import {
  IconArrow,
  IconCloud,
  IconCluster,
  IconExternal,
  IconHost,
  IconHunting,
  IconIngest,
  IconObservability,
  IconRouting,
  IconStorage,
} from '@site/src/components/landing/icons';
import styles from '@site/src/components/landing/landing.module.css';

// Every claim below is checkable on the page it links to.
const BADGES = [
  {label: 'BUSL-1.1 and Apache-2.0 source', href: 'https://github.com/hyperi-io'},
  {label: 'Kubernetes and Docker Compose', to: '/quickstart/'},
  {label: 'ClickHouse via Kafka or gRPC', to: '/overview/'},
];

const CAPABILITIES = [
  {
    key: 'ingest',
    Icon: IconIngest,
    eyebrow: 'Ingest',
    title: 'Every agent, one edge',
    text: 'dfe-receiver takes each agent in its own protocol and hands everything on as JSON.',
    chips: ['Elastic Beats', 'OpenTelemetry', 'Syslog', 'Webhooks'],
    to: '/dfe-receiver/architecture/',
  },
  {
    key: 'routing',
    Icon: IconRouting,
    eyebrow: 'Routing',
    title: 'Define a source once',
    text: 'Routing, schema and detections hang off the source. The receiver, the loader and the hunt runner all read that one definition.',
    chips: ['Sources', 'Kafka topics', 'Schemas'],
    to: '/dfe-engine/data-plane/source/',
  },
  {
    key: 'storage',
    Icon: IconStorage,
    eyebrow: 'Storage',
    title: 'Into ClickHouse',
    text: 'dfe-loader batches into ClickHouse as RowBinary. Delivery from Kafka is at-least-once.',
    chips: ['ClickHouse', 'RowBinary', 'Dead-letter queue'],
    to: '/dfe-loader/',
  },
  {
    key: 'hunting',
    Icon: IconHunting,
    eyebrow: 'Hunting',
    title: 'Rules and scheduled hunts',
    text: 'A rule is a SQL detection on a source. A hunt runs rules on a schedule and writes what matches to the alerts table.',
    chips: ['SQL rules', 'Hunts', 'Alerts'],
    to: '/dfe-engine/data-plane/source-detections/',
  },
  {
    key: 'observability',
    Icon: IconObservability,
    eyebrow: 'Observability',
    title: 'The stack reports on itself',
    text: 'Every service answers health and metrics probes. The stack ships its own logs and metrics over OpenTelemetry, so HyperDX shows its health before your first event.',
    chips: ['OpenTelemetry', 'HyperDX', '/livez /readyz /metrics'],
    to: '/dfe-docker/observability/',
  },
];

const DEPLOY_PATHS = [
  {
    key: 'docker',
    Icon: IconHost,
    eyebrow: 'dfe-docker',
    title: 'Docker Compose',
    text: 'One host. A laptop, a demo box or a proof of concept.',
    to: '/quickstart/docker/',
  },
  {
    key: 'kubernetes',
    Icon: IconCluster,
    eyebrow: 'dfe-infra',
    title: 'Kubernetes on-premises',
    text: 'Your cluster, Kubernetes 1.35 or later. RKE2 is the reference distribution. The charts need no particular one.',
    to: '/quickstart/kubernetes/',
  },
  {
    key: 'aws',
    Icon: IconCloud,
    eyebrow: 'dfe-infra',
    title: 'Amazon EKS',
    text: 'OpenTofu builds it on Graviton. Kafka comes from Amazon MSK, Confluent Cloud or Redpanda Cloud.',
    to: '/quickstart/aws/',
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

function Hero({stack, versionLabel}) {
  return (
    <section className={styles.hero} aria-labelledby="hero-title">
      <div className={`${styles.container} ${styles.heroGrid}`}>
        <div className={styles.heroCopy}>
          <p className={styles.eyebrow}>DFE {versionLabel} documentation</p>
          <h1 id="hero-title" className={styles.heroTitle}>
            Data Fusion <span className={styles.heroAccent}>Engine</span>
          </h1>
          <p className={styles.tagline}>
            A data pipeline you run yourself. Every agent in, ClickHouse out.
          </p>
          <div className={styles.actions}>
            <Link className={`${styles.button} ${styles.buttonPrimary}`} to="/quickstart/">
              Get started
            </Link>
            <Link className={`${styles.button} ${styles.buttonSecondary}`} to="/dfe-infra/deployment/">
              Install
            </Link>
            <Link className={`${styles.button} ${styles.buttonSecondary}`} to="/overview/">
              Docs
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
        <Quickstart stack={stack} />
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
        <Hero stack={stack} versionLabel={versionLabel} />
        <section className={styles.section} aria-labelledby="whats-new">
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

        <section className={`${styles.section} ${styles.sectionRaised}`} aria-labelledby="capabilities">
          <div className={styles.container}>
            <SectionHeader
              id="capabilities"
              eyebrow="Capabilities"
              title="What the suite does"
            />
            <ul className={styles.gridCapabilities}>
              {CAPABILITIES.map(({key, Icon, eyebrow, title, text, chips, to}) => (
                <li key={key}>
                  <Link className={`${styles.card} ${styles.linkCard} ${styles.featureCard}`} to={to}>
                    <span className={styles.iconTile}>
                      <Icon />
                    </span>
                    <p className={styles.eyebrow}>{eyebrow}</p>
                    <h3 className={styles.cardTitle}>{title}</h3>
                    <p className={styles.cardText}>{text}</p>
                    <ul className={styles.chips} aria-label={`${eyebrow} topics`}>
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

        <section className={styles.section} aria-labelledby="deploy-paths">
          <div className={styles.container}>
            <SectionHeader
              id="deploy-paths"
              eyebrow="Deploy"
              title="Where it runs"
              lead="Docker on one host. Kubernetes when it has to scale, on your own cluster or on EKS."
            />
            <ul className={styles.gridThree}>
              {DEPLOY_PATHS.map(({key, Icon, eyebrow, title, text, to}) => (
                <li key={key}>
                  <Link className={`${styles.card} ${styles.linkCard}`} to={to}>
                    <span className={styles.iconTile}>
                      <Icon />
                    </span>
                    <p className={styles.eyebrow}>{eyebrow}</p>
                    <h3 className={styles.cardTitle}>{title}</h3>
                    <p className={styles.cardText}>{text}</p>
                    <span className={styles.cardAction}>
                      Quickstart
                      <IconArrow />
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
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
