import {
  Badge,
  Button,
  Codicon,
  host,
  PALETTE_AREA,
  ROUTES_AREA,
  ScrollArea,
  Separator,
  SIDEBAR_NAV_AREA,
  useValue
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const WISDOM_ROUTE = '/collective-wisdom'

const COMMAND_GROUPS = [
  {
    description: 'Interactive setup is listed below for terminal-only entry. Run the copied command in your terminal to read status.',
    icon: 'settings-gear',
    title: 'Setup & status',
    commands: [
      {
        command: 'hermes wisdom status --json',
        note: 'Read local and Gateway Wisdom status, including unresolved operations.'
      }
    ]
  },
  {
    description: 'Read the shared catalog and this enrolment’s managed installations.',
    icon: 'search',
    title: 'Browse & installed',
    commands: [
      {
        command: 'hermes wisdom browse --json',
        note: 'List published team skills. Add a query in the terminal if needed.'
      },
      {
        command: 'hermes wisdom installed --json',
        note: 'Read managed installations for the enrolled owner.'
      },
      {
        command: 'hermes wisdom inbox --json',
        note: 'Read persisted owner-bound activity without polling or delivery.'
      }
    ]
  },
  {
    description: 'Prepare exact reviews in the terminal. A plan never installs, updates, or runs setup.',
    icon: 'checklist',
    title: 'Review plans',
    commands: [
      {
        command: "hermes wisdom install '<skill-id>@v1' --plan --json",
        note: 'Replace the placeholder in the terminal and inspect the complete hash-bound plan.'
      },
      {
        command: "hermes wisdom update '<skill-id>' --plan --json",
        note: 'Review the exact update and acknowledgements before any receipt is applied.'
      },
      {
        command: "hermes wisdom installed-setup inspect '<skill-id>' --json",
        note: 'Read setup guidance and prerequisites. Inspection does not run a command.'
      }
    ]
  },
  {
    description: 'Inspect local evidence and private draft bytes before any publication decision.',
    icon: 'lightbulb',
    title: 'Contribution guidance',
    commands: [
      {
        command: 'hermes wisdom review-week --dry-run --json',
        note: 'Preview bounded qualification evidence without queueing or calling a model.'
      },
      {
        command: "hermes wisdom prepared inspect '<local-draft-id>' --json",
        note: 'Export one complete private draft for local inspection or editing.'
      },
      {
        command: "hermes wisdom publication review '<local-draft-id>' --json",
        note: 'Create an exact publication review. It does not submit the draft.'
      }
    ]
  }
]

const TERMINAL_ACTIONS = [
  {
    label: 'Start interactive setup and review its disclosure',
    command: 'hermes wisdom setup'
  },
  {
    label: 'Prepare a local skill as a private draft (no upload or model review)',
    command: "hermes wisdom suggest '<skill-name>' --json"
  },
  {
    label: 'Save a complete edited JSON payload exported by prepared inspect',
    command: "hermes wisdom prepared save '<payload.json>' --json"
  },
  {
    label: 'Install after reviewing its plan',
    command: "hermes wisdom install --apply-receipt '<receipt>' --json"
  },
  {
    label: 'Update after reviewing its plan',
    command: "hermes wisdom update --apply-receipt '<receipt>' --json"
  },
  {
    label: 'Plan one exact installed-skill setup action',
    command:
      "hermes wisdom installed-setup plan '<skill-id>' --version '<version>' --phase '<prerequisite|setup|verify>' --index '<index>' --command '<exact command>' --json"
  },
  {
    label: 'Run setup only after reviewing that separate receipt',
    command: "hermes wisdom installed-setup apply '<setup-receipt>' --json"
  },
  {
    label: 'Submit only after reviewing the exact publication receipt',
    command: "hermes wisdom publication submit '<publication-receipt>' --json"
  }
]

function CommandCard({ command, copyCommand, note }) {
  const [copyState, setCopyState] = useState('idle')

  const copy = async () => {
    setCopyState('copying')
    try {
      const copied = await copyCommand(command)
      if (copied === true) {
        setCopyState('copied')
        host.notify({
          kind: 'success',
          message: 'Command copied. Run it in the intended terminal.'
        })
        return
      }
    } catch {
      // The OS bridge may reject as well as return false. Both are safe failures.
    }

    setCopyState('failed')
    host.notify({
      kind: 'error',
      message: 'Could not copy the command. Select it manually from the page.'
    })
  }

  const feedback =
    copyState === 'copied'
      ? 'Copied. Nothing was executed.'
      : copyState === 'failed'
        ? 'Copy failed. Select the command manually.'
        : 'Copy only — never runs the command.'

  return jsxs('article', {
    className:
      'flex flex-col gap-2 rounded-[4px] border border-(--ui-stroke-secondary) p-3 sm:flex-row sm:items-start sm:justify-between',
    children: [
      jsxs('div', {
        className: 'min-w-0 space-y-1.5',
        children: [
          jsx('code', {
            className:
              'block select-text overflow-x-auto text-xs leading-5 text-(--ui-text-primary)',
            children: command
          }),
          jsx('p', {
            className: 'text-xs leading-5 text-(--ui-text-tertiary)',
            children: note
          }),
          jsx('p', {
            'aria-live': 'polite',
            className: 'text-[0.6875rem] leading-4 text-(--ui-text-quaternary)',
            children: feedback
          })
        ]
      }),
      jsx(Button, {
        'aria-label': `Copy: ${command}`,
        'data-command': command,
        loading: copyState === 'copying',
        onClick: copy,
        size: 'sm',
        type: 'button',
        variant: 'secondary',
        children: copyState === 'copied' ? 'Copied' : 'Copy'
      })
    ]
  })
}

function CommandSection({ copyCommand, description, icon, title, commands }) {
  return jsxs('section', {
    'aria-labelledby': `wisdom-${title.toLowerCase().replaceAll(/[^a-z]+/g, '-')}`,
    className: 'space-y-3',
    children: [
      jsxs('div', {
        className: 'space-y-1',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx(Codicon, { name: icon, size: '0.875rem' }),
              jsx('h2', {
                className: 'text-sm font-semibold text-(--ui-text-primary)',
                id: `wisdom-${title.toLowerCase().replaceAll(/[^a-z]+/g, '-')}`,
                children: title
              })
            ]
          }),
          jsx('p', {
            className: 'text-xs leading-5 text-(--ui-text-tertiary)',
            children: description
          })
        ]
      }),
      jsx('div', {
        className: 'grid gap-2',
        children: commands.map(item =>
          jsx(
            CommandCard,
            {
              ...item,
              copyCommand
            },
            item.command
          )
        )
      })
    ]
  })
}

function TerminalActions() {
  return jsxs('section', {
    'aria-labelledby': 'wisdom-terminal-actions',
    className: 'space-y-3',
    children: [
      jsxs('div', {
        className: 'space-y-1',
        children: [
          jsx('h2', {
            className: 'text-sm font-semibold text-(--ui-text-primary)',
            id: 'wisdom-terminal-actions',
            children: 'Complete actions in a terminal'
          }),
          jsx('p', {
            className: 'text-xs leading-5 text-(--ui-text-tertiary)',
            children:
              'These exact action forms are intentionally not copy buttons. Read the complete plan or review in the terminal, verify its receipt and current profile, then type the matching action there. This page grants no approval.'
          })
        ]
      }),
      jsx('ul', {
        className: 'space-y-2',
        children: TERMINAL_ACTIONS.map(item =>
          jsxs(
            'li',
            {
              className: 'space-y-1 border-l border-(--ui-stroke-secondary) pl-3',
              children: [
                jsx('div', {
                  className: 'text-xs font-medium text-(--ui-text-secondary)',
                  children: item.label
                }),
                jsx('code', {
                  className:
                    'block select-text overflow-x-auto text-xs leading-5 text-(--ui-text-primary)',
                  children: item.command
                })
              ]
            },
            item.command
          )
        )
      })
    ]
  })
}

function ContextNotice() {
  const gateway = useValue(host.state.gateway)
  const profile = useValue(host.state.profile)

  return jsxs('aside', {
    className: 'space-y-2 rounded-[4px] border border-(--ui-stroke-secondary) p-3',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx(Badge, { children: `Desktop display profile only: ${profile || 'default'}` }),
          jsx(Badge, { children: `Gateway display: ${gateway || 'unknown'}` })
        ]
      }),
      jsx('p', {
        className: 'text-xs leading-5 text-(--ui-text-secondary)',
        children:
          'Desktop plugins are app-level. A copied command runs wherever that terminal’s hermes command is configured; this page does not route it to the profile shown above.'
      }),
      jsx('p', {
        className: 'text-xs leading-5 text-(--ui-text-tertiary)',
        children:
          'For a remote backend, open a remote terminal on that host (or an explicitly configured remote shell), run hermes wisdom status --json there, and verify the intended owner/profile before continuing. Displayed profile names are never inserted into shell commands.'
      })
    ]
  })
}

function WisdomPage({ copyCommand }) {
  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: [
      jsxs('header', {
        className: 'shrink-0 space-y-2 border-b border-(--ui-stroke-secondary) px-5 py-4',
        children: [
          jsxs('div', {
            className: 'flex flex-wrap items-center gap-2',
            children: [
              jsx('h1', {
                className: 'text-base font-semibold text-(--ui-text-primary)',
                children: 'Collective Wisdom'
              }),
              jsx(Badge, { children: 'Terminal handoff' })
            ]
          }),
          jsx('p', {
            className: 'max-w-3xl text-xs leading-5 text-(--ui-text-tertiary)',
            children:
              'A bounded Desktop guide for setup, discovery, installed state, review plans, and contributions. It does not execute commands, fetch private Wisdom data, apply receipts, approve setup, or submit publication.'
          })
        ]
      }),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: jsxs('main', {
          className: 'mx-auto w-full max-w-4xl space-y-6 p-5',
          children: [
            jsx(ContextNotice, {}),
            ...COMMAND_GROUPS.flatMap(group => [
              jsx(CommandSection, { ...group, copyCommand }, group.title),
              jsx(Separator, { key: `${group.title}-separator` })
            ]),
            jsx(TerminalActions, {}),
            jsx('p', {
              className: 'pb-2 text-xs leading-5 text-(--ui-text-quaternary)',
              children:
                'If clipboard access is unavailable, select the command manually. Replace placeholders only in the terminal after checking that terminal’s host and profile.'
            })
          ]
        })
      })
    ]
  })
}

export default {
  id: 'hermes-collective-wisdom',
  name: 'Collective Wisdom',
  description: 'Native Desktop guidance and safe terminal command handoffs for Collective Wisdom.',
  defaultEnabled: false,
  register(ctx) {
    const copyCommand = command => ctx.os.writeClipboard(command)
    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: WISDOM_ROUTE },
        render: () => jsx(WisdomPage, { copyCommand })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        order: 60,
        data: {
          codicon: 'library',
          label: 'Wisdom',
          path: WISDOM_ROUTE
        }
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'collective-wisdom.open',
          label: 'Collective Wisdom: Open guide',
          keywords: ['wisdom', 'skills', 'catalog', 'install', 'contribute'],
          run: () => host.navigate(WISDOM_ROUTE)
        }
      }
    ])
  }
}