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

export default function AccuracyPanel({ filters }: { filters: AccuracyFilters }) {
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<AccuracyItem[]>([])

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
  }, [filters.dateFrom, filters.dateTo, filters.leagueId, filters.season, filters.team])

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
        <div className="mb-3 hidden grid-cols-[220px_140px_minmax(180px,1fr)] gap-4 px-4 text-xs font-medium uppercase tracking-wide text-gray-400 sm:grid">
          <span>预测类别</span>
          <span>样本表现</span>
          <span>成功率</span>
        </div>
        <div className="space-y-3">
          {data.map((row, index) => {
            const percentage = row.accuracy === null ? 0 : Math.min(100, Math.max(0, row.accuracy * 100))
            return (
              <div key={row.key} className="grid gap-4 rounded-xl border border-gray-200 bg-white p-4 transition-colors hover:border-primary-200 hover:bg-primary-50/30 sm:grid-cols-[220px_140px_minmax(180px,1fr)] sm:items-center sm:gap-4">
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
