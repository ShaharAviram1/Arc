/**
 * The rules editor (spec §4.10 FR-D2, §4.9 FR-T5, roadmap M14).
 *
 * This tab is the milestone's definition of done: *every admin-configurable
 * value in the spec is editable here, without touching env or the database*.
 * That is why each field carries its default beside it and a way back to it —
 * a settings screen you cannot undo is one people stop touching.
 *
 * Three decisions worth stating:
 *
 * **Only changed keys are sent.** The form is bound to the whole object, but a
 * PUT of every key would silently overwrite whatever another admin changed
 * while this form sat open. `changedSettings` reduces it to the differences, so
 * two admins editing two fields is a non-event.
 *
 * **The form rebinds to the server's answer.** A save returns the whole payload
 * and both the draft and the baseline are replaced with it, so what is on
 * screen after a save is what the server holds — not what was typed at it.
 *
 * **`acquisition_paused` is not here.** It is in the same `settings` table, but
 * it is a switch an admin flips while watching downloads, not a value they edit
 * and save; it lives in the Acquisition tab beside the counts that say whether
 * it is doing anything. Since the draft never touches it, it is never in the
 * diff either — pausing from the other tab cannot be undone by saving here.
 */

import { useState, type ReactNode } from 'react'
import { ErrorState } from '@/components/ErrorState'
import {
  InlineError,
  Notice,
  Pill,
  SectionHeading,
  inputClass,
  panelClass,
  primaryButtonClass,
  subtleButtonClass,
  TableScroll,
  tdClass,
  thClass,
} from '@/components/admin/ui'
import {
  NUMBER_BOUNDS,
  RESOLUTIONS,
  adminErrorMessage,
  changedSettings,
  defaultLabel,
  rangeLabel,
  settingsFieldErrors,
  useSaveSettings,
  useSettings,
  type SettingsKey,
  type SettingsPayload,
  type SettingsValues,
} from '@/lib/admin'

const EXPLANATION =
  'These are the rules the whole server runs on: what acquisition looks for, how far ahead it ' +
  'looks, and how long files survive after they are watched. They take effect on the next job — ' +
  'nothing already downloading is disturbed.'

/**
 * The numeric rules, in the words the spec uses for them.
 *
 * The allowed range is *not* written into `help`: it is appended from
 * `NUMBER_BOUNDS` at render time, so the sentence under the field and the
 * `min`/`max` on the field itself cannot come to disagree — which is exactly
 * what happened when the server settled on 0–365 for G and D.
 */
const NUMBER_FIELDS: {
  key: 'look_ahead_n' | 'grace_days_g' | 'unwatched_days_d'
  label: string
  help: string
}[] = [
  {
    key: 'look_ahead_n',
    label: 'Look-ahead N',
    help: 'How many unwatched episodes ahead to keep for each show a user is watching (FR-A1).',
  },
  {
    key: 'grace_days_g',
    label: 'Grace days G',
    help: 'How long a watched episode’s files survive before the sweep deletes them (FR-T1).',
  },
  {
    key: 'unwatched_days_d',
    label: 'Unwatched days D',
    help: 'How long a ready episode may sit unwatched before its want is dropped (FR-T2).',
  },
]

/** Codes worth offering; the input stays free text, because ours are not all. */
const SUB_LANGS = ['en', 'es', 'pt', 'fr', 'de', 'it', 'ru', 'ar']
const AUDIO_LANGS = ['ja', 'en', 'zh', 'ko']

/**
 * One labelled field: the control, what it is for, its default, and — when the
 * server rejected it — why. The reset button is rendered only when the value
 * differs from the default, so the row is quiet when there is nothing to undo.
 */
function Field({
  id,
  label,
  help,
  fallback,
  onReset,
  error,
  children,
}: {
  id: string
  label: string
  help: string
  fallback: string
  onReset: () => void
  error: string | undefined
  children: ReactNode
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-baseline gap-2">
        <label htmlFor={id} className="text-sm font-medium text-[var(--arc-text)]">
          {label}
        </label>
        <span className="text-xs text-[var(--arc-text-muted)]">Default: {fallback}</span>
        <button
          type="button"
          className="text-xs text-[var(--arc-accent)] hover:underline"
          onClick={onReset}
        >
          Reset to default
        </button>
      </div>
      {children}
      <p className="text-xs text-[var(--arc-text-muted)]">{help}</p>
      {error === undefined ? null : <InlineError message={error} />}
    </div>
  )
}

/** Ordered release-group preferences, added and removed one at a time. */
function GroupsField({
  groups,
  onChange,
  fallback,
  onReset,
  error,
}: {
  groups: string[]
  onChange: (groups: string[]) => void
  fallback: string
  onReset: () => void
  error: string | undefined
}) {
  const [draft, setDraft] = useState('')

  function add() {
    const value = draft.trim()
    if (value === '' || groups.includes(value)) {
      setDraft('')
      return
    }
    onChange([...groups, value])
    setDraft('')
  }

  return (
    <Field
      id="rules-group-input"
      label="Preferred release groups"
      help="In order of preference. An empty list means no preference: releases are ranked on resolution and seeders alone (FR-A3)."
      fallback={fallback}
      onReset={onReset}
      error={error}
    >
      <div className="flex flex-wrap items-center gap-2">
        {groups.length === 0 ? (
          <span className="text-xs text-[var(--arc-text-muted)]">No preferred groups.</span>
        ) : (
          groups.map((group, index) => (
            <span
              key={group}
              className="inline-flex items-center gap-1.5 rounded-full border border-[var(--arc-border)] bg-[var(--arc-surface-raised)] px-2.5 py-0.5 text-xs text-[var(--arc-text)]"
            >
              <span className="text-[var(--arc-text-muted)]">{index + 1}.</span>
              {group}
              <button
                type="button"
                aria-label={`Remove ${group}`}
                className="text-[var(--arc-text-muted)] hover:text-[var(--arc-error)]"
                onClick={() => {
                  onChange(groups.filter((item) => item !== group))
                }}
              >
                ×
              </button>
            </span>
          ))
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          id="rules-group-input"
          type="text"
          value={draft}
          placeholder="SubsPlease"
          className={`w-56 ${inputClass}`}
          onChange={(event) => {
            setDraft(event.target.value)
          }}
          onKeyDown={(event) => {
            // Enter in a text field would otherwise submit the form and save
            // the group the admin has not finished adding.
            if (event.key === 'Enter') {
              event.preventDefault()
              add()
            }
          }}
        />
        <button type="button" className={subtleButtonClass} onClick={add}>
          Add group
        </button>
      </div>
    </Field>
  )
}

/** A resolution choice; "no preference" is a real answer, hence the blank. */
function ResolutionSelect({
  id,
  value,
  onChange,
}: {
  id: string
  value: string | null
  onChange: (value: string | null) => void
}) {
  return (
    <select
      id={id}
      value={value ?? ''}
      className={`w-40 ${inputClass}`}
      onChange={(event) => {
        onChange(event.target.value === '' ? null : event.target.value)
      }}
    >
      <option value="">No preference</option>
      {RESOLUTIONS.map((resolution) => (
        <option key={resolution} value={resolution}>
          {resolution}
        </option>
      ))}
    </select>
  )
}

/** Per-show rule overrides. Read-only until M16 gives them an editor. */
function OverridesPanel({ payload }: { payload: SettingsPayload }) {
  return (
    <section className="mt-10">
      <SectionHeading>Per-show overrides</SectionHeading>
      <p className="mt-1 max-w-3xl text-sm text-[var(--arc-text-muted)]">
        Shows with their own group or resolution rule, which win over the values above. Editing them
        from here arrives with M16; until then they are set on the show itself.
      </p>
      {payload.overrides.length === 0 ? (
        <p className="mt-4 text-sm text-[var(--arc-text-muted)]">No per-show overrides.</p>
      ) : (
        <div className="mt-4">
          <TableScroll>
            <table className="min-w-full border-collapse">
              <caption className="sr-only">Per-show overrides</caption>
              <thead>
                <tr>
                  <th className={thClass}>Show</th>
                  <th className={thClass}>Groups</th>
                  <th className={thClass}>Resolution</th>
                </tr>
              </thead>
              <tbody>
                {payload.overrides.map((override) => (
                  <tr key={override.anime_id} className="border-t border-[var(--arc-border)]">
                    <td className={tdClass}>{override.title}</td>
                    <td className={tdClass}>
                      {override.preferred_groups === null || override.preferred_groups.length === 0
                        ? '—'
                        : override.preferred_groups.join(', ')}
                    </td>
                    <td className={tdClass}>{override.resolution ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableScroll>
        </div>
      )}
    </section>
  )
}

/**
 * One key replaced in a copy of the rules.
 *
 * `Object.assign` rather than a spread with a computed key: with a *union* of
 * keys — which is what "reset this field" hands over — a computed property
 * widens the literal into an index signature, and the result stops being a
 * `SettingsValues`. This keeps the type and the call sites honest.
 */
function assign<K extends SettingsKey>(
  current: SettingsValues,
  key: K,
  value: SettingsValues[K],
): SettingsValues {
  return Object.assign({}, current, { [key]: value })
}

/**
 * The form itself, mounted only once the payload has arrived so that the draft
 * can be seeded from it in `useState` rather than synced on every render. A
 * background refetch therefore cannot eat half-typed edits.
 */
function RulesForm({ payload }: { payload: SettingsPayload }) {
  const save = useSaveSettings()
  const [loaded, setLoaded] = useState<SettingsValues>(payload.values)
  const [draft, setDraft] = useState<SettingsValues>(payload.values)
  const [saved, setSaved] = useState(false)

  const defaults = payload.defaults
  const fieldErrors = settingsFieldErrors(save.error)
  const changed = changedSettings(draft, loaded)
  const dirty = Object.keys(changed).length > 0

  function set<K extends SettingsKey>(key: K, value: SettingsValues[K]) {
    setSaved(false)
    setDraft((current) => assign(current, key, value))
  }

  function reset(key: SettingsKey) {
    setSaved(false)
    setDraft((current) => assign(current, key, defaults[key]))
  }

  function submit() {
    if (!dirty) return
    save.mutate(changed, {
      onSuccess: (next) => {
        setLoaded(next.values)
        setDraft(next.values)
        setSaved(true)
      },
    })
  }

  // A 422 that named no field at all still has to say something.
  const generalError =
    save.isError && Object.keys(fieldErrors).length === 0 ? adminErrorMessage(save.error) : null

  return (
    <form
      className={`mt-6 flex max-w-3xl flex-col gap-6 ${panelClass}`}
      onSubmit={(event) => {
        event.preventDefault()
        submit()
      }}
    >
      <GroupsField
        groups={draft.preferred_groups}
        fallback={defaultLabel(defaults.preferred_groups)}
        error={fieldErrors.preferred_groups}
        onChange={(groups) => {
          set('preferred_groups', groups)
        }}
        onReset={() => {
          reset('preferred_groups')
        }}
      />

      <Field
        id="rules-preferred-resolution"
        label="Preferred resolution"
        help="Tried first when ranking releases (FR-A3)."
        fallback={defaultLabel(defaults.preferred_resolution)}
        error={fieldErrors.preferred_resolution}
        onReset={() => {
          reset('preferred_resolution')
        }}
      >
        <ResolutionSelect
          id="rules-preferred-resolution"
          value={draft.preferred_resolution}
          onChange={(value) => {
            set('preferred_resolution', value)
          }}
        />
      </Field>

      <Field
        id="rules-fallback-resolution"
        label="Fallback resolution"
        help="Accepted when nothing at the preferred resolution is available."
        fallback={defaultLabel(defaults.fallback_resolution)}
        error={fieldErrors.fallback_resolution}
        onReset={() => {
          reset('fallback_resolution')
        }}
      >
        <ResolutionSelect
          id="rules-fallback-resolution"
          value={draft.fallback_resolution}
          onChange={(value) => {
            set('fallback_resolution', value)
          }}
        />
      </Field>

      {NUMBER_FIELDS.map(({ key, label, help }) => (
        <Field
          key={key}
          id={`rules-${key}`}
          label={label}
          help={`${help} Allowed: ${rangeLabel(key)}.`}
          fallback={defaultLabel(defaults[key])}
          error={fieldErrors[key]}
          onReset={() => {
            reset(key)
          }}
        >
          <input
            id={`rules-${key}`}
            type="number"
            min={NUMBER_BOUNDS[key].min}
            max={NUMBER_BOUNDS[key].max}
            value={String(draft[key])}
            className={`w-32 ${inputClass}`}
            onChange={(event) => {
              // An emptied box is 0 rather than NaN: 0 is inside every one of
              // these ranges, and it is what the box already shows.
              const value = Number(event.target.value)
              set(key, Number.isFinite(value) ? value : 0)
            }}
          />
        </Field>
      ))}

      <Field
        id="rules-sub-lang"
        label="Subtitle language"
        help="Which subtitle track is burned in when an episode is prepared (FR-P2). ISO 639-1."
        fallback={defaultLabel(defaults.sub_lang)}
        error={fieldErrors.sub_lang}
        onReset={() => {
          reset('sub_lang')
        }}
      >
        <>
          <input
            id="rules-sub-lang"
            type="text"
            list="rules-sub-langs"
            value={draft.sub_lang}
            className={`w-32 ${inputClass}`}
            onChange={(event) => {
              set('sub_lang', event.target.value)
            }}
          />
          <datalist id="rules-sub-langs">
            {SUB_LANGS.map((code) => (
              <option key={code} value={code} />
            ))}
          </datalist>
        </>
      </Field>

      <Field
        id="rules-audio-lang"
        label="Audio language"
        help="Which audio track is kept when a release has more than one (FR-P2). ISO 639-1."
        fallback={defaultLabel(defaults.audio_lang)}
        error={fieldErrors.audio_lang}
        onReset={() => {
          reset('audio_lang')
        }}
      >
        <>
          <input
            id="rules-audio-lang"
            type="text"
            list="rules-audio-langs"
            value={draft.audio_lang}
            className={`w-32 ${inputClass}`}
            onChange={(event) => {
              set('audio_lang', event.target.value)
            }}
          />
          <datalist id="rules-audio-langs">
            {AUDIO_LANGS.map((code) => (
              <option key={code} value={code} />
            ))}
          </datalist>
        </>
      </Field>

      <div className="flex flex-wrap items-center gap-3">
        <button type="submit" className={primaryButtonClass} disabled={!dirty || save.isPending}>
          {save.isPending ? 'Saving…' : 'Save rules'}
        </button>
        {dirty ? (
          <Pill tone="warn">
            {Object.keys(changed).length === 1
              ? '1 unsaved change'
              : `${String(Object.keys(changed).length)} unsaved changes`}
          </Pill>
        ) : null}
        {saved && !dirty ? <Notice>Rules saved.</Notice> : null}
      </div>

      {generalError === null ? null : <InlineError message={generalError} />}
    </form>
  )
}

export function RulesTab() {
  const settings = useSettings()

  if (settings.isPending) {
    return (
      <p role="status" className="text-sm text-[var(--arc-text-muted)]">
        Loading rules…
      </p>
    )
  }

  if (settings.isError) {
    return (
      <ErrorState
        message={adminErrorMessage(settings.error)}
        pending={settings.isFetching}
        onRetry={() => {
          void settings.refetch()
        }}
      />
    )
  }

  return (
    <div>
      <SectionHeading>Rules</SectionHeading>
      <p className="mt-1 max-w-3xl text-sm text-[var(--arc-text-muted)]">{EXPLANATION}</p>
      <RulesForm payload={settings.data} />
      <OverridesPanel payload={settings.data} />
    </div>
  )
}
