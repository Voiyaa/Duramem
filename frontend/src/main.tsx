import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './index.css'
import { getTheme, setTheme } from './theme'

// 初始化主题
setTheme(getTheme())

const container = document.getElementById('root')
if (!container) throw new Error('缺少 #root 挂载点')

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
)