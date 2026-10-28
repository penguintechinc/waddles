import { useEffect, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { Bars3Icon, XMarkIcon } from '@heroicons/react/24/outline';
import { useAuth } from '../contexts/AuthContext';
import GlobalBanner from '../components/GlobalBanner';
import { MAIN_CONTENT_ID, RouteAnnouncer, RouteOutlet, SkipLink } from '../components/ui';

function PublicLayout() {
  const { user, isAuthenticated, logout } = useAuth();
  const isPlatformAdmin = Boolean(user?.roles?.includes('platform-admin'));
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);

  // Collapse the mobile menu whenever the route changes.
  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  const navLinks = [
    { to: '/', label: 'Home' },
    { to: '/communities', label: 'Communities' },
    { to: '/live', label: 'Live Streams' },
  ];

  return (
    <div className="min-h-screen flex flex-col bg-app">
      <SkipLink />
      <RouteAnnouncer />
      <GlobalBanner />
      {/* Header */}
      <header className="bg-navy-900 border-b border-navy-700 sticky top-0 z-50">
        <nav aria-label="Main" className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between h-16">
            {/* Logo */}
            <div className="flex items-center">
              <Link to="/" className="flex items-center space-x-2">
                <img src="/waddlebot-logo.png" alt="" className="w-8 h-8" />
                <span className="text-xl font-bold text-gold-400">Waddles</span>
              </Link>
            </div>

            {/* Navigation */}
            <div className="hidden md:flex items-center space-x-8">
              {navLinks.map((link) => (
                <Link
                  key={link.to}
                  to={link.to}
                  aria-current={location.pathname === link.to ? 'page' : undefined}
                  className={`text-sm font-medium transition-colors ${
                    location.pathname === link.to
                      ? 'text-gold-400'
                      : 'text-navy-300 hover:text-sky-300'
                  }`}
                >
                  {link.label}
                </Link>
              ))}
            </div>

            {/* Auth buttons (hidden below md — repeated inside the mobile menu) */}
            <div className="hidden md:flex items-center space-x-4">
              {isAuthenticated ? (
                <div className="hidden md:flex items-center gap-4">
                  <Link
                    to="/dashboard"
                    className="text-sm font-medium text-navy-300 hover:text-sky-300"
                  >
                    Dashboard
                  </Link>
                  {isPlatformAdmin && (
                    <Link
                      to="/platform"
                      className="text-sm font-medium text-gold-400 hover:text-gold-300"
                    >
                      Admin
                    </Link>
                  )}
                  <button
                    onClick={logout}
                    className="text-sm font-medium text-navy-300 hover:text-sky-300"
                  >
                    Logout
                  </button>
                  <div className="flex w-8 h-8 rounded-full bg-navy-700 items-center justify-center border border-navy-600">
                    {user?.avatarUrl ? (
                      <img
                        src={user.avatarUrl}
                        alt=""
                        className="w-8 h-8 rounded-full"
                      />
                    ) : (
                      <span className="text-sky-400 font-medium">
                        {user?.username?.charAt(0).toUpperCase()}
                      </span>
                    )}
                  </div>
                </div>
              ) : (
                <Link to="/login" className="btn btn-primary btn-sm">
                  Sign In
                </Link>
              )}
            </div>

            {/* Mobile menu toggle */}
            <div className="flex items-center md:hidden">
              <button
                type="button"
                onClick={() => setMenuOpen((o) => !o)}
                aria-label={menuOpen ? 'Close menu' : 'Open menu'}
                aria-expanded={menuOpen}
                aria-controls="public-mobile-menu"
                data-testid="public-mobile-menu-toggle"
                className="inline-flex h-10 w-10 items-center justify-center rounded-control text-fg-secondary hover:bg-surface-raised hover:text-fg focus:outline-none focus:ring-2 focus:ring-sky-500"
              >
                {menuOpen ? <XMarkIcon className="h-6 w-6" /> : <Bars3Icon className="h-6 w-6" />}
              </button>
            </div>
          </div>
          {menuOpen && (
            <ul id="public-mobile-menu" data-testid="public-mobile-menu" className="md:hidden space-y-1 border-t border-line py-3">
              {navLinks.map((link) => (
                <li key={link.to}>
                  <Link
                    to={link.to}
                    aria-current={location.pathname === link.to ? 'page' : undefined}
                    className={`block rounded-control px-3 py-2 text-sm font-medium ${
                      location.pathname === link.to ? 'bg-surface-raised text-fg-accent' : 'text-fg-secondary hover:bg-surface-raised hover:text-fg'
                    }`}
                  >
                    {link.label}
                  </Link>
                </li>
              ))}
              {isAuthenticated && (
                <>
                  <li>
                    <Link to="/dashboard" className="block rounded-control px-3 py-2 text-sm font-medium text-fg-secondary hover:bg-surface-raised hover:text-fg">
                      Dashboard
                    </Link>
                  </li>
                  {isPlatformAdmin && (
                    <li>
                      <Link to="/platform" className="block rounded-control px-3 py-2 text-sm font-medium text-fg-accent hover:bg-surface-raised">
                        Admin
                      </Link>
                    </li>
                  )}
                  <li>
                    <button
                      type="button"
                      onClick={logout}
                      className="block w-full rounded-control px-3 py-2 text-left text-sm font-medium text-fg-secondary hover:bg-surface-raised hover:text-fg"
                    >
                      Logout
                    </button>
                  </li>
                </>
              )}
              {!isAuthenticated && (
                <li>
                  <Link to="/login" className="btn btn-primary block text-center">
                    Sign In
                  </Link>
                </li>
              )}
            </ul>
          )}
        </nav>
      </header>

      {/* Main content */}
      <main id={MAIN_CONTENT_ID} tabIndex={-1} className="flex-1 focus:outline-none">
        <RouteOutlet />
      </main>

      {/* Footer */}
      <footer className="bg-navy-900 text-navy-400 py-12 border-t border-navy-700">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="grid md:grid-cols-4 gap-8">
            <div>
              <div className="flex items-center space-x-2 mb-4">
                <img src="/waddlebot-logo.png" alt="" className="w-8 h-8" />
                <span className="text-lg font-bold text-gold-400">Waddles</span>
              </div>
              <p className="text-sm">
                Multi-platform community and workforce management across all your platforms.
              </p>
            </div>
            <div>
              <h4 className="text-sky-100 font-medium mb-4">Platform</h4>
              <ul className="space-y-2 text-sm">
                <li><Link to="/communities" className="hover:text-sky-300">Communities</Link></li>
                <li><Link to="/live" className="hover:text-sky-300">Live Streams</Link></li>
              </ul>
            </div>
            <div>
              <h4 className="text-sky-100 font-medium mb-4">Resources</h4>
              <ul className="space-y-2 text-sm">
                <li><a href="https://docs.waddlebot.io" className="hover:text-sky-300">Documentation</a></li>
                <li><a href="https://github.com/penguintech-io/waddlebot" className="hover:text-sky-300">GitHub</a></li>
              </ul>
            </div>
            <div>
              <h4 className="text-sky-100 font-medium mb-4">Company</h4>
              <ul className="space-y-2 text-sm">
                <li><a href="https://penguintech.io" className="hover:text-sky-300">PenguinTech</a></li>
              </ul>
            </div>
          </div>
          <div className="border-t border-navy-700 mt-8 pt-8 text-sm text-center">
            &copy; {new Date().getFullYear()} PenguinTech. All rights reserved.
          </div>
        </div>
      </footer>
    </div>
  );
}

export default PublicLayout;
