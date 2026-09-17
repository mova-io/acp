import { useCallback, useId, useRef, useState } from 'react'
import Drawer from './Drawer.jsx'
import './waterfall-visual-drawer.css'

const TABS = ['Overview', 'Changes', 'Attempts', 'Evidence']
const STAGES = ['rules', 'model', 'fallback1', 'fallback2', 'review', 'approval', 'verification']

// Identity is the authorized account/run scope. Changing stages retains the tab;
// changing identity remounts the body so old attempt selections cannot leak.
export default function WaterfallVisualDrawer(props) {
  return <ScopedDrawer key={props.identity} {...props} />
}

function ScopedDrawer({ stageTitle = 'AI waterfall', provider, model, status,
  stageKind = 'model', breadcrumb, overview, changes, attempts, evidence, footer, onClose,
  initialTab = 'Overview' }) {
  const [tab, setTab] = useState(TABS.includes(initialTab) ? initialTab : 'Overview')
  const id = useId()
  const tabsRef = useRef([])
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  // Keep shared Drawer focus restoration stable across telemetry renders.
  const close = useCallback(() => closeRef.current?.(), [])
  const selectTab = useCallback((name) => {
    if (TABS.includes(name)) {
      setTab(name)
      tabsRef.current[TABS.indexOf(name)]?.focus()
    }
  }, [])
  const slot = { Overview: overview, Changes: changes, Attempts: attempts, Evidence: evidence }[tab]
  const content = typeof slot === 'function' ? slot({ selectTab }) : slot
  const crumb = breadcrumb || `Run › ${stageTitle}`
  const accent = STAGES.includes(stageKind) ? stageKind : 'model'
  function navigate(event, index) {
    let next
    if (event.key === 'ArrowRight') next = (index + 1) % TABS.length
    else if (event.key === 'ArrowLeft') next = (index + TABS.length - 1) % TABS.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = TABS.length - 1
    else return
    event.preventDefault()
    setTab(TABS[next])
    tabsRef.current[next]?.focus()
  }
  return <div className={`waterfall-visual-drawer waterfall-visual-drawer--${accent}`}>
    <Drawer title={stageTitle} onClose={close}>
      <div className="waterfall-visual-drawer__identity">
        <div className="waterfall-visual-drawer__breadcrumb" aria-label="Selected waterfall path">{crumb}</div>
        {status && <span className="waterfall-visual-drawer__status">{status}</span>}
        {(provider || model) && <div className="waterfall-visual-drawer__model">
          {provider && <span>{provider}</span>}{provider && model && <span aria-hidden="true"> · </span>}
          {model && <span>{model}</span>}
        </div>}
      </div>
      <div className="waterfall-visual-drawer__tabs" role="tablist" aria-label="Waterfall stage details">
        {TABS.map((name, index) => <button key={name} type="button" role="tab"
          id={`${id}-${name}-tab`} aria-controls={tab === name ? `${id}-${name}-panel` : undefined} aria-selected={tab === name}
          tabIndex={tab === name ? 0 : -1} ref={el => { tabsRef.current[index] = el }}
          onClick={() => selectTab(name)} onKeyDown={event => navigate(event, index)}>{name}</button>)}
      </div>
      <section key={tab} className="waterfall-visual-drawer__body" role="tabpanel"
        id={`${id}-${tab}-panel`} aria-labelledby={`${id}-${tab}-tab`} tabIndex={0}>
        {content ?? <p className="waterfall-visual-drawer__empty">No recorded {tab.toLowerCase()} details are available for this stage.</p>}
      </section>
      {footer && <div className="waterfall-visual-drawer__footer">{footer}</div>}
    </Drawer>
  </div>
}
