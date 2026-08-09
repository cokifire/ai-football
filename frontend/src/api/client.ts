import axios from 'axios'

const TOKEN_KEY = 'api_token'

function getStoredToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

export function getApiToken(): string {
  return getStoredToken()
}

export function setApiToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* ignore storage errors */
  }
}

export function clearApiToken(): void {
  setApiToken('')
}

const apiClient = axios.create({
  baseURL: '/api',
  timeout: 60000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// 每次请求动态注入最新 token（来源：localStorage，而非构建期环境变量）
apiClient.interceptors.request.use((config) => {
  const token = getStoredToken()
  config.headers = config.headers || {}
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  } else {
    delete config.headers.Authorization
  }
  return config
})

// 收到 401 时派发事件，由 App 弹出登录框
apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    const message = error.response?.data?.detail || error.message || '请求失败'
    console.error('[API Error]', message)
    if (error.response?.status === 401) {
      window.dispatchEvent(new CustomEvent('api:unauthorized', { detail: message }))
    }
    return Promise.reject(error)
  },
)

export default apiClient
