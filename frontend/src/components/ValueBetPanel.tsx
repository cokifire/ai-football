import { useEffect, useState } from 'react'
import apiClient from '../api/client'

interface Selection {
  side: string
  label: string
  model_p: number
  raw_model_p?: number
  market_implied: number
  edge: number
  kelly: number
  odds: number
  handicap_num?: number
  handicap_team?: string
}

interface Market {
  available: boolean
  recommendation?: string
  best_edge?: number | null
  selections?: Selection[]
  line?: number
}

interface ValueBetData {
  fixture_id: number
  margin: number
  markets: {
    '1x2': Market
    ou: Market
    ah: Market
  }
}

const MARKET_LABELS: Record<string, string> = { '1x2': '胜负平', ou: '大小球', ah: '让球盘' }
const fmtPct = (v: number) => `${(v * 100).toFixed(1)}%`

export default function ValueBetPanel({ fixtureId }: { fixtureId: number }) {
  const [data, setData] = useState<ValueBetData | null>(null)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')

  const load = (refresh: boolean) => {
    setLoading(true)
    setError('')
    apiClient
      .get(`/predictions/value-bets/${fixtureId}`, { params: { refresh, margin: 0.03 } })
      .then((res) => {
        const d = res.data
        if (d.error) {
          setError(d.error)
          setData(null)
        } else {
          setData(d as ValueBetData)
        }
      })
      .catch((err) => {
        setError(err.response?.data?.detail || err.message || '加载失败')
      })
      .finally(() => {
        setLoading(false)
        setRefreshing(false)
      })
  }

  useEffect(() => {
    load(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fixtureId])

  const renderMarket = (key: keyof typeof MARKET_LABELS) => {
    const m = data!.markets[key]
    if (!m || !m.available) {
      return <p className="text-xs text-gray-400">无对应赔率数据</p>
    }
    const hasValue = !!m.recommendation && !m.recommendation.includes('无价值')
    return (
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-gray-600">
            {MARKET_LABELS[key]}
            {m.line != null ? `（盘口 ${m.line}）` : ''}
          </span>
          <span className={hasValue ? 'badge-green' : 'badge-gray'}>{m.recommendation}</span>
        </div>
        <div className="table-container">
          <table>
            <thead>
                <tr>
                  <th>方向</th>
                  <th>模型概率（校准）</th>
                  <th>原始</th>
                  <th>市场隐含</th>
                  <th>Edge</th>
                  <th>Kelly</th>
                  <th>赔率</th>
                </tr>
              </thead>
              <tbody>
                {(m.selections || []).map((s, i) => (
                  <tr key={i}>
                    <td className="font-medium">{s.label}</td>
                    <td className="text-primary-700 font-semibold">{fmtPct(s.model_p)}</td>
                    <td className="text-gray-400 text-xs">
                      {s.raw_model_p != null ? fmtPct(s.raw_model_p) : '-'}
                    </td>
                    <td className="text-gray-500">{fmtPct(s.market_implied)}</td>
                    <td className={s.edge > 0 ? 'text-green-600 font-semibold' : 'text-gray-400'}>
                      {s.edge >= 0 ? '+' : ''}
                      {fmtPct(s.edge)}
                    </td>
                    <td className={s.kelly > 0 ? 'text-primary-700 font-semibold' : 'text-gray-400'}>
                      {fmtPct(s.kelly)}
                    </td>
                    <td className="font-medium">{s.odds}</td>
                  </tr>
                ))}
              </tbody>
          </table>
        </div>
      </div>
    )
  }

  if (loading) {
    return <p className="text-sm text-gray-400">价值投注分析中…</p>
  }

  if (error) {
    return (
      <div className="space-y-3">
        <p className="text-sm text-gray-500">{error}</p>
        {error.includes('赔率') && (
          <button className="btn btn-primary btn-sm" onClick={() => { setRefreshing(true); load(true) }} disabled={refreshing}>
            {refreshing ? '抓取中…' : '实时抓取赔率并分析'}
          </button>
        )}
      </div>
    )
  }

  if (!data) return null

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-400">
          模型概率已用 Platt scaling 校准（原始概率见「原始」列，避免未校准的极端 Kelly 误导）；Edge = 校准概率 − 市场隐含；Kelly 建议注码上限 25%
        </p>
        <button className="btn btn-secondary btn-xs" onClick={() => { setRefreshing(true); load(true) }} disabled={refreshing}>
          {refreshing ? '抓取中…' : '刷新（实时赔率）'}
        </button>
      </div>
      {(['1x2', 'ou', 'ah'] as const).map((k) => (
        <div key={k} className="card">
          <div className="card-body">{renderMarket(k)}</div>
        </div>
      ))}
    </div>
  )
}
