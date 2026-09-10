import Loading from './Loading'

interface DrawSide {
  name: string
  logo: string
  winner: boolean
  team_id: number | null
  team_name: string
  team_logo: string
}

interface DrawScore {
  goals: string
  pen: string | null
}

interface DrawMatch {
  home: DrawSide | null
  away: DrawSide | null
  home_scores: DrawScore[]
  away_scores: DrawScore[]
}

interface DrawRound {
  name: string
  matches: DrawMatch[]
}

export interface DrawData {
  url: string
  league_id: number
  rounds: DrawRound[]
}

/** 单侧比分: 各回合进球, 点球用小号 (n) 标出 */
function Scores({ scores }: { scores: DrawScore[] }) {
  if (!scores || scores.length === 0) return null
  return (
    <div className="flex items-center gap-1">
      {scores.map((s, i) => (
        <div key={i} className="flex items-center gap-0.5">
          <span className="w-6 h-6 inline-flex items-center justify-center rounded bg-gray-50 text-xs font-semibold text-gray-800">
            {s.goals}
          </span>
          {s.pen && (
            <span className="text-[10px] text-gray-500 font-medium">({s.pen})</span>
          )}
        </div>
      ))}
    </div>
  )
}

function Side({
  side,
  scores,
}: {
  side: DrawSide | null
  scores: DrawScore[]
}) {
  if (!side) {
    return (
      <div className="flex items-center gap-2 px-3 py-2 text-sm text-gray-300 italic">
        待定
      </div>
    )
  }
  const logo = side.team_logo || side.logo
  return (
    <div className="flex items-center justify-between gap-2 px-3 py-2">
      <div className="flex items-center gap-2 min-w-0">
        {logo ? (
          <img src={logo} alt="" className="w-5 h-5 object-contain shrink-0" />
        ) : (
          <div className="w-5 h-5 rounded-full bg-gray-200 shrink-0" />
        )}
        <span
          className={`truncate text-sm ${
            side.winner ? 'font-semibold text-gray-900' : 'text-gray-400'
          }`}
          title={side.team_name || side.name}
        >
          {side.team_name || side.name}
        </span>
      </div>
      <Scores scores={scores} />
    </div>
  )
}

export default function BracketView({ data }: { data: DrawData }) {
  if (!data) return null
  const rounds = data.rounds || []
  if (rounds.length === 0) return null

  return (
    <div className="card">
      <div className="px-4 py-3 border-b border-gray-100 flex items-center justify-between">
        <span className="font-semibold">淘汰赛对阵</span>
        <span className="text-xs text-gray-400 hidden sm:inline">
          加粗为晋级球队，括号内为点球比分
        </span>
      </div>
      <div className="p-4 overflow-x-auto">
        <div className="flex gap-6 min-w-max">
          {rounds.map((round, ri) => (
            <div key={`${round.name}-${ri}`} className="w-64 shrink-0">
              <div className="text-center text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">
                {round.name || `第 ${ri + 1} 轮`}
              </div>
              <div className="flex flex-col gap-3 justify-around h-full">
                {round.matches.map((m, mi) => (
                  <div
                    key={mi}
                    className="border border-gray-200 rounded-md divide-y divide-gray-100 bg-white"
                  >
                    <Side side={m.home} scores={m.home_scores} />
                    <Side side={m.away} scores={m.away_scores} />
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

export function DrawLoading() {
  return (
    <div className="card">
      <div className="px-4 py-3 border-b border-gray-100 font-semibold">淘汰赛对阵</div>
      <div className="py-10">
        <Loading />
      </div>
    </div>
  )
}
