import { useEffect, useState, useCallback, useRef } from 'react'
import apiClient from '../api/client'
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

interface LogEntry {
  id: number
  task_id: string
  task_name: string
  status: string
  message: string | null
  started_at: string | null
  finished_at: string | null
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
  { key: 'fixtures',  label: '赛程同步', desc: '从 API 同步比赛赛程数据' },
  { key: 'standings', label: '积分榜同步', desc: '从 API 同步各联赛积分榜数据' },
  { key: 'players',   label: '球员同步', desc: '从 API 同步球员数据' },
]

function taskStatus(t: SchedulerTask): 'running' | 'paused' | 'idle' {
  if (t.is_running) return 'running'
  if (!t.is_enabled) return 'paused'
  return 'idle'
}

export default function SchedulerPage() {
  const [tasks, setTasks] = useState<SchedulerTask[]>([])
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState<string | null>(null)
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [message, setMessage] = useState('')
  const [streamLines, setStreamLines] = useState<string[]>([])  // SSE 实时日志行
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const esRef = useRef<EventSource | null>(null)

  const fetchTasks = useCallback(() => {
    apiClient
      .get('/scheduler/status')
      .then((res) => setTasks(res.data.tasks || []))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  const fetchLogs = useCallback(() => {
    apiClient
      .get('/scheduler/logs')
      .then((res) => setLogs(res.data.data || []))
      .catch(() => {})
  }, [])

  useEffect(() => {
    fetchTasks()
    fetchLogs()
  }, [fetchTasks, fetchLogs])

  // 触发后 SSE 自动推送日志，不再需要轮询
  const triggerSync = async (key: string) => {
    const taskId = TRIGGER_MAP[key]
    const label = taskActions.find((a) => TRIGGER_MAP[a.key] === taskId)?.label || taskId

    setSyncing(key)
    setMessage('')
    setStreamLines([])

    // 先打开 SSE 连接
    const es = new EventSource(`/api/scheduler/${taskId}/stream`)
    esRef.current = es

    es.onmessage = (event) => {
      if (event.data === '__DONE__') return
      setStreamLines((prev) => [...prev, event.data])
    }

    es.onerror = () => {
      // SSE 关闭（正常或异常）
      es.close()
      esRef.current = null
      setSyncing(null)
      fetchTasks()
      fetchLogs()
    }

    try {
      await apiClient.post(`/scheduler/${taskId}/trigger`, {}, { timeout: 0 })
    } catch (err: any) {
      setSyncing(null)
      es.close()
      esRef.current = null
      setMessage(`❌ 执行失败: ${err.response?.data?.detail || err.message}`)
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
      if (esRef.current) esRef.current.close()
      if (pollRef.current) clearInterval(pollRef.current)
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

      {/* 实时执行日志 */}
      {syncing && (
        <div className="card mb-6 border-l-4 border-l-blue-500">
          <div className="card-header flex items-center justify-between">
            <h2 className="font-semibold flex items-center gap-2">
              <span className="inline-block w-2 h-2 bg-blue-500 rounded-full animate-pulse" />
              实时执行日志 — {taskActions.find((a) => a.key === syncing)?.label}
            </h2>
            <span className="text-xs text-gray-400">{streamLines.length} 行</span>
          </div>
          <div className="card-body p-0">
            <div
              ref={logContainerRef}
              className="bg-gray-950 text-green-400 font-mono text-xs p-4 max-h-96 overflow-y-auto leading-relaxed"
            >
              {streamLines.length === 0 ? (
                <span className="text-gray-500">等待后台输出...</span>
              ) : (
                streamLines.map((line, i) => (
                  <div key={i} className="whitespace-pre-wrap">{line}</div>
                ))
              )}
            </div>
          </div>
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

      {/* 同步日志 */}
      <div className="card">
        <div className="card-header flex items-center justify-between">
          <h2 className="font-semibold">执行日志</h2>
          <button className="btn btn-secondary btn-sm" onClick={fetchLogs}>
            刷新
          </button>
        </div>
        <div className="card-body">
          {logs.length > 0 ? (
            <div className="bg-gray-900 text-green-400 rounded-lg p-4 max-h-80 overflow-y-auto font-mono text-xs">
              {logs.map((log) => (
                <div key={log.id} className="mb-0.5 whitespace-pre-wrap">
                  <span className={log.status === 'failed' ? 'text-red-400' : log.status === 'running' ? 'text-yellow-400' : ''}>
                    [{log.status}]
                  </span>{' '}
                  {log.started_at?.replace('T', ' ').substring(0, 19)}{' '}
                  <span className="text-gray-400">{log.task_name}</span>
                  {log.message && <span className="text-gray-500"> — {log.message}</span>}
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-gray-400 py-8 text-center">暂无日志</p>
          )}
        </div>
      </div>
    </div>
  )
}
