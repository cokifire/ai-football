import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import apiClient, { getApiToken } from '../api/client'
import Loading from '../components/Loading'

interface SchedulerTask {
  task_id: string
  name: string
  is_enabled: boolean
  is_running: boolean
  start_hour: number | null
  interval_seconds: number | null
  interval_desc: string
  last_run: string | null
  next_run: string | null
}

// 前端 key -> 后端 task_id 映射
const TRIGGER_MAP: Record<string, string> = {
  leagues:     'league_sync',
  teams:       'team_sync',
  fixtures:    'fixture_daily',
  standings:   'standing_sync',
  players:     'player_sync',
}

const taskActions = [
  { key: 'leagues',   label: '联赛同步', desc: '从 API 同步联赛和赛季数据' },
  { key: 'teams',     label: '球队同步', desc: '从 API 同步球队数据' },
  { key: 'players',   label: '球员同步', desc: '从 API 同步球员数据' },
  { key: 'standings', label: '积分榜同步', desc: '从 Flashscore 同步各联赛积分榜数据' },
  { key: 'fixtures',  label: '赛程同步', desc: '从 API 同步比赛赛程数据' },
]

// 按条件回填缺数据比赛（对应 backend/tools/backfill_fixtures_by_id.py）
const BACKFILL_KEY = 'backfill_fixtures'      // syncing 状态键（与 taskActions.key 同命名空间）
const BACKFILL_TASK_ID = 'backfill_fixtures'  // SSE 日志队列键 / 后端 task_id
const BACKFILL_LABEL = '按条件回填'
const SLEEP_OPTIONS = ['2.0', '4.0', '6.0', '8.0', '10.0']
const MAX_REQUEST_OPTIONS = ['20', '50', '100', '200']

function taskStatus(t: SchedulerTask): 'running' | 'paused' | 'idle' {
  if (t.is_running) return 'running'
  if (!t.is_enabled) return 'paused'
  return 'idle'
}

export default function SchedulerPage() {
  const [tasks, setTasks] = useState<SchedulerTask[]>([])
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState<string | null>(null)
  const [message, setMessage] = useState('')
  const [streamLines, setStreamLines] = useState<string[]>([])  // SSE 实时日志行
  const streamAbortRef = useRef<AbortController | null>(null)

  // 按条件回填：月份 / 联赛 / 间隔 / 上限（全部下拉框选择）
  const [bfMonth, setBfMonth] = useState('')                // '' = 不限月份
  const [bfLeagues, setBfLeagues] = useState<string[]>([])  // 空 = 全部联赛
  const [bfSleep, setBfSleep] = useState('4.0')
  const [bfMaxReq, setBfMaxReq] = useState('0')           // '0' = 不限（靠额度停止）
  const [leagueOptions, setLeagueOptions] = useState<{ id: number; name: string; country: string }[]>([])
  const [leagueOpen, setLeagueOpen] = useState(false)
  const leagueRef = useRef<HTMLDivElement | null>(null)

  const fetchTasks = useCallback(() => {
    apiClient
      .get('/scheduler/status')
      .then((res) => setTasks(res.data.tasks || []))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    fetchTasks()
  }, [fetchTasks])

  // 联赛下拉选项（回填用）
  useEffect(() => {
    apiClient
      .get('/leagues', { params: { page_size: 200 } })
      .then((res) => {
        const list = (res.data?.data ?? []).map((l: any) => ({
          id: l.id as number,
          name: (l.name as string) || '',
          country: (l.country_name as string) || '',
        }))
        list.sort((a: { name: string }, b: { name: string }) => a.name.localeCompare(b.name, 'zh'))
        setLeagueOptions(list)
      })
      .catch(() => {})
  }, [])

  // 联赛下拉：点击外部或按 Esc 自动折叠
  useEffect(() => {
    if (!leagueOpen) return
    const onDown = (e: MouseEvent) => {
      if (leagueRef.current && !leagueRef.current.contains(e.target as Node)) {
        setLeagueOpen(false)
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setLeagueOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [leagueOpen])

  // 月份下拉选项：当前月在前，倒序到 2022-01（库内最早比赛）
  const monthOptions = useMemo(() => {
    const out: string[] = []
    const now = new Date()
    const curY = now.getUTCFullYear()
    const curM = now.getUTCMonth() + 1
    for (let yy = curY; yy >= 2022; yy--) {
      const endM = yy === curY ? curM : 12
      for (let mm = endM; mm >= 1; mm--) {
        out.push(`${yy}-${String(mm).padStart(2, '0')}`)
      }
    }
    return out
  }, [])

  // 触发后 SSE 自动推送日志，不再需要轮询
  const runWithLogStream = async (
    key: string,
    taskId: string,
    label: string,
    triggerPath: string,
    body: Record<string, unknown> | null = null,
  ) => {
    setSyncing(key)
    setMessage('')
    setStreamLines([])

    const controller = new AbortController()
    streamAbortRef.current?.abort()
    streamAbortRef.current = controller

    const streamLogs = async () => {
      try {
        const token = getApiToken()
        const response = await fetch(`/api/scheduler/${taskId}/stream`, {
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
          signal: controller.signal,
        })
        if (!response.ok || !response.body) {
          throw new Error(`日志流连接失败（HTTP ${response.status}）`)
        }
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const events = buffer.split('\n\n')
          buffer = events.pop() || ''
          for (const event of events) {
            const data = event.split('\n').find((line) => line.startsWith('data: '))?.slice(6)
            if (data && data !== '__DONE__') setStreamLines((prev) => [...prev, data])
            if (data === '__DONE__') return
          }
        }
      } catch (err: any) {
        if (err.name !== 'AbortError') setMessage(err.message || '日志流连接失败')
      } finally {
        if (streamAbortRef.current === controller) streamAbortRef.current = null
        setSyncing(null)
        fetchTasks()
      }
    }

    void streamLogs()

    try {
      await apiClient.post(triggerPath, body ?? undefined, { timeout: 0 })
    } catch (err: any) {
      setSyncing(null)
      controller.abort()
      if (streamAbortRef.current === controller) streamAbortRef.current = null
      setMessage(`❌ ${label}执行失败: ${err.response?.data?.detail || err.message}`)
    }
  }

  // 既有定时/同步任务
  const triggerSync = (key: string) => {
    const taskId = TRIGGER_MAP[key]
    const label = taskActions.find((a) => TRIGGER_MAP[a.key] === taskId)?.label || taskId
    return runWithLogStream(key, taskId, label, `/scheduler/${taskId}/trigger`)
  }

  // 按条件回填缺数据比赛
  const triggerBackfill = () => {
    const picked = bfLeagues.map(Number).filter((n) => !Number.isNaN(n) && n > 0)
    setLeagueOpen(false)
    if (!bfMonth && picked.length === 0) {
      setMessage('❌ 请至少选择「月份」或「联赛」之一，避免无界回填')
      return
    }
    return runWithLogStream(
      BACKFILL_KEY,
      BACKFILL_TASK_ID,
      BACKFILL_LABEL,
      `/scheduler/${BACKFILL_TASK_ID}/trigger`,
      {
        month: bfMonth || null,
        leagues: picked.length ? picked : null,
        sleep: Number(bfSleep),
        max_requests: Number(bfMaxReq),
      },
    )
  }

  // 终止正在运行的回填子进程
  const stopBackfill = async () => {
    try {
      await apiClient.post(`/scheduler/${BACKFILL_TASK_ID}/stop`)
    } catch (err: any) {
      setMessage(`❌ 停止失败: ${err.response?.data?.detail || err.message}`)
    }
  }

  const toggleTask = async (taskId: string, currentStatus: string) => {
    try {
      const action = currentStatus === 'paused' ? 'start' : 'stop'
      await apiClient.post(`/scheduler/${taskId}/${action}`)
      fetchTasks()
    } catch (err: any) {
      setMessage(`❌ 操作失败: ${err.response?.data?.detail || err.message}`)
    }
  }

  useEffect(() => {
    return () => {
      streamAbortRef.current?.abort()
    }
  }, [])

  // 实时日志自动滚动到底部
  const logContainerRef = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    if (logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight
    }
  }, [streamLines])

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">数据同步管理</h1>

      {message && (
        <div className={`mb-4 p-3 rounded-lg text-sm ${
          message.startsWith('✅') ? 'bg-green-50 text-green-800' : 'bg-red-50 text-red-800'
        }`}>
          {message}
        </div>
      )}

      {/* 手动触发 */}
      <div className="card mb-6">
        <div className="card-header">
          <h2 className="font-semibold">手动触发同步</h2>
        </div>
        <div className="card-body">
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-5 gap-3">
            {taskActions.map((action) => (
              <button
                key={action.key}
                className="btn btn-primary p-4 flex-col items-start text-left h-auto"
                onClick={() => triggerSync(action.key)}
                disabled={syncing !== null}
              >
                <span className="font-medium">
                  {syncing === action.key ? '执行中...' : action.label}
                </span>
                <span className="text-xs opacity-75 mt-1 font-normal">{action.desc}</span>
              </button>
            ))}
          </div>

          {/* 按条件回填缺数据比赛 */}
          <div className="mt-6 pt-5 border-t border-gray-200">
            <h3 className="text-sm font-medium mb-1">按条件回填缺数据比赛</h3>
            <p className="text-xs text-gray-500 mb-3">
              从数据库查出已完赛但缺统计数据的比赛并回填（backfill_fixtures_by_id.py）
            </p>
            <div className="flex flex-wrap items-end gap-4">
              <div>
                <label className="block text-xs text-gray-500 mb-1">月份</label>
                <select
                  className="select min-w-[130px]"
                  value={bfMonth}
                  onChange={(e) => setBfMonth(e.target.value)}
                  disabled={syncing !== null}
                >
                  <option value="">不限（全时段）</option>
                  {monthOptions.map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </select>
              </div>

              <div className="relative" ref={leagueRef}>
                <label className="block text-xs text-gray-500 mb-1">联赛</label>
                <button
                  type="button"
                  className="input flex items-center justify-between min-w-[240px] text-left"
                  onClick={() => setLeagueOpen((v) => !v)}
                  disabled={syncing !== null}
                >
                  <span className={bfLeagues.length ? '' : 'text-gray-400'}>
                    {bfLeagues.length ? `已选 ${bfLeagues.length} 个联赛` : '全部联赛（不限）'}
                  </span>
                  <span className="ml-2 text-gray-400">{leagueOpen ? '▲' : '▼'}</span>
                </button>

                {leagueOpen && (
                  <div className="absolute z-20 mt-1 w-[280px] max-h-64 overflow-y-auto rounded-lg border border-gray-300 bg-white shadow-lg">
                    <div className="flex items-center justify-between px-3 py-2 border-b border-gray-200">
                      <span className="text-xs text-gray-500">不选 = 全部联赛</span>
                      <button
                        type="button"
                        className="text-xs text-primary-600 hover:underline"
                        onClick={() => setBfLeagues([])}
                      >
                        清空
                      </button>
                    </div>
                    {leagueOptions.map((l) => (
                      <label
                        key={l.id}
                        className="flex items-center gap-2 px-3 py-1.5 text-sm hover:bg-gray-50 cursor-pointer"
                      >
                        <input
                          type="checkbox"
                          checked={bfLeagues.includes(String(l.id))}
                          onChange={() => {
                            const v = String(l.id)
                            setBfLeagues((prev) =>
                              prev.includes(v) ? prev.filter((x) => x !== v) : [...prev, v])
                          }}
                        />
                        <span className="truncate">
                          {l.name}{l.country ? ` · ${l.country}` : ''}
                        </span>
                      </label>
                    ))}
                  </div>
                )}

                <p className="text-xs text-gray-400 mt-1">不选 = 全部联赛；可多选</p>
              </div>

              <div>
                <label className="block text-xs text-gray-500 mb-1">请求间隔</label>
                <select
                  className="select min-w-[110px]"
                  value={bfSleep}
                  onChange={(e) => setBfSleep(e.target.value)}
                  disabled={syncing !== null}
                >
                  {SLEEP_OPTIONS.map((s) => (
                    <option key={s} value={s}>{s} 秒</option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-xs text-gray-500 mb-1">请求上限</label>
                <select
                  className="select min-w-[130px]"
                  value={bfMaxReq}
                  onChange={(e) => setBfMaxReq(e.target.value)}
                  disabled={syncing !== null}
                >
                  <option value="0">不限（靠额度停止）</option>
                  {MAX_REQUEST_OPTIONS.map((n) => (
                    <option key={n} value={n}>{n} 次</option>
                  ))}
                </select>
              </div>

              <button
                className="btn btn-primary"
                onClick={() => void triggerBackfill()}
                disabled={syncing !== null}
              >
                {syncing === BACKFILL_KEY ? '回填中...' : '启动回填'}
              </button>

              <button
                className="btn btn-danger"
                onClick={() => void stopBackfill()}
                disabled={syncing !== BACKFILL_KEY}
              >
                停止
              </button>
            </div>

            <p className="text-xs text-gray-500 mt-3">
              筛选范围：{bfMonth || '不限月份'} ·{' '}
              {bfLeagues.length ? `已选 ${bfLeagues.length} 个联赛` : '全部联赛'}
            </p>
          </div>
        </div>
      </div>

      {/* 任务状态 */}
      {loading ? (
        <Loading />
      ) : (
        <div className="card mb-6">
          <div className="card-header flex items-center justify-between">
            <h2 className="font-semibold">定时任务状态</h2>
            <button className="btn btn-secondary btn-sm" onClick={fetchTasks}>
              刷新
            </button>
          </div>
          <div className="table-container">
            <table>
              <thead>
                <tr>
                  <th>任务名称</th>
                  <th>状态</th>
                  <th>上次运行</th>
                  <th>下次运行</th>
                  <th>上次结果</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {tasks.length > 0 ? (
                  tasks.map((task) => {
                    const st = taskStatus(task)
                    return (
                    <tr key={task.task_id}>
                      <td className="font-medium">{task.name || task.task_id}</td>
                      <td>
                        {st === 'running' && <span className="badge-green">运行中</span>}
                        {st === 'paused' && <span className="badge-yellow">已暂停</span>}
                        {st === 'idle' && <span className="badge-gray">空闲</span>}
                      </td>
                      <td className="text-xs text-gray-500">{task.last_run || '-'}</td>
                      <td className="text-xs text-gray-500">{task.next_run || task.interval_desc || '-'}</td>
                      <td className="text-xs text-gray-500">{task.interval_desc || '-'}</td>
                      <td>
                        {st === 'paused' ? (
                          <button className="btn-primary btn-xs" onClick={() => toggleTask(task.task_id, 'paused')}>启用</button>
                        ) : (
                          <button className="btn-secondary btn-xs" onClick={() => toggleTask(task.task_id, st)}>暂停</button>
                        )}
                      </td>
                    </tr>
                  )})
                ) : (
                  <tr>
                    <td colSpan={6} className="text-center text-gray-400 py-8">
                      暂无定时任务
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* 实时执行日志（始终显示） */}
      <div className="card">
        <div className="card-header flex items-center justify-between">
          <h2 className="font-semibold flex items-center gap-2">
            <span className={`inline-block w-2 h-2 rounded-full ${syncing ? 'bg-blue-500 animate-pulse' : 'bg-gray-400'}`} />
            实时执行日志
            {syncing && <span className="text-xs font-normal text-blue-500">— {syncing === BACKFILL_KEY ? BACKFILL_LABEL : taskActions.find((a) => a.key === syncing)?.label} 执行中</span>}
          </h2>
          <span className="text-xs text-gray-400">{streamLines.length} 行</span>
        </div>
        <div className="card-body p-0">
          <div
            ref={logContainerRef}
            className="bg-gray-950 text-green-400 font-mono text-xs p-4 max-h-96 overflow-y-auto leading-relaxed"
          >
            {streamLines.length === 0 ? (
              <span className="text-gray-500">暂无实时日志，点击上方按钮触发同步即可查看后台输出</span>
            ) : (
              streamLines.map((line, i) => (
                <div key={i} className="whitespace-pre-wrap">{line}</div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
