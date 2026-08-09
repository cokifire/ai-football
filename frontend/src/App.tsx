import { useEffect, useState, useCallback } from 'react'
import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import LoginModal from './components/LoginModal'
import { getApiToken } from './api/client'
import Dashboard from './pages/Dashboard'
import LeaguesPage from './pages/LeaguesPage'
import StandingsPage from './pages/StandingsPage'
import TeamsPage from './pages/TeamsPage'
import PlayersPage from './pages/PlayersPage'
import FixturesPage from './pages/FixturesPage'
import PredictionsPage from './pages/PredictionsPage'
import SchedulerPage from './pages/SchedulerPage'

export default function App() {
  // 已登录（localStorage 有 token）则隐藏登录框；触发 401 或手动登出时弹出
  const [needLogin, setNeedLogin] = useState(() => !getApiToken())
  const [loginReason, setLoginReason] = useState<string | undefined>(undefined)

  const openLogin = useCallback((reason?: string) => {
    setLoginReason(reason)
    setNeedLogin(true)
  }, [])

  const closeLogin = useCallback(() => {
    setNeedLogin(false)
    setLoginReason(undefined)
  }, [])

  useEffect(() => {
    const onUnauthorized = (e: Event) => {
      const detail = (e as CustomEvent<string>).detail
      openLogin(detail || '登录已失效或 token 无效，请重新输入。')
    }
    window.addEventListener('api:unauthorized', onUnauthorized)
    return () => window.removeEventListener('api:unauthorized', onUnauthorized)
  }, [openLogin])

  return (
    <Layout onRequireLogin={openLogin}>
      {needLogin && <LoginModal open={needLogin} onClose={closeLogin} reason={loginReason} />}
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/leagues" element={<LeaguesPage />} />
        <Route path="/standings" element={<StandingsPage />} />
        <Route path="/teams" element={<TeamsPage />} />
        <Route path="/players" element={<PlayersPage />} />
        <Route path="/fixtures" element={<FixturesPage />} />
        <Route path="/predictions" element={<PredictionsPage />} />
        <Route path="/scheduler" element={<SchedulerPage />} />
      </Routes>
    </Layout>
  )
}
