import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
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

interface Team {
  id: number
  name: string
  name_zh?: string | null
  logo: string
  country?: string
  founded?: number
  venue_name?: string
  venue_capacity?: number
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
  // 亚盘（Asian Handicap）
  ah_line?: number | null
  ah_home_odd?: number | null
  ah_away_odd?: number | null
  ah_home_raw?: number | null
  ah_away_raw?: number | null
  // 大小球（Goals Over/Under）
  ou_line?: number | null
  ou_over_odd?: number | null
  ou_under_odd?: number | null
  ou_over_raw?: number | null
  ou_under_raw?: number | null
}

interface BookmakerOdds {
  bookmaker: string
  entries: OddsEntry[]
}

interface OddsData {
  text: string
  odds_data?: BookmakerOdds[]
}

// 判断某条按日期的赔率记录是否含有任何赔率数据（用于隐藏无数据日期）
const hasOddsData = (e: any): boolean =>
  !!e && (
    e.home_raw != null || e.draw_raw != null || e.away_raw != null ||
    e.ah_home_raw != null || e.ou_over_raw != null
  )

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

// 返回北京时间当天的日期字符串 (YYYY-MM-DD)，与后端 date 参数约定一致
function getBeijingToday(): string {
  const now = new Date()
  const beijing = new Date(now.getTime() + now.getTimezoneOffset() * 60000 + 8 * 3600000)
  const y = beijing.getUTCFullYear()
  const m = String(beijing.getUTCMonth() + 1).padStart(2, '0')
  const d = String(beijing.getUTCDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

export default function FixturesPage() {
  const [fixtures, setFixtures] = useState<Fixture[]>([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [leagueId, setLeagueId] = useState('')
  const [season, setSeason] = useState('')
  const [status, setStatus] = useState('')
  const [date, setDate] = useState(getBeijingToday())
  const [teamName, setTeamName] = useState('')
  const [selectedFixture, setSelectedFixture] = useState<FixtureDetail | null>(null)
  const [fixtureDetail, setFixtureDetail] = useState<FixtureDetail | null>(null)
  const [refreshingFixtureId, setRefreshingFixtureId] = useState<number | null>(null)
  const [fetchingXgId, setFetchingXgId] = useState<number | null>(null)
  const [fetchXgError, setFetchXgError] = useState<string | null>(null)
  const [predictingIds, setPredictingIds] = useState<Set<number>>(new Set())
  const [predictMsg, setPredictMsg] = useState<string | null>(null)
  const [predictResult, setPredictResult] = useState<{ fixture: Fixture; result: any } | null>(null)
  const [teamDetail, setTeamDetail] = useState<Team | null>(null)
  const [teamLoading, setTeamLoading] = useState(false)
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
      team_name: teamName.trim() || undefined,
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

  const refreshFixtureDetail = () => {
    if (!selectedFixture) return
    setRefreshingFixtureId(selectedFixture.id)
    apiClient
      .post(`/fixtures/${selectedFixture.id}/refresh`)
      .then((res) => setFixtureDetail(res.data))
      .catch(() => {
        // 刷新失败（如 API 限额/网络异常）时回退到本地 DB 数据
        apiClient
          .get(`/fixtures/${selectedFixture.id}`)
          .then((res) => setFixtureDetail(res.data))
          .catch(() => setFixtureDetail(selectedFixture as FixtureDetail))
      })
      .finally(() => setRefreshingFixtureId(null))
  }

  const fetchXg = () => {
    if (!selectedFixture) return
    setFetchingXgId(selectedFixture.id)
    setFetchXgError(null)
    apiClient
      .post(`/fixtures/${selectedFixture.id}/fetch-xg`)
      .then((res) => setFixtureDetail(res.data))
      .catch((err) => {
        const detail = err?.response?.data?.detail
        setFetchXgError(typeof detail === 'string' ? detail : '获取 xG 失败')
      })
      .finally(() => setFetchingXgId(null))
  }

  const handlePredict = (fixture: Fixture) => {
    setPredictingIds((prev) => new Set(prev).add(fixture.id))
    setPredictMsg(null)
    apiClient
      .post(`/predict/${fixture.id}`, {}, { timeout: 120000 })
      .then((res) => {
        setPredictResult({ fixture, result: res.data.result })
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

  const openTeamDetail = (teamId: number) => {
    if (!teamId) return
    setTeamLoading(true)
    setTeamDetail(null)
    apiClient
      .get(`/teams/${teamId}`)
      .then((res) => setTeamDetail(res.data))
      .catch(() => setTeamDetail(null))
      .finally(() => setTeamLoading(false))
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
              <label className="block text-xs text-gray-500 mb-1">球队名称</label>
              <input className="input" placeholder="球队名称" value={teamName} onChange={(e) => setTeamName(e.target.value)} />
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
          {/* 桌面端：表格（隐藏于窄屏） */}
          <div className="table-container hidden md:block">
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
                    <td className="text-xs text-gray-500">
                    {f.league_id ? (
                      <Link className="text-primary-600 hover:underline" to={`/standings?league=${f.league_id}`}>
                        {f.league_name || f.league_id}
                      </Link>
                    ) : (
                      f.league_name || f.league_id
                    )}
                  </td>
                    <td className="font-medium">
                      <div className="flex items-center gap-2">
                        {f.home_logo && <img src={f.home_logo} alt="" className="w-5 h-5 object-contain" />}
                        <button className="text-primary-600 hover:underline" onClick={() => openTeamDetail(f.home_id)}>
                          {f.home_name || f.home_id}
                        </button>
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
                        <button className="text-primary-600 hover:underline" onClick={() => openTeamDetail(f.away_id)}>
                          {f.away_name || f.away_id}
                        </button>
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

          {/* 手机端：卡片列表（隐藏于 md 及以上） */}
          <div className="md:hidden divide-y divide-gray-100">
            {fixtures.map((f) => (
              <FixtureCard
                key={f.id}
                fixture={f}
                onViewDetail={viewDetail}
                onFetchOdds={handleFetchOdds}
                onPredict={handlePredict}
                onOpenTeam={openTeamDetail}
                fetchingOdds={fetchingOddsIds.has(f.id)}
                predicting={predictingIds.has(f.id)}
                finished={FINISHED_STATUSES.has(f.status_short)}
              />
            ))}
            {fixtures.length === 0 && (
              <div className="text-center text-gray-400 py-8">
                暂无比赛数据，请先同步赛程
              </div>
            )}
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
        headerExtra={
          <button
            className="btn btn-secondary btn-xs"
            disabled={refreshingFixtureId !== null}
            onClick={refreshFixtureDetail}
          >
            {refreshingFixtureId !== null ? '刷新中...' : '刷新'}
          </button>
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

            {/* xG 抓取: 任一方 xG 或 Goals prevented 缺失时显示 Get xG 按钮 */}
            {(() => {
              const stats = fixtureDetail.statistics || []
              const hasXg = stats.some((s) => s.stat_type === 'expected_goals' && s.stat_value)
              const hasGp = stats.some((s) => s.stat_type === 'goals_prevented' && s.stat_value)
              if (hasXg && hasGp) return null
              const disabled = fetchingXgId !== null
              const missing = []
              if (!hasXg) missing.push('Expected goals (xG)')
              if (!hasGp) missing.push('Goals prevented')
              return (
                <div className="flex items-center gap-3 rounded-lg border border-dashed border-primary-300 bg-primary-50 px-3 py-2">
                  <span className="text-sm text-gray-600">
                    缺少数据：{missing.join('、')}
                  </span>
                  <button
                    className="btn btn-primary btn-sm"
                    disabled={disabled}
                    title={fetchXgError || '从 Flashscore 抓取 Expected goals (xG) 与 Goals prevented 并写入数据库'}
                    onClick={fetchXg}
                  >
                    {disabled ? '获取中…' : 'Get xG'}
                  </button>
                  {fetchXgError && (
                    <span className="text-xs text-red-500" title={fetchXgError}>
                      无法获取: {fetchXgError.length > 50 ? fetchXgError.slice(0, 50) + '…' : fetchXgError}
                    </span>
                  )}
                </div>
              )
            })()}

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
              oddsData.odds_data.map((bm) => {
                const rows = (bm.entries || []).filter(hasOddsData)
                if (rows.length === 0) return null
                return (
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
                          <th>亚盘</th>
                          <th>大小球</th>
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((e) => (
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
                            <td className="text-center">
                              {e.ah_home_raw != null ? (
                                <div>
                                  <div className="font-medium">
                                    {e.ah_line != null ? `${e.ah_line} ` : ''}主{e.ah_home_raw}/客{e.ah_away_raw}
                                  </div>
                                  {e.ah_home_odd != null && (
                                    <span className="text-xs text-gray-400">
                                      主{(e.ah_home_odd * 100).toFixed(0)}% 客{(e.ah_away_odd * 100).toFixed(0)}%
                                    </span>
                                  )}
                                </div>
                              ) : '-'}
                            </td>
                            <td className="text-center">
                              {e.ou_over_raw != null ? (
                                <div>
                                  <div className="font-medium">
                                    {e.ou_line != null ? `${e.ou_line} ` : ''}大{e.ou_over_raw}/小{e.ou_under_raw}
                                  </div>
                                  {e.ou_over_odd != null && (
                                    <span className="text-xs text-gray-400">
                                      大{(e.ou_over_odd * 100).toFixed(0)}% 小{(e.ou_under_odd * 100).toFixed(0)}%
                                    </span>
                                  )}
                                </div>
                              ) : '-'}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
                )
              })
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

      {/* 预测结果弹窗 */}
      <Modal
        open={!!predictResult}
        onClose={() => setPredictResult(null)}
        title={
          predictResult
            ? `${predictResult.fixture.home_name || ''} vs ${predictResult.fixture.away_name || ''} 预测结果`
            : '预测结果'
        }
        size="xl"
      >
        {predictResult && (
          <PredictionResult result={predictResult.result} fixture={predictResult.fixture} />
        )}
      </Modal>

      {/* 球队详情弹窗 */}
      <Modal
        open={!!teamDetail || teamLoading}
        onClose={() => setTeamDetail(null)}
        title={teamDetail?.name || '球队详情'}
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
                {teamDetail.name_zh && (
                  <p className="text-sm text-gray-500">{teamDetail.name_zh}</p>
                )}
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

// ──── 辅助组件 ────

// 任意值安全转文本：避免把对象/数组直接作为 React 子节点渲染导致整页崩溃
const toText = (v: any): string => {
  if (v === null || v === undefined) return ''
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  if (Array.isArray(v)) return v.map(toText).join(', ')
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}
// 概率格式化：0~1 之间视为概率转百分比，否则按已为百分比处理
const fmtPct = (v: any): string => {
  if (v === null || v === undefined || v === '') return '-'
  const n = Number(v)
  if (isNaN(n)) return String(v)
  if (n > 0 && n <= 1) return (n * 100).toFixed(1) + '%'
  return n.toFixed(1) + '%'
}
// 概率转进度条占比（0~1）
const frac = (v: any): number => {
  if (v === null || v === undefined) return 0
  const n = Number(v)
  if (isNaN(n)) return 0
  return n > 0 && n <= 1 ? n : Math.min(Math.max(n / 100, 0), 1)
}
const winnerLabel = (w: any): string => {
  if (w === 'home' || w === 'H' || w === '主') return '主胜'
  if (w === 'away' || w === 'A' || w === '客') return '客胜'
  if (w === 'draw' || w === 'D' || w === '平') return '平局'
  if (typeof w === 'string') return w
  return '-'
}

function PredictionResult({ result, fixture }: { result: any; fixture?: any }) {
  if (!result) return null
  const xgb = result
  const llm = result.llm || {}

  return (
    <div className="space-y-6">
      {/* 总览 */}
      <div className="flex items-center justify-center gap-10 py-2">
        <div className="text-center">
          <p className="text-xs text-gray-400">推荐赛果</p>
          <p className="text-2xl font-bold text-primary-600">{winnerLabel(llm.win)}</p>
          {llm.score && <p className="text-sm text-gray-500 mt-1">比分 {toText(llm.score)}</p>}
        </div>
        {llm.win_pct != null && (
          <div className="text-center">
            <p className="text-xs text-gray-400">置信度</p>
            <p className="text-2xl font-bold">{fmtPct(llm.win_pct)}</p>
          </div>
        )}
      </div>

      {/* 胜平负概率（模型） */}
      <div>
        <h4 className="font-semibold mb-2">胜平负概率（模型）</h4>
        <div className="space-y-2">
          {[
            { label: '主胜', v: xgb.win_home, cls: 'bg-primary-500' },
            { label: '平局', v: xgb.win_draw, cls: 'bg-gray-400' },
            { label: '客胜', v: xgb.win_away, cls: 'bg-green-500' },
          ].map((r) => (
            <div key={r.label} className="flex items-center gap-3">
              <span className="w-12 text-sm text-gray-500">{r.label}</span>
              <div className="flex-1 h-2.5 bg-gray-100 rounded-full overflow-hidden">
                <div className={`${r.cls} h-full`} style={{ width: `${frac(r.v) * 100}%` }} />
              </div>
              <span className="w-14 text-right text-sm font-medium">{fmtPct(r.v)}</span>
            </div>
          ))}
        </div>
      </div>

      {/* 大小球 + 让球 */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="border border-gray-100 rounded-lg p-3">
          <h4 className="font-semibold mb-2">大小球</h4>
          <div className="text-sm space-y-1">
            <div className="flex justify-between">
              <span className="text-gray-500">大2.5球概率(模型)</span>
              <span className="font-medium">{fmtPct(xgb.over25_prob)}</span>
            </div>
            {(llm.over_under || (llm.ou_line != null && llm.ou_type)) && (
              <div className="flex justify-between">
                <span className="text-gray-500">推荐</span>
                <span className="font-medium">
                  {toText(llm.over_under) || `${toText(llm.ou_line)} ${toText(llm.ou_type)} (${fmtPct(llm.ou_pct)})`}
                </span>
              </div>
            )}
            {llm.ou_line != null && (
              <div className="flex justify-between">
                <span className="text-gray-500">盘口</span>
                <span className="font-medium">{toText(llm.ou_line)}</span>
              </div>
            )}
            {llm.ou_pct != null && (
              <div className="flex justify-between">
                <span className="text-gray-500">概率</span>
                <span className="font-medium">{fmtPct(llm.ou_pct)}</span>
              </div>
            )}
          </div>
        </div>
        <div className="border border-gray-100 rounded-lg p-3">
          <h4 className="font-semibold mb-2">让球</h4>
          <div className="text-sm space-y-1">
            <div className="flex justify-between">
              <span className="text-gray-500">模型盘口</span>
              <span className="font-medium">{xgb.handicap || '-'}</span>
            </div>
            {(llm.handicap || (llm.handicap_team && llm.handicap_num != null)) && (
              <div className="flex justify-between">
                <span className="text-gray-500">推荐</span>
                <span className="font-medium">
                  {toText(llm.handicap) || `${toText(llm.handicap_team)} ${toText(llm.handicap_num)} (${fmtPct(llm.handicap_pct)})`}
                </span>
              </div>
            )}
            {llm.handicap_team && (
              <div className="flex justify-between">
                <span className="text-gray-500">方向</span>
                <span className="font-medium">{toText(llm.handicap_team)}</span>
              </div>
            )}
            {llm.handicap_pct != null && (
              <div className="flex justify-between">
                <span className="text-gray-500">概率</span>
                <span className="font-medium">{fmtPct(llm.handicap_pct)}</span>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* 比分 Top3 */}
      {Array.isArray(xgb.top3) && xgb.top3.length > 0 && (
        <div>
          <h4 className="font-semibold mb-2">比分概率 Top3（泊松模型）</h4>
          <div className="flex flex-wrap gap-2">
            {xgb.top3.map((t: any, i: number) => {
              // top3 为 [{score, prob}, ...] 字典列表（也可能退化为 [score, prob] 元组）
              const score = t && typeof t === 'object' && !Array.isArray(t) ? t.score : Array.isArray(t) ? t[0] : t
              const prob = t && typeof t === 'object' && !Array.isArray(t) ? t.prob : Array.isArray(t) ? t[1] : null
              return (
                <div key={i} className="px-3 py-1.5 rounded-lg bg-gray-50 border border-gray-100 text-sm">
                  <span className="font-bold">{toText(score)}</span>
                  <span className="text-gray-400 ml-2">{fmtPct(prob)}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* 文本分析 */}
      {llm.brief_analysis && (
        <div>
          <h4 className="font-semibold mb-2">简析</h4>
          <p className="text-sm text-gray-700 whitespace-pre-wrap leading-relaxed">{llm.brief_analysis}</p>
        </div>
      )}
      {llm.core_data && (
        <div>
          <h4 className="font-semibold mb-2">核心数据</h4>
          <p className="text-sm text-gray-700 whitespace-pre-wrap leading-relaxed">{llm.core_data}</p>
        </div>
      )}
      {llm.deep_report && (
        <div>
          <h4 className="font-semibold mb-2">深度报告</h4>
          <p className="text-sm text-gray-700 whitespace-pre-wrap leading-relaxed">{llm.deep_report}</p>
        </div>
      )}

      {/* 核对战绩：脚本喂给 LLM 的近10场原始数据（仅本次展示，不入库） */}
      {(llm.home_stats || llm.away_stats) && (
        <div>
          <h4 className="font-semibold mb-2">
            核对战绩
            <span className="ml-1 text-xs font-normal text-gray-400">
              (脚本喂给 LLM 的近10场原始数据，可核对 LLM 摘要是否一致)
            </span>
          </h4>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div className="p-3 rounded-lg bg-gray-50">
              <div className="text-xs text-gray-500 mb-1">
                {fixture?.home_name || '主队'} 近况
              </div>
              <p className="text-xs whitespace-pre-wrap font-mono leading-relaxed">
                {toText(llm.home_stats) || '无'}
              </p>
            </div>
            <div className="p-3 rounded-lg bg-gray-50">
              <div className="text-xs text-gray-500 mb-1">
                {fixture?.away_name || '客队'} 近况
              </div>
              <p className="text-xs whitespace-pre-wrap font-mono leading-relaxed">
                {toText(llm.away_stats) || '无'}
              </p>
            </div>
          </div>
        </div>
      )}

      {result.model_group && (
        <p className="text-xs text-gray-400 text-center">模型组: {result.model_group}</p>
      )}
    </div>
  )
}

function FixtureCard({
  fixture,
  onViewDetail,
  onFetchOdds,
  onPredict,
  fetchingOdds,
  predicting,
  finished,
}: {
  fixture: Fixture
  onViewDetail: (f: Fixture) => void
  onFetchOdds: (f: Fixture) => void
  onPredict: (f: Fixture) => void
  onOpenTeam: (teamId: number) => void
  fetchingOdds: boolean
  predicting: boolean
  finished: boolean
}) {
  const f = fixture
  return (
    <div className="p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs text-gray-500 truncate">
          {f.league_id ? (
            <Link className="text-primary-600 hover:underline" to={`/standings?league=${f.league_id}`}>
              {f.league_name || f.league_id}
            </Link>
          ) : (
            f.league_name || f.league_id
          )}
          {' '}· #{f.id}
        </span>
        <StatusBadge status={f.status_short} />
      </div>

      <div className="flex items-center justify-between gap-2">
        <div className="flex-1 min-w-0 text-right">
          <div className="flex items-center justify-end gap-2">
            {f.home_logo && <img src={f.home_logo} alt="" className="w-5 h-5 object-contain" />}
            <button className="font-medium truncate text-primary-600 hover:underline" onClick={() => onOpenTeam(f.home_id)}>
              {f.home_name || f.home_id}
            </button>
          </div>
        </div>

        <div className="px-2 text-center whitespace-nowrap">
          {f.status_short === 'NS' || f.status_short === 'TBD' ? (
            <span className="text-gray-400 text-sm">vs</span>
          ) : (
            <span className="text-lg font-bold">{f.goals_home ?? '-'} - {f.goals_away ?? '-'}</span>
          )}
        </div>

        <div className="flex-1 min-w-0 text-left">
          <div className="flex items-center gap-2">
            <button className="font-medium truncate text-primary-600 hover:underline" onClick={() => onOpenTeam(f.away_id)}>
              {f.away_name || f.away_id}
            </button>
            {f.away_logo && <img src={f.away_logo} alt="" className="w-5 h-5 object-contain" />}
          </div>
        </div>
      </div>

      <div className="text-xs text-gray-400 mt-1 text-center">{f.date}</div>

      <div className="flex items-center gap-2 mt-3">
        <button className="btn btn-secondary btn-xs flex-1" onClick={() => onViewDetail(f)}>详情</button>
        <button
          className="btn btn-secondary btn-xs flex-1"
          disabled={fetchingOdds}
          onClick={() => onFetchOdds(f)}
        >
          {fetchingOdds ? '获取中...' : '赔率'}
        </button>
        <button
          className="btn btn-primary btn-xs flex-1"
          disabled={finished || predicting}
          onClick={() => onPredict(f)}
        >
          {predicting ? '预测中...' : '预测'}
        </button>
      </div>
    </div>
  )
}

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
