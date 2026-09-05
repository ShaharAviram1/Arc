import { PagePlaceholder } from '@/components/PagePlaceholder'
import { useHealth } from '@/lib/health'

function HealthBadge() {
  const { data, isPending, isError } = useHealth()

  if (isPending) {
    return <span className="text-[var(--arc-text-muted)]">API: checking…</span>
  }
  if (isError || !data) {
    return <span className="text-[var(--arc-error)]">API: unreachable</span>
  }
  return (
    <>
      <span className="text-[var(--arc-ok)]">API: {data.status}</span>
      <span className="ml-2 text-[var(--arc-text-muted)]">
        {data.version} · {data.env}
      </span>
    </>
  )
}

export function Home() {
  return (
    <PagePlaceholder title="Home" description="Continue watching, Behind on, and New this week.">
      <div className="rounded-lg border border-[var(--arc-border)] bg-[var(--arc-surface)] px-4 py-3 text-sm">
        <HealthBadge />
      </div>
    </PagePlaceholder>
  )
}
