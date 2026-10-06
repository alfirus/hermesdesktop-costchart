/**
 * Cost Chart — daily AI spend (Xiaomi · OpenCode Go · Meta · Local LM) + Vectorizer savings.
 *
 * Unified Hermes plugin package: this is the DESKTOP half (`desktop/plugin.js`),
 * with the ledger aggregation backend in `dashboard/plugin_api.py` mounted at
 * /api/plugins/costchart/ (requires `costchart` in `plugins.enabled`, config.yaml).
 *
 * Plain ESM, loaded uncompiled — UI is jsx() calls, not JSX syntax.
 * Only these imports resolve: @hermes/plugin-sdk, react, react/jsx-runtime.
 */

import { host, useQuery, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, Skeleton } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'costchart'
const PATH = '/costchart'

// Captured at register() so components can call the scoped backend.
let ctx = null

const LANES = [
  { key: 'xiaomi', label: 'Xiaomi (MiMo)', opacity: 1 },
  { key: 'opencode-go', label: 'OpenCode Go (muse-spark)', opacity: 0.8 },
  { key: 'meta', label: 'Meta (muse-spark)', opacity: 0.55 },
  { key: 'local', label: 'Local LM Studio', opacity: 0.3 },
  { key: 'other', label: 'Other', opacity: 0.15 }
]

const fmtUsd = n => (n >= 100 ? '$' + Math.round(n) : '$' + (Math.round(n * 100) / 100).toFixed(2))
const fmtTok = n => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(Math.round(n))

// Plan burn card colors by warning level.
const BURN_COLORS = {
  ok:       { bg: 'var(--ui-bg-secondary)',   border: 'var(--ui-stroke-secondary)', text: 'var(--ui-text-primary)' },
  warning:  { bg: '#7c2d12',                  border: '#ea580c',                 text: '#fed7aa' },
  critical: { bg: '#450a0a',                  border: '#dc2626',                 text: '#fecaca' }
}

function sum(arr) {
  return arr.reduce((a, b) => a + b, 0)
}

/** Stacked bar chart, pure SVG (viewBox scales, no canvas resize traps). */
function BarChart({ days, series, fmt }) {
  const W = 820, H = 240, PAD_L = 56, PAD_B = 28, PAD_T = 10
  const plotW = W - PAD_L - 10, plotH = H - PAD_T - PAD_B
  const totals = days.map((_, i) => sum(series.map(s => s.values[i] || 0)))
  const max = Math.max(1e-9, ...totals)
  const bw = plotW / Math.max(1, days.length)
  const labelEvery = Math.max(1, Math.ceil(days.length / 12))
  const ticks = [0, 0.25, 0.5, 0.75, 1]

  return jsx('svg', {
    viewBox: `0 0 ${W} ${H}`,
    style: { width: '100%', height: 'auto', display: 'block' },
    children: [
      ...ticks.map(f => jsxs('g', { children: [
        jsx('line', {
          x1: PAD_L, x2: W - 10, y1: PAD_T + plotH * (1 - f), y2: PAD_T + plotH * (1 - f),
          style: { stroke: 'var(--ui-stroke-secondary)', strokeWidth: 0.5 }
        }),
        jsx('text', {
          x: PAD_L - 6, y: PAD_T + plotH * (1 - f) + 3, 'text-anchor': 'end',
          style: { fill: 'var(--ui-text-tertiary)', fontSize: '10px' },
          children: fmt(max * f)
        })
      ]}, `t${f}`)),
      ...days.map((d, i) => {
        let acc = 0
        return jsx('g', {
          children: series.map(s => {
            const v = s.values[i] || 0
            const h = (v / max) * plotH
            const y = PAD_T + plotH - acc - h
            acc += h
            return v > 0 ? jsx('rect', {
              x: PAD_L + i * bw + bw * 0.15, y, width: bw * 0.7, height: Math.max(0, h),
              style: { fill: 'var(--ui-accent)', fillOpacity: s.opacity }
            }, s.key) : null
          })
        }, d)
      }),
      ...days.map((d, i) => i % labelEvery === 0 ? jsx('text', {
        x: PAD_L + i * bw + bw / 2, y: H - 10, 'text-anchor': 'middle',
        style: { fill: 'var(--ui-text-tertiary)', fontSize: '9px' },
        children: d.slice(5)
      }, 'x' + d) : null)
    ]
  })
}

function Legend() {
  return jsx('div', {
    className: 'flex flex-wrap items-center gap-3 text-[0.6875rem] text-(--ui-text-tertiary)',
    children: LANES.map(l => jsxs('span', {
      className: 'inline-flex items-center gap-1',
      children: [
        jsx('span', {
          style: {
            width: '10px', height: '10px', borderRadius: '2px', display: 'inline-block',
            background: 'var(--ui-accent)', opacity: l.opacity
          }
        }),
        l.label
      ]
    }, l.key))
  })
}

function StatCard({ label, value, detail }) {
  return jsxs('div', {
    className: 'flex flex-col gap-0.5 rounded-md border border-(--ui-stroke-secondary) px-3 py-2',
    children: [
      jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: label }),
      jsx('div', { className: 'text-base font-medium', children: value }),
      detail ? jsx('div', { className: 'text-[0.625rem] text-(--ui-text-quaternary)', children: detail }) : null
    ]
  })
}

/** Plan burn stat card — shows MTD %, warning bar, and month-end projection. */
function BurnCard({ plan_burn }) {
  if (!plan_burn || !plan_burn.enabled) return null

  const colors = BURN_COLORS[plan_burn.warning_level] || BURN_COLORS.ok
  const pct = plan_burn.burn_pct
  const isWarning = plan_burn.warning_level !== 'ok'

  // Visual progress bar (0–100% of cap).
  const barWidth = Math.min(100, pct)

  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded-md border px-3 py-2',
    style: { borderColor: colors.border },
    children: [
      // Header row.
      jsx('div', {
        className: 'flex items-center justify-between',
        children: jsxs('span', {
          className: 'text-[0.6875rem] font-medium',
          style: { color: colors.text },
          children: [
            'Plan burn (MTD)',
            isWarning && jsx('span', {
              className: 'ml-1 inline-flex items-center gap-0.5 rounded px-1 text-[0.625rem] font-bold',
              style: { background: colors.border, color: '#fff' },
              children: plan_burn.warning_level === 'critical' ? '! CRITICAL' : '⚠ WARNING'
            })
          ]
        })
      }),

      // Big percentage.
      jsx('div', {
        className: 'text-lg font-bold tabular-nums',
        style: { color: colors.text },
        children: pct.toFixed(1) + '%'
      }),

      // Progress bar.
      jsx('div', {
        className: 'h-2 w-full overflow-hidden rounded bg-(--ui-bg-tertiary)',
        children: jsx('div', {
          className: 'h-full transition-all duration-300',
          style: { width: barWidth + '%', background: colors.border }
        })
      }),

      // Details row.
      jsxs('div', {
        className: 'flex items-center justify-between text-[0.625rem]',
        style: { color: isWarning ? '#fdba74' : 'var(--ui-text-tertiary)' },
        children: [
          jsx('span', {
            children: fmtTok(plan_burn.mtd_tokens_used) + ' / ' + fmtTok(plan_burn.cap) + ' tokens used'
          }),
          jsx('span', {
            children: plan_burn.days_elapsed + '/' + (plan_burn.days_elapsed + plan_burn.days_remaining_in_month) + ' days elapsed'
          })
        ]
      }),

      // Projection.
      isWarning ? jsx('div', {
        className: 'text-[0.625rem] font-medium',
        style: { color: colors.text },
        children: 'Projected month-end: ' + fmtTok(plan_burn.projected_month_end) + ' tokens' +
          (plan_burn.projected_month_end > plan_burn.cap ? ' (' + fmtTok(Math.round((plan_burn.projected_month_end - plan_burn.cap))) + ' over cap)' : '')
      }) : null
    ]
  })
}

function CostChartPage() {
  const q = useQuery({
    queryKey: ['costchart', 'daily'],
    queryFn: () => ctx.rest('/daily'),
    refetchInterval: 300000,
    staleTime: 60000
  })

  if (q.isLoading) {
    return jsx('div', {
      className: 'flex h-full flex-col gap-3 p-6',
      children: [1, 2, 3].map(i => jsx(Skeleton, { className: 'h-24 w-full' }, i))
    })
  }
  if (q.isError) {
    return jsxs('div', {
      className: 'flex h-full flex-col items-center justify-center gap-2 p-6 text-sm',
      children: [
        jsx('div', { className: 'font-medium', children: 'Cost Chart backend unavailable' }),
        jsx('div', {
          className: 'text-(--ui-text-tertiary)',
          children: 'Enable the plugin backend: add "costchart" to plugins.enabled in config.yaml, then reload.'
        })
      ]
    })
  }

  const d = q.data || {}
  const days = d.days || []
  const win = d.window_days || 31
  const prov = d.providers || {}
  const vec = d.vectorizer || {}
  const m = (vec.method || {})
  const plan_burn = d.plan_burn || {}

  const costSeries = LANES.map(l => ({ key: l.key, opacity: l.opacity, values: (prov[l.key] || {}).daily ? prov[l.key].daily.cost_usd : [] }))
  const tokSeries = LANES.map(l => ({
    key: l.key, opacity: l.opacity,
    values: (prov[l.key] || {}).daily
      ? prov[l.key].daily.input_tokens.map((v, i) => v + prov[l.key].daily.output_tokens[i])
      : []
  }))
  const vecSeries = [{ key: 'saved', opacity: 0.85, values: vec.daily_tokens_saved || [] }]

  return jsxs('div', {
    className: 'flex h-full flex-col gap-4 overflow-y-auto p-6 text-sm',
    children: [
      jsxs('div', {
        className: 'flex items-baseline justify-between',
        children: [
          jsx('div', {
            className: 'text-lg font-medium',
            children: 'AI Cost Chart — daily'
          }),
          jsx('div', {
            className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
            children: `last ${win} days · generated ${d.generated_at || '?'} · ${d.timezone || ''}`
          })
        ]
      }),
      jsx('div', {
        className: 'grid grid-cols-2 gap-2 md:grid-cols-6',
        children: [
          jsx(StatCard, {
            label: `Xiaomi (${win}d)`, value: fmtUsd((prov.xiaomi || {}).total ? prov.xiaomi.total.cost_usd : 0),
            detail: 'MiMo token plan'
          }),
          jsx(StatCard, {
            label: `OpenCode Go (${win}d)`, value: fmtUsd((prov['opencode-go'] || {}).total ? prov['opencode-go'].total.cost_usd : 0),
            detail: 'muse-spark-1.3-contributor · ledger, else list rates'
          }),
          jsx(StatCard, {
            label: `Meta (${win}d)`, value: fmtUsd((prov.meta || {}).total ? prov.meta.total.cost_usd : 0),
            detail: 'muse-spark-1.3-contributor · priced at list rates'
          }),
          jsx(StatCard, {
            label: `Local LM (${win}d)`, value: fmtTok((prov.local || {}).total ? prov.local.total.input_tokens + prov.local.total.output_tokens : 0) + ' tok',
            detail: '$0 API cost — compute only'
          }),
          jsx(StatCard, {
            label: `Vectorizer saved (${win}d, est.)`, value: fmtUsd(vec.total_usd_saved || 0),
            detail: fmtTok(vec.total_tokens_saved || 0) + ' tokens · ' + (vec.total_calls || 0) + ' retrievals'
          }),
          jsx(StatCard, {
            label: `Total spend (${win}d)`, value: fmtUsd(LANES.reduce((a, l) => a + ((prov[l.key] || {}).total ? prov[l.key].total.cost_usd : 0), 0)),
            detail: 'all providers'
          })
        ]
      }),
      jsx(BurnCard, { plan_burn: plan_burn }),
      jsxs('div', { className: 'flex flex-col gap-1', children: [
        jsx('div', { className: 'font-medium', children: 'Daily cost by provider (USD, est.)' }),
        jsx(BarChart, { days, series: costSeries, fmt: fmtUsd }),
        jsx(Legend, {})
      ]}),
      jsxs('div', { className: 'flex flex-col gap-1', children: [
        jsx('div', { className: 'font-medium', children: 'Daily tokens by provider (input + output)' }),
        jsx(BarChart, { days, series: tokSeries, fmt: fmtTok }),
        jsx(Legend, {})
      ]}),
      jsxs('div', { className: 'flex flex-col gap-1', children: [
        jsx('div', { className: 'font-medium', children: 'Daily tokens saved by Vectorizer (est., floor model)' }),
        jsx(BarChart, { days, series: vecSeries, fmt: fmtTok }),
        jsxs('div', {
          className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
          children: [
            `${vec.total_calls || 0} retrievals × ${fmtTok(m.avoided_tokens_per_call || 0)} avoided tokens/call`,
            ` (${m.replaced_per_lookup || 2}× knowledge read avg ${fmtTok(m.avg_knowledge_read_tokens || 0)} − retrieval avg ${fmtTok(m.avg_vectorizer_result_tokens || 0)})`,
            ` · ≈ ${fmtUsd(vec.total_usd_saved || 0)} at blended token price`
          ]
        })
      ]}),
      jsx('div', {
        className: 'flex flex-col gap-1 border-t border-(--ui-stroke-secondary) pt-3 text-[0.6875rem] text-(--ui-text-tertiary)',
        children: [
          jsx('div', { className: 'font-medium text-(--ui-text-secondary)', children: 'Method' }),
          ...(d.notes || []).map((n, i) => jsx('div', { children: '· ' + n }, i)),
          jsx('div', { children: '· Savings formula: ' + ((vec.method || {}).formula || '') })
        ]
      })
    ]
  })
}

export default {
  id: ID,
  name: 'Cost Chart',
  defaultEnabled: false,
  register(c) {
    ctx = c
    c.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: PATH },
        render: () => jsx(CostChartPage, {})
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: PATH, label: 'Cost Chart', codicon: 'graph' }
      },
      {
        id: 'palette',
        area: PALETTE_AREA,
        data: {
          id: 'open-cost-chart',
          label: 'Open Cost Chart',
          keywords: ['cost', 'chart', 'xiaomi', 'muse', 'vectorizer', 'spend', 'opencode', 'zen go'],
          run: () => host.navigate(PATH)
        }
      }
    ])
  }
}
