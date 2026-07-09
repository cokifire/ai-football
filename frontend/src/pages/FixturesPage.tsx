import { useEffect, useState } from 'react'
import apiClient from '../api/client'
import Loading from '../components/Loading'
import Pagination from '../components/Pagination'
import Modal from '../components/Modal'

interface Fixture {
  id: number
  league_id: number
  league_name?: string
  home_id: number
  away_id: number
  home_name?: string
  away_name?: string
  home_logo?: string
  away_logo?: string
  date: string
  status_short: string
  goals_home?: number
  goals_away?: number
  round?: string
  venue_name?: string
  venue_city?: string
}

interface FixtureDetail extends Fixture {
  events?: FixtureAPIEvent[]
  lineups?: FixtureAPILineup[]
  statistics?: FixtureAPIStat[]
  player_stats?: FixtureAPIPlayerStat[]
}

interface FixtureAPIEvent {
  elapsed?: number
  extra?: number
  type?: string
  detail?: string
  comments?: string
  team_id?: number
  team_name?: string
  player_id?: number
  player_name?: string
}

interface FixtureAPILineup {
  team_id?: number
  team_name?: string
  formation?: string
  player_name?: string
  player_number?: number
  player_position?: string
  is_substitute: boolean
}

interface FixtureAPIStat {
  team_id?: number
  team_name?: string
  stat_type?: string
  stat_value?: string
}

interface FixtureAPIPlayerStat {
  team_id?: number
  team_name?: string
  player_name?: string
  player_photo?: string
  player_number?: string
  player_position?: string
  games?: { minutes?: number }
  goals?: { total?: number }
  offsides?: number
  shots?: { total?: number }
  passes?: { total?: number; accuracy?: string }
  tackles?: { total?: number }
  duels?: { total?: number }
  dribbles?: { attempts?: number }
  fouls?: { committed?: number; drawn?: number }
  cards?: { yellow?: number; red?: number }
}

interface OddsEntry {
  date: string
  home_odd?: number | null
  draw_odd?: number | null
  away_odd?: number | null
  home_raw?: number | null
  draw_raw?: number | null
  away_raw?: number | null
}

interface BookmakerOdds {
  bookmaker: string
  entries: OddsEntry[]
}

interface OddsData {
  text: string
  odds_data?: BookmakerOdds[]
}

const statusLabels: Record<string, string> = {
  TBD: '待定',
  NS: '未开始',
  '1H': '上半场',
  HT: '中场',
  '2H': '下半场',
  ET: '加时',
  P: '点球',
  FT: '完赛',
  AET: '加时完赛',
  PEN: '点球完赛',
  PST: '延期',
  CANC: '取消',
  ABD: '中断',
  WO: '判罚',
}

function StatusBadge({ status }: { status: string }) {
  const colorMap: Record<string, string> = {
    NS: 'badge-blue',
    '1H': 'badge-yellow',
    HT: 'badge-yellow',
    '2H': 'badge-yellow',
    FT: 'badge-green',
    AET: 'badge-green',
    PEN: 'badge-green',
    PST: 'badge-red',
    CANC: 'badge-red',
    ABD: 'badge-red',
  }
  return (
    <span className={colorMap[status] || 'badge-gray'}>
      {statusLabels[status] || status}
    </span>
  )
}

const FINISHED_STATUSES = new Set(['FT', 'AET', 'PEN'])

export default function FixturesPage() {
  const [fixtures, setFixtures] = useState<Fixture[]>([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [leagueId, setLeagueId] = useState('')
  const [season, setSeason] = useState('')
  const [status, setStatus] = useState('')
  const [date, setDate] = useState('')
  const [selectedFixture, setSelectedFixture] = useState<FixtureDetail | null>(null)
  const [fixtureDetail, setFixtureDetail] = useState<FixtureDetail | null>(null)
  const [predictingIds, setPredictingIds] = useState<Set<number>>(new Set())
  const [predictMsg, setPredictMsg] = useState<string | null>(null)
  const [oddsFixture, setOddsFixture] = useState<Fixture | null>(null)
  const [oddsData, setOddsData] = useState<OddsData | null>(null)
  const [oddsError, setOddsError] = useState<string | null>(null)
  const [fetchingOddsIds, setFetchingOddsIds] = useState<Set<number>>(new Set())
  const pageSize = 20

  const fetchFixtures = () => {
    setLoading(true)
    const params: Record<string, string | number | undefined> = {
      page,
      page_size: pageSize,
      league_id: leagueId || undefined,
      season: season || undefined,
      status: status || undefined,
    }
    // 后端接受 date 参数（北京时间日期）
    if (date) params.date = date
    apiClient
      .get('/fixtures', { params })
      .then((res) => {
        setFixtures(res.data.data || [])
        setTotal(res.data.total || 0)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    fetchFixtures()
  }, [page])

  const handleSearch = () => {
    setPage(1)
    fetchFixtures()
  }

  const viewDetail = (fixture: Fixture) => {
    setSelectedFixture(fixture as FixtureDetail)
    setFixtureDetail(null)
    apiClient
      .get(`/fixtures/${fixture.id}`)
      .then((res) => setFixtureDetail(res.data))
      .catch(() => setFixtureDetail(fixture as FixtureDetail))
  }

  const handlePredict = (fixture: Fixture) => {
    setPredictingIds((prev) => new Set(prev).add(fixture.id))
    setPredictMsg(null)
    apiClient
      .post(`/predict/${fixture.id}`, {}, { timeout: 120000 })
      .then((res) => {
        setPredictMsg(`${fixture.home_name || ''} vs ${fixture.away_name || ''} 预测完成`)
      })
      .catch((err: any) => {
        console.error('[Predict Error]', err)
        if (err?.response?.data?.detail) {
          setPredictMsg(`预测失败: ${err.response.data.detail}`)
        } else if (err?.code === 'ECONNABORTED') {
          setPredictMsg('预测请求超时，请稍后重试')
        } else if (err?.message) {
          setPredictMsg(`预测失败: ${err.message}`)
        } else {
          setPredictMsg('预测失败: 网络请求异常')
        }
      })
      .finally(() => {
        setPredictingIds((prev) => {
          const next = new Set(prev)
          next.delete(fixture.id)
          return next
        })
      })
  }

  const handleFetchOdds = (fixture: Fixture) => {
    setOddsFixture(fixture)
    setOddsData(null)
    setOddsError(null)
    setFetchingOddsIds((prev) => new Set(prev).add(fixture.id))
    setPredictMsg(null)
    apiClient
      .post(`/odds/${fixture.id}`, {}, { timeout: 60000 })
      .then((res) => {
        setOddsData(res.data.data || null)
      })
      .catch((err: any) => {
        console.error('[Odds Error]', err)
        if (err?.response?.data?.detail) {
          setOddsError(`赔率获取失败: ${err.response.data.detail}`)
        } else if (err?.code === 'ECONNABORTED') {
          setOddsError('赔率获取超时，请稍后重试')
        } else if (err?.message) {
          setOddsError(`赔率获取失败: ${err.message}`)
        } else {
          setOddsError('赔率获取失败: 网络请求异常')
        }
      })
      .finally(() => {
        setFetchingOddsIds((prev) => {
          const next = new Set(prev)
          next.delete(fixture.id)
          return next
        })
      })
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">比赛中心</h1>

      {predictMsg && (
        <div className="mb-4 px-4 py-2 rounded text-sm bg-blue-50 text-blue-700 border border-blue-200">
          {predictMsg}
          <button className="ml-3 text-blue-500 hover:underline" onClick={() => setPredictMsg(null)}>关闭</button>
        </div>
      )}

      {/* 筛选 */}
      <div className="card mb-6">
        <div className="card-body">
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
            <div>
              <label className="block text-xs text-gray-500 mb-1">联赛ID</label>
              <input className="input" placeholder="联赛ID" value={leagueId} onChange={(e) => setLeagueId(e.target.value)} />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">赛季</label>
              <input className="input" placeholder="例: 2024" value={season} onChange={(e) => setSeason(e.target.value)} />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">状态</label>
              <select className="select" value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="">全部</option>
                {Object.entries(statusLabels).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">日期</label>
              <input type="date" className="input" value={date} onChange={(e) => setDate(e.target.value)} />
            </div>
            <div className="flex items-end">
              <button className="btn btn-primary w-full" onClick={handleSearch}>
                搜索
              </button>
            </div>
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
                  <th>联赛</th>
                  <th>主队</th>
                  <th>比分</th>
                  <th>客队</th>
                  <th>日期</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {fixtures.map((f) => (
                  <tr key={f.id}>
                    <td className="text-gray-400 text-xs">{f.id}</td>
                    <td className="text-xs text-gray-500">{f.league_name || f.league_id}</td>
                    <td className="font-medium">
                      <div className="flex items-center gap-2">
                        {f.home_logo && <img src={f.home_logo} alt="" className="w-5 h-5 object-contain" />}
                        {f.home_name || f.home_id}
                      </div>
                    </td>
                    <td className="font-bold text-center">
                      {f.status_short === 'NS' || f.status_short === 'TBD' ? (
                        <span className="text-gray-400">vs</span>
                      ) : (
                        <span className="text-lg">{f.goals_home ?? '-'} - {f.goals_away ?? '-'}</span>
                      )}
                    </td>
                    <td className="font-medium">
                      <div className="flex items-center gap-2">
                        {f.away_logo && <img src={f.away_logo} alt="" className="w-5 h-5 object-contain" />}
                        {f.away_name || f.away_id}
                      </div>
                    </td>
                    <td className="text-xs text-gray-500">{f.date}</td>
                    <td><StatusBadge status={f.status_short} /></td>
                    <td>
                      <div className="flex items-center gap-2">
                        <button className="btn btn-secondary btn-xs" onClick={() => viewDetail(f)}>
                          详情
                        </button>
                        <button
                          className="btn btn-secondary btn-xs"
                          disabled={fetchingOddsIds.has(f.id)}
                          onClick={() => handleFetchOdds(f)}
                        >
                          {fetchingOddsIds.has(f.id) ? '获取中...' : '赔率'}
                        </button>
                        <button
                          className="btn btn-primary btn-xs"
                          disabled={FINISHED_STATUSES.has(f.status_short) || predictingIds.has(f.id)}
                          onClick={() => handlePredict(f)}
                        >
                          {predictingIds.has(f.id) ? '预测中...' : '预测'}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
                {fixtures.length === 0 && (
                  <tr>
                    <td colSpan={8} className="text-center text-gray-400 py-8">
                      暂无比赛数据，请先同步赛程
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

      {/* 比赛详情弹窗 */}
      <Modal
        open={!!selectedFixture}
        onClose={() => setSelectedFixture(null)}
        title={
          selectedFixture
            ? `${selectedFixture.home_name || ''} vs ${selectedFixture.away_name || ''}`
            : '比赛详情'
        }
        size="xl"
      >
        {fixtureDetail ? (
          <div className="space-y-6">
            {/* 基本信息 */}
            <div className="flex items-center justify-center gap-6 py-4">
              <div className="text-center">
                {fixtureDetail.home_logo && (
                  <img src={fixtureDetail.home_logo} alt="" className="w-12 h-12 mx-auto object-contain" />
                )}
                <p className="font-bold mt-1">{fixtureDetail.home_name}</p>
              </div>
              <div className="text-center">
                <div className="text-3xl font-bold">
                  {fixtureDetail.goals_home ?? '-'} - {fixtureDetail.goals_away ?? '-'}
                </div>
                <StatusBadge status={fixtureDetail.status_short} />
                <p className="text-xs text-gray-400 mt-1">{fixtureDetail.date}</p>
              </div>
              <div className="text-center">
                {fixtureDetail.away_logo && (
                  <img src={fixtureDetail.away_logo} alt="" className="w-12 h-12 mx-auto object-contain" />
                )}
                <p className="font-bold mt-1">{fixtureDetail.away_name}</p>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
              {fixtureDetail.round && (
                <div><span className="text-gray-500">轮次:</span> {fixtureDetail.round}</div>
              )}
              {fixtureDetail.venue_name && (
                <div><span className="text-gray-500">场馆:</span> {fixtureDetail.venue_name}{fixtureDetail.venue_city ? `, ${fixtureDetail.venue_city}` : ''}</div>
              )}
            </div>

            {/* 事件 */}
            {fixtureDetail.events && fixtureDetail.events.length > 0 && (
              <div>
                <h4 className="font-semibold mb-2">比赛事件</h4>
                <div className="space-y-1">
                  {fixtureDetail.events.map((e, i) => (
                    <div key={i} className="flex items-center gap-3 p-1.5 rounded text-sm hover:bg-gray-50">
                      <span className="w-10 text-center font-mono text-xs font-bold bg-gray-100 rounded px-1 py-0.5">
                        {e.elapsed ?? '-'}'
                      </span>
                      <span className="badge-blue text-xs">{e.type || '事件'}</span>
                      <span>{e.player_name || '-'}</span>
                      <span className="text-gray-400 text-xs">{e.team_name}</span>
                      {e.detail && <span className="text-gray-400 text-xs">({e.detail})</span>}
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* 技术统计 */}
            {fixtureDetail.statistics && fixtureDetail.statistics.length > 0 && (
              <FixtureStats stats={fixtureDetail.statistics} />
            )}

            {/* 阵容 */}
            {fixtureDetail.lineups && fixtureDetail.lineups.length > 0 && (
              <div>
                <h4 className="font-semibold mb-2">阵容</h4>
                <div className="grid grid-cols-2 gap-4">
                  {(() => {
                    const homeLineups = fixtureDetail.lineups.filter((l) => fixtureDetail.home_id && l.team_id === fixtureDetail.home_id)
                    const awayLineups = fixtureDetail.lineups.filter((l) => fixtureDetail.away_id && l.team_id === fixtureDetail.away_id)
                    return (
                      <>
                        <LineupTable players={homeLineups} teamName={fixtureDetail.home_name || '主队'} />
                        <LineupTable players={awayLineups} teamName={fixtureDetail.away_name || '客队'} />
                      </>
                    )
                  })()}
                </div>
              </div>
            )}

            {/* 球员数据 */}
            {fixtureDetail.player_stats && fixtureDetail.player_stats.length > 0 && (
              <div>
                <h4 className="font-semibold mb-2">球员表现</h4>
                <div className="table-container">
                  <table>
                    <thead>
                      <tr>
                        <th>球员</th>
                        <th>号码</th>
                        <th>位置</th>
                        <th>球队</th>
                        <th>出场</th>
                        <th>进球</th>
                        <th>射门</th>
                        <th>传球</th>
                        <th>抢断</th>
                        <th>犯规</th>
                        <th>黄/红</th>
                      </tr>
                    </thead>
                    <tbody>
                      {fixtureDetail.player_stats.map((p, i) => (
                        <tr key={i}>
                          <td className="font-medium flex items-center gap-2">
                            {p.player_photo && <img src={p.player_photo} alt="" className="w-5 h-5 rounded-full" />}
                            {p.player_name}
                          </td>
                          <td>{p.player_number ?? '-'}</td>
                          <td className="text-xs text-gray-500">{p.player_position ?? '-'}</td>
                          <td className="text-xs text-gray-500">{p.team_name}</td>
                          <td>{p.games?.minutes ?? '-'}'</td>
                          <td className="text-green-600 font-medium">{p.goals?.total ?? 0}</td>
                          <td>{p.shots?.total ?? 0}</td>
                          <td>{p.passes?.total ?? 0}{p.passes?.accuracy ? ` (${p.passes.accuracy})` : ''}</td>
                          <td>{p.tackles?.total ?? 0}</td>
                          <td>{p.fouls?.committed ?? 0}</td>
                          <td>{p.cards?.yellow ?? 0}/{p.cards?.red ?? 0}</td>
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

      {/* 赔率弹窗 */}
      <Modal
        open={!!oddsFixture}
        onClose={() => setOddsFixture(null)}
        title={
          oddsFixture
            ? `${oddsFixture.home_name || ''} vs ${oddsFixture.away_name || ''} 赔率`
            : '赔率'
        }
        size="xl"
      >
        {fetchingOddsIds.has(oddsFixture?.id ?? -1) && !oddsData && !oddsError ? (
          <Loading />
        ) : oddsError ? (
          <div className="text-sm text-red-600">{oddsError}</div>
        ) : oddsData ? (
          <div className="space-y-5">
            {oddsData.odds_data && oddsData.odds_data.length > 0 ? (
              oddsData.odds_data.map((bm) => (
                <div key={bm.bookmaker}>
                  <h4 className="font-semibold mb-2">{bm.bookmaker}</h4>
                  <div className="table-container">
                    <table>
                      <thead>
                        <tr>
                          <th>日期</th>
                          <th>主胜</th>
                          <th>平局</th>
                          <th>客胜</th>
                        </tr>
                      </thead>
                      <tbody>
                        {bm.entries.map((e) => (
                          <tr key={e.date}>
                            <td className="text-xs text-gray-500">{e.date}</td>
                            <td className="text-center">
                              {e.home_odd != null ? `${(e.home_odd * 100).toFixed(0)}% (${e.home_raw})` : '-'}
                            </td>
                            <td className="text-center">
                              {e.draw_odd != null ? `${(e.draw_odd * 100).toFixed(0)}% (${e.draw_raw})` : '-'}
                            </td>
                            <td className="text-center">
                              {e.away_odd != null ? `${(e.away_odd * 100).toFixed(0)}% (${e.away_raw})` : '-'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ))
            ) : null}
            {oddsData.text && (
              <div>
                <h4 className="font-semibold mb-2">原始数据</h4>
                <pre className="whitespace-pre-wrap text-sm bg-gray-50 rounded p-4 overflow-auto">{oddsData.text}</pre>
              </div>
            )}
          </div>
        ) : (
          <p className="text-sm text-gray-400 text-center py-4">暂无赔率数据</p>
        )}
      </Modal>
    </div>
  )
}

// ──── 辅助组件 ────

function FixtureStats({ stats }: { stats: FixtureAPIStat[] }) {
  // 按 team_id 分组，第一个作为主队统计，第二个作为客队统计
  const teamIds = [...new Set(stats.map((s) => s.team_id))]
  if (teamIds.length < 2)
    return <p className="text-sm text-gray-400 py-4 text-center">统计数据不完整</p>

  const homeId = teamIds[0]!
  const awayId = teamIds[1]!
  const homeStats = stats.filter((s) => s.team_id === homeId)
  const homeName = homeStats[0]?.team_name || '主队'
  const awayName = stats.find((s) => s.team_id === awayId)?.team_name || '客队'

  const statTypes = [...new Set(stats.map((s) => s.stat_type!))].filter(Boolean)

  return (
    <div>
      <h4 className="font-semibold mb-2">技术统计</h4>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-gray-500">
            <th className="text-right py-1 pr-2">{homeName}</th>
            <th className="text-center py-1 px-2 w-20"></th>
            <th className="text-left py-1 pl-2">{awayName}</th>
          </tr>
        </thead>
        <tbody>
          {statTypes.map((type) => {
            const h = homeStats.find((s) => s.stat_type === type)
            const a = stats.find((s) => s.stat_type === type && s.team_id === awayId)
            const hv = parseFloat(h?.stat_value || '0') || 0
            const av = parseFloat(a?.stat_value || '0') || 0
            const total = hv + av || 1
            return (
              <tr key={type} className="border-t border-gray-100">
                <td className="text-right font-medium py-2 pr-2">{h?.stat_value ?? '-'}</td>
                <td className="text-center py-2 px-2">
                  <div className="text-xs text-gray-400 mb-0.5">{type}</div>
                  <div className="h-1 bg-gray-100 rounded-full flex overflow-hidden">
                    <div className="bg-primary-500 h-full rounded-l-full" style={{ width: `${(hv / total) * 100}%` }} />
                    <div className="bg-gray-300 h-full rounded-r-full" style={{ width: `${(av / total) * 100}%` }} />
                  </div>
                </td>
                <td className="font-medium py-2 pl-2">{a?.stat_value ?? '-'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function LineupTable({ players, teamName }: { players: FixtureAPILineup[]; teamName: string }) {
  const starters = players.filter((p) => !p.is_substitute)
  const subs = players.filter((p) => p.is_substitute)
  return (
    <div>
      <h5 className="font-semibold mb-2 text-sm text-gray-500">{teamName}</h5>
      <div className="space-y-0.5">
        {starters.map((p, i) => (
          <div key={i} className="flex items-center gap-2 p-1 text-sm hover:bg-gray-50 rounded">
            <span className="w-6 text-center text-gray-400 text-xs">{p.player_number}</span>
            <span className="text-xs text-gray-400 w-8">{p.player_position}</span>
            <span>{p.player_name}</span>
          </div>
        ))}
        {subs.length > 0 && (
          <>
            <div className="text-xs text-gray-400 pt-2 pb-1 font-medium">替补</div>
            {subs.map((p, i) => (
              <div key={`sub-${i}`} className="flex items-center gap-2 p-1 text-sm hover:bg-gray-50 rounded opacity-70">
                <span className="w-6 text-center text-gray-400 text-xs">{p.player_number}</span>
                <span className="text-xs text-gray-400 w-8">{p.player_position}</span>
                <span>{p.player_name}</span>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  )
}
