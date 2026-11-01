/*
 * Project:      dfe-infra
 * File:         docs-site/src/pages/index.js
 * Purpose:      The landing page of each docs build, laid out from copy/landing.yaml.
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

// copy/landing.yaml names each card's icon by these keys.
const ICONS = {
  ingest: IconIngest,
  routing: IconRouting,
  storage: IconStorage,
  hunting: IconHunting,
  observability: IconObservability,
  host: IconHost,
  cluster: IconCluster,
  cloud: IconCloud,
};

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

function Actions({actions}) {
  return (
    <div className={styles.actions}>
      {actions.map((action) => {
        const kind = action.primary ? styles.buttonPrimary : styles.buttonSecondary;
        return (
          <CardLink
            key={action.label}
            className={`${styles.button} ${kind}`}
            to={action.to}
            href={action.href}
          >
            {action.label}
            {action.href && <IconExternal />}
          </CardLink>
        );
      })}
    </div>
  );
}

function Hero({hero, quickstart}) {
  return (
    <section className={styles.hero} aria-labelledby="hero-title">
      <div className={`${styles.container} ${styles.heroGrid}`}>
        <div className={styles.heroCopy}>
          <p className={styles.eyebrow}>{hero.eyebrow}</p>
          <h1 id="hero-title" className={styles.heroTitle}>
            {hero.title} <span className={styles.heroAccent}>{hero.accent}</span>
          </h1>
          <p className={styles.tagline}>{hero.tagline}</p>
          <Actions actions={hero.actions} />
          <ul className={styles.badges} aria-label="Facts">
            {hero.badges.map((badge) => (
              <li key={badge.label}>
                <CardLink className={styles.badge} to={badge.to} href={badge.href}>
                  {badge.label}
                </CardLink>
              </li>
            ))}
          </ul>
        </div>
        <Quickstart copy={quickstart} />
      </div>
    </section>
  );
}

export default function Home() {
  const {siteConfig} = useDocusaurusContext();
  const {landing} = siteConfig.customFields;
  const {whats_new: news, capabilities, deploy, closing} = landing;
  return (
    <Layout description={landing.description}>
      <main>
        <Hero hero={landing.hero} quickstart={landing.quickstart} />
        <section className={styles.section} aria-labelledby="whats-new">
          <div className={styles.container}>
            <SectionHeader
              id="whats-new"
              eyebrow={news.eyebrow}
              title={news.title}
              link={
                <a className={styles.textLink} href={news.link.href}>
                  {news.link.label}
                  <IconExternal />
                </a>
              }
            />
            <ul className={styles.gridThree}>
              {news.cards.map((card) => (
                <li key={card.title}>
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
            <SectionHeader id="capabilities" eyebrow={capabilities.eyebrow} title={capabilities.title} />
            <ul className={styles.gridCapabilities}>
              {capabilities.cards.map((card) => {
                const Icon = ICONS[card.icon];
                return (
                  <li key={card.icon}>
                    <Link className={`${styles.card} ${styles.linkCard} ${styles.featureCard}`} to={card.to}>
                      <span className={styles.iconTile}>
                        <Icon />
                      </span>
                      <p className={styles.eyebrow}>{card.eyebrow}</p>
                      <h3 className={styles.cardTitle}>{card.title}</h3>
                      <p className={styles.cardText}>{card.text}</p>
                      <ul className={styles.chips} aria-label={`${card.eyebrow} topics`}>
                        {card.chips.map((chip) => (
                          <li key={chip} className={styles.chip}>
                            {chip}
                          </li>
                        ))}
                      </ul>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        </section>

        <section className={styles.section} aria-labelledby="deploy-paths">
          <div className={styles.container}>
            <SectionHeader id="deploy-paths" eyebrow={deploy.eyebrow} title={deploy.title} lead={deploy.lead} />
            <ul className={styles.gridThree}>
              {deploy.cards.map((card) => {
                const Icon = ICONS[card.icon];
                return (
                  <li key={card.icon}>
                    <Link className={`${styles.card} ${styles.linkCard}`} to={card.to}>
                      <span className={styles.iconTile}>
                        <Icon />
                      </span>
                      <p className={styles.eyebrow}>{card.eyebrow}</p>
                      <h3 className={styles.cardTitle}>{card.title}</h3>
                      <p className={styles.cardText}>{card.text}</p>
                      <span className={styles.cardAction}>
                        {deploy.action}
                        <IconArrow />
                      </span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        </section>

        <section className={styles.closing} aria-labelledby="closing">
          <div className={`${styles.container} ${styles.closingInner}`}>
            <h2 id="closing" className={styles.sectionTitle}>
              {closing.title}
            </h2>
            <p className={styles.sectionLead}>{closing.lead}</p>
            <Actions actions={closing.actions} />
          </div>
        </section>
      </main>
    </Layout>
  );
}
