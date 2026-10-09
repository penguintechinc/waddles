/**
 * Tests for the detailed cookie-preferences dialog: category rendering,
 * initial state from stored consent, the locked essential category, the three
 * submit paths (save / accept all / reject all) and their failure handling.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

import CookiePreferencesModal from '../CookiePreferencesModal';
import { useCookieConsentContext } from '../../contexts/CookieConsentContext';

vi.mock('../../contexts/CookieConsentContext', () => ({
  useCookieConsentContext: vi.fn(),
}));

const actions = {
  setShowPreferences: vi.fn(),
  savePreferences: vi.fn(),
  acceptAll: vi.fn(),
  rejectNonEssential: vi.fn(),
};

function mount({ showPreferences = true, consent = null } = {}) {
  useCookieConsentContext.mockReturnValue({ showPreferences, consent, ...actions });
  return render(<CookiePreferencesModal />);
}

const toggle = (name) => screen.getByRole('checkbox', { name: `Toggle ${name}` });

beforeEach(() => {
  vi.clearAllMocks();
  actions.savePreferences.mockResolvedValue(undefined);
  actions.acceptAll.mockResolvedValue(undefined);
  actions.rejectNonEssential.mockResolvedValue(undefined);
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('CookiePreferencesModal rendering', () => {
  it('renders nothing while preferences are closed', () => {
    const { container } = mount({ showPreferences: false });
    expect(container).toBeEmptyDOMElement();
  });

  it('lists the four categories with example cookies and a Cookie Policy link', () => {
    mount();
    expect(screen.getByRole('heading', { name: 'Cookie Preferences' })).toBeInTheDocument();
    for (const name of ['Essential Cookies', 'Functional Cookies', 'Analytics Cookies', 'Marketing Cookies']) {
      expect(screen.getByRole('heading', { name })).toBeInTheDocument();
    }
    expect(screen.getByText('session_token')).toBeInTheDocument();
    expect(screen.getByText('_ga')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Cookie Policy page' })).toHaveAttribute('href', '/cookie-policy');
  });

  it('locks the essential category on and marks it Required', () => {
    mount();
    expect(toggle('Essential Cookies')).toBeChecked();
    expect(toggle('Essential Cookies')).toBeDisabled();
    expect(screen.getByText('Required')).toBeInTheDocument();
  });

  it('defaults every optional category to Disabled when no consent exists yet', () => {
    mount();
    for (const name of ['Functional Cookies', 'Analytics Cookies', 'Marketing Cookies']) {
      expect(toggle(name)).not.toBeChecked();
    }
    expect(screen.getAllByText('Disabled')).toHaveLength(3);
    expect(screen.getAllByText('Enabled')).toHaveLength(1);
  });

  it('initialises the toggles from stored consent', () => {
    mount({
      consent: { essential_cookies: true, functional_cookies: true, analytics_cookies: false, marketing_cookies: true },
    });
    expect(toggle('Functional Cookies')).toBeChecked();
    expect(toggle('Analytics Cookies')).not.toBeChecked();
    expect(toggle('Marketing Cookies')).toBeChecked();
  });
});

describe('CookiePreferencesModal actions', () => {
  it('toggles optional categories and saves the exact selection', async () => {
    mount();
    fireEvent.click(toggle('Functional Cookies'));
    fireEvent.click(toggle('Marketing Cookies'));
    fireEvent.click(toggle('Marketing Cookies'));
    fireEvent.click(toggle('Analytics Cookies'));
    fireEvent.click(screen.getByRole('button', { name: 'Save Preferences' }));

    await waitFor(() => expect(actions.savePreferences).toHaveBeenCalledTimes(1));
    expect(actions.savePreferences).toHaveBeenCalledWith({
      essential_cookies: true,
      functional_cookies: true,
      analytics_cookies: true,
      marketing_cookies: false,
    });
  });

  it('cannot switch essential cookies off', () => {
    mount();
    fireEvent.click(toggle('Essential Cookies'));
    expect(toggle('Essential Cookies')).toBeChecked();
  });

  it('Accept All and Reject All delegate to the context', async () => {
    mount();
    fireEvent.click(screen.getByRole('button', { name: 'Accept All' }));
    await waitFor(() => expect(actions.acceptAll).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: 'Reject All' }));
    await waitFor(() => expect(actions.rejectNonEssential).toHaveBeenCalledTimes(1));
  });

  it('closes via the header X', () => {
    mount();
    const header = screen.getByRole('heading', { name: 'Cookie Preferences' }).parentElement;
    fireEvent.click(within(header).getByRole('button'));
    expect(actions.setShowPreferences).toHaveBeenCalledWith(false);
  });

  it('disables every submit button and shows Saving... while a save is in flight', async () => {
    let release;
    actions.savePreferences.mockReturnValue(new Promise((resolve) => { release = resolve; }));
    mount();

    fireEvent.click(screen.getByRole('button', { name: 'Save Preferences' }));

    expect(await screen.findByRole('button', { name: /Saving/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Accept All' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Reject All' })).toBeDisabled();
    release();
    expect(await screen.findByRole('button', { name: 'Save Preferences' })).toBeEnabled();
  });

  it.each([
    ['Save Preferences', 'savePreferences', 'Failed to save preferences:'],
    ['Accept All', 'acceptAll', 'Failed to accept all cookies:'],
    ['Reject All', 'rejectNonEssential', 'Failed to reject non-essential cookies:'],
  ])('logs and recovers when %s fails', async (label, action, message) => {
    actions[action].mockRejectedValue(new Error('server down'));
    mount();

    fireEvent.click(screen.getByRole('button', { name: label }));

    await waitFor(() => expect(console.error).toHaveBeenCalledWith(message, expect.any(Error)));
    await waitFor(() => expect(screen.getByRole('button', { name: label })).toBeEnabled());
  });
});
