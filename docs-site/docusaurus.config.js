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
const yaml = require('js-yaml');
const {journeySidebar} = require('./sidebar-journeys.js');
const {describe} = require('./description.js');

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
// The stack a reader installs from this build: its own stack, or the newest release for /next/.
const stack = build.name === 'next' ? site.newest || build.name : build.name;
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

// Empty on the newest release, whose stack make up and make demo pick by default.
const versionArg = stack === site.newest ? '' : ` VERSION=${stack}`;
const COPY_VARS = {
  stack,
  version_arg: versionArg,
  version: versionLabel,
  newest: site.newest,
  release_notes: releaseNotes,
  api_path: apiPath,
  year: String(new Date().getFullYear()),
};

// Fills {name} from COPY_VARS. A name with no value drops the map holding it, so an item linking to a page this build lacks disappears.
function fillCopy(node) {
  if (typeof node === 'string') {
    let missing = false;
    const text = node.replace(/\{(\w+)\}/g, (_, name) => {
      if (!(name in COPY_VARS)) {
        throw new Error(`copy/: {${name}} is not one of ${Object.keys(COPY_VARS).join(', ')}`);
      }
      missing = missing || COPY_VARS[name] == null;
      return COPY_VARS[name] ?? '';
    });
    return missing ? undefined : text;
  }
  if (Array.isArray(node)) {
    return node.map(fillCopy).filter((item) => item !== undefined);
  }
  if (node && typeof node === 'object') {
    const out = {};
    for (const [key, value] of Object.entries(node)) {
      const filled = fillCopy(value);
      if (filled === undefined) {
        return undefined;
      }
      out[key] = filled;
    }
    return out;
  }
  return node;
}

function loadCopy(name) {
  return fillCopy(yaml.load(fs.readFileSync(path.join(__dirname, 'copy', `${name}.yaml`), 'utf8')));
}

// The landing tab's block after <!-- landing -->, then the block's `then`.
// Only aggregate.py's one-line compose fill writes that marker, so other builds show no command.
function landingFrom({code_from: page, then}) {
  const file = path.join(CONTENT, build.name, page);
  const text = fs.readFileSync(file, 'utf8');
  const match = text.match(/<!-- landing -->\s*```[^\n]*\n([\s\S]*?)\n```/);
  if (!match) {
    return [];
  }
  const code = match[1]
    .split('\n')
    .map((line) => line.replace(/\s+#.*$/, ''))
    .join('\n');
  return [{code}, ...(then ? [{text: then}] : [])];
}

const siteCopy = loadCopy('site');
const landingCopy = loadCopy('landing');
for (const tab of landingCopy.quickstart.tabs) {
  tab.blocks = tab.blocks.flatMap((block) => (block.code_from === undefined ? [block] : landingFrom(block)));
}

// Markdown links and backticks as the plain text another site can show.
function plainText(text) {
  return text.replace(/\[([^\]]+)\]\([^)]+\)/g, '$1').replace(/`/g, '');
}

// The demo packs of the dfe-docker release this build pins, as aggregate.py saved it.
function demoPacks() {
  const file = path.join(CONTENT, '_quickstart', `${build.name}.json`);
  if (!fs.existsSync(file)) {
    return [];
  }
  const release = JSON.parse(fs.readFileSync(file, 'utf8'));
  return (release.packs || []).map((pack) => ({
    id: pack.name,
    title: pack.title,
    use_case: pack.use_case,
    commands: pack[release.form],
  }));
}

// The hero's quickstart tabs as /quickstart.json, so other sites show the same commands.
// A note between two code blocks rides into the next command as a shell comment.
// A tab with `packs: true` lists the release's demo packs, each use_case a path on hyperi.io.
function quickstartJson(siteUrl, baseUrl) {
  const packs = demoPacks();
  const tabs = landingCopy.quickstart.tabs.map((tab) => {
    const texts = [];
    const commands = [];
    let pending = null;
    for (const block of tab.blocks) {
      if (block.code !== undefined) {
        commands.push(pending ? `# ${pending}\n${block.code}` : block.code);
        pending = null;
      } else if (commands.length === 0) {
        texts.push(plainText(block.text));
      } else {
        pending = plainText(block.text);
      }
    }
    return {
      id: tab.value,
      label: tab.label,
      needs: texts.join(' ') || null,
      commands,
      then: pending,
      guide: tab.guide ? `${siteUrl}${baseUrl}${tab.guide.to.replace(/^\//, '')}` : null,
      packs: tab.packs ? packs : [],
    };
  });
  return {stack, tabs};
}

function quickstartJsonPlugin(context) {
  return {
    name: 'quickstart-json',
    async postBuild({outDir}) {
      const body = quickstartJson(context.siteConfig.url, context.siteConfig.baseUrl);
      fs.writeFileSync(path.join(outDir, 'quickstart.json'), `${JSON.stringify(body, null, 2)}\n`);
    },
  };
}

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
  return journeySidebar(items, journeys, placements, build.members.map((member) => member.path));
}

// Every doc's route, title, description and source file, for scripts/agent_layer.py.
function pageManifestPlugin() {
  return {
    name: 'dfe-page-manifest',
    async allContentLoaded({allContent}) {
      const docs = allContent['docusaurus-plugin-content-docs'].default;
      const pages = docs.loadedVersions.flatMap((version) =>
        version.docs.map(({id, title, description, source, permalink}) => ({
          id,
          title,
          description,
          source: source.replace(/^@site\//, ''),
          permalink,
        })),
      );
      const out = path.join(CONTENT, '_manifests', `${build.name}.json`);
      fs.mkdirSync(path.dirname(out), {recursive: true});
      fs.writeFileSync(out, `${JSON.stringify(pages, null, 2)}\n`);
    },
  };
}

// The API pages are generated from the spec, which llms.txt links to whole instead.
const apiPages = build.openapi
  ? [`${path.posix.relative(build.name, build.openapi.output)}/**`]
  : [];
const memberRoots = [...new Set(build.members.map((member) => member.path.split('/')[0]))];
const llmsPlugin = [
  'docusaurus-plugin-llms',
  {
    docsDir: [{path: docsPath, routeBasePath: '/'}],
    title: `DFE documentation ${versionLabel}`,
    description:
      'The Data Fusion Engine (DFE) is a product suite an organisation deploys to run its own ' +
      'data pipeline: events in over HTTP, through Kafka or gRPC, into ClickHouse.',
    rootContent:
      `Release ${versionLabel}. Every prose page has a markdown copy at its URL with .md on the end, ` +
      `and ${site.url}/latest/ always serves the newest release.` +
      (build.openapi
        ? ` ${site.url}${build.base_url}openapi/dfe-engine.json is the engine's HTTP API spec.`
        : ''),
    generateLLMsFullTxt: false,
    generateMarkdownFiles: true,
    excludeImports: true,
    rewriteImageUrls: true,
    ignoreFiles: apiPages,
    customLLMFiles: [
      {
        filename: 'llms/overview.txt',
        includePatterns: ['**'],
        ignorePatterns: [...memberRoots.map((root) => `${root}/**`), ...apiPages],
        fullContent: true,
        title: 'DFE documentation: overview, quickstarts and operating',
      },
      ...memberRoots.map((root) => ({
        filename: `llms/${root}.txt`,
        includePatterns: [`${root}/**`],
        ignorePatterns: apiPages,
        fullContent: true,
        title: `DFE documentation: ${root}`,
      })),
    ],
  },
];

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
  title: siteCopy.title,
  tagline: siteCopy.tagline,
  favicon: `${GRAPHICS}/product-dfe/tertiary/product-dfe_square.svg`,
  // The brand tokens and @font-face rules, read anonymously like every other build input.
  stylesheets: [`${GRAPHICS}/tokens/tokens.css`],
  customFields: {landing: landingCopy},
  url: site.url,
  baseUrl: build.base_url,
  trailingSlash: true,
  noIndex: !build.listed,
  organizationName: 'hyperi-io',
  projectName: 'dfe-infra',
  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',
  i18n: {defaultLocale: 'en', locales: ['en']},
  markdown: {
    // .md parses as CommonMark and .mdx as MDX, so plain repo docs need no MDX escaping.
    format: 'detect',
    mermaid: true,
    // A page that sets no description gets its first real paragraph, not its first line.
    parseFrontMatter: async (params) => {
      const result = await params.defaultParseFrontMatter(params);
      if (!result.frontMatter.description) {
        const description = describe(result.content);
        if (description) {
          result.frontMatter.description = description;
        }
      }
      return result;
    },
    hooks: {onBrokenMarkdownLinks: 'throw', onBrokenMarkdownImages: 'warn'},
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
  plugins: ['docusaurus-plugin-sass', quickstartJsonPlugin, pageManifestPlugin, llmsPlugin, ...openapiPlugins],
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
    colorMode: {respectPrefersColorScheme: true, disableSwitch: true},
    announcementBar: siteCopy.announcement && releaseNotes
      ? {
          // Keyed on the release, so a reader who closed one sees the next.
          id: `release-${site.newest}`,
          content:
            `<span class="announcement-tag">${siteCopy.announcement.tag}</span>${siteCopy.announcement.text} ` +
            `<a href="${releaseNotes}">${siteCopy.announcement.link}${ARROW_SVG}</a>`,
          backgroundColor: 'var(--role-raised)',
          textColor: 'var(--role-body)',
          isCloseable: true,
        }
      : undefined,
    navbar: {
      title: siteCopy.navbar.title,
      // hyperi-graphics public/hyperi-hound/tertiary: the one hound colour that reads on both modes.
      logo: {
        alt: siteCopy.navbar.logo_alt,
        src: 'img/hyperi-hound.svg',
        width: 47,
        height: 32,
      },
      items: [
        ...siteCopy.navbar.items.map((item) => ({...item, position: 'left'})),
        {
          type: 'dropdown',
          label: versionLabel,
          position: 'right',
          items: versionItems,
        },
        {...siteCopy.navbar.github, position: 'right'},
      ],
    },
    footer: {
      style: 'dark',
      logo: {
        alt: siteCopy.navbar.logo_alt,
        src: `${GRAPHICS}/hyperi-horizontal/dark/hyperi-horizontal_bare.svg`,
        href: 'https://hyperi.io',
      },
      links: siteCopy.footer.links,
      copyright: siteCopy.footer.copyright,
    },
    mermaid: {theme: {light: 'neutral', dark: 'dark'}},
    prism: {
      theme: require('./src/prism-theme.js'),
      darkTheme: require('./src/prism-theme.js'),
      additionalLanguages: ['bash', 'toml', 'sql', 'hcl', 'docker', 'ini', 'diff'],
    },
  },
};
