/*
 * Project:      dfe-infra
 * File:         docs-site/docusaurus.config.js
 * Purpose:      One build of the DFE docs site, chosen by DFE_DOCS_BUILD from the
 *               builds scripts/aggregate.py lists in content/site.json.
 * Language:     JavaScript
 *
 * License:      BUSL-1.1
 * Copyright:    (c) 2026 HYPERI PTY LIMITED
 */

const fs = require('node:fs');
const path = require('node:path');
const {journeySidebar} = require('./sidebar-journeys.js');

const CONTENT = path.join(__dirname, 'content');
const SITE_FILE = path.join(CONTENT, 'site.json');

if (!fs.existsSync(SITE_FILE)) {
  throw new Error(`${SITE_FILE} is missing: run scripts/aggregate.py first`);
}
const site = JSON.parse(fs.readFileSync(SITE_FILE, 'utf8'));
const buildName = process.env.DFE_DOCS_BUILD || site.newest || site.builds[0].name;
const build = site.builds.find((candidate) => candidate.name === buildName);
if (!build) {
  throw new Error(`content/site.json lists no build named ${buildName}`);
}
const docsPath = `content/${build.name}`;
// The stack a reader installs from this build: its own release, or the newest one for /next/.
const stack = build.listed ? build.name : site.newest || build.name;
const versionLabel = build.listed ? build.name : `${build.name} (unreleased)`;
// release.yml cuts each stack as a dfe-infra tag, with its notes on the GitHub release.
const releaseNotes = site.newest
  ? `https://github.com/hyperi-io/dfe-infra/releases/tag/${site.newest}`
  : null;
const GRAPHICS = 'https://graphics.hyperi.io';
const ARROW_SVG =
  '<svg viewBox="0 0 24 24" width="1em" height="1em" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14"/><path d="M13 6l6 6-6 6"/></svg>';

// gen-api-docs names the spec's introduction page after its title, lowercased and hyphenated.
function apiIntroPath(openapi) {
  if (!openapi) {
    return null;
  }
  const spec = JSON.parse(fs.readFileSync(path.join(CONTENT, openapi.spec), 'utf8'));
  const slug = spec.info.title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  return `/${path.posix.dirname(openapi.slice)}/${slug}/`;
}
const apiPath = apiIntroPath(build.openapi);

// Every listed build, as a full-page load without this build's own baseUrl in front.
const versionItems = site.builds
  .filter((candidate) => candidate.listed)
  .map((candidate) => ({
    label: candidate.name === site.newest ? `${candidate.name} (latest)` : candidate.name,
    href: `pathname://${candidate.base_url}`,
    autoAddBaseUrl: false,
    className: 'dfe-version-link',
    target: '_self',
  }));

// The category aggregate.py marks with openapiSlice takes the sidebar gen-api-docs wrote.
function spliceApiSlice(item) {
  if (item.type !== 'category') {
    return item;
  }
  const {openapiSlice, ...customProps} = item.customProps || {};
  if (!openapiSlice) {
    return {...item, items: item.items.map(spliceApiSlice).filter(Boolean)};
  }
  const slicePath = path.join(CONTENT, build.name, openapiSlice);
  if (!fs.existsSync(slicePath)) {
    return null;
  }
  const loaded = require(slicePath);
  const apiDir = path.posix.dirname(openapiSlice);
  return {...item, customProps: {...customProps, apiDir}, items: loaded.default || loaded};
}

// journeys.yaml regroups the pages into reader journeys when aggregate.py placed them.
async function sidebarItemsGenerator({defaultSidebarItemsGenerator, ...args}) {
  const items = (await defaultSidebarItemsGenerator(args)).map(spliceApiSlice).filter(Boolean);
  const journeys = site.journeys || [];
  const placements = build.placements || {};
  if (journeys.length === 0 || Object.keys(placements).length === 0) {
    return items;
  }
  return journeySidebar(items, journeys, placements);
}

const openapiPlugins = build.openapi
  ? [
      [
        'docusaurus-plugin-openapi-docs',
        {
          id: 'openapi',
          docsPluginId: 'classic',
          config: {
            engine: {
              specPath: `content/${build.openapi.spec}`,
              outputDir: `content/${build.openapi.output}`,
              sidebarOptions: {groupPathsBy: 'tag', categoryLinkSource: 'tag'},
            },
          },
        },
      ],
    ]
  : [];

module.exports = {
  title: 'DFE documentation',
  tagline: 'The Data Fusion Engine suite',
  favicon: `${GRAPHICS}/product-dfe/tertiary/product-dfe_square.svg`,
  // The brand tokens and @font-face rules, read anonymously like every other build input.
  stylesheets: [`${GRAPHICS}/tokens/tokens.css`],
  customFields: {stack, versionLabel, newest: site.newest, releaseNotes, apiPath},
  url: 'https://dfe.docs.hyperi.io',
  baseUrl: build.base_url,
  trailingSlash: true,
  noIndex: !build.listed,
  organizationName: 'hyperi-io',
  projectName: 'dfe-infra',
  onBrokenLinks: 'warn',
  onBrokenAnchors: 'warn',
  i18n: {defaultLocale: 'en', locales: ['en']},
  markdown: {
    // .md parses as CommonMark and .mdx as MDX, so plain repo docs need no MDX escaping.
    format: 'detect',
    mermaid: true,
    hooks: {onBrokenMarkdownLinks: 'warn', onBrokenMarkdownImages: 'warn'},
  },
  clientModules: [require.resolve('./src/version-switch.js')],
  presets: [
    [
      'classic',
      {
        docs: {
          path: docsPath,
          routeBasePath: '/',
          sidebarPath: require.resolve('./sidebars.js'),
          sidebarItemsGenerator,
          docItemComponent: '@theme/ApiItem',
        },
        blog: false,
        pages: {},
        theme: {customCss: require.resolve('./src/css/custom.css')},
      },
    ],
  ],
  plugins: ['docusaurus-plugin-sass', ...openapiPlugins],
  themes: [
    '@docusaurus/theme-mermaid',
    'docusaurus-theme-openapi-docs',
    [
      require.resolve('@easyops-cn/docusaurus-search-local'),
      {
        hashed: true,
        indexBlog: false,
        indexPages: false,
        docsRouteBasePath: '/',
        language: ['en'],
        // /next/ is noindex for crawlers, and its own search still needs its pages.
        forceIgnoreNoIndex: true,
      },
    ],
  ],
  themeConfig: {
    // The toggle cycles system, light and dark. Every surface reads the mode through the roles in custom.css.
    colorMode: {defaultMode: 'light', disableSwitch: false, respectPrefersColorScheme: true},
    announcementBar: releaseNotes
      ? {
          // Keyed on the release, so a reader who closed one sees the next.
          id: `release-${site.newest}`,
          content:
            `<span class="announcement-tag">Latest</span>DFE ${site.newest} is the current release. ` +
            `<a href="${releaseNotes}">Release notes${ARROW_SVG}</a>`,
          backgroundColor: 'var(--dfe-accent)',
          textColor: 'var(--dfe-navy)',
          isCloseable: true,
        }
      : undefined,
    navbar: {
      title: 'Data Fusion Engine',
      // hyperi-graphics public/hyperi-hound/tertiary: the one hound colour that reads on both modes.
      logo: {
        alt: 'HyperI',
        src: 'img/hyperi-hound.svg',
        width: 47,
        height: 32,
      },
      items: [
        {to: '/overview/', label: 'Docs', position: 'left'},
        {to: '/quickstart/', label: 'Quickstart', position: 'left'},
        ...(apiPath ? [{to: apiPath, label: 'API', position: 'left'}] : []),
        {
          type: 'dropdown',
          label: versionLabel,
          position: 'right',
          items: versionItems,
        },
        {href: 'https://github.com/hyperi-io', label: 'GitHub', position: 'right'},
      ],
    },
    footer: {
      style: 'dark',
      logo: {
        alt: 'HyperI',
        src: `${GRAPHICS}/hyperi-horizontal/dark/hyperi-horizontal_bare.svg`,
        href: 'https://hyperi.io',
      },
      links: [
        {
          title: 'Docs',
          items: [
            {label: 'Overview', to: '/overview/'},
            {label: 'Quickstarts', to: '/quickstart/'},
            ...(apiPath ? [{label: 'HTTP API reference', to: apiPath}] : []),
            ...(releaseNotes ? [{label: 'Release notes', href: releaseNotes}] : []),
          ],
        },
        {
          title: 'Source',
          items: [
            {label: 'GitHub', href: 'https://github.com/hyperi-io'},
            {label: 'dfe-infra', href: 'https://github.com/hyperi-io/dfe-infra'},
            {label: 'dfe-docker', href: 'https://github.com/hyperi-io/dfe-docker'},
          ],
        },
        {
          title: 'HyperI',
          items: [{label: 'hyperi.io', href: 'https://hyperi.io'}],
        },
      ],
      copyright: `Copyright (c) ${new Date().getFullYear()} HYPERI PTY LIMITED`,
    },
    mermaid: {theme: {light: 'neutral', dark: 'dark'}},
    prism: {
      theme: require('./src/prism-theme.js'),
      darkTheme: require('./src/prism-theme.js'),
      additionalLanguages: ['bash', 'toml', 'sql', 'hcl', 'docker', 'ini', 'diff'],
    },
  },
};
