import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import apiClient from '../api/client'
import Loading from '../components/Loading'
import Pagination from '../components/Pagination'
import Modal from '../components/Modal'

interface League {
  id: number
  name: string
  country: string
  logo: string
  enabled: boolean
  seasons?: Season[]
}

interface Season {
  id: number
  season: string
  start_date: string
  end_date: string
  current: boolean
}

export default function LeaguesPage() {
  const [leagues, setLeagues] = useState<League[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [filterEnabled, setFilterEnabled] = useState<string>('')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [selectedLeague, setSelectedLeague] = useState<League | null>(null)
  const [leagueDetail, setLeagueDetail] = useState<League | null>(null)
  const pageSize = 20

  const fetchLeagues = () => {
    setLoading(true)
    apiClient
      .get('/leagues', {
        params: { page, page_size: pageSize, search: search || undefined, enabled: filterEnabled || undefined },
      })
      .then((res) => {
        setLeagues(res.data.items || res.data.data || [])
        setTotal(res.data.total || 0)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    fetchLeagues()
  }, [page, filterEnabled])

  const handleSearch = () => {
    setPage(1)
    fetchLeagues()
  }

  const viewDetail = (league: League) => {
    setSelectedLeague(league)
    setLeagueDetail(null)
    apiClient
      .get(`/leagues/${league.id}`)
      .then((res) => setLeagueDetail(res.data))
      .catch(() => setLeagueDetail(league))
  }

  const toggleEnabled = (league: League) => {
    apiClient
      .patch(`/leagues/${league.id}/toggle`)
      .then(() => fetchLeagues())
      .catch((err) => alert('操作失败: ' + (err.response?.data?.detail || err.message)))
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">联赛管理</h1>

      {/* 搜索筛选 */}
      <div className="card mb-6">
        <div className="card-body">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex-1 min-w-[200px]">
              <label className="block text-xs text-gray-500 mb-1">搜索</label>
              <input
                className="input"
                placeholder="搜索联赛名称..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
              />
            </div>
            <div className="w-40">
              <label className="block text-xs text-gray-500 mb-1">状态</label>
              <select
                className="select"
                value={filterEnabled}
                onChange={(e) => { setFilterEnabled(e.target.value); setPage(1) }}
              >
                <option value="">全部</option>
                <option value="true">已启用</option>
                <option value="false">已禁用</option>
              </select>
            </div>
            <button className="btn btn-primary" onClick={handleSearch}>
              搜索
            </button>
          </div>
        </div>
      </div>

      {/* 列表 */}
      {loading ? (
        <Loading />
      ) : (
        <div className="card">
          <div className="table-container">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>联赛名称</th>
                  <th>国家</th>
                  <th>Logo</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {leagues.map((league) => (
                  <tr key={league.id}>
                    <td className="text-gray-400 text-xs">{league.id}</td>
                    <td className="font-medium">
                      <Link
                        to={`/standings?league=${league.id}`}
                        className="text-primary-700 hover:underline"
                        title="查看积分榜"
                      >
                        {league.name}
                      </Link>
                    </td>
                    <td>{league.country}</td>
                    <td>
                      {league.logo ? (
                        <img src={league.logo} alt="" className="w-6 h-6 object-contain" />
                      ) : (
                        '-'
                      )}
                    </td>
                    <td>
                      <span className={league.enabled ? 'badge-green' : 'badge-gray'}>
                        {league.enabled ? '启用' : '禁用'}
                      </span>
                    </td>
                    <td>
                      <div className="flex gap-2">
                        <button className="btn btn-secondary btn-xs" onClick={() => viewDetail(league)}>
                          详情
                        </button>
                        <button
                          className={league.enabled ? 'btn btn-secondary btn-xs' : 'btn btn-primary btn-xs'}
                          onClick={() => toggleEnabled(league)}
                        >
                          {league.enabled ? '禁用' : '启用'}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
                {leagues.length === 0 && (
                  <tr>
                    <td colSpan={6} className="text-center text-gray-400 py-8">
                      暂无联赛数据，请先同步联赛
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="px-6 py-3">
            <Pagination page={page} pageSize={pageSize} total={total} onChange={setPage} />
          </div>
        </div>
      )}

      {/* 详情弹窗 */}
      <Modal
        open={!!selectedLeague}
        onClose={() => setSelectedLeague(null)}
        title={selectedLeague?.name || '联赛详情'}
        size="md"
      >
        {leagueDetail ? (
          <div className="space-y-4">
            <div className="flex items-center gap-4">
              {leagueDetail.logo && (
                <img src={leagueDetail.logo} alt="" className="w-16 h-16 object-contain" />
              )}
              <div>
                <h3 className="text-xl font-bold">{leagueDetail.name}</h3>
                <p className="text-gray-500">{leagueDetail.country}</p>
              </div>
            </div>
            <div>
              <span className={leagueDetail.enabled ? 'badge-green' : 'badge-gray'}>
                {leagueDetail.enabled ? '已启用' : '已禁用'}
              </span>
            </div>
            {leagueDetail.seasons && leagueDetail.seasons.length > 0 && (
              <div>
                <h4 className="font-semibold mb-2">赛季</h4>
                <div className="space-y-2">
                  {leagueDetail.seasons.map((s: Season) => (
                    <div
                      key={s.id}
                      className="flex items-center justify-between p-2 rounded-lg border border-gray-100"
                    >
                      <span className="font-medium">{s.season}</span>
                      <div className="flex items-center gap-2">
                        <span className="text-xs text-gray-500">
                          {s.start_date} ~ {s.end_date}
                        </span>
                        {s.current && <span className="badge-blue">当前</span>}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        ) : (
          <Loading />
        )}
      </Modal>
    </div>
  )
}
