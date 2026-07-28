import Loading from './Loading'

export interface AccuracyItem {
  key: string
  label: string
  total: number
  correct: number
  accuracy: number | null
}

interface Props {
  data: AccuracyItem[]
  loading?: boolean
}

const barColor = (accuracy: number | null): string => {
  if (accuracy == null) return 'bg-gray-300'
  if (accuracy >= 0.6) return 'bg-green-500'
  if (accuracy >= 0.4) return 'bg-yellow-500'
  return 'bg-red-500'
}

export default function AccuracyPanel({ data, loading }: Props) {
  if (loading) {
    return (
      <div className="card mb-6">
        <div className="card-body">
          <Loading />
        </div>
      </div>
    )
  }

  return (
    <div className="card mb-6">
      <div className="card-header flex items-center justify-between">
        <h3 className="font-semibold">预测准确率分析</h3>
        <span className="text-xs text-gray-400">整体分类准确率</span>
      </div>
      <div className="card-body space-y-4">
        {data.map((it) => {
          const width = it.accuracy == null ? 0 : Math.round(it.accuracy * 100)
          const color = barColor(it.accuracy)
          return (
            <div key={it.key}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium text-gray-700">{it.label}</span>
                <span className="text-sm font-bold text-gray-900">
                  {it.accuracy == null ? '无数据' : `${(it.accuracy * 100).toFixed(1)}%`}
                </span>
              </div>
              <div className="w-full bg-gray-100 rounded-full h-3 overflow-hidden">
                <div className={`h-3 rounded-full transition-all ${color}`} style={{ width: `${width}%` }} />
              </div>
              <div className="text-xs text-gray-400 mt-1">
                正确 {it.correct} / 共 {it.total}
              </div>
            </div>
          )
        })}
        <p className="text-xs text-gray-400 pt-2 border-t border-gray-100">
          统计范围：已完赛且回填实际比分的预测；大小球走水（盘口线打平）不计入分母。
        </p>
      </div>
    </div>
  )
}
