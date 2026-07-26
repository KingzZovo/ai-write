'use client'

import { useState, type FormEvent } from 'react'
import { apiFetch, setToken } from '@/lib/api'

export default function LoginPage() {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)

    try {
      const data = await apiFetch<{ token: string; username: string }>(
        '/api/auth/login',
        {
          method: 'POST',
          body: JSON.stringify({ username, password }),
        }
      )
      setToken(data.token)
      window.location.href = '/workspace'
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录失败，请检查用户名和密码')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 bg-gradient-to-b from-brand-50/70 via-gray-50 to-gray-50 px-4">
      <div className="w-full max-w-sm bg-white rounded-2xl border border-gray-200/80 shadow-modal p-6 md:p-8 animate-slide-up">
        <div
          aria-hidden
          className="w-11 h-11 mx-auto mb-4 rounded-xl bg-gradient-to-br from-brand-500 to-brand-700 text-white flex items-center justify-center shadow-card"
        >
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 19l7-7 3 3-7 7-3-3z" />
            <path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z" />
          </svg>
        </div>
        <h1 className="text-2xl font-semibold tracking-tight text-center text-gray-900 mb-1">
          AI Write
        </h1>
        <p className="text-center text-sm text-gray-500 mb-8">AI 小说写作平台</p>

        <form onSubmit={handleSubmit} className="space-y-5">
          <div>
            <label htmlFor="username" className="block text-sm font-medium text-gray-700 mb-1">
              用户名
            </label>
            <input
              id="username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
              autoComplete="username"
              className="w-full px-3 py-2 rounded-lg border border-gray-300 bg-white text-sm text-gray-900 placeholder:text-gray-400 outline-none transition-[box-shadow,border-color] duration-150 focus:border-brand-500 focus:ring-2 focus:ring-brand-500/25"
              placeholder="请输入用户名"
            />
          </div>

          <div>
            <label htmlFor="password" className="block text-sm font-medium text-gray-700 mb-1">
              密码
            </label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              autoComplete="current-password"
              className="w-full px-3 py-2 rounded-lg border border-gray-300 bg-white text-sm text-gray-900 placeholder:text-gray-400 outline-none transition-[box-shadow,border-color] duration-150 focus:border-brand-500 focus:ring-2 focus:ring-brand-500/25"
              placeholder="请输入密码"
            />
          </div>

          {error && (
            <p className="animate-shake rounded-lg border border-danger-500/20 bg-danger-50 px-3 py-2 text-sm text-danger-700 text-center">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={loading}
            className="w-full py-2.5 bg-brand-600 text-white rounded-lg font-medium shadow-card hover:bg-brand-700 active:bg-brand-800 disabled:opacity-50 disabled:cursor-not-allowed transition-colors text-sm"
          >
            {loading ? '登录中...' : '登 录'}
          </button>
        </form>
      </div>
    </div>
  )
}
