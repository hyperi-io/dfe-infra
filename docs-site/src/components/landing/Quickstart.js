/*
 * Project:      dfe-infra
 * File:         docs-site/src/components/landing/Quickstart.js
 * Purpose:      The landing page's tabbed quickstart: the demo, Docker, Kubernetes on-premises and AWS.
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

// Each command must match the source named beside it, and the matching page under content-root/quickstart/.

// dfe-docker Makefile targets init, stack, up and demo. The profile names are service_profiles.yaml's.
function dockerCommands(stack, profile) {
  return [
    'git clone https://github.com/hyperi-io/dfe-docker && cd dfe-docker',
    'make init',
    `make stack VERSION=${stack}`,
    `echo 'DFE_PROFILE=${profile}' >> .env`,
    'make up',
  ];
}

const demoCommands = (stack) => [...dockerCommands(stack, 'single-kafka'), 'make demo'].join('\n');

// dfe-docker docs/evaluating.md, "Seeing your own data go through".
const DOCKER_SEND = [
  'curl -X POST http://localhost:8080/ingest \\',
  "  -H 'Content-Type: application/json' \\",
  `  -d '{"_source":"main","message":"hello dfe"}'`,
].join('\n');

// bootstrap/local.env.example and scripts/dfe-ops preflight, stack-deploy.
function kubernetesCommands(stack) {
  return [
    `git clone --branch ${stack} https://github.com/hyperi-io/dfe-infra && cd dfe-infra`,
    '(umask 077 && cp bootstrap/local.env.example bootstrap/local.env)',
    'python3 scripts/dfe-ops preflight --mode single --env-file bootstrap/local.env',
    `python3 scripts/dfe-ops stack-deploy --stack ${stack} --mode single \\`,
    `  --target-revision ${stack} --env-file bootstrap/local.env`,
  ].join('\n');
}

// dfe-infra docs/deployment/wizard.md, then docs/deployment/aws.md "Resolving sizing artefacts".
const AWS_DIAL = [
  'python3 scripts/dfe-ops init',
  'python3 scripts/resolve_sizing.py --dial deployment.yaml --live \\',
  '  --out terraform/environments/aws',
  'python3 scripts/render_dial.py --tofu',
].join('\n');

// dfe-infra docs/deployment/aws.md, "Applying the plan".
function awsApply(stack) {
  return [
    '(cd terraform/environments/aws && tofu init \\',
    '  && tofu plan -out=deployment.tfplan && tofu apply deployment.tfplan)',
    'eval "$(tofu -chdir=terraform/environments/aws output -raw kubeconfig_command)"',
    `python3 scripts/dfe-ops stack-deploy --stack ${stack} --mode scale \\`,
    '  --from-terraform terraform/environments/aws',
  ].join('\n');
}

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
        <TabItem value="demo" label="Demo" default>
          <p className={styles.quickstartLead}>
            Live firewall syslog, parsed per vendor, on one host. You need Docker Compose v2, Python 3 and{' '}
            <a href="https://docs.astral.sh/uv/">uv</a>.
          </p>
          <CodeBlock language="bash" title="bash">{demoCommands(stack)}</CodeBlock>
          <p className={styles.quickstartLead}>
            Cisco ASA and FortiGate lines stream in at 20 a second. Each is matched to its vendor, parsed, and lands in{' '}
            <code>dfe.cisco-asa</code> or <code>dfe.fortinet</code>. <code>make demo-stop</code> ends it.
          </p>
          <GuideLink to="/quickstart/demo/">The demo</GuideLink>
        </TabItem>
        <TabItem value="docker" label="Docker">
          <p className={styles.quickstartLead}>
            One host, your own data. <code>slim-grpc</code> is the core path over gRPC, the least that moves data.{' '}
            <code>single-kafka</code> adds Kafka and every per-source app.
          </p>
          <CodeBlock language="bash" title="bash">{dockerCommands(stack, 'slim-grpc').join('\n')}</CodeBlock>
          <p className={styles.quickstartLead}>
            <code>make up</code> ends on a self test and prints the console login. Then send it an event. It lands in{' '}
            <code>dfe.main</code> a couple of seconds later:
          </p>
          <CodeBlock language="bash" title="bash">{DOCKER_SEND}</CodeBlock>
          <GuideLink to="/quickstart/docker/">Docker quickstart</GuideLink>
        </TabItem>
        <TabItem value="kubernetes" label="Kubernetes">
          <p className={styles.quickstartLead}>
            A cluster you run, Kubernetes 1.35 or later, with <code>kubectl</code> pointed at it. Set{' '}
            <code>DFE_DOMAIN</code> and <code>DFE_REPO_URL</code> in the env file before the preflight:
          </p>
          <CodeBlock language="bash" title="bash">{kubernetesCommands(stack)}</CodeBlock>
          <p className={styles.quickstartLead}>
            It finishes by sending an event through to ClickHouse and checking the stack's own telemetry landed.
          </p>
          <GuideLink to="/quickstart/kubernetes/">Kubernetes on-premises quickstart</GuideLink>
        </TabItem>
        <TabItem value="aws" label="AWS">
          <p className={styles.quickstartLead}>
            Amazon EKS on Graviton. From a dfe-infra checkout at <code>{stack}</code>, answer the wizard: aws, the{' '}
            <code>scale</code> tier, and Kafka from MSK, Confluent Cloud or Redpanda Cloud.
          </p>
          <CodeBlock language="bash" title="bash">{AWS_DIAL}</CodeBlock>
          <p className={styles.quickstartLead}>Then build the cluster and deploy onto it:</p>
          <CodeBlock language="bash" title="bash">{awsApply(stack)}</CodeBlock>
          <GuideLink to="/quickstart/aws/">AWS quickstart</GuideLink>
        </TabItem>
      </Tabs>
    </section>
  );
}
