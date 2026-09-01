import React, { useEffect, useState } from 'react'
import apiClient from '../api/client'

interface AccuracyItem {
  key: string
  label: string
  total: number
  correct: number
  accuracy: number | null
}

interface AccuracyResponse {
  data: AccuracyItem[]
}

interface AccuracyFilters {
  dateFrom: string
  dateTo: string
  leagueId: string
  season: string
  team: string
}

type PctRange = { min: string; max: string }
type PctInputs = Record<'win' | 'over25' | 'handicap', PctRange>

const EMPTY_PCT: PctInputs = {
  win: { min: '', max: '' },
  over25: { min: '', max: '' },
  handicap: { min: '', max: '' },
}

// 各类别 pct 字段对应的后端查询参数与其业务含义
const PCT_FILTERS: Record<string, { minKey: string; maxKey: string; hint: string }> = {
  win: { minKey: 'win_pct_min', maxKey: 'win_pct_max', hint: '仅统计「胜负信心百分比」在该区间内的预测' },
  over25: { minKey: 'ou_pct_min', maxKey: 'ou_pct_max', hint: '仅统计「大小球概率百分比」在该区间内的预测' },
  handicap: { minKey: 'hand_pct_min', maxKey: 'hand_pct_max', hint: '仅统计「盘口赢盘概率百分比」在该区间内的预测' },
}

function useDebounced<T>(value: T, delay = 400): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(id)
  }, [value, delay])
  return debounced
}

export default function AccuracyPanel({ filters }: { filters: AccuracyFilters }) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<AccuracyItem[]>([])

  // 每行独立的置信度区间（本地状态，不影响父级全局筛选）
  const [pctInputs, setPctInputs] = useState<PctInputs>(EMPTY_PCT)
  const debouncedPct = useDebounced(pctInputs, 400)

  useEffect(() => {
    const fetchAccuracy = async () => {
    setLoading(true)
    setError(null)
    try {
      const params: Record<string, unknown> = {}
      if (filters.dateFrom) params.date_from = filters.dateFrom
      if (filters.dateTo) params.date_to = filters.dateTo
      if (filters.leagueId.trim()) params.league_id = filters.leagueId.trim()
      if (filters.season.trim()) params.season = filters.season.trim()
      if (filters.team.trim()) params.team = filters.team.trim()
      // 置信度区间：按类别分别下发，仅影响对应类别的样本与命中统计
      for (const [key, cfg] of Object.entries(PCT_FILTERS)) {
        const range = debouncedPct[key as keyof PctInputs]
        if (!range) continue
        const min = range.min.trim()
        const max = range.max.trim()
        if (min !== '' && Number.isFinite(Number(min))) params[cfg.minKey] = Number(min)
        if (max !== '' && Number.isFinite(Number(max))) params[cfg.maxKey] = Number(max)
      }
      const res = await apiClient.get<AccuracyResponse>('/predictions/accuracy', { params })
      setData(res.data.data || [])
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(msg || '加载预测准确率失败')
      setData([])
    } finally {
      setLoading(false)
    }
    }
    fetchAccuracy()
  }, [filters.dateFrom, filters.dateTo, filters.leagueId, filters.season, filters.team, debouncedPct])

  const setPctField = (key: keyof PctInputs, field: 'min' | 'max', value: string) => {
    setPctInputs((prev) => ({ ...prev, [key]: { ...prev[key], [field]: value } }))
  }

  const clearPct = (key: keyof PctInputs) => {
    setPctInputs((prev) => ({ ...prev, [key]: { min: '', max: '' } }))
  }

  const categoryColors = [
    'bg-primary-500',
    'bg-blue-500',
    'bg-amber-500',
    'bg-violet-500',
    'bg-rose-500',
  ]

  return (
    <div className="card mt-6 overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-200 px-6 py-5">
        <div>
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full bg-primary-500" />
            <h2 className="text-lg font-bold text-gray-900">预测统计</h2>
          </div>
          <p className="mt-1 text-sm text-gray-500">按预测类别查看模型的历史命中表现</p>
        </div>
        <span className="badge-blue">{data.length} 个类别</span>
      </div>

      {error && (
        <div className="px-4 py-3 text-sm text-red-600 dark:text-red-400">{error}</div>
      )}

      <div className="px-6 py-5">
        <div className="mb-3 hidden grid-cols-[190px_110px_minmax(140px,1fr)_210px] gap-4 px-4 text-xs font-medium uppercase tracking-wide text-gray-400 sm:grid">
          <span>预测类别</span>
          <span>样本表现</span>
          <span>成功率</span>
          <span>置信度区间</span>
        </div>
        <div className="space-y-3">
          {data.map((row, index) => {
            const percentage = row.accuracy === null ? 0 : Math.min(100, Math.max(0, row.accuracy * 100))
            const pctCfg = PCT_FILTERS[row.key]
            const pctRange = pctInputs[row.key as keyof PctInputs]
            const hasPct = !!pctRange && (pctRange.min !== '' || pctRange.max !== '')
            return (
              <div key={row.key} className="grid gap-4 rounded-xl border border-gray-200 bg-white p-4 transition-colors hover:border-primary-200 hover:bg-primary-50/30 sm:grid-cols-[190px_110px_minmax(140px,1fr)_210px] sm:items-center sm:gap-4">
                <div className="flex items-center gap-3">
                  <span className={`h-9 w-1.5 rounded-full ${categoryColors[index % categoryColors.length]}`} />
                  <div>
                    <p className="font-semibold text-gray-800">{row.label}</p>
                    <p className="mt-0.5 text-xs text-gray-400">历史预测表现</p>
                  </div>
                </div>
                <div className="flex items-center gap-5 text-sm">
                  <div><p className="text-xs text-gray-400">命中</p><p className="mt-0.5 font-semibold text-gray-700">{row.correct}</p></div>
                  <div><p className="text-xs text-gray-400">样本</p><p className="mt-0.5 font-semibold text-gray-700">{row.total}</p></div>
                </div>
                <div>
                  <div className="mb-2 flex items-center justify-between gap-3">
                    <span className="text-xs text-gray-400">成功率</span>
                    {row.accuracy === null ? <span className="font-semibold text-gray-400">—</span> : <span className="font-bold text-primary-600">{percentage.toFixed(1)}%</span>}
                  </div>
                  <div className="progress-bar h-2.5 bg-gray-100">
                    <div className="progress-fill bg-primary-500" style={{ width: `${percentage}%` }} />
                  </div>
                </div>

                {/* 置信度区间筛选：仅胜负 / 大小球 / 盘口 三类有对应 pct 字段 */}
                <div>
                  {pctCfg && pctRange ? (
                    <div className="flex items-center gap-1" title={pctCfg.hint}>
                      <input
                        type="number"
                        min={0}
                        max={100}
                        placeholder="0"
                        aria-label={`${row.label}置信度下限`}
                        className="input h-8 w-16 px-2 py-1 text-xs"
                        value={pctRange.min}
                        onChange={(e) => setPctField(row.key as keyof PctInputs, 'min', e.target.value)}
                      />
                      <span className="text-xs text-gray-400">% –</span>
                      <input
                        type="number"
                        min={0}
                        max={100}
                        placeholder="100"
                        aria-label={`${row.label}置信度上限`}
                        className="input h-8 w-16 px-2 py-1 text-xs"
                        value={pctRange.max}
                        onChange={(e) => setPctField(row.key as keyof PctInputs, 'max', e.target.value)}
                      />
                      <span className="text-xs text-gray-400">%</span>
                      {hasPct && (
                        <button
                          type="button"
                          onClick={() => clearPct(row.key as keyof PctInputs)}
                          title="清除该类别的区间筛选"
                          className="ml-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-600"
                        >
                          ×
                        </button>
                      )}
                    </div>
                  ) : (
                    <span className="text-xs text-gray-300">—</span>
                  )}
                </div>
              </div>
            )
          })}
          {!loading && data.length === 0 && !error && (
            <div className="rounded-xl border border-dashed border-gray-300 px-4 py-10 text-center text-sm text-gray-400">暂无符合条件的预测数据</div>
          )}
        </div>
      </div>

      {loading && (
        <div className="px-4 py-3 text-sm text-gray-400">加载中…</div>
      )}
    </div>
  )
}
