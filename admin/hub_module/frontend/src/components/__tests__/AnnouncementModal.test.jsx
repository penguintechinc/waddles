/**
 * Tests for the announcement create/edit modal: field set (incl. platform
 * checkboxes only when broadcast is toggled), edit-mode prefill, and the
 * payload handed to onSave (trimmed text, selected platforms only).
 */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import AnnouncementModal from '../AnnouncementModal';

vi.mock('@penguintechinc/react-libs', async () => ({
  FormModalBuilder: (await import('../../test/formModalStub')).FormModalStub,
}));

function mount(props = {}) {
  const onSave = vi.fn().mockResolvedValue(undefined);
  const onClose = vi.fn();
  const view = render(<AnnouncementModal isOpen onClose={onClose} onSave={onSave} announcement={null} {...props} />);
  return { onSave, onClose, ...view };
}

const dialog = (name) => within(screen.getByRole('dialog', { name }));

describe('AnnouncementModal create mode', () => {
  it('shows the create title, submit text and base fields with defaults', () => {
    mount();
    const d = dialog('Create Announcement');
    expect(d.getByRole('button', { name: 'Create Announcement' })).toBeInTheDocument();
    expect(d.getByLabelText('Title')).toHaveValue('');
    expect(d.getByLabelText('Announcement Type')).toHaveValue('general');
    expect(d.getByLabelText('Save As')).toHaveValue('draft');
    expect(d.getByLabelText('Pin this announcement')).not.toBeChecked();
    expect(d.queryByLabelText('Broadcast to connected platforms')).not.toBeInTheDocument();
  });

  it('renders nothing while closed', () => {
    mount({ isOpen: false });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('submits a trimmed payload with no broadcast when there are no connected platforms', async () => {
    const { onSave } = mount();
    const d = dialog('Create Announcement');
    fireEvent.change(d.getByLabelText('Title'), { target: { value: '  Big news ' } });
    fireEvent.change(d.getByLabelText('Content'), { target: { value: ' All hands ' } });
    fireEvent.change(d.getByLabelText('Announcement Type'), { target: { value: 'important' } });
    fireEvent.change(d.getByLabelText('Save As'), { target: { value: 'published' } });
    fireEvent.click(d.getByLabelText('Pin this announcement'));
    fireEvent.click(d.getByRole('button', { name: 'Create Announcement' }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    const payload = onSave.mock.calls[0][0];
    expect(payload).toMatchObject({
      title: 'Big news',
      content: 'All hands',
      announcement_type: 'important',
      is_pinned: true,
      status: 'published',
      selected_platforms: [],
    });
    expect(payload.broadcast_to_platforms).toBeFalsy();
  });

  it('adds broadcast controls per connected platform, shown only once broadcast is toggled', () => {
    mount({ connectedPlatforms: ['discord', 'twitch'] });
    const d = dialog('Create Announcement');
    expect(d.getByLabelText('Broadcast to connected platforms')).not.toBeChecked();
    expect(d.queryByLabelText('Discord')).not.toBeInTheDocument();

    fireEvent.click(d.getByLabelText('Broadcast to connected platforms'));
    expect(d.getByLabelText('Discord')).not.toBeChecked();
    expect(d.getByLabelText('Twitch')).not.toBeChecked();
  });

  it('submits only the platforms that were ticked', async () => {
    const { onSave } = mount({ connectedPlatforms: ['discord', 'twitch'] });
    const d = dialog('Create Announcement');
    fireEvent.change(d.getByLabelText('Title'), { target: { value: 't' } });
    fireEvent.click(d.getByLabelText('Broadcast to connected platforms'));
    fireEvent.click(d.getByLabelText('Twitch'));
    fireEvent.click(d.getByRole('button', { name: 'Create Announcement' }));

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave.mock.calls[0][0]).toMatchObject({ broadcast_to_platforms: true, selected_platforms: ['twitch'] });
  });

  it('does not flag a broadcast when the toggle is on but no platform is ticked', async () => {
    const { onSave } = mount({ connectedPlatforms: ['discord'] });
    const d = dialog('Create Announcement');
    fireEvent.click(d.getByLabelText('Broadcast to connected platforms'));
    fireEvent.click(d.getByRole('button', { name: 'Create Announcement' }));

    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave.mock.calls[0][0]).toMatchObject({ broadcast_to_platforms: false, selected_platforms: [] });
  });

  it('closes from Cancel', () => {
    const { onClose } = mount();
    fireEvent.click(dialog('Create Announcement').getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalled();
  });
});

describe('AnnouncementModal edit mode', () => {
  const EXISTING = {
    title: 'Old title',
    content: 'Old body',
    announcement_type: 'event',
    status: 'published',
    is_pinned: true,
    broadcast_to_platforms: true,
    selected_platforms: ['twitch'],
  };

  it('prefills every field from the announcement and uses edit labels', () => {
    mount({ announcement: EXISTING, connectedPlatforms: ['discord', 'twitch'] });
    const d = dialog('Edit Announcement');
    expect(d.getByRole('button', { name: 'Save Changes' })).toBeInTheDocument();
    expect(d.getByLabelText('Title')).toHaveValue('Old title');
    expect(d.getByLabelText('Content')).toHaveValue('Old body');
    expect(d.getByLabelText('Announcement Type')).toHaveValue('event');
    expect(d.getByLabelText('Save As')).toHaveValue('published');
    expect(d.getByLabelText('Pin this announcement')).toBeChecked();
    expect(d.getByLabelText('Broadcast to connected platforms')).toBeChecked();
    expect(d.getByLabelText('Twitch')).toBeChecked();
    expect(d.getByLabelText('Discord')).not.toBeChecked();
  });

  it('falls back to defaults for a sparse announcement and saves edits', async () => {
    const { onSave } = mount({ announcement: {} });
    const d = dialog('Edit Announcement');
    expect(d.getByLabelText('Title')).toHaveValue('');
    expect(d.getByLabelText('Announcement Type')).toHaveValue('general');
    expect(d.getByLabelText('Save As')).toHaveValue('draft');

    fireEvent.change(d.getByLabelText('Title'), { target: { value: 'New' } });
    fireEvent.click(d.getByRole('button', { name: 'Save Changes' }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(onSave.mock.calls[0][0]).toMatchObject({ title: 'New', announcement_type: 'general', status: 'draft' });
  });
});
