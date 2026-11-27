/**
 * AdminRadioStations runs against hub-api's served `music/radio-stations` routes (stubbed only
 * at the HTTP adapter): paginated list, add, test stream, set default, delete; loading, empty
 * and error states are all visible.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import api from '../../../services/api';
import { apiClient } from '../../../lib/apiClient';
import { stubHttp } from '../../../test/httpStub';
import { isFeatureServed } from '../../../lib/unservedFeatures';
import AdminRadioStations from '../AdminRadioStations';

const CID = '0b6b0a52-5f0e-4f6a-9a39-1b6a3d6d9f41';
const BASE = `/api/v1/admin/${CID}/music/radio-stations`;
const S1 = '1c7c1b63-6a1f-4a7b-8b4a-2c7b4e7e0a41';
const S2 = '1c7c1b63-6a1f-4a7b-8b4a-2c7b4e7e0a42';

const station = (id, over = {}) => ({
  id,
  communityId: CID,
  name: `Station ${id.slice(-2)}`,
  url: `https://stream.example.test/${id.slice(-2)}`,
  description: null,
  genre: 'lofi',
  isActive: true,
  isDefault: false,
  lastTestedAt: null,
  createdAt: '2026-11-01T00:00:00Z',
  updatedAt: '2026-11-01T00:00:00Z',
  ...over,
});
const page = (stations, pagination = { page: 1, limit: 25, total: stations.length, pages: 1 }) => ({
  success: true,
  pagination,
  stations,
});
const apiError = (status, message) =>
  Object.assign(new Error(message), { isAxiosError: true, response: { status, data: { error: { message } } } });

let http;
afterEach(() => {
  http?.restore();
  vi.restoreAllMocks();
});

function mount() {
  return render(
    <MemoryRouter initialEntries={[`/admin/${CID}/music/radio`]}>
      <Routes>
        <Route path="/admin/:communityId/music/radio" element={<AdminRadioStations />} />
      </Routes>
    </MemoryRouter>,
  );
}

const card = (name) => screen.getByText(name).closest('[data-testid="radio-station"]');

describe('AdminRadioStations', () => {
  it('is no longer registered as unserved; shows loading then the empty state', async () => {
    expect(isFeatureServed('radio-station-actions')).toBe(true);
    http = stubHttp([api, apiClient], { [`GET ${BASE}`]: page([]) });
    mount();
    expect(screen.getByRole('status', { name: 'Loading radio stations' })).toBeInTheDocument();
    expect(await screen.findByTestId('radio-stations-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('not-available-yet')).toBeNull();
    expect(http.seen[0].params).toEqual({ page: 1, limit: 25 });
  });

  it('shows the load error (never an empty list) with a working retry', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    let fail = true;
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: () => {
        if (fail) throw apiError(500, 'Internal error');
        return page([station(S1)]);
      },
    });
    mount();
    expect(await screen.findByRole('alert')).toHaveTextContent('Internal error');
    expect(screen.queryByTestId('radio-stations-empty')).toBeNull();
    fail = false;
    fireEvent.click(screen.getByText('Retry'));
    expect(await screen.findByText('Station 41')).toBeInTheDocument();
  });

  it('adds a station with the served {name, url, genre} contract', async () => {
    const stations = [];
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: () => page([...stations]),
      [`POST ${BASE}`]: (req) => {
        stations.push(station(S1, { name: req.body.name, url: req.body.url, genre: req.body.genre }));
        return { success: true, station: stations[0] };
      },
    });
    mount();
    await screen.findByTestId('radio-stations-empty');
    fireEvent.click(screen.getAllByText('Add Station')[0]);
    fireEvent.change(screen.getByLabelText('Station Name *'), { target: { value: 'Chill' } });
    fireEvent.change(screen.getByLabelText('Stream URL *'), { target: { value: 'https://radio.example.test/live' } });
    fireEvent.change(screen.getByLabelText('Genre'), { target: { value: 'ambient' } });
    fireEvent.click(screen.getAllByText('Add Station')[1]);
    expect(await screen.findByText('Radio station added successfully')).toBeInTheDocument();
    expect(http.seen.find((r) => r.method === 'POST').body).toEqual({
      name: 'Chill',
      url: 'https://radio.example.test/live',
      genre: 'ambient',
    });
    expect(await screen.findByText('Chill')).toBeInTheDocument();
  });

  it('rejects an empty form client-side and shows a server 400 on add', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: page([]),
      [`POST ${BASE}`]: () => {
        throw apiError(400, 'URL resolves to a disallowed network address: internal');
      },
    });
    mount();
    await screen.findByTestId('radio-stations-empty');
    fireEvent.click(screen.getAllByText('Add Station')[0]);
    fireEvent.click(screen.getAllByText('Add Station')[1]);
    expect(await screen.findByText('Station name and stream URL are required')).toBeInTheDocument();
    expect(http.seen.some((r) => r.method === 'POST')).toBe(false);

    fireEvent.change(screen.getByLabelText('Station Name *'), { target: { value: 'x' } });
    fireEvent.change(screen.getByLabelText('Stream URL *'), { target: { value: 'http://internal/x' } });
    fireEvent.click(screen.getAllByText('Add Station')[1]);
    expect(await screen.findByText(/disallowed network address/)).toBeInTheDocument();
  });

  it('tests a stream: reachable -> success, unreachable -> visible error', async () => {
    let reachable = true;
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: page([station(S1)]),
      [`POST ${BASE}/${S1}/test`]: () => ({
        success: true,
        result: reachable
          ? { reachable: true, httpStatus: 200, contentType: 'audio/mpeg', testedAt: '2026-11-27T00:00:00Z' }
          : { reachable: false, httpStatus: null, contentType: null, testedAt: '2026-11-27T00:00:00Z' },
      }),
    });
    mount();
    fireEvent.click(await screen.findByText('Test Stream'));
    expect(await screen.findByText('Stream reachable (HTTP 200).')).toBeInTheDocument();

    reachable = false;
    fireEvent.click(await screen.findByText('Test Stream'));
    expect(await screen.findByText('Stream unreachable: no response from the stream URL.')).toBeInTheDocument();
  });

  it('surfaces a 402 plan gate on Test Stream', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: page([station(S1)]),
      [`POST ${BASE}/${S1}/test`]: () => {
        throw apiError(402, 'The Music Station is not enabled for this plan');
      },
    });
    mount();
    fireEvent.click(await screen.findByText('Test Stream'));
    expect(await screen.findByText('The Music Station is not enabled for this plan')).toBeInTheDocument();
  });

  it('sets a default station and re-renders the badge from the refetch', async () => {
    let defaultId = S1;
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: () =>
        page([station(S1, { isDefault: defaultId === S1 }), station(S2, { isDefault: defaultId === S2 })]),
      [`PUT ${BASE}/${S2}/default`]: () => {
        defaultId = S2;
        return { success: true, station: station(S2, { isDefault: true }) };
      },
    });
    mount();
    await screen.findByText('Station 42');
    expect(within(card('Station 41')).getByText('Default')).toBeInTheDocument();
    expect(within(card('Station 41')).queryByText('Set Default')).toBeNull();
    fireEvent.click(within(card('Station 42')).getByText('Set Default'));
    expect(await screen.findByText('Default station updated successfully')).toBeInTheDocument();
    await waitFor(() => expect(within(card('Station 42')).getByText('Default')).toBeInTheDocument());
    expect(within(card('Station 41')).queryByText('Default')).toBeNull();
  });

  it('hides Set Default for an inactive station', async () => {
    http = stubHttp([api, apiClient], { [`GET ${BASE}`]: page([station(S1, { isActive: false })]) });
    mount();
    await screen.findByText('Station 41');
    expect(screen.queryByText('Set Default')).toBeNull();
    expect(screen.getByText('Inactive')).toBeInTheDocument();
  });

  it('deletes after confirmation', async () => {
    let stations = [station(S1)];
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: () => page(stations),
      [`DELETE ${BASE}/${S1}`]: () => {
        stations = [];
        return { success: true, message: 'Radio station removed' };
      },
    });
    mount();
    fireEvent.click(await screen.findByLabelText('Delete Station 41'));
    fireEvent.click(screen.getByText('Remove Station'));
    expect(await screen.findByTestId('radio-stations-empty')).toBeInTheDocument();
  });

  it('paginates with the server pagination block', async () => {
    http = stubHttp([api, apiClient], {
      [`GET ${BASE}`]: (req) =>
        page([station(req.params.page === 1 ? S1 : S2)], { page: req.params.page, limit: 25, total: 26, pages: 2 }),
    });
    mount();
    expect(await screen.findByText('Page 1 of 2 (26 stations)')).toBeInTheDocument();
    expect(screen.getByText('Previous')).toBeDisabled();
    fireEvent.click(screen.getByText('Next'));
    expect(await screen.findByText('Station 42')).toBeInTheDocument();
    expect(http.seen.at(-1).params).toEqual({ page: 2, limit: 25 });
    expect(screen.getByText('Next')).toBeDisabled();
  });
});
