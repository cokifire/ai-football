import { useEffect, useState } from 'react'
import apiClient from '../api/client'
import Loading from '../components/Loading'
import Pagination from '../components/Pagination'
import Modal from '../components/Modal'

interface Team {
  id: number
  name: string
  logo: string
  country?: string
  founded?: number
  venue_name?: string
  venue_capacity?: number
}

export default function TeamsPage() {
  const [teams, setTeams] = useState<Team[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [selectedTeam, setSelectedTeam] = useState<Team | null>(null)
  const [teamDetail, setTeamDetail] = useState<Team | null>(null)
  const pageSize = 20

  const fetchTeams = () => {
    setLoading(true)
    apiClient
      .get('/teams', {
        params: { page, page_size: pageSize, search: search || undefined },
      })
      .then((res) => {
        setTeams(res.data.items || res.data.data || [])
        setTotal(res.data.total || 0)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    fetchTeams()
  }, [page])

  const handleSearch = () => {
    setPage(1)
    fetchTeams()
  }

  const viewDetail = (team: Team) => {
    setSelectedTeam(team)
    apiClient
      .get(`/teams/${team.id}`)
      .then((res) => setTeamDetail(res.data))
      .catch(() => setTeamDetail(team))
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">球队浏览</h1>

      <div className="card mb-6">
        <div className="card-body">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex-1 min-w-[200px]">
              <label className="block text-xs text-gray-500 mb-1">搜索球队</label>
              <input
                className="input"
                placeholder="输入球队名称..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
              />
            </div>
            <button className="btn btn-primary" onClick={handleSearch}>
              搜索
            </button>
          </div>
        </div>
      </div>

      {loading ? (
        <Loading />
      ) : (
        <div className="card">
          <div className="table-container">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Logo</th>
                  <th>球队名称</th>
                  <th>国家</th>
                  <th>主场</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {teams.map((team) => (
                  <tr key={team.id}>
                    <td className="text-gray-400 text-xs">{team.id}</td>
                    <td>
                      {team.logo ? (
                        <img src={team.logo} alt="" className="w-8 h-8 object-contain" />
                      ) : (
                        <div className="w-8 h-8 rounded-full bg-gray-200" />
                      )}
                    </td>
                    <td className="font-medium">{team.name}</td>
                    <td>{team.country || '-'}</td>
                    <td className="text-xs text-gray-500">{team.venue_name || '-'}</td>
                    <td>
                      <button className="btn btn-secondary btn-xs" onClick={() => viewDetail(team)}>
                        详情
                      </button>
                    </td>
                  </tr>
                ))}
                {teams.length === 0 && (
                  <tr>
                    <td colSpan={6} className="text-center text-gray-400 py-8">
                      暂无球队数据，请先同步球队
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

      <Modal
        open={!!selectedTeam}
        onClose={() => setSelectedTeam(null)}
        title={selectedTeam?.name || '球队详情'}
        size="sm"
      >
        {teamDetail ? (
          <div className="space-y-4">
            <div className="flex items-center gap-4">
              {teamDetail.logo && (
                <img src={teamDetail.logo} alt="" className="w-20 h-20 object-contain" />
              )}
              <div>
                <h3 className="text-xl font-bold">{teamDetail.name}</h3>
                <p className="text-gray-500">{teamDetail.country}</p>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3">
              {teamDetail.founded && (
                <div className="p-3 rounded-lg bg-gray-50">
                  <div className="text-xs text-gray-500">成立年份</div>
                  <div className="font-semibold">{teamDetail.founded}</div>
                </div>
              )}
              {teamDetail.venue_name && (
                <div className="p-3 rounded-lg bg-gray-50">
                  <div className="text-xs text-gray-500">主场场馆</div>
                  <div className="font-semibold">{teamDetail.venue_name}</div>
                </div>
              )}
              {teamDetail.venue_capacity && (
                <div className="p-3 rounded-lg bg-gray-50">
                  <div className="text-xs text-gray-500">场馆容量</div>
                  <div className="font-semibold">
                    {teamDetail.venue_capacity.toLocaleString()}
                  </div>
                </div>
              )}
            </div>
          </div>
        ) : (
          <Loading />
        )}
      </Modal>
    </div>
  )
}
