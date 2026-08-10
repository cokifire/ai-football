import { useEffect, useState } from 'react'
import apiClient from '../api/client'
import Loading from './Loading'
import Modal from './Modal'

interface RecentFixture {
  id: number
  date?: string | null
  league_name?: string | null
  home_id?: number | null
  home_name?: string | null
  home_logo?: string | null
  away_id?: number | null
  away_name?: string | null
  away_logo?: string | null
  goals_home?: number | null
  goals_away?: number | null
}

export interface TeamDetail {
  id: number
  name: string
  name_zh?: string | null
  logo?: string | null
  country?: string | null
  founded?: number | null
  venue?: { name?: string | null; capacity?: number | null } | null
  recent_fixtures?: RecentFixture[]
}

export default function TeamDetailModal({ teamId, onClose }: { teamId: number | null; onClose: () => void }) {
  const [team, setTeam] = useState<TeamDetail | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!teamId) return
    setTeam(null)
    setLoading(true)
    apiClient.get(`/teams/${teamId}`)
      .then((res) => setTeam(res.data))
      .catch(() => setTeam(null))
      .finally(() => setLoading(false))
  }, [teamId])

  return (
    <Modal open={teamId !== null} onClose={onClose} title={team?.name || '球队详情'} size="lg">
      {loading ? <Loading /> : team ? (
        <div className="space-y-5">
          <div className="flex items-start gap-3 sm:gap-4">
            {team.logo && <img src={team.logo} alt="" className="w-16 h-16 sm:w-20 sm:h-20 object-contain shrink-0" />}
            <div className="min-w-0 flex-1">
              <h3 className="text-xl font-bold break-words leading-tight">{team.name}</h3>
              {team.name_zh && <p className="text-sm text-gray-500 break-words">{team.name_zh}</p>}
              {team.country && <p className="text-gray-500 break-words">{team.country}</p>}
            </div>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
            {team.founded && <Info label="成立年份" value={team.founded} />}
            {team.venue?.name && <Info label="主场场馆" value={team.venue.name} />}
            {team.venue?.capacity && <Info label="场馆容量" value={team.venue.capacity.toLocaleString()} />}
          </div>
          <div>
            <h3 className="font-semibold mb-2">近10场比赛</h3>
            {team.recent_fixtures?.length ? (
              <div className="border rounded-lg divide-y divide-gray-100">
                {team.recent_fixtures.map((f) => {
                  const isHome = f.home_id === team.id
                  const gf = isHome ? f.goals_home : f.goals_away
                  const ga = isHome ? f.goals_away : f.goals_home
                  const result = gf == null || ga == null ? '-' : gf > ga ? '胜' : gf < ga ? '负' : '平'
                  const resultClass = result === '胜' ? 'badge-green' : result === '负' ? 'badge-red' : 'badge-yellow'
                  return <div key={f.id} className="px-3 py-3 text-sm">
                    <div className="flex items-center gap-2 mb-2 text-xs text-gray-400">
                      <span>{f.date?.substring(0, 10) || '-'}</span>
                      <span className="truncate">{f.league_name || '-'}</span>
                      <span className="ml-auto">#{f.id}</span>
                    </div>
                    <div className="flex items-start justify-center gap-2 sm:items-center">
                      <span className="min-w-0 flex-1 break-words text-right leading-tight">{f.home_name || '-'}</span>
                      <span className="font-bold whitespace-nowrap">{f.goals_home ?? '-'} - {f.goals_away ?? '-'}</span>
                      <span className="min-w-0 flex-1 break-words leading-tight">{f.away_name || '-'}</span>
                      <span className={`${resultClass} shrink-0`}>{result}</span>
                    </div>
                  </div>
                })}
              </div>
            ) : <p className="text-sm text-gray-400 py-3">暂无已完赛记录</p>}
          </div>
        </div>
      ) : <p className="text-sm text-gray-500">球队详情加载失败</p>}
    </Modal>
  )
}

function Info({ label, value }: { label: string; value: string | number }) {
  return <div className="p-3 rounded-lg bg-gray-50"><div className="text-xs text-gray-500">{label}</div><div className="font-semibold">{value}</div></div>
}
