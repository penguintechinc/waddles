/**
 * Tests for the community profile editor: profile load/defaults, field and
 * radio editing, save payload, and the logo / banner upload + delete flows
 * (type and size validation, previews, confirmations, error messages).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';

import AdminCommunityProfile from '../AdminCommunityProfile';
import { adminApi, publicApi } from '../../../services/api';

vi.mock('../../../services/api', () => ({
  publicApi: { getCommunityProfile: vi.fn() },
  adminApi: {
    updateCommunityProfile: vi.fn(),
    uploadCommunityLogo: vi.fn(),
    deleteCommunityLogo: vi.fn(),
    uploadCommunityBanner: vi.fn(),
    deleteCommunityBanner: vi.fn(),
  },
}));

const COMMUNITY = {
  displayName: 'Penguin Club',
  description: 'Waddle together',
  aboutExtended: 'A long story',
  websiteUrl: 'https://penguin.example',
  discordInviteUrl: 'https://discord.gg/abc',
  socialLinks: { twitter: 'https://twitter.com/p' },
  visibility: 'registered',
  join_mode: 'approval',
  logoUrl: 'https://img/logo.png',
  bannerUrl: 'https://img/banner.png',
};

function mount() {
  return render(
    <MemoryRouter initialEntries={['/admin/7/profile']}>
      <Routes>
        <Route path="/admin/:communityId/profile" element={<AdminCommunityProfile />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function loaded() {
  const view = mount();
  await screen.findByText('Community Profile');
  return view;
}

function imageFile(name = 'pic.png', type = 'image/png', size = 1024) {
  const file = new File(['x'], name, { type });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

/** Deterministic FileReader: resolves in a microtask, always before the (timer-delayed) upload mock. */
class InstantFileReader {
  readAsDataURL() {
    this.result = 'data:image/png;base64,AAAA';
    queueMicrotask(() => this.onload?.());
  }
}

const fileInputs = (container) => container.querySelectorAll('input[type="file"]');
const choose = (input, file) => fireEvent.change(input, { target: { files: file ? [file] : [] } });
const slow = (value) => () => new Promise((resolve) => setTimeout(() => resolve(value), 40));

beforeEach(() => {
  vi.clearAllMocks();
  publicApi.getCommunityProfile.mockResolvedValue({ data: { success: true, community: COMMUNITY } });
  adminApi.updateCommunityProfile.mockResolvedValue({ data: { success: true } });
  adminApi.uploadCommunityLogo.mockImplementation(slow({ data: { success: true, logoUrl: 'https://cdn/new-logo.png' } }));
  adminApi.uploadCommunityBanner.mockImplementation(slow({ data: { success: true, bannerUrl: 'https://cdn/new-banner.png' } }));
  adminApi.deleteCommunityLogo.mockResolvedValue({ data: { success: true } });
  adminApi.deleteCommunityBanner.mockResolvedValue({ data: { success: true } });
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('FileReader', InstantFileReader);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('AdminCommunityProfile loading', () => {
  it('shows a spinner then loads the community profile', async () => {
    const { container } = mount();
    expect(container.querySelector('.animate-spin')).toBeInTheDocument();
    await screen.findByText('Community Profile');
    expect(publicApi.getCommunityProfile).toHaveBeenCalledWith('7');
  });

  it('populates every field and both previews from the profile', async () => {
    await loaded();
    expect(screen.getByPlaceholderText('Community display name')).toHaveValue('Penguin Club');
    expect(screen.getByPlaceholderText('Brief description of your community')).toHaveValue('Waddle together');
    expect(screen.getByText('15/200')).toBeInTheDocument();
    expect(screen.getByText('12/5000')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('https://yourwebsite.com')).toHaveValue('https://penguin.example');
    expect(screen.getByPlaceholderText('https://discord.gg/...')).toHaveValue('https://discord.gg/abc');
    expect(screen.getByPlaceholderText('https://twitter.com/...')).toHaveValue('https://twitter.com/p');
    expect(screen.getByPlaceholderText('https://youtube.com/...')).toHaveValue('');
    expect(screen.getByRole('radio', { name: /Registered Users/ })).toBeChecked();
    expect(screen.getByRole('radio', { name: /Requires Approval/ })).toBeChecked();
    expect(screen.getByAltText('Logo')).toHaveAttribute('src', 'https://img/logo.png');
    expect(screen.getByAltText('Banner')).toHaveAttribute('src', 'https://img/banner.png');
  });

  it('applies defaults for a sparse profile and omits the previews', async () => {
    publicApi.getCommunityProfile.mockResolvedValue({ data: { success: true, community: {} } });
    await loaded();
    expect(screen.getByPlaceholderText('Community display name')).toHaveValue('');
    expect(screen.getByRole('radio', { name: /Public/ })).toBeChecked();
    expect(screen.getByRole('radio', { name: /Open/ })).toBeChecked();
    expect(screen.queryByAltText('Logo')).not.toBeInTheDocument();
    expect(screen.queryByAltText('Banner')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '' })).not.toBeInTheDocument();
  });

  it('keeps the defaults when the API reports success:false', async () => {
    publicApi.getCommunityProfile.mockResolvedValue({ data: { success: false } });
    await loaded();
    expect(screen.getByPlaceholderText('Community display name')).toHaveValue('');
  });

  it('shows an error message when the profile fails to load', async () => {
    publicApi.getCommunityProfile.mockRejectedValue(new Error('x'));
    mount();
    expect(await screen.findByText(/Failed to load community profile/)).toBeInTheDocument();
  });
});

describe('AdminCommunityProfile editing and saving', () => {
  it('saves the edited profile, including social links and radio choices', async () => {
    await loaded();
    fireEvent.change(screen.getByPlaceholderText('Community display name'), { target: { value: 'Emperor Club' } });
    fireEvent.change(screen.getByPlaceholderText('Brief description of your community'), { target: { value: 'Short' } });
    fireEvent.change(screen.getByPlaceholderText(/Tell visitors/), { target: { value: 'Details' } });
    fireEvent.change(screen.getByPlaceholderText('https://yourwebsite.com'), { target: { value: 'https://e.example' } });
    fireEvent.change(screen.getByPlaceholderText('https://youtube.com/...'), { target: { value: 'https://youtube.com/e' } });
    fireEvent.click(screen.getByRole('radio', { name: /Members Only/ }));
    fireEvent.click(screen.getByRole('radio', { name: /Invite Only/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Profile' }));

    expect(await screen.findByText('Profile saved successfully')).toBeInTheDocument();
    expect(adminApi.updateCommunityProfile).toHaveBeenCalledWith('7', {
      displayName: 'Emperor Club',
      description: 'Short',
      aboutExtended: 'Details',
      websiteUrl: 'https://e.example',
      discordInviteUrl: 'https://discord.gg/abc',
      socialLinks: { twitter: 'https://twitter.com/p', youtube: 'https://youtube.com/e' },
      visibility: 'members_only',
      join_mode: 'invite',
    });
    expect(screen.getByText('5/200')).toBeInTheDocument();
  });

  it('shows the server message when saving fails', async () => {
    adminApi.updateCommunityProfile.mockRejectedValue({ response: { data: { error: { message: 'name taken' } } } });
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'Save Profile' }));
    expect(await screen.findByText(/name taken/)).toBeInTheDocument();
  });

  it('falls back to a generic save error and lets the message be dismissed', async () => {
    adminApi.updateCommunityProfile.mockRejectedValue(new Error('x'));
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'Save Profile' }));
    const message = await screen.findByText(/Failed to save profile/);
    fireEvent.click(within(message).getByRole('button', { name: '×' }));
    expect(screen.queryByText(/Failed to save profile/)).not.toBeInTheDocument();
  });

  it('shows Saving... while the request is in flight', async () => {
    let release;
    adminApi.updateCommunityProfile.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    await loaded();
    fireEvent.click(screen.getByRole('button', { name: 'Save Profile' }));
    expect(await screen.findByRole('button', { name: 'Saving...' })).toBeDisabled();
    release({ data: {} });
    await screen.findByRole('button', { name: 'Save Profile' });
  });
});

describe.each([
  {
    kind: 'Logo',
    index: 1,
    upload: 'uploadCommunityLogo',
    remove: 'deleteCommunityLogo',
    uploadButton: 'Upload Logo',
    limit: 5 * 1024 * 1024,
    tooBig: 'Logo must be less than 5MB',
    newUrl: 'https://cdn/new-logo.png',
    uploaded: 'Logo uploaded successfully',
    uploadFailed: 'Failed to upload logo',
    deleted: 'Logo deleted',
    deleteFailed: 'Failed to delete logo',
    confirmText: 'Delete community logo?',
  },
  {
    kind: 'Banner',
    index: 0,
    upload: 'uploadCommunityBanner',
    remove: 'deleteCommunityBanner',
    uploadButton: 'Upload Banner',
    limit: 10 * 1024 * 1024,
    tooBig: 'Banner must be less than 10MB',
    newUrl: 'https://cdn/new-banner.png',
    uploaded: 'Banner uploaded successfully',
    uploadFailed: 'Failed to upload banner',
    deleted: 'Banner deleted',
    deleteFailed: 'Failed to delete banner',
    confirmText: 'Delete community banner?',
  },
])('AdminCommunityProfile $kind image', (c) => {
  const deleteButton = () => {
    const section = screen.getByAltText(c.kind).closest('div.card');
    return within(section).getAllByRole('button').find((b) => b.textContent === '');
  };

  it('opens the hidden file picker from the upload button', async () => {
    const { container } = await loaded();
    const click = vi.spyOn(fileInputs(container)[c.index], 'click').mockImplementation(() => {});
    fireEvent.click(screen.getByRole('button', { name: c.uploadButton }));
    expect(click).toHaveBeenCalledTimes(1);
  });

  it('uploads a valid image and swaps in the returned URL', async () => {
    const { container } = await loaded();
    const file = imageFile();
    choose(fileInputs(container)[c.index], file);

    // Local preview shows immediately, before the upload round-trip finishes.
    await waitFor(() => expect(screen.getByAltText(c.kind)).toHaveAttribute('src', 'data:image/png;base64,AAAA'));
    expect(await screen.findByText(c.uploaded)).toBeInTheDocument();
    expect(adminApi[c.upload]).toHaveBeenCalledWith('7', file);
    expect(screen.getByAltText(c.kind)).toHaveAttribute('src', c.newUrl);
  });

  it('ignores an empty selection', async () => {
    const { container } = await loaded();
    choose(fileInputs(container)[c.index], null);
    expect(adminApi[c.upload]).not.toHaveBeenCalled();
  });

  it('rejects a non-image file', async () => {
    const { container } = await loaded();
    choose(fileInputs(container)[c.index], imageFile('doc.pdf', 'application/pdf'));
    expect(await screen.findByText(/valid image file/)).toBeInTheDocument();
    expect(adminApi[c.upload]).not.toHaveBeenCalled();
  });

  it('rejects an oversized image', async () => {
    const { container } = await loaded();
    choose(fileInputs(container)[c.index], imageFile('big.png', 'image/png', c.limit + 1));
    expect(await screen.findByText(c.tooBig)).toBeInTheDocument();
    expect(adminApi[c.upload]).not.toHaveBeenCalled();
  });

  it('reports a failed upload', async () => {
    adminApi[c.upload].mockRejectedValue(new Error('boom'));
    const { container } = await loaded();
    choose(fileInputs(container)[c.index], imageFile());
    expect(await screen.findByText(c.uploadFailed)).toBeInTheDocument();
  });

  it('does not announce success when the API reports success:false', async () => {
    adminApi[c.upload].mockResolvedValue({ data: { success: false } });
    const { container } = await loaded();
    choose(fileInputs(container)[c.index], imageFile());
    await waitFor(() => expect(adminApi[c.upload]).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText(c.uploaded)).not.toBeInTheDocument();
  });

  it('deletes after confirmation and clears the preview', async () => {
    await loaded();
    fireEvent.click(deleteButton());

    expect(await screen.findByText(c.deleted)).toBeInTheDocument();
    expect(confirm).toHaveBeenCalledWith(c.confirmText);
    expect(adminApi[c.remove]).toHaveBeenCalledWith('7');
    expect(screen.queryByAltText(c.kind)).not.toBeInTheDocument();
  });

  it('keeps the image when deletion is declined', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false));
    await loaded();
    fireEvent.click(deleteButton());
    expect(adminApi[c.remove]).not.toHaveBeenCalled();
    expect(screen.getByAltText(c.kind)).toBeInTheDocument();
  });

  it('reports a failed deletion and keeps the image', async () => {
    adminApi[c.remove].mockRejectedValue(new Error('x'));
    await loaded();
    fireEvent.click(deleteButton());
    expect(await screen.findByText(c.deleteFailed)).toBeInTheDocument();
    expect(screen.getByAltText(c.kind)).toBeInTheDocument();
  });
});
