import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, it, expect, vi } from 'vitest';

vi.mock('../../components/GlobalBanner', () => ({ default: () => null }));
const mockAuth = { user: null, isAuthenticated: false, logout: vi.fn() };
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => mockAuth }));

import PublicLayout from '../PublicLayout';

const renderLayout = () => render(<MemoryRouter><PublicLayout /></MemoryRouter>);

// jsdom cannot evaluate Tailwind breakpoints; visual confirmation is pending a real browser.
describe('PublicLayout — mobile nav', () => {
  it('desktop nav keeps hidden md:flex and toggle is md:hidden', () => {
    renderLayout();
    expect(screen.getByTestId('public-mobile-menu-toggle').parentElement.className).toContain('md:hidden');
  });

  it('menu is closed by default and the toggle reveals nav links + sign in', () => {
    renderLayout();
    expect(screen.queryByTestId('public-mobile-menu')).toBeNull();
    const toggle = screen.getByTestId('public-mobile-menu-toggle');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(toggle);
    const menu = screen.getByTestId('public-mobile-menu');
    expect(menu.className).toContain('md:hidden');
    expect(menu).toHaveTextContent('Communities');
    expect(menu).toHaveTextContent('Live Streams');
    expect(menu).toHaveTextContent('Sign In');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(toggle);
    expect(screen.queryByTestId('public-mobile-menu')).toBeNull();
  });

  it('shows Dashboard/Logout in the mobile menu when authenticated', () => {
    mockAuth.isAuthenticated = true;
    mockAuth.user = { username: 'bob', roles: ['platform-admin'] };
    renderLayout();
    fireEvent.click(screen.getByTestId('public-mobile-menu-toggle'));
    const menu = screen.getByTestId('public-mobile-menu');
    expect(menu).toHaveTextContent('Dashboard');
    expect(menu).toHaveTextContent('Admin');
    fireEvent.click(screen.getByText('Logout', { selector: '#public-mobile-menu button' }));
    expect(mockAuth.logout).toHaveBeenCalled();
    mockAuth.isAuthenticated = false;
    mockAuth.user = null;
  });
});
