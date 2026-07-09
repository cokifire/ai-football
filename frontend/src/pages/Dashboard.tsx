import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import apiClient from '../api/client'
import Loading from '../components/Loading'

interface DashboardStats {
  leagues: number
  teams: number
  players: number
  fixtures: number
  predictions: number
  recent_predictions: Array<{
    id: number
    fixture_id: number
    home_team: string
    away_team: string
    win_prob: number
    draw_prob: number
    lose_prob: number
    date: string
  }>
}

export default function Dashboard() {
  const [stats, setStats] = useState<DashboardStats | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    apiClient
      .get('/dashboard/stats')
      .then((res) => setStats(res.data))
      .catch(() => {
        // 如果 /dashboard/stats 不存在，用各接口分别获取
        setStats({
          leagues: 0,
          teams: 0,
          players: 0,
          fixtures: 0,
          predictions: 0,
          recent_predictions: [],
        })
      })
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <Loading />

  const cards = [
    { label: '联赛数', value: stats?.leagues ?? '-', to: '/leagues', color: 'from-blue-500 to-blue-600' },
    { label: '球队数', value: stats?.teams ?? '-', to: '/teams', color: 'from-green-500 to-green-600' },
    { label: '球员数', value: stats?.players ?? '-', to: '/players', color: 'from-purple-500 to-purple-600' },
    { label: '比赛数', value: stats?.fixtures ?? '-', to: '/fixtures', color: 'from-orange-500 to-orange-600' },
    { label: '预测数', value: stats?.predictions ?? '-', to: '/predictions', color: 'from-pink-500 to-pink-600' },
  ]

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">仪表盘</h1>

      {/* 统计卡片 */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-4 mb-8">
        {cards.map((card) => (
          <Link
            key={card.label}
            to={card.to}
            className={`rounded-xl bg-gradient-to-br ${card.color} text-white p-5 hover:shadow-lg transition-shadow`}
          >
            <div className="text-3xl font-bold">{card.value}</div>
            <div className="text-sm opacity-80 mt-1">{card.label}</div>
          </Link>
        ))}
      </div>

      {/* 快捷入口 */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="card">
          <div className="card-header flex items-center justify-between">
            <h2 className="font-semibold">快捷操作</h2>
          </div>
          <div className="card-body grid grid-cols-2 gap-3">
            <Link to="/fixtures" className="btn btn-primary block text-center">
              查看赛程
            </Link>
            <Link to="/predictions" className="btn btn-secondary block text-center">
              查看预测
            </Link>
            <Link to="/leagues" className="btn btn-secondary block text-center">
              联赛管理
            </Link>
            <Link to="/scheduler" className="btn btn-secondary block text-center">
              数据同步
            </Link>
          </div>
        </div>

        {/* 最近预测 */}
        <div className="card">
          <div className="card-header">
            <h2 className="font-semibold">最近预测</h2>
          </div>
          <div className="card-body">
            {stats?.recent_predictions && stats.recent_predictions.length > 0 ? (
              <div className="space-y-3">
                {stats.recent_predictions.map((p) => (
                  <Link
                    key={p.id}
                    to="/predictions"
                    className="block p-3 rounded-lg border border-gray-100 hover:border-primary-200 hover:bg-primary-50 transition-colors"
                  >
                    <div className="font-medium text-sm">
                      {p.home_team} vs {p.away_team}
                    </div>
                    <div className="flex gap-3 mt-1 text-xs text-gray-500">
                      <span>胜: {(p.win_prob * 100).toFixed(1)}%</span>
                      <span>平: {(p.draw_prob * 100).toFixed(1)}%</span>
                      <span>负: {(p.lose_prob * 100).toFixed(1)}%</span>
                    </div>
                    <div className="text-xs text-gray-400 mt-1">{p.date}</div>
                  </Link>
                ))}
              </div>
            ) : (
              <p className="text-sm text-gray-400 py-4 text-center">
                暂无预测数据，请先同步数据并执行预测
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
