import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { LiveActivityRow } from '../LiveActivityRow';
import { ReputationLeaderboardTab } from '../ReputationLeaderboardTab';
import { MyCommunityCard } from '../../dashboard/MyCommunityCard';
import { MemberRow } from '../../dashboard/MemberRow';
import { CommunityHeader } from '../../dashboard/CommunityHeader';
import { AnnouncementsPanel } from '../../dashboard/AnnouncementsPanel';
import { httpError, mockApiGet } from '../../../test/api';
import { renderWithProviders } from '../../../test/renderWithProviders';

describe('LiveActivityRow', () => {
  it('renders a bare event and tolerates an unparseable timestamp', () => {
    render(<ul><LiveActivityRow event={{ id: 1, platform: 'twitch', actor: null, messageIn: null, replyOut: null, occurredAt: 'garbage' }} /></ul>);
    const row = screen.getByTestId('live-activity-row');
    expect(within(row).getByText('Unknown user')).toBeInTheDocument();
    expect(row.querySelector('time')).toHaveTextContent('');
    expect(within(row).queryByText(/Waddles:/)).not.toBeInTheDocument();
  });
});

describe('ReputationLeaderboardTab', () => {
  it('colours each tier band and keeps the server tier text', async () => {
    mockApiGet(() => ({
      status: 'success',
      data: {
        entries: [
          { display_name: 'a', score: 800, tier: 'Legend' },
          { display_name: 'b', score: 700, tier: 'Trusted' },
          { display_name: 'c', score: 600, tier: 'Fair' },
          { display_name: 'd', score: 400, tier: 'Newcomer' },
        ],
      },
    }));
    renderWithProviders(<ReputationLeaderboardTab communityId={5} />);
    expect(await screen.findByText('Legend')).toBeInTheDocument();
    for (const tier of ['Trusted', 'Fair', 'Newcomer']) expect(screen.getByText(tier)).toBeInTheDocument();
    expect(screen.getByText('#4')).toBeInTheDocument();
  });
});

describe('MyCommunityCard', () => {
  it('renders from membership alone, and shows platform/logo/description when details exist', () => {
    const community = { id: 3, name: 'n', displayName: 'Name', role: 'vip' };
    const { rerender } = render(<MemoryRouter><MyCommunityCard community={community} /></MemoryRouter>);
    expect(screen.getByText('Name')).toBeInTheDocument();
    expect(screen.queryByText(/members$/)).not.toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <MyCommunityCard
          community={community}
          details={{ id: 3, name: 'n', displayName: 'Name', description: null, logoUrl: 'https://x/l.png', bannerUrl: null, platform: 'discord', memberCount: 2, joinMode: null, createdAt: null }}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText('No description')).toBeInTheDocument();
    expect(screen.getByText('2 members')).toBeInTheDocument();
    expect(document.querySelector('img[src="https://x/l.png"]')).not.toBeNull();
  });
});

describe('MemberRow', () => {
  it('shows avatars when present and initials otherwise', () => {
    const base = { id: 1, userId: null, username: 'zed', avatarUrl: null, role: 'vip', joinedAt: null };
    const { rerender } = render(<MemoryRouter><ul><MemberRow member={base} /></ul></MemoryRouter>);
    expect(screen.getByText('Z')).toBeInTheDocument();
    expect(screen.getByText('Joined Unknown')).toBeInTheDocument();
    rerender(<MemoryRouter><ul><MemberRow member={{ ...base, avatarUrl: 'https://x/a.png' }} /></ul></MemoryRouter>);
    expect(document.querySelector('img[src="https://x/a.png"]')).not.toBeNull();
  });
});

describe('CommunityHeader', () => {
  const membership = { id: 7, name: 'bc', displayName: 'Bar Citizen', role: 'community-owner' };
  it('prefers public details, falls back to membership, and gates the admin link', () => {
    const { rerender } = render(<MemoryRouter><CommunityHeader membership={membership} details={undefined} canAdminister={false} /></MemoryRouter>);
    expect(screen.getByRole('heading', { name: 'Bar Citizen' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Admin Panel' })).not.toBeInTheDocument();
    rerender(
      <MemoryRouter>
        <CommunityHeader
          membership={membership}
          canAdminister
          details={{ id: 7, name: 'bc', displayName: 'HQ', description: null, logoUrl: 'https://x/l.png', bannerUrl: null, platform: null, memberCount: 1, joinMode: null, createdAt: null }}
        />
      </MemoryRouter>,
    );
    expect(screen.getByRole('heading', { name: 'HQ' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Admin Panel' })).toHaveAttribute('href', '/admin/7');
  });
});

describe('AnnouncementsPanel', () => {
  const item = (over: Record<string, unknown> = {}) => ({
    id: 1,
    title: 'Hello',
    content: 'Body',
    announcement_type: 'event',
    is_pinned: false,
    created_by_name: null,
    created_at: null,
    ...over,
  });

  it('renders types, pinned marker, fallbacks and the manage link for admins', async () => {
    mockApiGet(() => ({ data: [item(), item({ id: 2, title: 'Pinned', announcement_type: 'update', is_pinned: true, created_by_name: 'Ann', created_at: '2026-10-01T00:00:00Z' })] }));
    renderWithProviders(<AnnouncementsPanel communityId={7} canManage />);
    expect(await screen.findByText('Hello')).toBeInTheDocument();
    expect(screen.getByLabelText('Pinned')).toBeInTheDocument();
    expect(screen.getByText('Event')).toBeInTheDocument();
    expect(screen.getByText('Update')).toBeInTheDocument();
    expect(screen.getByText('Admin')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Manage' })).toHaveAttribute('href', '/admin/7/announcements');
  });

  it('renders nothing for an empty feed and warns on a non-HTTP failure', async () => {
    mockApiGet(() => ({ data: [] }));
    const empty = renderWithProviders(<AnnouncementsPanel communityId={7} canManage={false} />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(empty.container).toBeEmptyDOMElement();
    empty.unmount();

    mockApiGet(() => {
      throw new Error('network down');
    });
    renderWithProviders(<AnnouncementsPanel communityId={7} canManage={false} />);
    expect(await screen.findByText('We could not load announcements')).toBeInTheDocument();
    expect(screen.getByText('network down')).toBeInTheDocument();
  });

  it('treats an HTTP error without a numeric status as unexpected', async () => {
    mockApiGet(() => {
      throw Object.assign(new Error('weird'), { response: { data: 'x' } });
    });
    renderWithProviders(<AnnouncementsPanel communityId={7} canManage={false} />);
    expect(await screen.findByText('We could not load announcements')).toBeInTheDocument();
  });

  it('stays silent for expected denials', async () => {
    mockApiGet(() => {
      throw httpError(402, { error: 'plan' });
    });
    const view = renderWithProviders(<AnnouncementsPanel communityId={7} canManage={false} />);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(view.container).toBeEmptyDOMElement();
  });
});
