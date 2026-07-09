import { Routes, Route } from 'react-router-dom'
import Layout from './components/Layout'
import Dashboard from './pages/Dashboard'
import LeaguesPage from './pages/LeaguesPage'
import StandingsPage from './pages/StandingsPage'
import TeamsPage from './pages/TeamsPage'
import PlayersPage from './pages/PlayersPage'
import FixturesPage from './pages/FixturesPage'
import PredictionsPage from './pages/PredictionsPage'
import SchedulerPage from './pages/SchedulerPage'

export default function App() {
  return (
    <Layout>
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
