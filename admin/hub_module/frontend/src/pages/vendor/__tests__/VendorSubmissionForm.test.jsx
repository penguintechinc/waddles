/**
 * Tests for the bundle-onboarding vendor submission form -- gating
 * (login/vendor-status required), the multipart submit call, and the
 * PUBLISHED/REJECTED result rendering from `POST /api/v1/apps/{app_id}/versions`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import VendorSubmissionForm from '../VendorSubmissionForm';
import { bundleApi } from '../../../services/api';
import * as AuthContext from '../../../contexts/AuthContext';

vi.mock('../../../services/api', () => ({
  bundleApi: { createVersion: vi.fn() },
}));

function mockAuth(overrides = {}) {
  vi.spyOn(AuthContext, 'useAuth').mockReturnValue({
    user: { id: 42, email: 'vendor@example.com' },
    loading: false,
    isVendor: true,
    hasRole: () => false,
    ...overrides,
  });
}

function mount() {
  return render(
    <MemoryRouter initialEntries={['/vendor/submit']}>
      <VendorSubmissionForm />
    </MemoryRouter>,
  );
}

function manifestFile() {
  return new File(['schema_version: 2'], 'bundle.yaml', { type: 'application/x-yaml' });
}

function componentFile() {
  return new File(['\0asm'], 'bundle.wasm', { type: 'application/wasm' });
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('VendorSubmissionForm', () => {
  it('prompts sign-in when no user is present', () => {
    mockAuth({ user: null });
    mount();
    expect(screen.getByTestId('vendor-submit-login-required')).toBeInTheDocument();
  });

  it('requires vendor status (or platform-admin) to see the form', () => {
    mockAuth({ isVendor: false, hasRole: () => false });
    mount();
    expect(screen.getByTestId('vendor-submit-not-vendor')).toBeInTheDocument();
  });

  it('allows a platform admin without the vendor role through the gate', () => {
    mockAuth({ isVendor: false, hasRole: (role) => role === 'admin' });
    mount();
    expect(screen.getByTestId('vendor-submit-app-id')).toBeInTheDocument();
  });

  it('blocks submit client-side when the component file is missing', async () => {
    mockAuth();
    mount();

    fireEvent.change(screen.getByTestId('vendor-submit-app-id'), {
      target: { value: 'waddles.integrations.vendor-42.mybundle' },
    });
    fireEvent.change(screen.getByTestId('vendor-submit-manifest-file'), {
      target: { files: [manifestFile()] },
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('vendor-submit-button'));
    });

    expect(screen.getByTestId('vendor-submit-error')).toHaveTextContent(
      'pre-built .wasm component is required',
    );
    expect(bundleApi.createVersion).not.toHaveBeenCalled();
  });

  it('submits multipart manifest + component and renders a PUBLISHED (staged) result', async () => {
    mockAuth();
    bundleApi.createVersion.mockResolvedValue({
      data: { success: true, versionId: 7, status: 'PUBLISHED', rejectReason: null },
    });
    mount();

    fireEvent.change(screen.getByTestId('vendor-submit-app-id'), {
      target: { value: 'waddles.integrations.vendor-42.mybundle' },
    });
    fireEvent.change(screen.getByTestId('vendor-submit-manifest-file'), {
      target: { files: [manifestFile()] },
    });
    fireEvent.change(screen.getByTestId('vendor-submit-component-file'), {
      target: { files: [componentFile()] },
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('vendor-submit-button'));
    });

    expect(bundleApi.createVersion).toHaveBeenCalledTimes(1);
    const [calledAppId, calledFormData] = bundleApi.createVersion.mock.calls[0];
    expect(calledAppId).toBe('waddles.integrations.vendor-42.mybundle');
    expect(calledFormData.get('manifest')).toBeInstanceOf(File);
    expect(calledFormData.get('component')).toBeInstanceOf(File);
    expect(calledFormData.get('source')).toBeNull();

    expect(await screen.findByTestId('vendor-submit-status')).toHaveTextContent('PUBLISHED');
  });

  it('renders the WIT-conformance rejection reason for a REJECTED result', async () => {
    mockAuth();
    bundleApi.createVersion.mockResolvedValue({
      data: {
        success: true,
        versionId: 8,
        status: 'REJECTED',
        rejectReason: 'missing required export waddle:bundle/stage@1.0.0',
      },
    });
    mount();

    fireEvent.change(screen.getByTestId('vendor-submit-app-id'), {
      target: { value: 'waddles.integrations.vendor-42.mybundle' },
    });
    fireEvent.change(screen.getByTestId('vendor-submit-manifest-file'), {
      target: { files: [manifestFile()] },
    });
    fireEvent.change(screen.getByTestId('vendor-submit-component-file'), {
      target: { files: [componentFile()] },
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('vendor-submit-button'));
    });

    expect(await screen.findByTestId('vendor-submit-status')).toHaveTextContent('REJECTED');
    expect(screen.getByTestId('vendor-submit-reject-reason')).toHaveTextContent(
      'missing required export',
    );
  });

  it('shows the server error message on a failed submit (e.g. namespace 403)', async () => {
    mockAuth();
    bundleApi.createVersion.mockRejectedValue({
      response: {
        status: 403,
        data: { error: { code: 'vendor_namespace_forbidden', message: 'not your namespace' } },
      },
    });
    mount();

    fireEvent.change(screen.getByTestId('vendor-submit-app-id'), {
      target: { value: 'waddles.integrations.vendor-99.mybundle' },
    });
    fireEvent.change(screen.getByTestId('vendor-submit-manifest-file'), {
      target: { files: [manifestFile()] },
    });
    fireEvent.change(screen.getByTestId('vendor-submit-component-file'), {
      target: { files: [componentFile()] },
    });

    await act(async () => {
      fireEvent.click(screen.getByTestId('vendor-submit-button'));
    });

    expect(await screen.findByTestId('vendor-submit-error')).toHaveTextContent(
      'not your namespace',
    );
  });
});
