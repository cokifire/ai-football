import { useEffect, useState } from 'react'
import apiClient from '../api/client'
import Loading from '../components/Loading'
import Pagination from '../components/Pagination'
import Modal from '../components/Modal'

interface Player {
  id: number
  name: string
  photo?: string
  nationality?: string
  position?: string
  birth_date?: string
  stats?: PlayerStats[]
}

interface PlayerStats {
  season: string
  appearances: number
  goals: number
  assists: number
  yellow_cards: number
  red_cards: number
  minutes_played: number
}

export default function PlayersPage() {
  const [players, setPlayers] = useState<Player[]>([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [nationality, setNationality] = useState('')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [selectedPlayer, setSelectedPlayer] = useState<Player | null>(null)
  const [playerDetail, setPlayerDetail] = useState<Player | null>(null)
  const pageSize = 20

  const fetchPlayers = () => {
    setLoading(true)
    apiClient
      .get('/players', {
        params: {
          page,
          page_size: pageSize,
          search: search || undefined,
          nationality: nationality || undefined,
        },
      })
      .then((res) => {
        setPlayers(res.data.items || res.data.data || [])
        setTotal(res.data.total || 0)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    fetchPlayers()
  }, [page])

  const handleSearch = () => {
    setPage(1)
    fetchPlayers()
  }

  const viewDetail = (player: Player) => {
    setSelectedPlayer(player)
    apiClient
      .get(`/players/${player.id}`)
      .then((res) => setPlayerDetail(res.data))
      .catch(() => setPlayerDetail(player))
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">球员浏览</h1>

      <div className="card mb-6">
        <div className="card-body">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex-1 min-w-[200px]">
              <label className="block text-xs text-gray-500 mb-1">搜索球员</label>
              <input
                className="input"
                placeholder="输入球员姓名（支持中英文）..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
              />
            </div>
            <div className="w-40">
              <label className="block text-xs text-gray-500 mb-1">国籍</label>
              <input
                className="input"
                placeholder="例如: China"
                value={nationality}
                onChange={(e) => setNationality(e.target.value)}
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
                  <th>照片</th>
                  <th>姓名</th>
                  <th>国籍</th>
                  <th>位置</th>
                  <th>出生日期</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {players.map((player) => (
                  <tr key={player.id}>
                    <td className="text-gray-400 text-xs">{player.id}</td>
                    <td>
                      {player.photo ? (
                        <img src={player.photo} alt="" className="w-8 h-8 rounded-full object-cover" />
                      ) : (
                        <div className="w-8 h-8 rounded-full bg-gray-200 flex items-center justify-center text-xs text-gray-400">
                          {player.name?.charAt(0)}
                        </div>
                      )}
                    </td>
                    <td className="font-medium">{player.name}</td>
                    <td>{player.nationality || '-'}</td>
                    <td>
                      <span className="badge-blue">{player.position || '-'}</span>
                    </td>
                    <td className="text-xs text-gray-500">{player.birth_date || '-'}</td>
                    <td>
                      <button className="btn btn-secondary btn-xs" onClick={() => viewDetail(player)}>
                        详情
                      </button>
                    </td>
                  </tr>
                ))}
                {players.length === 0 && (
                  <tr>
                    <td colSpan={7} className="text-center text-gray-400 py-8">
                      {search ? (
                        <div>
                          <p>未找到匹配 "{search}" 的球员</p>
                          <p className="text-xs mt-1">
                            提示：中文名翻译尚未完成，可尝试用英文名搜索（如 Messi、Ronaldo、Mbappé）
                          </p>
                        </div>
                      ) : (
                        '暂无球员数据，请先同步球员'
                      )}
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
        open={!!selectedPlayer}
        onClose={() => setSelectedPlayer(null)}
        title={selectedPlayer?.name || '球员详情'}
        size="md"
      >
        {playerDetail ? (
          <div className="space-y-4">
            <div className="flex items-center gap-4">
              {playerDetail.photo ? (
                <img src={playerDetail.photo} alt="" className="w-16 h-16 rounded-full object-cover" />
              ) : (
                <div className="w-16 h-16 rounded-full bg-gray-200 flex items-center justify-center text-2xl text-gray-400">
                  {playerDetail.name?.charAt(0)}
                </div>
              )}
              <div>
                <h3 className="text-xl font-bold">{playerDetail.name}</h3>
                <div className="flex gap-2 mt-1">
                  <span className="badge-blue">{playerDetail.position || '未知位置'}</span>
                  <span className="badge-gray">{playerDetail.nationality || '未知国籍'}</span>
                </div>
                {playerDetail.birth_date && (
                  <p className="text-sm text-gray-500 mt-1">出生: {playerDetail.birth_date}</p>
                )}
              </div>
            </div>

            {playerDetail.stats && playerDetail.stats.length > 0 && (
              <div>
                <h4 className="font-semibold mb-2">赛季统计</h4>
                <div className="table-container">
                  <table>
                    <thead>
                      <tr>
                        <th>赛季</th>
                        <th>出场</th>
                        <th>进球</th>
                        <th>助攻</th>
                        <th>黄牌</th>
                        <th>红牌</th>
                        <th>分钟</th>
                      </tr>
                    </thead>
                    <tbody>
                      {playerDetail.stats.map((stat, i) => (
                        <tr key={i}>
                          <td className="font-medium">{stat.season}</td>
                          <td>{stat.appearances}</td>
                          <td className="text-green-600 font-medium">{stat.goals}</td>
                          <td className="text-blue-600">{stat.assists}</td>
                          <td>{stat.yellow_cards}</td>
                          <td>{stat.red_cards}</td>
                          <td>{stat.minutes_played}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
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
