import axios from 'axios'

export const apiToken = import.meta.env.VITE_API_TOKEN || ''

export const apiAuthHeaders: Record<string, string> = apiToken
  ? { Authorization: `Bearer ${apiToken}` }
  : {}

const apiClient = axios.create({
  baseURL: '/api',
  timeout: 60000,
  headers: {
    'Content-Type': 'application/json',
    ...apiAuthHeaders,
  },
})

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    const message = error.response?.data?.detail || error.message || '请求失败'
    console.error('[API Error]', message)
    return Promise.reject(error)
  },
)

export default apiClient
