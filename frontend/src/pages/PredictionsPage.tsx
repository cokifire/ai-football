import { useEffect, useState } from 'react'
import apiClient from '../api/client'
import Loading from '../components/Loading'
import Pagination from '../components/Pagination'
import Modal from '../components/Modal'

interface PredictionDetail {
  basic: {
    fixture_id: number
    home_name: string | null
    away_name: string | null
    home_logo: string | null
    away_logo: string | null
    league_name: string | null
    match_date: string | null
    status_short: string | null
    category: string | null
  }
  xgb: {
    model_group: string | null
    prob: { home: number | null; draw: number | null; away: number | null }
    over25: { over: number | null; under: number | null } | null
    lambda: { home: number | null; away: number | null } | null
    top3: Array<{ score: string; prob: number }> | null
    handicap: string | null
  } | null
  llm: {
    win: string | null
    win_pct: number | null
    score: string | null
    handicap: string | null
    handicap_num: number | null
    handicap_team: string | null
    handicap_pct: number | null
    over_under: string | null
    ou_line: number | null
    ou_type: string | null
    ou_pct: number | null
    brief: string | null
    core_data: string | null
    deep_report: string | null
  } | null
  result: {
    score: string | null
    win_correct: boolean | null
    over25_correct: boolean | null
    handicap_correct: boolean | null
    top3_correct: boolean | null
  } | null
}

export default function PredictionsPage() {
  const [predictions, setPredictions] = useState<PredictionDetail[]>([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [date, setDate] = useState('')
  const [category, setCategory] = useState('')
  const [selectedPred, setSelectedPred] = useState<PredictionDetail | null>(null)
  const pageSize = 20

  const fetchPredictions = () => {
    setLoading(true)
    apiClient
      .get('/predictions', {
        params: {
          page,
          page_size: pageSize,
          date: date || undefined,
          category: category || undefined,
        },
      })
      .then((res) => {
        setPredictions(res.data.data || [])
        setTotal(res.data.total || 0)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    fetchPredictions()
  }, [page])

  const handleSearch = () => {
    setPage(1)
    fetchPredictions()
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">预测中心</h1>

      {/* 筛选 */}
      <div className="card mb-6">
        <div className="card-body">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div>
              <label className="block text-xs text-gray-500 mb-1">日期</label>
              <input type="date" className="input" value={date} onChange={(e) => setDate(e.target.value)} />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">分类</label>
              <input className="input" placeholder="分类过滤" value={category} onChange={(e) => setCategory(e.target.value)} />
            </div>
            <div className="flex items-end">
              <button className="btn btn-primary w-full" onClick={handleSearch}>搜索</button>
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
                  <th>比赛ID</th>
                  <th>比赛</th>
                  <th>日期</th>
                  <th>胜%</th>
                  <th>平%</th>
                  <th>负%</th>
                  <th>大小球</th>
                  <th>比分预测</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {predictions.map((p) => {
                  const b = p.basic
                  const x = p.xgb
                  const h = x?.prob?.home
                  const d = x?.prob?.draw
                  const a = x?.prob?.away
                  return (
                    <tr key={b.fixture_id}>
                      <td className="text-gray-400 text-xs">{b.fixture_id}</td>
                      <td className="font-medium">
                        {b.home_name || '主队'} vs {b.away_name || '客队'}
                      </td>
                      <td className="text-xs text-gray-500">{b.match_date?.substring(0, 10) || '-'}</td>
                      <td className="text-green-600 font-medium">
                        {h != null ? `${(h * 100).toFixed(1)}%` : '-'}
                      </td>
                      <td className="text-yellow-600">
                        {d != null ? `${(d * 100).toFixed(1)}%` : '-'}
                      </td>
                      <td className="text-red-600">
                        {a != null ? `${(a * 100).toFixed(1)}%` : '-'}
                      </td>
                      <td>
                        {x?.over25 ? (
                          <span className={x.over25.over != null && x.over25.over > 0.5 ? 'badge-yellow' : 'badge-blue'}>
                            大{x.over25.over != null ? x.over25.over.toFixed(2) : '-'}
                          </span>
                        ) : '-'}
                      </td>
                      <td className="text-xs">{x?.top3?.[0]?.score || p.llm?.score || '-'}</td>
                      <td>
                        <button className="btn btn-secondary btn-xs" onClick={() => setSelectedPred(p)}>
                          详情
                        </button>
                      </td>
                    </tr>
                  )
                })}
                {predictions.length === 0 && (
                  <tr>
                    <td colSpan={9} className="text-center text-gray-400 py-8">
                      暂无预测数据，请先执行预测
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

      {/* 预测详情弹窗 - 直接从列表数据中获取 */}
      <Modal
        open={!!selectedPred}
        onClose={() => setSelectedPred(null)}
        title={
          selectedPred
            ? `${selectedPred.basic.home_name || '主队'} vs ${selectedPred.basic.away_name || '客队'}`
            : '预测详情'
        }
        size="xl"
      >
        {selectedPred ? (
          <div className="space-y-6">
            {/* 基本信息 */}
            <div className="flex items-center justify-between p-4 rounded-lg bg-gray-50">
              <div className="text-center flex items-center gap-2">
                {selectedPred.basic.home_logo && (
                  <img src={selectedPred.basic.home_logo} alt="" className="w-10 h-10 object-contain" />
                )}
                <p className="font-bold text-lg">{selectedPred.basic.home_name || '主队'}</p>
              </div>
              <div className="text-center">
                <p className="text-2xl font-bold text-gray-400">VS</p>
                <p className="text-xs text-gray-500">{selectedPred.basic.match_date?.substring(0, 10) || ''}</p>
                {selectedPred.basic.status_short && (
                  <span className="badge-gray text-xs">{selectedPred.basic.status_short}</span>
                )}
              </div>
              <div className="text-center flex items-center gap-2">
                <p className="font-bold text-lg">{selectedPred.basic.away_name || '客队'}</p>
                {selectedPred.basic.away_logo && (
                  <img src={selectedPred.basic.away_logo} alt="" className="w-10 h-10 object-contain" />
                )}
              </div>
            </div>

            {/* XGBoost 概率 */}
            {selectedPred.xgb && (
              <div className="card">
                <div className="card-header">
                  <h3 className="font-semibold">XGBoost 概率预测</h3>
                </div>
                <div className="card-body">
                  <div className="grid grid-cols-3 gap-4 text-center">
                    <div className="p-3 rounded-lg bg-green-50">
                      <div className="text-xs text-gray-500">主胜</div>
                      <div className="text-xl font-bold text-green-700">
                        {selectedPred.xgb.prob.home != null ? `${(selectedPred.xgb.prob.home * 100).toFixed(1)}%` : '-'}
                      </div>
                    </div>
                    <div className="p-3 rounded-lg bg-yellow-50">
                      <div className="text-xs text-gray-500">平局</div>
                      <div className="text-xl font-bold text-yellow-600">
                        {selectedPred.xgb.prob.draw != null ? `${(selectedPred.xgb.prob.draw * 100).toFixed(1)}%` : '-'}
                      </div>
                    </div>
                    <div className="p-3 rounded-lg bg-red-50">
                      <div className="text-xs text-gray-500">客胜</div>
                      <div className="text-xl font-bold text-red-600">
                        {selectedPred.xgb.prob.away != null ? `${(selectedPred.xgb.prob.away * 100).toFixed(1)}%` : '-'}
                      </div>
                    </div>
                  </div>

                  {/* 大小球 */}
                  {selectedPred.xgb.over25 && (
                    <div className="grid grid-cols-2 gap-4 mt-4 text-center">
                      <div className="p-3 rounded-lg bg-blue-50">
                        <div className="text-xs text-gray-500">大球概率</div>
                        <div className="text-xl font-bold text-blue-600">
                          {selectedPred.xgb.over25.over != null ? `${(selectedPred.xgb.over25.over * 100).toFixed(1)}%` : '-'}
                        </div>
                      </div>
                      <div className="p-3 rounded-lg bg-indigo-50">
                        <div className="text-xs text-gray-500">小球概率</div>
                        <div className="text-xl font-bold text-indigo-600">
                          {selectedPred.xgb.over25.under != null ? `${(selectedPred.xgb.over25.under * 100).toFixed(1)}%` : '-'}
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* 比分预测 */}
            {selectedPred.xgb?.top3 && selectedPred.xgb.top3.length > 0 && (
              <div className="card">
                <div className="card-header">
                  <h3 className="font-semibold">比分预测 Top 3</h3>
                </div>
                <div className="card-body">
                  <div className="grid grid-cols-3 gap-3">
                    {selectedPred.xgb.top3.map((s, i) => (
                      <div key={i} className="p-3 rounded-lg bg-gray-50 text-center">
                        <div className="text-lg font-bold text-primary-700">{s.score}</div>
                        <div className="text-xs text-gray-500">{(s.prob * 100).toFixed(1)}%</div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {/* 亚盘分析 */}
            {selectedPred.xgb?.handicap && (
              <div className="card">
                <div className="card-header">
                  <h3 className="font-semibold">亚盘分析 (XGBoost)</h3>
                </div>
                <div className="card-body">
                  <p className="text-sm">{selectedPred.xgb.handicap}</p>
                </div>
              </div>
            )}

            {/* LLM 分析 */}
            {selectedPred.llm && (
              <div className="card">
                <div className="card-header">
                  <h3 className="font-semibold">LLM 深度分析</h3>
                </div>
                <div className="card-body space-y-4">
                  {selectedPred.llm.win && (
                    <div>
                      <h4 className="text-sm font-medium text-gray-500 mb-1">胜平负结论</h4>
                      <p className="text-sm whitespace-pre-wrap">{selectedPred.llm.win}</p>
                    </div>
                  )}
                  {selectedPred.llm.score && (
                    <div>
                      <h4 className="text-sm font-medium text-gray-500 mb-1">比分预测</h4>
                      <p className="text-sm whitespace-pre-wrap">{selectedPred.llm.score}</p>
                    </div>
                  )}
                  {selectedPred.llm.handicap && (
                    <div>
                      <h4 className="text-sm font-medium text-gray-500 mb-1">盘口分析</h4>
                      <p className="text-sm whitespace-pre-wrap">{selectedPred.llm.handicap}</p>
                    </div>
                  )}
                  {selectedPred.llm.core_data && (
                    <div>
                      <h4 className="text-sm font-medium text-gray-500 mb-1">核心数据对比</h4>
                      <p className="text-sm whitespace-pre-wrap">{selectedPred.llm.core_data}</p>
                    </div>
                  )}
                  {selectedPred.llm.brief && (
                    <div>
                      <h4 className="text-sm font-medium text-gray-500 mb-1">简要分析</h4>
                      <p className="text-sm whitespace-pre-wrap">{selectedPred.llm.brief}</p>
                    </div>
                  )}
                  {selectedPred.llm.deep_report && (
                    <div>
                      <h4 className="text-sm font-medium text-gray-500 mb-1">深度报告</h4>
                      <p className="text-sm whitespace-pre-wrap">{selectedPred.llm.deep_report}</p>
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* 实际结果对比 */}
            {selectedPred.result && (
              <div className="card border-primary-200">
                <div className="card-header bg-primary-50">
                  <h3 className="font-semibold">实际结果对比</h3>
                </div>
                <div className="card-body">
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                    <div className="p-3 rounded-lg bg-gray-50">
                      <div className="text-xs text-gray-500">胜负预测</div>
                      <span className={selectedPred.result.win_correct ? 'badge-green' : 'badge-red'}>
                        胜负{selectedPred.result.win_correct ? '✓' : '✗'}
                      </span>
                    </div>
                    <div className="p-3 rounded-lg bg-gray-50">
                      <div className="text-xs text-gray-500">大小球</div>
                      <span className={selectedPred.result.over25_correct ? 'badge-green' : 'badge-red'}>
                        {selectedPred.result.over25_correct != null
                          ? (selectedPred.result.over25_correct ? '对✓' : '错✗')
                          : '-'}
                      </span>
                    </div>
                    <div className="p-3 rounded-lg bg-gray-50">
                      <div className="text-xs text-gray-500">盘口</div>
                      <span className={selectedPred.result.handicap_correct ? 'badge-green' : 'badge-red'}>
                        {selectedPred.result.handicap_correct != null
                          ? (selectedPred.result.handicap_correct ? '对✓' : '错✗')
                          : '-'}
                      </span>
                    </div>
                    {selectedPred.result.score && (
                      <div className="p-3 rounded-lg bg-gray-50">
                        <div className="text-xs text-gray-500">实际比分</div>
                        <div className="text-lg font-bold">{selectedPred.result.score}</div>
                      </div>
                    )}
                  </div>
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
