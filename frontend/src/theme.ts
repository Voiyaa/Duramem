/** 主题管理：读写 localStorage，切换 <html> 的 data-theme 属性。 */

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'dm-theme'

export function getTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'light' || stored === 'dark') return stored
  return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

export function setTheme(theme: Theme) {
  localStorage.setItem(STORAGE_KEY, theme)
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme
}

export function toggleTheme(): Theme {
  const next = getTheme() === 'dark' ? 'light' : 'dark'
  setTheme(next)
  return next
}
