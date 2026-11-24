/**
 * Quartermaster member pages against the per-community gate: a 402 (plan lacks the feature)
 * renders UpgradeNotice with the server message, a 403 (not an active member) a clear hint,
 * and a normal response still renders the item / checkout lists.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import InventoryBrowse from '../InventoryBrowse';
import InventoryMyItems from '../InventoryMyItems';
import { inventoryApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  inventoryApi: { listAvailable: vi.fn(), getMyCheckouts: vi.fn(), checkout: vi.fn(), checkin: vi.fn() },
}));

const PLAN_MSG = 'Community inventory requires a Professional plan or higher';

function mount(path, element) {
  return render(
    <MemoryRouter initialEntries={[`/community/7/${path}`]}>
      <Routes>
        <Route path={`/community/:communityId/${path}`} element={element} />
      </Routes>
    </MemoryRouter>,
  );
}

const rejection = (status, message) => ({ response: { status, data: { error: { message } } } });

describe.each([
  ['inventory', 'listAvailable', () => <InventoryBrowse />],
  ['inventory/my-items', 'getMyCheckouts', () => <InventoryMyItems />],
])('%s page gate handling', (path, method, element) => {
  beforeEach(() => vi.clearAllMocks());

  it('renders UpgradeNotice with the server message on 402', async () => {
    inventoryApi[method].mockRejectedValue(rejection(402, PLAN_MSG));
    mount(path, element());
    expect(await screen.findByTestId('upgrade-notice')).toBeTruthy();
    expect(screen.getByText(PLAN_MSG)).toBeTruthy();
  });

  it('explains a 403 as a membership requirement', async () => {
    inventoryApi[method].mockRejectedValue(rejection(403, 'Community membership required'));
    mount(path, element());
    expect(await screen.findByText(/Only active members of this community/)).toBeTruthy();
    expect(screen.queryByTestId('upgrade-notice')).toBeNull();
  });

  it('keeps the generic message for other failures', async () => {
    inventoryApi[method].mockRejectedValue(rejection(500, 'boom'));
    mount(path, element());
    expect(await screen.findByText(/^Failed to load/)).toBeTruthy();
    expect(screen.queryByTestId('upgrade-notice')).toBeNull();
  });
});
