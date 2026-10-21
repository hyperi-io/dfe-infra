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
  return {...item, customProps, items: loaded.default || loaded};
}

async function sidebarItemsGenerator({defaultSidebarItemsGenerator, ...args}) {
  const items = await defaultSidebarItemsGenerator(args);
  return items.map(spliceApiSlice).filter(Boolean);
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
  favicon: 'img/product-dfe_square.svg',
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
        pages: false,
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
    colorMode: {respectPrefersColorScheme: true},
    navbar: {
      style: 'dark',
      logo: {
        alt: 'Data Fusion Engine',
        src: 'img/product-dfe-named-inline_bare.svg',
        srcDark: 'img/product-dfe-named-inline_bare.svg',
      },
      items: [
        {
          type: 'dropdown',
          label: build.listed ? build.name : `${build.name} (unreleased)`,
          position: 'right',
          items: versionItems,
        },
        {href: 'https://github.com/hyperi-io', label: 'GitHub', position: 'right'},
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {label: 'hyperi.io', href: 'https://hyperi.io'},
        {label: 'GitHub', href: 'https://github.com/hyperi-io'},
      ],
      copyright: `Copyright (c) ${new Date().getFullYear()} HYPERI PTY LIMITED`,
    },
    mermaid: {theme: {light: 'neutral', dark: 'dark'}},
    prism: {additionalLanguages: ['bash', 'toml', 'sql', 'hcl', 'docker', 'ini', 'diff']},
  },
};
