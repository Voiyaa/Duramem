import {
  IconActivity,
  IconCpu,
  IconDatabase,
  IconFlask,
  IconInbox,
  IconLayers,
  IconMessages,
  IconSliders,
  type IconType,
} from './components/icons'

export type PageKey =
  | 'databases'
  | 'chunks'
  | 'sessions'
  | 'timeline'
  | 'debug'
  | 'import'
  | 'providers'
  | 'settings'

export type PageMeta = { key: PageKey; label: string; hint: string; icon: IconType }

export const NAV: { group: string; pages: PageMeta[] }[] = [
  {
    group: '记忆',
    pages: [
      { key: 'databases', label: '库管理', hint: '多库、挂载与规模', icon: IconDatabase },
      { key: 'chunks', label: '切片浏览器', hint: '查看与编辑记忆', icon: IconLayers },
      { key: 'sessions', label: '会话', hint: '概览与导航（L1）', icon: IconMessages },
      { key: 'timeline', label: '记忆时间线', hint: '时间分布与命中', icon: IconActivity },
    ],
  },
  {
    group: '工具',
    pages: [
      { key: 'debug', label: '检索调试', hint: '三路排名并列', icon: IconFlask },
      { key: 'import', label: '导入对话', hint: '挑历史对话入库', icon: IconInbox },
    ],
  },
  {
    group: '配置',
    pages: [
      { key: 'providers', label: '模型', hint: '嵌入 / 重排 / 摘要', icon: IconCpu },
      { key: 'settings', label: '设置', hint: '运行时参数', icon: IconSliders },
    ],
  },
]

export const PAGES: PageMeta[] = NAV.flatMap((group) => group.pages)

export type Route = { page: PageKey; uid: string | null }

/** 地址栏 #/页面[/切片uid]：刷新停在原页、前进后退可用、切片可以直接给链接。 */
export function parseHash(hash: string): Route {
  const [rawKey, rawUid] = hash.replace(/^#\/?/, '').split('/')
  const page = PAGES.find((item) => item.key === rawKey)?.key ?? 'databases'
  const uid = page === 'chunks' && rawUid ? decodeURIComponent(rawUid) : null
  return { page, uid }
}

export function hashFor(route: Route): string {
  return `#/${route.page}${route.uid ? `/${encodeURIComponent(route.uid)}` : ''}`
}
