/**
 * Licensed-feature 402 handling for the Polls and Forms admin pages: a gated community sees the
 * shared "Upgrade to Premium" state (not an error banner), other failures keep the error banner,
 * and a nested hub-api error envelope never crashes the page.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminPolls from '../AdminPolls';
import AdminForms from '../AdminForms';
import { adminApi } from '../../../services/api';

vi.mock('@penguintechinc/react-libs', async () => ({
  FormModalBuilder: (await import('../../../test/formModalStub')).FormModalStub,
}));
vi.mock('../../../services/api', () => ({
  adminApi: {
    getPolls: vi.fn(),
    getPoll: vi.fn(),
    createPoll: vi.fn(),
    deletePoll: vi.fn(),
    getForms: vi.fn(),
    getForm: vi.fn(),
    createForm: vi.fn(),
    deleteForm: vi.fn(),
  },
}));

const licensed402 = () => ({
  response: {
    status: 402,
    data: { success: false, error: { code: 'FEATURE_NOT_ENABLED', message: 'not enabled for this plan' } },
  },
});

function renderPage(Page) {
  return render(
    <MemoryRouter initialEntries={['/admin/7/page']}>
      <Routes>
        <Route path="/admin/:communityId/page" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
}

const CASES = [
  ['AdminPolls', AdminPolls, 'getPolls', 'Failed to load polls'],
  ['AdminForms', AdminForms, 'getForms', 'Failed to load forms'],
];

describe.each(CASES)('%s licensed-feature handling', (_name, Page, apiFn, failText) => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the Upgrade to Premium state on a 402, with no error banner', async () => {
    adminApi[apiFn].mockRejectedValue(licensed402());
    renderPage(Page);
    expect(await screen.findByText('Upgrade to Premium for this feature')).toBeInTheDocument();
    expect(screen.queryByText(failText)).not.toBeInTheDocument();
  });

  it('keeps the error banner for a non-402 failure', async () => {
    adminApi[apiFn].mockRejectedValue({ response: { status: 500, data: { error: { code: 'X', message: 'boom' } } } });
    renderPage(Page);
    await waitFor(() => expect(screen.getByText(failText)).toBeInTheDocument());
    expect(screen.queryByText('Upgrade to Premium for this feature')).not.toBeInTheDocument();
  });
});
