/*
 * Project:      dfe-infra
 * File:         docs-site/src/components/landing/Quickstart.js
 * Purpose:      The landing page's tabbed quickstart: Docker, Kubernetes on-premises and AWS.
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
import {IconArrow} from './icons';
import styles from './landing.module.css';

// Each command is copied character for character from the guide named beside it, at this stack.

// dfe-docker docs/evaluating.md, "Getting it running".
function dockerCommands(stack) {
  return [
    'make init                  # create .env and env/, generate secrets',
    `make stack VERSION=${stack}   # pin every image from the DFE stack manifest`,
    'make ci                    # pull and start',
  ].join('\n');
}

// dfe-infra docs/deployment/on-prem.md, "Deploy", the integrated path.
const KUBERNETES = [
  '(umask 077 && cp bootstrap/local.env.example bootstrap/local.env)   # 0600 from creation; fill in creds',
  'bash bootstrap/deploy.sh --cloud local',
].join('\n');

// dfe-infra docs/deployment/aws.md, "Resolving sizing artefacts" and "Applying the plan".
const AWS_RESOLVE = [
  'python3 scripts/resolve_sizing.py --dial deployment.yaml --live --out terraform/environments/aws',
  'python3 scripts/render_dial.py --tofu',
].join('\n');
const AWS_APPLY = ['tofu init', 'tofu plan -out=deployment.tfplan', 'tofu apply deployment.tfplan'].join('\n');

function GuideLink({to, children}) {
  return (
    <Link className={styles.textLink} to={to}>
      {children}
      <IconArrow />
    </Link>
  );
}

export default function Quickstart({stack}) {
  return (
    <section className={styles.quickstart} aria-labelledby="quickstart-title">
      <p className={styles.eyebrow}>Quickstart</p>
      <h2 id="quickstart-title" className={styles.quickstartTitle}>
        Stand DFE up
      </h2>
      <Tabs groupId="dfe-quickstart" className={styles.quickstartTabs}>
        <TabItem value="docker" label="Docker" default>
          <p className={styles.quickstartLead}>
            One host, under Docker Compose. From a{' '}
            <a href="https://github.com/hyperi-io/dfe-docker">dfe-docker</a> checkout:
          </p>
          <CodeBlock language="bash" title="bash">{dockerCommands(stack)}</CodeBlock>
          <GuideLink to="/quickstart/docker/">Docker quickstart</GuideLink>
        </TabItem>
        <TabItem value="kubernetes" label="Kubernetes">
          <p className={styles.quickstartLead}>
            A cluster you run. From a{' '}
            <a href="https://github.com/hyperi-io/dfe-infra">dfe-infra</a> checkout, with a kubeconfig
            for the cluster:
          </p>
          <CodeBlock language="bash" title="bash">{KUBERNETES}</CodeBlock>
          <GuideLink to="/quickstart/kubernetes/">Kubernetes on-premises quickstart</GuideLink>
        </TabItem>
        <TabItem value="aws" label="AWS">
          <p className={styles.quickstartLead}>
            Amazon EKS. From a dfe-infra checkout, with <code>deployment.yaml</code> copied from{' '}
            <code>deployment.example.yaml</code>:
          </p>
          <CodeBlock language="bash" title="bash">{AWS_RESOLVE}</CodeBlock>
          <p className={styles.quickstartLead}>
            Then, from <code>terraform/environments/aws</code>:
          </p>
          <CodeBlock language="bash" title="bash">{AWS_APPLY}</CodeBlock>
          <GuideLink to="/quickstart/aws/">AWS quickstart</GuideLink>
        </TabItem>
      </Tabs>
    </section>
  );
}
