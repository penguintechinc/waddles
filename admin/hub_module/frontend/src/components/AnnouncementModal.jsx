/**
 * Announcement Modal Component
 * Modal for creating/editing announcements with platform broadcast capabilities
 * Migrated to use FormModalBuilder from @penguintechinc/react-libs
 */
import { useMemo } from 'react';
import { FormModalBuilder } from './FormModal';
import { WADDLES_COLORS } from '../theme/waddlebotTheme';

export default function AnnouncementModal({
  isOpen,
  onClose,
  onSave,
  announcement,
  connectedPlatforms = [],
}) {
  // Build fields dynamically based on connected platforms
  const fields = useMemo(() => {
    const baseFields = [
      {
        name: 'title',
        type: 'text',
        label: 'Title',
        required: true,
        max: 255,
        placeholder: 'Enter announcement title',
        helpText: 'Max 255 characters',
      },
      {
        name: 'content',
        type: 'textarea',
        label: 'Content',
        required: true,
        max: 2000,
        rows: 6,
        placeholder: 'Enter announcement content',
        helpText: 'Max 2000 characters',
      },
      {
        name: 'announcement_type',
        type: 'select',
        label: 'Announcement Type',
        defaultValue: 'general',
        options: [
          { value: 'general', label: 'General' },
          { value: 'important', label: 'Important' },
          { value: 'event', label: 'Event' },
          { value: 'update', label: 'Update' },
        ],
      },
      {
        name: 'status',
        type: 'select',
        label: 'Save As',
        defaultValue: 'draft',
        description: 'Choose whether to save as draft or publish immediately',
        options: [
          { value: 'draft', label: 'Draft (save for later)' },
          { value: 'published', label: 'Published (visible now)' },
        ],
      },
      {
        name: 'is_pinned',
        type: 'checkbox',
        label: 'Pin this announcement',
        defaultValue: false,
      },
    ];

    // Add broadcast section if there are connected platforms
    if (connectedPlatforms.length > 0) {
      baseFields.push({
        name: 'broadcast_to_platforms',
        type: 'checkbox',
        label: 'Broadcast to connected platforms',
        defaultValue: false,
        description: 'Send this announcement to your connected chat platforms',
      });

      // Add a checkbox for each connected platform
      connectedPlatforms.forEach((platform) => {
        baseFields.push({
          name: `platform_${platform}`,
          type: 'checkbox',
          label: platform.charAt(0).toUpperCase() + platform.slice(1),
          defaultValue: false,
          showWhen: (values) => values.broadcast_to_platforms,
        });
      });
    }

    return baseFields;
  }, [connectedPlatforms]);

  // Handle form submission
  const handleSubmit = async (data) => {
    // Extract selected platforms from individual checkbox fields
    const selectedPlatforms = connectedPlatforms.filter(
      (platform) => data[`platform_${platform}`]
    );

    // Build the payload matching the original API format
    const payload = {
      title: data.title?.trim(),
      content: data.content?.trim(),
      announcement_type: data.announcement_type,
      is_pinned: data.is_pinned,
      status: data.status,
      broadcast_to_platforms: data.broadcast_to_platforms && selectedPlatforms.length > 0,
      selected_platforms: selectedPlatforms,
    };

    await onSave(payload);
  };

  // Create fields with initial values applied from existing announcement (for edit mode)
  const fieldsWithDefaults = useMemo(() => {
    if (!announcement) {
      return fields;
    }

    const initialValues = {
      title: announcement.title || '',
      content: announcement.content || '',
      announcement_type: announcement.announcement_type || 'general',
      status: announcement.status || 'draft',
      is_pinned: announcement.is_pinned || false,
      broadcast_to_platforms: announcement.broadcast_to_platforms || false,
    };

    // Set individual platform checkboxes
    const selectedPlatforms = announcement.selected_platforms || [];
    connectedPlatforms.forEach((platform) => {
      initialValues[`platform_${platform}`] = selectedPlatforms.includes(platform);
    });

    return fields.map((field) => ({
      ...field,
      defaultValue: initialValues[field.name] ?? field.defaultValue,
    }));
  }, [fields, announcement, connectedPlatforms]);

  // Determine submit button text based on mode
  const submitButtonText = announcement ? 'Save Changes' : 'Create Announcement';

  return (
    <FormModalBuilder
      title={announcement ? 'Edit Announcement' : 'Create Announcement'}
      fields={fieldsWithDefaults}
      isOpen={isOpen}
      onClose={onClose}
      onSubmit={handleSubmit}
      submitButtonText={submitButtonText}
      cancelButtonText="Cancel"
      width="lg"
      themeMode="dark"
      colors={WADDLES_COLORS}
    />
  );
}
