# AI Football 前端

基于 React 18 + TypeScript + Vite + Tailwind CSS 的足球预测智能分析平台前端，配合后端 FastAPI 服务（[../README.md](../README.md)）提供数据可视化与交互能力。

前端通过统一的 Axios 实例将 `/api` 请求代理到本地后端（`http://127.0.0.1:8000`），用于浏览联赛、球队、球员、积分榜、赛程、比赛详情、预测结果与实时数据同步。

---

## 功能概览

| 页面 | 路由 | 说明 |
|------|------|------|
| 仪表盘 | `/` | 展示联赛、球队、球员、比赛、预测数量统计卡片，快捷入口与最近预测列表 |
| 联赛管理 | `/leagues` | 联赛列表搜索、启用/禁用切换、赛季详情查看 |
| 积分榜 | `/standings` | 按联赛与赛季查看积分榜，支持分组聚合与近况（W/D/L）徽标 |
| 球队浏览 | `/teams` | 球队搜索、Logo/国家/主场展示与详情 |
| 球员浏览 | `/players` | 球员搜索（姓名/国籍）、位置与赛季统计详情 |
| 比赛中心 | `/fixtures` | 赛程筛选（联赛/赛季/状态/日期）、比赛详情（事件/技术统计/阵容/球员表现）、在线赔率获取、单场预测触发 |
| 预测中心 | `/predictions` | 预测结果列表（胜平负概率、大小球、比分），详情弹窗展示 XGBoost 概率、比分 Top3、亚盘、LLM 深度分析与实际结果对比 |
| 数据同步 | `/scheduler` | 手动触发同步任务（联赛/球队/赛程/积分榜/球员）、任务状态启停、SSE 实时执行日志与历史日志 |

---

## 技术栈

| 技术 | 说明 |
|------|------|
| React 18 | 视图框架 |
| TypeScript 5 | 类型安全 |
| Vite 5 | 构建与开发服务器 |
| Tailwind CSS 3 | 样式与组件原子化 |
| React Router 6 | 前端路由 |
| Axios | 与后端 REST API 通信 |

---

## 目录结构

```text
frontend/
├── index.html              # HTML 模板入口
├── package.json           # 依赖与脚本
├── dev.ps1                # Windows 一键启动前后端（PowerShell），Linux/macOS 无需使用
├── vite.config.ts         # Vite 配置，含 /api 代理
├── tailwind.config.js      # Tailwind 主题（primary 色板）
├── postcss.config.js
├── tsconfig.json
└── src/
    ├── main.tsx           # 应用入口（BrowserRouter）
    ├── App.tsx            # 路由表
    ├── index.css          # Tailwind 与全局组件样式（.card/.btn/.badge/.table 等）
    ├── api/
    │   └── client.ts      # Axios 实例，baseURL='/api'，统一错误处理
    ├── components/
    │   ├── Layout.tsx     # 侧边栏导航 + 主内容布局（可折叠）
    │   ├── Loading.tsx    # 加载占位
    │   ├── Modal.tsx      # 通用弹窗
    │   └── Pagination.tsx # 分页组件
    └── pages/
        ├── Dashboard.tsx
        ├── LeaguesPage.tsx
        ├── StandingsPage.tsx
        ├── TeamsPage.tsx
        ├── PlayersPage.tsx
        ├── FixturesPage.tsx
        ├── PredictionsPage.tsx
        └── SchedulerPage.tsx
```

---

## 快速开始

### 环境要求

- Node.js 18+（建议 20+）
- Python 3.10+ 与 `pip`，用于后端虚拟环境
- **MySQL / MariaDB**（后端强依赖）：默认连接 `localhost:3306`，库名 `football`，用户 `root`，密码为空。需在启动前确保数据库已运行且已初始化（建库、表结构，见后端主仓库说明）。连接参数可通过后端目录下的 `.env` 覆盖（`db_host` / `db_port` / `db_user` / `db_password` / `db_name`）。

### 安装依赖

```bash
cd frontend
npm install
```

### 本地开发

方式一：一键启动前后端（跨平台，Windows / Linux / macOS 通用）

```bash
npm run dev
```

该命令通过 `concurrently` 同时拉起后端 `uvicorn`（端口 8000）与前端 `vite`（端口 3000）：

- 后端由 `dev:backend` 内联的 Node 命令启动，它会自动探测项目根目录 `venv` 内的 Python 解释器（Windows: `venv/Scripts/python.exe`，Linux/macOS: `venv/bin/python` 或 `venv/bin/python3`），无需手动激活虚拟环境，也避免了系统 `python` 别名缺失的问题；
- 若后端因数据库未就绪等原因退出，前端不会被连带关闭，可单独查看后端日志排查；
- 访问地址：前端 `http://localhost:3000`，后端 `http://localhost:8000`，API 文档 `http://localhost:8000/docs`。

> Windows 下也可继续使用 `dev.ps1`（`npm run dev` 在该平台同样生效），它会额外释放旧端口进程并自动打开浏览器。

方式二：仅启动前端（需自行启动后端）

```bash
npm run dev:frontend
```

方式三：仅启动后端（需自行启动前端）

```bash
npm run dev:backend
```

### 后端虚拟环境

`npm run dev:backend` 会自动查找项目根目录的 `venv`，因此**无需手动激活**虚拟环境。首次使用请先创建虚拟环境并安装依赖（Windows 与 Linux/macOS 的 venv 不通用，迁移系统后需重新创建）：

```bash
# 在项目根目录（ai-football/）执行
python3 -m venv venv
source venv/bin/activate        # Linux / macOS
# Windows: venv\Scripts\activate
pip install -r backend/requirements.txt   # 安装后端依赖（若后端无该文件，请按实际依赖安装）

# 确保 MySQL 已启动且 football 库已初始化，然后启动开发服务
cd frontend
npm run dev
```

> 注意：Windows 创建的 `venv` 不能直接在 Linux/macOS 上复用，迁移系统后需重新创建虚拟环境。`dev:backend` 在找不到 `venv` 时会回退到系统 `python3`（Linux/macOS）或 `python`（Windows），但需确保该解释器已安装 `uvicorn`/`fastapi` 等后端依赖。

### 接口代理

`vite.config.ts` 已配置 `/api` 代理到 `http://127.0.0.1:8000`，因此前端开发时所有 `axios` 请求无需关心跨域：

```ts
server: {
  proxy: {
    '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
  },
}
```

### 构建与预览

```bash
npm run build      # tsc 类型检查 + vite 生产构建，产物在 dist/
npm run preview    # 本地预览构建产物
```

---

## 主要依赖接口

前端基于后端 `GET/POST /api/...` 接口，常用如下：

| 方法 | 路径 | 页面 |
|------|------|------|
| GET | `/api/dashboard/stats` | 仪表盘 |
| GET | `/api/leagues`、`/api/leagues/{id}`、`PATCH /api/leagues/{id}/toggle` | 联赛管理 |
| GET | `/api/standings`、`/api/standings/seasons` | 积分榜 |
| GET | `/api/teams`、`/api/teams/{id}` | 球队浏览 |
| GET | `/api/players`、`/api/players/{id}` | 球员浏览 |
| GET | `/api/fixtures`、`/api/fixtures/{id}`、`POST /api/predict/{id}`、`POST /api/odds/{id}` | 比赛中心 |
| GET | `/api/predictions` | 预测中心 |
| GET | `/api/scheduler/status`、`POST /api/scheduler/{id}/trigger`、`POST /api/scheduler/{id}/start|stop` | 数据同步 |
| GET (SSE) | `/api/scheduler/{id}/stream` | 数据同步实时日志 |
| GET | `/api/scheduler/logs` | 数据同步历史日志 |

> 若后端未提供 `/api/dashboard/stats`，仪表盘会自动降级为空白统计卡片，不影响其他功能。
> 完整后端接口说明见主仓库 [../README.md](../README.md)。

---

## 说明与免责

- 前端静态资源、构建产物（`node_modules/`、`dist/`、`.vite/`）已被仓库 `.gitignore` 忽略。
- 本项目仅供技术学习、数据分析和模型研究使用，不构成任何投注建议。
