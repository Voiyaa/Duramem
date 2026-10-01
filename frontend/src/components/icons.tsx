import type { ComponentType, ReactNode, SVGProps } from 'react'

/** 24 栅格线性图标，描边跟随 currentColor。只收录界面用到的那些。 */
export type IconProps = Omit<SVGProps<SVGSVGElement>, 'children'> & { size?: number }
export type IconType = ComponentType<IconProps>

function icon(name: string, paths: ReactNode): IconType {
  function Icon({ size = 16, ...rest }: IconProps) {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.75}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        focusable="false"
        {...rest}
      >
        {paths}
      </svg>
    )
  }
  Icon.displayName = name
  return Icon
}

export const IconDatabase = icon('IconDatabase', <><ellipse cx="12" cy="5.5" rx="7.5" ry="2.75" /><path d="M4.5 5.5v13c0 1.52 3.36 2.75 7.5 2.75s7.5-1.23 7.5-2.75v-13" /><path d="M4.5 12c0 1.52 3.36 2.75 7.5 2.75s7.5-1.23 7.5-2.75" /></>)
export const IconLayers = icon('IconLayers', <><path d="M12 3 3 7.5l9 4.5 9-4.5L12 3Z" /><path d="m3 12 9 4.5 9-4.5" /><path d="m3 16.5 9 4.5 9-4.5" /></>)
export const IconMessages = icon('IconMessages', <path d="M20 12.5a7.5 7.5 0 0 1-10.9 6.7L4 20.5l1.3-4.6A7.5 7.5 0 1 1 20 12.5Z" />)
export const IconSearch = icon('IconSearch', <><circle cx="11" cy="11" r="6.5" /><path d="m20 20-4.4-4.4" /></>)
export const IconFlask = icon('IconFlask', <><path d="M9.5 3h5" /><path d="M10 3v6.2L4.8 18.1A1.9 1.9 0 0 0 6.5 21h11a1.9 1.9 0 0 0 1.7-2.9L14 9.2V3" /><path d="M7.2 15h9.6" /></>)
export const IconActivity = icon('IconActivity', <path d="M3 12h4l2.5-7 5 14 2.5-7h4" />)
export const IconChart = icon('IconChart', <path d="M4 20h16M7 16v-4M12 16V7M17 16V9" />)
export const IconInbox = icon('IconInbox', <><path d="M3.5 13.5h5l1.5 2.5h4l1.5-2.5h5" /><path d="M5.7 5.6 3.5 13.5V18a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-4.5l-2.2-7.9A2 2 0 0 0 16.9 4H7.1a2 2 0 0 0-1.4 1.6Z" /></>)
export const IconCpu = icon('IconCpu', <><rect x="6" y="6" width="12" height="12" rx="2" /><rect x="9.5" y="9.5" width="5" height="5" rx="1" /><path d="M9.5 2.5V6M14.5 2.5V6M9.5 18v3.5M14.5 18v3.5M2.5 9.5H6M2.5 14.5H6M18 9.5h3.5M18 14.5h3.5" /></>)
export const IconSliders = icon('IconSliders', <><path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1" /><circle cx="15" cy="6" r="2" /><circle cx="9" cy="12" r="2" /><circle cx="17" cy="18" r="2" /></>)
export const IconPlus = icon('IconPlus', <path d="M12 5v14M5 12h14" />)
export const IconUpload = icon('IconUpload', <><path d="M12 15V4" /><path d="m7.5 8.5 4.5-4.5 4.5 4.5" /><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" /></>)
export const IconDownload = icon('IconDownload', <><path d="M12 4v11" /><path d="m7.5 10.5 4.5 4.5 4.5-4.5" /><path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" /></>)
export const IconRefresh = icon('IconRefresh', <><path d="M20 12a8 8 0 0 0-14.6-4.5" /><path d="M4 4v4.5h4.5" /><path d="M4 12a8 8 0 0 0 14.6 4.5" /><path d="M20 20v-4.5h-4.5" /></>)
export const IconTrash = icon('IconTrash', <><path d="M4 7h16M10 11v6M14 11v6" /><path d="m6 7 1 12.2A2 2 0 0 0 9 21h6a2 2 0 0 0 2-1.8L18 7" /><path d="M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7" /></>)
export const IconUndo = icon('IconUndo', <><path d="M9 14 4 9l5-5" /><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11" /></>)
export const IconChevronDown = icon('IconChevronDown', <path d="m6 9 6 6 6-6" />)
export const IconChevronRight = icon('IconChevronRight', <path d="m9 6 6 6-6 6" />)
export const IconMore = icon('IconMore', <g fill="currentColor" stroke="none"><circle cx="5" cy="12" r="1.5" /><circle cx="12" cy="12" r="1.5" /><circle cx="19" cy="12" r="1.5" /></g>)
export const IconCheck = icon('IconCheck', <path d="M5 12.5 10 17.5 19 7" />)
export const IconSun = icon('IconSun', <><circle cx="12" cy="12" r="4" /><path d="M12 3v1m0 16v1M21 12h-1M4 12H3m15.4-6.4-.7.7M6.3 17.7l-.7.7m12.8 0-.7-.7M6.3 6.3l-.7-.7" /></>)
export const IconMoon = icon('IconMoon', <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" />)
export const IconX = icon('IconX', <path d="M6 6l12 12M18 6 6 18" />)
export const IconInfo = icon('IconInfo', <><circle cx="12" cy="12" r="9" /><path d="M12 11v5.5M12 7.8h.01" /></>)
export const IconAlert = icon('IconAlert', <><path d="M10.3 3.9 2.5 17.5A2 2 0 0 0 4.2 20.5h15.6a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" /><path d="M12 9.5v4M12 17h.01" /></>)
export const IconCheckCircle = icon('IconCheckCircle', <><circle cx="12" cy="12" r="9" /><path d="m8.5 12.3 2.4 2.4 4.6-5" /></>)
export const IconXCircle = icon('IconXCircle', <><circle cx="12" cy="12" r="9" /><path d="m9.5 9.5 5 5M14.5 9.5l-5 5" /></>)
export const IconSparkles = icon('IconSparkles', <><path d="M12 3c.6 4.1 2.9 6.4 7 7-4.1.6-6.4 2.9-7 7-.6-4.1-2.9-6.4-7-7 4.1-.6 6.4-2.9 7-7Z" /><path d="M19 15.5c.25 1.6 1 2.35 2.5 2.5-1.5.15-2.25.9-2.5 2.5-.25-1.6-1-2.35-2.5-2.5 1.5-.15 2.25-.9 2.5-2.5Z" /></>)
export const IconLink = icon('IconLink', <><path d="M10 13.5a4 4 0 0 0 6 .4l2.8-2.8a4 4 0 0 0-5.7-5.7l-1.4 1.4" /><path d="M14 10.5a4 4 0 0 0-6-.4l-2.8 2.8a4 4 0 0 0 5.7 5.7l1.4-1.4" /></>)
export const IconEye = icon('IconEye', <><path d="M2.5 12C4.5 7.5 8 5 12 5s7.5 2.5 9.5 7c-2 4.5-5.5 7-9.5 7s-7.5-2.5-9.5-7Z" /><circle cx="12" cy="12" r="3" /></>)
export const IconNote = icon('IconNote', <><path d="M15 20.5H6a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v9Z" /><path d="M15 20.5V16a1.5 1.5 0 0 1 1.5-1.5H20" /><path d="M8 8h8M8 12h5" /></>)
export const IconPencil = icon('IconPencil', <><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7.5 18.5l-4 2 2-4Z" /><path d="m14.5 5.5 3 3" /></>)
export const IconCamera = icon('IconCamera', <><path d="M4 8a2 2 0 0 1 2-2h1.8l1.4-2h5.6l1.4 2H18a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Z" /><circle cx="12" cy="12.5" r="3.5" /></>)
export const IconArchive = icon('IconArchive', <><rect x="3" y="4" width="18" height="4.5" rx="1.2" /><path d="M5 8.5V18a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8.5M10 12.5h4" /></>)
export const IconMinusCircle = icon('IconMinusCircle', <><circle cx="12" cy="12" r="9" /><path d="M8 12h8" /></>)
export const IconTarget = icon('IconTarget', <><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="5" /><circle cx="12" cy="12" r="1.2" fill="currentColor" /></>)
export const IconClock = icon('IconClock', <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3.2 2" /></>)
export const IconHash = icon('IconHash', <path d="M5 9h14M5 15h14M10 4 8.5 20M15.5 4 14 20" />)
export const IconTerminal = icon('IconTerminal', <><rect x="3" y="4" width="18" height="16" rx="2.5" /><path d="m7.5 9.5 3 2.5-3 2.5M13 15h3.5" /></>)
export const IconPlay = icon('IconPlay', <path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.4-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5Z" />)
export const IconPlug = icon('IconPlug', <><path d="M9 2.5V7M15 2.5V7M12 16v5.5" /><path d="M6.5 7h11v3.5a5.5 5.5 0 0 1-11 0Z" /></>)
export const IconTrending = icon('IconTrending', <><path d="m3 17 6-6 4 4 8-8" /><path d="M15 7h6v6" /></>)
export const IconGraph = icon('IconGraph', <><circle cx="6" cy="6" r="2.5" /><circle cx="18" cy="8" r="2.5" /><circle cx="9" cy="18" r="2.5" /><path d="m8.4 6.6 7.2 1M7 8.4l1.4 7.2M16.2 9.8l-5.5 6.5" /></>)

/** 品牌标：三条逐级变短的光带——原文 → 概览 → 摘要，与 favicon 同形。 */
export function LogoMark({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 40 40" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id="dm-logo-a" x1="0" x2="1">
          <stop offset="0" stopColor="#22d3ee" />
          <stop offset="1" stopColor="#38bdf8" />
        </linearGradient>
        <linearGradient id="dm-logo-b" x1="0" x2="1">
          <stop offset="0" stopColor="#38bdf8" />
          <stop offset="1" stopColor="#818cf8" />
        </linearGradient>
        <linearGradient id="dm-logo-c" x1="0" x2="1">
          <stop offset="0" stopColor="#818cf8" />
          <stop offset="1" stopColor="#a78bfa" />
        </linearGradient>
      </defs>
      <rect x="5" y="7" width="30" height="7" rx="3.5" fill="url(#dm-logo-a)" />
      <rect x="5" y="16.5" width="22" height="7" rx="3.5" fill="url(#dm-logo-b)" />
      <rect x="5" y="26" width="14" height="7" rx="3.5" fill="url(#dm-logo-c)" />
    </svg>
  )
}
