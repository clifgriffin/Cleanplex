import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { RotateCcw, Volume2, SkipForward } from 'lucide-react'

interface CategoryPref {
  category: string
  level: number
  action: string
  has_segments: boolean
}

interface CategoriesResponse {
  username: string
  uses_defaults: boolean
  categories: CategoryPref[]
}

// Same 0–3 scale as VideoSkip / skp-forge: a segment fires when viewer level
// plus content grade (1 children / 2 teens / 3 adults) exceeds 3.
const LEVEL_LABELS = ['Off', 'Adults only', 'Teens and up', 'Including mild']

const CATEGORY_LABELS: Record<string, string> = {
  commercial: 'Commercials',
  discrimination: 'Discrimination',
  dispensable: 'Filler',
  drugs: 'Drugs and alcohol',
  fear: 'Frightening scenes',
  language: 'Profanity',
  nudity: 'Nudity',
  sex: 'Sexual content',
  violence: 'Violence and gore',
  other: 'Other',
}

interface Props {
  username: string
  /** Master switch: when off, the matrix is shown but visibly inert. */
  enabled: boolean
}

export default function CategoryMatrix({ username, enabled }: Props) {
  const [data, setData] = useState<CategoriesResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<Record<string, boolean>>({})
  const [error, setError] = useState('')

  // Refetch when the selected user changes; an in-flight request for the previous
  // user would otherwise overwrite this one's state.
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    api
      .get<CategoriesResponse>(`/api/users/${encodeURIComponent(username)}/categories`, {
        signal: controller.signal,
      })
      .then(d => {
        setData(d)
        setLoading(false)
      })
      .catch(err => {
        if (err.name !== 'AbortError') {
          setError('Could not load category preferences.')
          setLoading(false)
        }
      })
    return () => controller.abort()
  }, [username])

  const update = async (category: string, level: number, action: string) => {
    setSaving(s => ({ ...s, [category]: true }))
    setError('')
    try {
      await api.put(`/api/users/${encodeURIComponent(username)}/categories/${category}`, {
        level,
        action,
      })
      setData(d =>
        d
          ? {
              ...d,
              uses_defaults: false,
              categories: d.categories.map(c =>
                c.category === category ? { ...c, level, action } : c,
              ),
            }
          : d,
      )
    } catch {
      setError('Could not save that change.')
    } finally {
      setSaving(s => ({ ...s, [category]: false }))
    }
  }

  const reset = async () => {
    await api.delete(`/api/users/${encodeURIComponent(username)}/categories`)
    const fresh = await api.get<CategoriesResponse>(
      `/api/users/${encodeURIComponent(username)}/categories`,
    )
    setData(fresh)
  }

  if (loading) return <div className="text-gray-500 text-sm px-5 py-4">Loading categories...</div>
  if (!data) return null

  return (
    <div className={enabled ? '' : 'opacity-40 pointer-events-none'}>
      <div className="flex items-center justify-between px-5 py-3 border-b border-plex-border">
        <div>
          <p className="text-sm font-medium text-gray-200">Category filtering</p>
          <p className="text-xs text-gray-500 mt-0.5">
            {data.uses_defaults
              ? 'Using defaults: teen-and-up profanity, everything else.'
              : 'Customised for this user.'}
            {!enabled && ' Filtering is off for this user.'}
          </p>
        </div>
        {!data.uses_defaults && (
          <button
            onClick={reset}
            className="text-xs text-gray-400 hover:text-gray-200 flex items-center gap-1"
          >
            <RotateCcw size={12} /> Reset
          </button>
        )}
      </div>

      {error && <p className="text-xs text-red-400 px-5 pt-3">{error}</p>}

      <div className="divide-y divide-plex-border">
        {data.categories.map(cat => (
          <div key={cat.category} className="px-5 py-3 flex items-center gap-4">
            <div className="w-44 flex-shrink-0">
              <p className="text-sm text-gray-200">
                {CATEGORY_LABELS[cat.category] ?? cat.category}
              </p>
              {cat.category === 'language' && (
                <p className="text-[11px] text-gray-600 mt-0.5">
                  1 hell/damn · 2 bitch/shit · 3 f-words
                </p>
              )}
              {!cat.has_segments && (
                <p className="text-[11px] text-gray-600 mt-0.5">No segments yet</p>
              )}
            </div>

            <div className="flex-1 flex items-center gap-3">
              <input
                type="range"
                min={0}
                max={3}
                step={1}
                value={cat.level}
                disabled={saving[cat.category]}
                onChange={e => update(cat.category, Number(e.target.value), cat.action)}
                className="flex-1 accent-plex-orange"
              />
              <span className="text-xs text-gray-500 w-36">{LEVEL_LABELS[cat.level]}</span>
            </div>

            {/* Action override. Only skip and mute are possible through the Plex
                playback API, so those are the only choices offered. */}
            <div className="flex gap-1">
              {(['', 'skip', 'mute'] as const).map(act => (
                <button
                  key={act || 'auto'}
                  onClick={() => update(cat.category, cat.level, act)}
                  disabled={saving[cat.category]}
                  title={act === '' ? 'Use the segment’s own action' : `Always ${act}`}
                  className={`px-2 py-1 rounded text-[11px] border transition-colors ${
                    cat.action === act
                      ? 'bg-plex-orange border-plex-orange text-white'
                      : 'border-plex-border text-gray-500 hover:text-gray-300'
                  }`}
                >
                  {act === '' ? 'Auto' : act === 'skip' ? <SkipForward size={11} /> : <Volume2 size={11} />}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
