import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import SkipAnalytics from '../../components/SkipAnalytics'

vi.mock('../../api/client', () => ({
  api: { get: vi.fn() },
}))

import { api } from '../../api/client'
const mockApi = api as { get: ReturnType<typeof vi.fn> }

function mockEndpoints(opts: {
  categories?: unknown[]
  clients?: unknown[]
  titles?: unknown[]
}) {
  mockApi.get.mockImplementation((path: string) => {
    if (path.includes('by-category')) return Promise.resolve({ categories: opts.categories ?? [] })
    if (path.includes('by-client')) return Promise.resolve({ clients: opts.clients ?? [] })
    return Promise.resolve({ titles: opts.titles ?? [] })
  })
}

beforeEach(() => vi.clearAllMocks())

describe('SkipAnalytics', () => {
  it('explains an empty history rather than rendering blank charts', async () => {
    mockEndpoints({})

    render(<SkipAnalytics />)

    expect(await screen.findByText(/No skips recorded yet/)).toBeInTheDocument()
  })

  it('renders category counts', async () => {
    mockEndpoints({ categories: [{ category: 'nudity', count: 7 }] })

    render(<SkipAnalytics />)

    expect(await screen.findByText('nudity')).toBeInTheDocument()
    expect(screen.getByText('7')).toBeInTheDocument()
  })

  it('shows per-client failure rate and latency', async () => {
    mockEndpoints({
      clients: [
        {
          client_title: 'Roku',
          client_identifier: 'c1',
          count: 4,
          failures: 2,
          avg_latency_ms: 850,
          failure_rate: 0.5,
        },
      ],
    })

    render(<SkipAnalytics />)

    expect(await screen.findByText('Roku')).toBeInTheDocument()
    expect(screen.getByText('50%')).toBeInTheDocument()
    expect(screen.getByText('850ms')).toBeInTheDocument()
  })

  it('lists the most-skipped titles', async () => {
    mockEndpoints({ titles: [{ plex_guid: 'g1', title: 'Movie A', count: 12 }] })

    render(<SkipAnalytics />)

    expect(await screen.findByText('Movie A')).toBeInTheDocument()
  })

  it('aborts its requests on unmount', async () => {
    mockEndpoints({ categories: [{ category: 'nudity', count: 1 }] })

    const { unmount } = render(<SkipAnalytics />)
    await waitFor(() => expect(mockApi.get).toHaveBeenCalled())
    const signal = mockApi.get.mock.calls[0][1].signal as AbortSignal

    unmount()

    expect(signal.aborted).toBe(true)
  })
})
