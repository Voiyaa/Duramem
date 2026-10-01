import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import type { ActiveDbInfo, Health } from './types'
import { ErrorBox, NoticeBox } from './components/ui'
import Sidebar from './components/Sidebar'
import TopBar from './components/TopBar'
import { PAGES, hashFor, parseHash, type Route } from './routes'
import Databases from './pages/Databases'
import Chunks from './pages/Chunks'
import Debug from './pages/Debug'
import Timeline from './pages/Timeline'
import Sessions from './pages/Sessions'
import Import from './pages/Import'
import Providers from './pages/Providers'
import Settings from './pages/Settings'

const DB_STORAGE_KEY = 'duramem.selectedDb'

export default function App() {
  // uid 是「打开某条切片」的一次性跳转目标（会话导航或 #/chunks/<uid> 链接带进来）
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash))
  const { page, uid: jumpUid } = route
  const meta = PAGES.find((item) => item.key === page) ?? PAGES[0]
  const [health, setHealth] = useState<Health | null>(null)
  const [fatal, setFatal] = useState<string | null>(null)
  const [databases, setDatabases] = useState<string[]>([])
  const [db, setDb] = useState<string>('')
  // 当前记忆库（模型读写的那个）——与"浏览哪个库"分开
  const [active, setActive] = useState<ActiveDbInfo | null>(null)
  const [notice, setNotice] = useState<{ tone: 'success' | 'error'; text: string } | null>(null)
  const [loading, setLoading] = useState(true)
  const mainRef = useRef<HTMLElement>(null)

  const go = useCallback((next: Route) => {
    setRoute(next)
    setNotice(null)
    const hash = hashFor(next)
    if (window.location.hash !== hash) window.history.pushState(null, '', hash)
  }, [])

  useEffect(() => {
    const sync = () => setRoute(parseHash(window.location.hash))
    window.addEventListener('popstate', sync)
    window.addEventListener('hashchange', sync)
    return () => {
      window.removeEventListener('popstate', sync)
      window.removeEventListener('hashchange', sync)
    }
  }, [])

  useEffect(() => {
    mainRef.current?.scrollTo({ top: 0 })
    document.title = `${meta.label} · Duramem`
  }, [meta])

  const refreshDatabases = useCallback(async () => {
    try {
      const [{ databases: infos }, status, activeInfo] = await Promise.all([
        api.listDatabases(),
        api.health(),
        api.activeDb().catch(() => null),
      ])
      setActive(activeInfo)
      const names = infos.map((item) => item.name)
      setDatabases(names)
      setHealth(status)
      setFatal(null)
      setDb((current) => {
        if (current && names.includes(current)) return current
        const remembered = localStorage.getItem(DB_STORAGE_KEY)
        if (remembered && names.includes(remembered)) return remembered
        return names[0] ?? ''
      })
    } catch (error) {
      setFatal(error instanceof Error ? error.message : String(error))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshDatabases()
  }, [refreshDatabases])

  useEffect(() => {
    if (db) localStorage.setItem(DB_STORAGE_KEY, db)
  }, [db])

  const setAsActive = useCallback(
    async (name: string) => {
      try {
        await api.setActiveDb(name)
        setNotice({
          tone: 'success',
          text: `已把「${name}」设为当前记忆库：模型的检索与写入都跟随它（下一轮检索生效）`,
        })
        await refreshDatabases()
      } catch (error) {
        setNotice({ tone: 'error', text: error instanceof Error ? error.message : String(error) })
      }
    },
    [refreshDatabases],
  )

  const activeName = active?.exists ? String(active.db) : null

  return (
    <div className="relative isolate flex h-full overflow-hidden">
      <div className="dm-backdrop" aria-hidden="true" />
      <Sidebar
        page={page}
        onNavigate={(key) => go({ page: key, uid: null })}
        health={health}
        loading={loading}
        fatal={fatal}
      />

      <div className="relative flex min-w-0 flex-1 flex-col">
        <TopBar
          meta={meta}
          databases={databases}
          db={db}
          onSelectDb={setDb}
          active={active}
          loading={loading}
          onSetActive={(name) => void setAsActive(name)}
        />

        <main ref={mainRef} className="relative flex-1 overflow-y-auto">
          <div key={page} className="mx-auto w-full max-w-[1680px] animate-fade-up space-y-4 px-6 py-5">
            {notice &&
              (notice.tone === 'error' ? (
                <ErrorBox onClose={() => setNotice(null)}>{notice.text}</ErrorBox>
              ) : (
                <NoticeBox tone="success" onClose={() => setNotice(null)}>
                  {notice.text}
                </NoticeBox>
              ))}
            {fatal && (
              <ErrorBox>
                连不上后端：{fatal}
                <div className="mt-1 text-rose-200/80">
                  先执行 <code>duramem serve</code>（默认 127.0.0.1:8001），或确认开发代理指向正确端口。
                </div>
              </ErrorBox>
            )}

            {page === 'databases' && (
              <Databases
                db={db}
                onSelect={setDb}
                activeName={activeName}
                onSetActive={setAsActive}
                onChanged={refreshDatabases}
              />
            )}
            {page === 'chunks' && <Chunks db={db} onChanged={refreshDatabases} initialUid={jumpUid} />}
            {page === 'sessions' && (
              <Sessions db={db} onOpenChunk={(uid) => go({ page: 'chunks', uid })} />
            )}
            {page === 'debug' && <Debug db={db} />}
            {page === 'timeline' && <Timeline db={db} />}
            {page === 'import' && (
              <Import db={activeName ?? active?.fallback ?? db} onChanged={refreshDatabases} />
            )}
            {page === 'providers' && <Providers onChanged={refreshDatabases} />}
            {page === 'settings' && <Settings />}
          </div>
        </main>
      </div>
    </div>
  )
}
