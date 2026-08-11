import React, { useEffect, useState, useMemo, useCallback } from 'react'
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

interface LeagueOption {
  id: number
  name: string
  name_zh?: string
  seasons?: { year: number }[]
}

function useDebounced<T>(value: T, delay = 400): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(id)
  }, [value, delay])
  return debounced
}

const CATEGORIES = ['胜平负', 'WDL', '大小球', '亚洲盘', '比分']

export default function AccuracyPanel() {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<AccuracyItem[]>([])

  // 联赛 / 赛季选项
  const [leagues, setLeagues] = useState<LeagueOption[]>([])
  const [leagueId, setLeagueId] = useState<number | ''>('')
  const [season, setSeason] = useState<number | ''>('')
  const seasons = useMemo(() => {
    const lg = leagues.find((l) => l.id === leagueId)
    return lg?.seasons?.map((s) => s.year) || []
  }, [leagues, leagueId])

  // 筛选条件
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [team, setTeam] = useState('')
  const [category, setCategory] = useState<string | ''>('')

  const debTeam = useDebounced(team, 500)

  // 加载已启用联赛（含赛季）
  useEffect(() => {
    let cancelled = false
    apiClient
      .get('/leagues', { params: { enabled: true, page_size: 200 } })
      .then((res) => {
        if (!cancelled) setLeagues(res.data.data || [])
      })
      .catch(() => {
        /* 联赛列表失败不阻断准确率面板 */
      })
    return () => {
      cancelled = true
    }
  }, [])

  // 联赛变化时重置赛季选择
  useEffect(() => {
    setSeason('')
  }, [leagueId])

  const fetchAccuracy = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const params: Record<string, unknown> = {}
      if (dateFrom) params.date_from = dateFrom
      if (dateTo) params.date_to = dateTo
      if (leagueId !== '') params.league_id = leagueId
      if (season !== '') params.season = season
      if (debTeam.trim()) params.team = debTeam.trim()
      if (category) params.category = category
      const res = await apiClient.get<AccuracyResponse>('/predictions/accuracy', { params })
      setData(res.data.data || [])
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(msg || '加载预测准确率失败')
      setData([])
    } finally {
      setLoading(false)
    }
  }, [dateFrom, dateTo, leagueId, season, debTeam, category])

  useEffect(() => {
    fetchAccuracy()
  }, [fetchAccuracy])

  const resetFilters = () => {
    setDateFrom('')
    setDateTo('')
    setLeagueId('')
    setSeason('')
    setTeam('')
    setCategory('')
  }

  const hasFilter = dateFrom || dateTo || leagueId !== '' || season !== '' || debTeam.trim() || category

  const inputCls =
    'rounded-md border border-gray-300 dark:border-slate-600 bg-white dark:bg-slate-800 px-2 py-1.5 text-sm text-gray-800 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-emerald-500'
  const labelCls = 'text-xs font-medium text-gray-500 dark:text-gray-400'
  const cell = 'px-4 py-3 text-sm'

  return (
    <div className="rounded-lg border border-gray-200 dark:border-slate-700 bg-white dark:bg-slate-900 shadow-sm">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-3 border-b border-gray-200 dark:border-slate-700 px-4 py-3">
        <span className="text-sm font-semibold text-gray-700 dark:text-gray-200">筛选</span>

        <div className="flex flex-col gap-1">
          <label className={labelCls}>联赛</label>
          <select
            className={inputCls}
            value={leagueId}
            onChange={(e) => setLeagueId(e.target.value === '' ? '' : Number(e.target.value))}
          >
            <option value="">全部</option>
            {leagues.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name_zh || l.name}
              </option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label className={labelCls}>赛季</label>
          <select
            className={inputCls}
            value={season}
            onChange={(e) => setSeason(e.target.value === '' ? '' : Number(e.target.value))}
            disabled={!leagueId || seasons.length === 0}
          >
            <option value="">全部</option>
            {seasons.map((y) => (
              <option key={y} value={y}>
                {y}
              </option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label className={labelCls}>起始日期</label>
          <input
            type="date"
            className={inputCls}
            value={dateFrom}
            max={dateTo || undefined}
            onChange={(e) => setDateFrom(e.target.value)}
          />
        </div>

        <div className="flex flex-col gap-1">
          <label className={labelCls}>结束日期</label>
          <input
            type="date"
            className={inputCls}
            value={dateTo}
            min={dateFrom || undefined}
            onChange={(e) => setDateTo(e.target.value)}
          />
        </div>

        <div className="flex flex-col gap-1">
          <label className={labelCls}>球队</label>
          <input
            type="text"
            className={inputCls}
            placeholder="队名含…"
            value={team}
            onChange={(e) => setTeam(e.target.value)}
          />
        </div>

        <div className="flex flex-col gap-1">
          <label className={labelCls}>类别</label>
          <select
            className={inputCls}
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          >
            <option value="">全部</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </div>

        <button
          type="button"
          onClick={resetFilters}
          disabled={!hasFilter}
          className="self-end rounded-md border border-gray-300 dark:border-slate-600 px-3 py-1.5 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-slate-800 disabled:opacity-40"
        >
          重置
        </button>
      </div>

      {error && (
        <div className="px-4 py-3 text-sm text-red-600 dark:text-red-400">{error}</div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-left">
          <thead>
            <tr className="border-b border-gray-200 dark:border-slate-700 text-gray-500 dark:text-gray-400">
              <th className={cell + ' font-medium'}>预测类别</th>
              <th className={cell + ' font-medium'}>样本数</th>
              <th className={cell + ' font-medium'}>命中数</th>
              <th className={cell + ' font-medium'}>准确率</th>
            </tr>
          </thead>
          <tbody>
            {data.map((row) => (
              <tr
                key={row.key}
                className="border-b border-gray-100 dark:border-slate-800 last:border-0"
              >
                <td className={cell + ' font-medium text-gray-800 dark:text-gray-100'}>
                  {row.label}
                </td>
                <td className={cell + ' text-gray-600 dark:text-gray-300'}>{row.total}</td>
                <td className={cell + ' text-gray-600 dark:text-gray-300'}>{row.correct}</td>
                <td className={cell}>
                  {row.accuracy === null ? (
                    <span className="text-gray-400">—</span>
                  ) : (
                    <span className="font-semibold text-emerald-600 dark:text-emerald-400">
                      {(row.accuracy * 100).toFixed(1)}%
                    </span>
                  )}
                </td>
              </tr>
            ))}
            {!loading && data.length === 0 && !error && (
              <tr>
                <td colSpan={4} className={cell + ' text-center text-gray-400'}>
                  暂无符合条件的预测数据
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {loading && (
        <div className="px-4 py-3 text-sm text-gray-400">加载中…</div>
      )}
    </div>
  )
}
