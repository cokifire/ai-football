import { useState } from 'react'

interface PaginationProps {
  page: number
  pageSize: number
  total: number
  onChange: (page: number) => void
}

export default function Pagination({ page, pageSize, total, onChange }: PaginationProps) {
  const totalPages = Math.ceil(total / pageSize)
  const [jumpValue, setJumpValue] = useState('')

  const handleJump = () => {
    const n = parseInt(jumpValue, 10)
    if (n >= 1 && n <= totalPages) {
      onChange(n)
      setJumpValue('')
    }
  }

  const handleJumpKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') handleJump()
  }

  if (totalPages <= 1) return null

  const pages: (number | string)[] = []
  const maxVisible = 5

  if (totalPages <= maxVisible + 2) {
    for (let i = 1; i <= totalPages; i++) pages.push(i)
  } else {
    pages.push(1)
    const start = Math.max(2, page - 1)
    const end = Math.min(totalPages - 1, page + 1)
    if (start > 2) pages.push('...')
    for (let i = start; i <= end; i++) pages.push(i)
    if (end < totalPages - 1) pages.push('...')
    pages.push(totalPages)
  }

  return (
    <div className="flex items-center justify-between mt-4 text-sm">
      <span className="text-gray-500">
        共 {total} 条，第 {page}/{totalPages} 页
      </span>
      <div className="flex items-center gap-2">
        <button
          onClick={() => onChange(page - 1)}
          disabled={page <= 1}
          className="btn btn-secondary btn-sm"
        >
          上一页
        </button>
        {pages.map((p, i) =>
          typeof p === 'number' ? (
            <button
              key={i}
              onClick={() => onChange(p)}
              className={`btn btn-sm min-w-[36px] ${
                p === page ? 'btn-primary' : 'btn-secondary'
              }`}
            >
              {p}
            </button>
          ) : (
            <span key={i} className="px-1 text-gray-400">
              ...
            </span>
          ),
        )}
        <button
          onClick={() => onChange(page + 1)}
          disabled={page >= totalPages}
          className="btn btn-secondary btn-sm"
        >
          下一页
        </button>
        {totalPages > 9 && (
          <span className="flex items-center gap-1 ml-1">
            <input
              type="number"
              className="input w-14 h-7 text-center text-xs px-1 py-0"
              placeholder={String(page)}
              value={jumpValue}
              min={1}
              max={totalPages}
              onChange={(e) => setJumpValue(e.target.value)}
              onKeyDown={handleJumpKeyDown}
            />
            <button
              className="btn btn-secondary btn-sm h-7"
              onClick={handleJump}
            >
              确定
            </button>
          </span>
        )}
      </div>
    </div>
  )
}
