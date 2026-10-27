import { useState, useEffect } from 'react';
import { Outlet, Link, useLocation } from 'react-router-dom';
import { Bars3Icon, XMarkIcon } from '@heroicons/react/24/outline';
import { useAuth } from '../contexts/AuthContext';
import GlobalBanner from '../components/GlobalBanner';

function PublicLayout() {
  const { user, isAuthenticated, logout } = useAuth();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);

  // Close the mobile menu on navigation
  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  const navLinks = [
    { to: '/', label: 'Home' },
    { to: '/communities', label: 'Communities' },
    { to: '/live', label: 'Live Streams' },
  ];

  return (
    <div className="min-h-screen flex flex-col bg-navy-950">
      <GlobalBanner />
      {/* Header */}
      <header className="bg-navy-900 border-b border-navy-700 sticky top-0 z-50">
        <nav className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between h-16">
            {/* Logo */}
            <div className="flex items-center">
              <Link to="/" className="flex items-center space-x-2">
                <img src="/waddlebot-logo.png" alt="Waddles" className="w-8 h-8" />
                <span className="text-xl font-bold text-gold-400">Waddles</span>
              </Link>
            </div>

            {/* Navigation */}
            <div className="hidden md:flex items-center space-x-8">
              {navLinks.map((link) => (
                <Link
                  key={link.to}
                  to={link.to}
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
                <>
                  <Link
                    to="/dashboard"
                    className="text-sm font-medium text-navy-300 hover:text-sky-300"
                  >
                    Dashboard
                  </Link>
                  {user?.roles?.includes('platform-admin') && (
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
                  <div className="w-8 h-8 rounded-full bg-navy-700 flex items-center justify-center border border-navy-600">
                    {user?.avatarUrl ? (
                      <img
                        src={user.avatarUrl}
                        alt={user.username}
                        className="w-8 h-8 rounded-full"
                      />
                    ) : (
                      <span className="text-sky-400 font-medium">
                        {user?.username?.charAt(0).toUpperCase()}
                      </span>
                    )}
                  </div>
                </>
              ) : (
                <Link to="/login" className="btn btn-primary">
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
                className="p-2 rounded-lg text-navy-300 hover:text-sky-300 hover:bg-navy-800 focus:outline-none focus:ring-2 focus:ring-sky-500"
              >
                {menuOpen ? <XMarkIcon className="w-6 h-6" /> : <Bars3Icon className="w-6 h-6" />}
              </button>
            </div>
          </div>

          {/* Mobile menu panel */}
          {menuOpen && (
            <div id="public-mobile-menu" data-testid="public-mobile-menu" className="md:hidden pb-4 space-y-1">
              {navLinks.map((link) => (
                <Link
                  key={link.to}
                  to={link.to}
                  className={`block px-3 py-2 rounded-lg text-sm font-medium ${
                    location.pathname === link.to
                      ? 'text-gold-400 bg-navy-800'
                      : 'text-navy-300 hover:text-sky-300 hover:bg-navy-800'
                  }`}
                >
                  {link.label}
                </Link>
              ))}
              <div className="border-t border-navy-700 pt-2 mt-2 space-y-1">
                {isAuthenticated ? (
                  <>
                    <Link to="/dashboard" className="block px-3 py-2 rounded-lg text-sm font-medium text-navy-300 hover:text-sky-300 hover:bg-navy-800">
                      Dashboard
                    </Link>
                    {user?.roles?.includes('platform-admin') && (
                      <Link to="/platform" className="block px-3 py-2 rounded-lg text-sm font-medium text-gold-400 hover:text-gold-300 hover:bg-navy-800">
                        Admin
                      </Link>
                    )}
                    <button
                      type="button"
                      onClick={logout}
                      className="block w-full text-left px-3 py-2 rounded-lg text-sm font-medium text-navy-300 hover:text-sky-300 hover:bg-navy-800"
                    >
                      Logout
                    </button>
                  </>
                ) : (
                  <Link to="/login" className="btn btn-primary block text-center">
                    Sign In
                  </Link>
                )}
              </div>
            </div>
          )}
        </nav>
      </header>

      {/* Main content */}
      <main className="flex-1">
        <Outlet />
      </main>

      {/* Footer */}
      <footer className="bg-navy-900 text-navy-400 py-12 border-t border-navy-700">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="grid md:grid-cols-4 gap-8">
            <div>
              <div className="flex items-center space-x-2 mb-4">
                <img src="/waddlebot-logo.png" alt="Waddles" className="w-8 h-8" />
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
