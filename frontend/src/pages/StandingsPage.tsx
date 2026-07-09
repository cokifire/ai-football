import { useEffect, useState, useMemo, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import apiClient from '../api/client'
import Loading from '../components/Loading'

interface LeagueOption {
  id: number
  name: string
  logo: string | null
  country: string | null
}

interface Standing {
  id: number
  league_id: number
  season: number
  group_name: string | null
  rank: number
  team_id: number
  team_name: string
  team_logo: string | null
  points: number | null
  goals_diff: number | null
  form: string | null
  status: string | null
  description: string | null
  all_played: number | null
  all_win: number | null
  all_draw: number | null
  all_lose: number | null
  all_goals_for: number | null
  all_goals_against: number | null
}

const statusColor: Record<string, string> = {
  up: 'bg-green-50 border-l-4 border-green-500',
  down: 'bg-red-50 border-l-4 border-red-500',
  same: '',
}

function FormBadges({ form }: { form: string | null }) {
  if (!form) return <span className="text-gray-300">-</span>
  return (
    <div className="flex gap-0.5">
      {form.split('').map((c, i) => {
        const cls =
          c === 'W'
            ? 'bg-green-500 text-white'
            : c === 'D'
            ? 'bg-gray-400 text-white'
            : c === 'L'
            ? 'bg-red-500 text-white'
            : 'bg-gray-200 text-gray-600'
        return (
          <span
            key={i}
            className={`w-5 h-5 inline-flex items-center justify-center rounded text-[10px] font-bold ${cls}`}
          >
            {c}
          </span>
        )
      })}
    </div>
  )
}

export default function StandingsPage() {
  const [leagues, setLeagues] = useState<LeagueOption[]>([])
  const [leagueId, setLeagueId] = useState<number | ''>('')
  const [seasons, setSeasons] = useState<number[]>([])
  const [season, setSeason] = useState<number | ''>('')
  const [standings, setStandings] = useState<Standing[]>([])
  const [loading, setLoading] = useState(false)
  const [loadingMeta, setLoadingMeta] = useState(true)
  const [searchParams, setSearchParams] = useSearchParams()
  const initialLeagueRef = useRef(searchParams.get('league'))

  // 加载启用的联赛
  useEffect(() => {
    setLoadingMeta(true)
    apiClient
      .get('/leagues', { params: { enabled: true, page_size: 200 } })
      .then((res) => {
        const items: LeagueOption[] = (res.data.items || res.data.data || []).map(
          (l: any) => ({
            id: l.id,
            name: l.name,
            logo: l.logo || null,
            country: l.country_name || null,
          }),
        )
        setLeagues(items)
        const param = initialLeagueRef.current
        const initial = items.find((l) => String(l.id) === param)
        setLeagueId(initial ? initial.id : items.length > 0 ? items[0].id : '')
      })
      .catch(() => {})
      .finally(() => setLoadingMeta(false))
  }, [])

  const handleLeagueChange = (id: number) => {
    setLeagueId(id)
    setSearchParams({ league: String(id) }, { replace: true })
  }

  // 联赛切换 -> 加载赛季
  useEffect(() => {
    if (leagueId === '') return
    setSeasons([])
    setSeason('')
    apiClient
      .get('/standings/seasons', { params: { league_id: leagueId } })
      .then((res) => {
        const list: number[] = res.data || []
        setSeasons(list)
        if (list.length > 0) setSeason(list[0])
      })
      .catch(() => setSeasons([]))
  }, [leagueId])

  // 联赛/赛季变化 -> 加载积分榜
  useEffect(() => {
    if (leagueId === '' || season === '') return
    setLoading(true)
    apiClient
      .get('/standings', {
        params: { league_id: leagueId, season, page: 1, page_size: 100 },
      })
      .then((res) => {
        setStandings(res.data.data || res.data.items || [])
      })
      .catch(() => setStandings([]))
      .finally(() => setLoading(false))
  }, [leagueId, season])

  // 按分组聚合
  const groups = useMemo(() => {
    const map = new Map<string, Standing[]>()
    for (const s of standings) {
      const key = s.group_name || '总积分榜'
      if (!map.has(key)) map.set(key, [])
      map.get(key)!.push(s)
    }
    return Array.from(map.entries())
  }, [standings])

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">积分榜</h1>

      {/* 选择区 */}
      <div className="card mb-6">
        <div className="card-body">
          <div className="flex flex-wrap items-end gap-4">
            <div className="w-64">
              <label className="block text-xs text-gray-500 mb-1">联赛</label>
              {loadingMeta ? (
                <div className="select flex items-center text-gray-400">加载中…</div>
              ) : (
                <select
                  className="select"
                  value={leagueId}
                  onChange={(e) => handleLeagueChange(Number(e.target.value))}
                >
                  {leagues.length === 0 && <option value="">暂无启用的联赛</option>}
                  {leagues.map((l) => (
                    <option key={l.id} value={l.id}>
                      {l.name}
                      {l.country ? `（${l.country}）` : ''}
                    </option>
                  ))}
                </select>
              )}
            </div>
            <div className="w-40">
              <label className="block text-xs text-gray-500 mb-1">赛季</label>
              <select
                className="select"
                value={season}
                disabled={seasons.length === 0}
                onChange={(e) => setSeason(Number(e.target.value))}
              >
                {seasons.length === 0 && <option value="">无数据</option>}
                {seasons.map((s) => (
                  <option key={s} value={s}>
                    {s} 赛季
                  </option>
                ))}
              </select>
            </div>
            {season !== '' && (
              <span className="text-sm text-gray-500 pb-1">
                共 {standings.length} 支球队
              </span>
            )}
          </div>
        </div>
      </div>

      {/* 积分榜内容 */}
      {loading ? (
        <Loading />
      ) : standings.length === 0 ? (
        <div className="card">
          <div className="text-center text-gray-400 py-12">
            {leagueId === '' || seasons.length === 0
              ? '该联赛暂无积分榜数据，请先在「数据同步」中同步积分榜'
              : '该赛季暂无积分榜数据'}
          </div>
        </div>
      ) : (
        groups.map(([groupName, rows]) => (
          <div className="card mb-6" key={groupName}>
            <div className="px-4 py-3 border-b border-gray-100 font-semibold">
              {groupName}
            </div>
            <div className="table-container">
              <table>
                <thead>
                  <tr>
                    <th className="w-10 text-center">#</th>
                    <th>球队</th>
                    <th className="text-center">赛</th>
                    <th className="text-center">胜</th>
                    <th className="text-center">平</th>
                    <th className="text-center">负</th>
                    <th className="text-center">进</th>
                    <th className="text-center">失</th>
                    <th className="text-center">净</th>
                    <th className="text-center">近况</th>
                    <th className="text-center">积分</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((s) => (
                    <tr
                      key={s.id}
                      className={statusColor[s.status || ''] || ''}
                      title={s.description || ''}
                    >
                      <td className="text-center font-bold text-gray-700">{s.rank}</td>
                      <td>
                        <div className="flex items-center gap-2">
                          {s.team_logo ? (
                            <img
                              src={s.team_logo}
                              alt=""
                              className="w-6 h-6 object-contain"
                            />
                          ) : (
                            <div className="w-6 h-6 rounded-full bg-gray-200" />
                          )}
                          <span className="font-medium">{s.team_name}</span>
                          {s.description && (
                            <span className="text-[11px] text-gray-400 hidden lg:inline">
                              {s.description}
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="text-center">{s.all_played ?? '-'}</td>
                      <td className="text-center">{s.all_win ?? '-'}</td>
                      <td className="text-center">{s.all_draw ?? '-'}</td>
                      <td className="text-center">{s.all_lose ?? '-'}</td>
                      <td className="text-center">{s.all_goals_for ?? '-'}</td>
                      <td className="text-center">{s.all_goals_against ?? '-'}</td>
                      <td className="text-center">
                        {s.goals_diff === null || s.goals_diff === undefined
                          ? '-'
                          : s.goals_diff > 0
                          ? `+${s.goals_diff}`
                          : s.goals_diff}
                      </td>
                      <td className="text-center">
                        <FormBadges form={s.form} />
                      </td>
                      <td className="text-center font-bold text-primary-700">
                        {s.points ?? '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))
      )}
    </div>
  )
}
