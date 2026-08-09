import { useState } from 'react'
import { getApiToken, setApiToken, clearApiToken } from '../api/client'

export default function LoginModal({
  open,
  onClose,
  reason,
}: {
  open: boolean
  onClose?: () => void
  reason?: string
}) {
  const [token, setToken] = useState('')
  const [error, setError] = useState('')

  if (!open) return null

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    const value = token.trim()
    if (!value) {
      setError('请输入 API token')
      return
    }
    setApiToken(value)
    setToken('')
    setError('')
    onClose?.()
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm mx-4 bg-white rounded-xl shadow-lg p-6"
      >
        <h2 className="text-lg font-bold mb-1">登录以继续</h2>
        <p className="text-sm text-gray-500 mb-4">
          {reason || '请输入 backend/.env 中的 ADMIN_API_KEY（或 READ_API_KEY）以使用管理功能。'}
        </p>
        <input
          type="password"
          autoFocus
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder="Bearer token"
          className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500 mb-2"
        />
        {error && <p className="text-sm text-red-600 mb-2">{error}</p>}
        <div className="flex items-center justify-between mt-2">
          <button
            type="button"
            onClick={() => {
              clearApiToken()
              onClose?.()
            }}
            className="text-sm text-gray-500 hover:text-gray-700"
          >
            清除已存 token
          </button>
          <button
            type="submit"
            className="px-4 py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700"
          >
            登录
          </button>
        </div>
        <p className="text-xs text-gray-400 mt-4">
          token 仅保存在本浏览器 localStorage，不会写入代码或构建产物。
        </p>
      </form>
    </div>
  )
}

// 供其他组件读取当前是否已登录
export function currentToken(): string {
  return getApiToken()
}
