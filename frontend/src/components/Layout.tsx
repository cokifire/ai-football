import { useState } from 'react'
import { Link, useLocation } from 'react-router-dom'

const navItems = [
  { path: '/', label: '仪表盘', icon: '📊' },
  { path: '/leagues', label: '联赛管理', icon: '🏆' },
  { path: '/standings', label: '积分榜', icon: '📋' },
  { path: '/teams', label: '球队浏览', icon: '⚽' },
  { path: '/players', label: '球员浏览', icon: '👤' },
  { path: '/fixtures', label: '比赛中心', icon: '📅' },
  { path: '/predictions', label: '预测中心', icon: '🔮' },
  { path: '/scheduler', label: '数据同步', icon: '🔄' },
]

export default function Layout({ children }: { children: React.ReactNode }) {
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(false)

  return (
    <div className="flex h-screen overflow-hidden">
      {/* 侧边栏 */}
      <aside
        className={`bg-gray-900 text-white flex flex-col transition-all duration-300 ${
          collapsed ? 'w-16' : 'w-60'
        }`}
      >
        <div className="flex items-center justify-between h-16 px-4 border-b border-gray-800">
          {!collapsed && (
            <h1 className="text-lg font-bold whitespace-nowrap">AI Football</h1>
          )}
          <button
            onClick={() => setCollapsed(!collapsed)}
            className="p-1.5 rounded-lg hover:bg-gray-800 text-gray-400 hover:text-white ml-auto"
          >
            {collapsed ? '☰' : '✕'}
          </button>
        </div>
        <nav className="flex-1 overflow-y-auto py-4">
          {navItems.map((item) => {
            const active = location.pathname === item.path
            return (
              <Link
                key={item.path}
                to={item.path}
                className={`flex items-center gap-3 px-4 py-3 mx-2 rounded-lg transition-colors mb-1 ${
                  active
                    ? 'bg-primary-600 text-white'
                    : 'text-gray-400 hover:bg-gray-800 hover:text-white'
                }`}
                title={item.label}
              >
                <span className="text-lg flex-shrink-0">{item.icon}</span>
                {!collapsed && <span className="text-sm">{item.label}</span>}
              </Link>
            )
          })}
        </nav>
        <div className="px-4 py-3 border-t border-gray-800 text-xs text-gray-500">
          {!collapsed && 'AI Football v1.0'}
        </div>
      </aside>

      {/* 主内容 */}
      <main className="flex-1 overflow-y-auto bg-gray-50">
        <div className="p-6">{children}</div>
      </main>
    </div>
  )
}
