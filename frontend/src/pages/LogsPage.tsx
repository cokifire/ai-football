import { useCallback, useEffect, useMemo, useState } from 'react'
import apiClient from '../api/client'

interface LogFile {
  name: string
  date: string
  size: number
}

const levelOptions = ['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const
type LogLevel = (typeof levelOptions)[number]

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function lineClass(line: string) {
  if (line.includes('| ERROR') || line.includes('| CRITICAL')) return 'text-red-300'
  if (line.includes('| WARNING')) return 'text-amber-300'
  if (line.includes('| DEBUG')) return 'text-slate-500'
  return 'text-slate-300'
}

export default function LogsPage() {
  const [files, setFiles] = useState<LogFile[]>([])
  const [selectedFile, setSelectedFile] = useState('')
  const [dateMenuOpen, setDateMenuOpen] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [level, setLevel] = useState<LogLevel>('ALL')
  const [logLines, setLogLines] = useState<string[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')

  const loadFiles = useCallback(async () => {
    const response = await apiClient.get('/logs/files')
    const nextFiles: LogFile[] = response.data.files || []
    setFiles(nextFiles)
    setSelectedFile((current) => current || nextFiles[0]?.name || '')
  }, [])

  const loadLogs = useCallback(async () => {
    setRefreshing(true)
    setError('')
    try {
      const params = { log_name: selectedFile || undefined, keyword: keyword || undefined, level, lines: 500 }
      const response = await apiClient.get('/logs', { params })
      setLogLines(response.data.lines || [])
      setTotal(response.data.total || 0)
    } catch (err: any) {
      setError(err.response?.data?.detail || '日志加载失败，请稍后重试')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [keyword, level, selectedFile])

  useEffect(() => {
    loadFiles().catch(() => setError('日志文件列表加载失败'))
  }, [loadFiles])

  useEffect(() => {
    if (selectedFile || files.length === 0) void loadLogs()
  }, [files.length, loadLogs, selectedFile])

  useEffect(() => {
    const timer = window.setInterval(() => void loadLogs(), 10000)
    return () => window.clearInterval(timer)
  }, [loadLogs])

  const selectedMeta = useMemo(() => files.find((file) => file.name === selectedFile), [files, selectedFile])

  const copyLogs = async () => {
    await navigator.clipboard?.writeText(logLines.join('\n'))
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-2xl font-bold">运行日志</h1>
            <span className="badge-green"><span className="mr-1.5 h-1.5 w-1.5 rounded-full bg-green-500 inline-block" />实时可查</span>
          </div>
          <p className="mt-1 text-sm text-gray-500">查看后端服务按日期保存的运行记录，默认展示最新 500 行。</p>
        </div>
        <button className="btn-secondary" onClick={() => { void loadFiles().then(loadLogs) }} disabled={refreshing}>
          <span className={refreshing ? 'animate-spin' : ''}>↻</span> {refreshing ? '刷新中...' : '刷新日志'}
        </button>
      </div>

      <div className="card relative z-20 overflow-visible p-4">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(180px,1fr)_180px_minmax(180px,1.5fr)_auto]">
          <div className="relative text-sm text-gray-600">
            <span>日志日期</span>
            <button
              type="button"
              className="input mt-1 flex items-center justify-between text-left"
              onClick={() => setDateMenuOpen((open) => !open)}
              aria-expanded={dateMenuOpen}
              aria-haspopup="listbox"
            >
              <span>{selectedMeta ? `${selectedMeta.date} · ${formatSize(selectedMeta.size)}` : '暂无日志'}</span>
              <span className="ml-2 text-gray-400">{dateMenuOpen ? '▴' : '▾'}</span>
            </button>
            {dateMenuOpen && files.length > 0 && (
              <div role="listbox" className="absolute left-0 right-0 top-full z-[100] mt-1 max-h-72 overflow-y-auto rounded-lg border border-gray-200 bg-white p-1 shadow-xl">
                {files.map((file) => (
                  <button
                    type="button"
                    role="option"
                    aria-selected={file.name === selectedFile}
                    key={file.name}
                    className={`flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm hover:bg-primary-50 ${file.name === selectedFile ? 'bg-primary-50 text-primary-700' : 'text-gray-700'}`}
                    onClick={() => { setSelectedFile(file.name); setDateMenuOpen(false) }}
                  >
                    <span>{file.date}</span><span className="ml-3 text-xs text-gray-400">{formatSize(file.size)}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
          <label className="text-sm text-gray-600">级别
            <select className="select mt-1" value={level} onChange={(event) => setLevel(event.target.value as LogLevel)}>
              {levelOptions.map((option) => <option key={option} value={option}>{option === 'ALL' ? '全部级别' : option}</option>)}
            </select>
          </label>
          <label className="text-sm text-gray-600">关键词
            <input className="input mt-1" value={keyword} onChange={(event) => setKeyword(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void loadLogs() }} placeholder="搜索日志内容" />
          </label>
          <button className="btn-primary self-end" onClick={() => void loadLogs()} disabled={refreshing}>查询</button>
        </div>
      </div>

      {error && <div className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</div>}

      <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-950 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-4 py-3 text-xs text-slate-400">
          <div className="flex items-center gap-3"><span className="font-mono text-slate-200">{selectedMeta?.name || '暂无日志'}</span><span>{total.toLocaleString()} 条匹配记录</span></div>
          <div className="flex items-center gap-3"><span>{logLines.length ? `显示末尾 ${logLines.length} 行` : '暂无内容'}</span><span className="text-slate-600">每 10 秒刷新</span><button onClick={() => void copyLogs()} className="rounded border border-slate-700 px-2 py-1 hover:bg-slate-800">复制当前日志</button></div>
        </div>
        <div className="max-h-[calc(100vh-350px)] min-h-[320px] overflow-auto p-4 font-mono text-xs leading-6">
          {loading ? <div className="text-slate-500">正在加载日志...</div> : logLines.length ? logLines.map((line, index) => <div key={`${index}-${line}`} className={`${lineClass(line)} whitespace-pre-wrap break-all`}><span className="mr-4 inline-block w-10 select-none text-right text-slate-700">{index + 1}</span>{line}</div>) : <div className="text-slate-500">当前筛选条件下没有日志记录。</div>}
        </div>
      </div>
    </div>
  )
}
