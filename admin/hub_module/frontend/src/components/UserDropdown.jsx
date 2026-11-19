import { useState, useRef, useEffect, useCallback } from 'react';
import { Link, useNavigate, useLocation } from 'react-router-dom';
import {
  UserIcon,
  Cog6ToothIcon,
  BuildingStorefrontIcon,
  ShieldCheckIcon,
  ChartBarIcon,
  UserGroupIcon,
  LinkIcon,
  KeyIcon,
  ArrowLeftOnRectangleIcon,
  ChevronDownIcon,
} from '@heroicons/react/24/outline';
import { useAuth } from '../contexts/AuthContext';

/**
 * UserDropdown — header avatar/username trigger with a sectioned dropdown menu.
 *
 * Sections:
 *  1. Profile    — avatar, display name, username; My Profile, Account Settings
 *  2. Portals    — Vendor Portal, Admin Panel, Super Admin (conditional)
 *  3. Communities — up to 5 joined communities + "View All" overflow link
 *  4. Connections — Connected Platforms, Personal Access Tokens
 *  5. Account    — Logout
 */
function UserDropdown() {
  const { user, logout, isVendor, isSuperAdmin, isCommunityAdmin } = useAuth();
  const [open, setOpen] = useState(false);
  const containerRef = useRef(null);
  const triggerRef = useRef(null);
  const menuRef = useRef(null);
  const navigate = useNavigate();
  const location = useLocation();

  // Close on route change
  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);

  // Close on outside click
  useEffect(() => {
    if (!open) return;

    function handleClick(e) {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setOpen(false);
      }
    }

    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  // Close on Escape; arrow-key navigation
  useEffect(() => {
    if (!open) return;

    function handleKeyDown(e) {
      if (e.key === 'Escape') {
        setOpen(false);
        triggerRef.current?.focus();
        return;
      }

      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const focusable = Array.from(
          menuRef.current?.querySelectorAll(
            'a[href], button:not([disabled])'
          ) ?? []
        );
        if (!focusable.length) return;
        const idx = focusable.indexOf(document.activeElement);
        if (e.key === 'ArrowDown') {
          const next = focusable[(idx + 1) % focusable.length];
          next?.focus();
        } else {
          const prev = focusable[(idx - 1 + focusable.length) % focusable.length];
          prev?.focus();
        }
      }
    }

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [open]);

  // Move focus into menu when it opens
  useEffect(() => {
    if (open) {
      const first = menuRef.current?.querySelector('a[href], button:not([disabled])');
      first?.focus();
    }
  }, [open]);

  const handleLogout = useCallback(async () => {
    setOpen(false);
    await logout();
    navigate('/login');
  }, [logout, navigate]);

  const handleNavClick = useCallback(() => {
    setOpen(false);
  }, []);

  // Derived data
  const initial = (user?.displayName || user?.username || '?').charAt(0).toUpperCase();
  const displayName = user?.displayName || user?.username || 'Account';
  const username = user?.username || '';

  const communities = user?.communities ?? [];
  const visibleCommunities = communities.slice(0, 5);
  const hasMoreCommunities = communities.length > 5;

  // Find first community where user is admin/owner/moderator for the "Admin Panel" link
  const adminRoles = ['community-owner', 'community-admin', 'moderator'];
  const firstAdminCommunity = communities.find((c) => adminRoles.includes(c.role));

  const showPortalsSection = isVendor || firstAdminCommunity || isSuperAdmin;

  return (
    <div className="relative" ref={containerRef}>
      {/* Trigger button */}
      <button
        ref={triggerRef}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="true"
        aria-expanded={open}
        aria-label="User menu"
        className="flex items-center space-x-2 rounded-lg px-2 py-1.5 hover:bg-navy-800 focus:outline-none focus:ring-2 focus:ring-sky-500 focus:ring-offset-1 focus:ring-offset-navy-900 transition-colors"
      >
        {/* Avatar */}
        {user?.avatarUrl ? (
          <img
            src={user.avatarUrl}
            alt={username}
            className="w-8 h-8 rounded-full object-cover"
          />
        ) : (
          <div className="w-8 h-8 rounded-full bg-navy-700 border border-navy-600 flex items-center justify-center flex-shrink-0">
            <span className="text-sky-400 font-medium text-sm">{initial}</span>
          </div>
        )}

        {/* Username — hidden on very small screens */}
        <span className="hidden sm:block text-sm font-medium text-sky-100">
          {username}
        </span>
        <ChevronDownIcon
          className={`w-4 h-4 text-navy-400 transition-transform duration-200 ${open ? 'rotate-180' : ''}`}
        />
      </button>

      {/* Dropdown panel */}
      {open && (
        <div
          ref={menuRef}
          role="menu"
          aria-label="User menu"
          className="absolute right-0 mt-2 w-64 sm:w-72 max-sm:left-0 max-sm:right-0 max-sm:w-screen max-sm:-mx-4
                     bg-navy-800 border border-navy-700 rounded-lg shadow-xl z-50
                     divide-y divide-navy-700 overflow-hidden"
        >
          {/* ── Section 1: Profile info + links ── */}
          <div className="px-4 py-3">
            <div className="flex items-center space-x-3 mb-3">
              {user?.avatarUrl ? (
                <img
                  src={user.avatarUrl}
                  alt={username}
                  className="w-10 h-10 rounded-full object-cover flex-shrink-0"
                />
              ) : (
                <div className="w-10 h-10 rounded-full bg-navy-700 border border-navy-600 flex items-center justify-center flex-shrink-0">
                  <span className="text-sky-400 font-semibold">{initial}</span>
                </div>
              )}
              <div className="min-w-0">
                <p className="text-sm font-semibold text-gold-400 truncate">{displayName}</p>
                {username && displayName !== username && (
                  <p className="text-xs text-navy-400 truncate">@{username}</p>
                )}
                {!displayName || displayName === username ? (
                  <p className="text-xs text-navy-400 truncate">@{username}</p>
                ) : null}
              </div>
            </div>

            <DropdownLink
              to="/dashboard/profile"
              icon={UserIcon}
              label="My Profile"
              onClick={handleNavClick}
            />
            <DropdownLink
              to="/dashboard/settings"
              icon={Cog6ToothIcon}
              label="Account Settings"
              onClick={handleNavClick}
            />
          </div>

          {/* ── Section 2: Portals (conditional) ── */}
          {showPortalsSection && (
            <div className="px-4 py-2">
              <p className="text-xs font-semibold text-navy-400 uppercase tracking-wider mb-1 px-2">
                Portals
              </p>
              {isVendor && (
                <DropdownLink
                  to="/vendor/dashboard"
                  icon={BuildingStorefrontIcon}
                  label="Vendor Portal"
                  onClick={handleNavClick}
                />
              )}
              {firstAdminCommunity && (
                <DropdownLink
                  to={`/admin/${firstAdminCommunity.id}`}
                  icon={ShieldCheckIcon}
                  label="Admin Panel"
                  onClick={handleNavClick}
                />
              )}
              {isSuperAdmin && (
                <DropdownLink
                  to="/superadmin"
                  icon={ChartBarIcon}
                  label="Super Admin"
                  onClick={handleNavClick}
                />
              )}
            </div>
          )}

          {/* ── Section 3: Communities ── */}
          {visibleCommunities.length > 0 && (
            <div className="px-4 py-2">
              <p className="text-xs font-semibold text-navy-400 uppercase tracking-wider mb-1 px-2">
                Communities
              </p>
              {visibleCommunities.map((c) => (
                <DropdownLink
                  key={c.id}
                  to={`/dashboard/community/${c.id}`}
                  icon={UserGroupIcon}
                  label={c.name || 'Unnamed community'}
                  onClick={handleNavClick}
                />
              ))}
              {hasMoreCommunities && (
                <DropdownLink
                  to="/dashboard"
                  icon={UserGroupIcon}
                  label="View All Communities"
                  onClick={handleNavClick}
                  muted
                />
              )}
            </div>
          )}

          {/* ── Section 4: Connections ── */}
          <div className="px-4 py-2">
            <p className="text-xs font-semibold text-navy-400 uppercase tracking-wider mb-1 px-2">
              Connections
            </p>
            <DropdownLink
              to="/dashboard/my-channels"
              icon={LinkIcon}
              label="Connected Platforms"
              onClick={handleNavClick}
            />
            <DropdownLink
              to="/account/tokens"
              icon={KeyIcon}
              label="Personal Access Tokens"
              onClick={handleNavClick}
            />
          </div>

          {/* ── Section 5: Account / Logout ── */}
          <div className="px-4 py-2">
            <button
              role="menuitem"
              onClick={handleLogout}
              className="w-full flex items-center space-x-3 px-2 py-2 rounded-md text-sm
                         text-red-400 hover:text-red-300 hover:bg-red-900/20
                         focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-1 focus:ring-offset-navy-800
                         transition-colors"
            >
              <ArrowLeftOnRectangleIcon className="w-4 h-4 flex-shrink-0" />
              <span>Logout</span>
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/** Reusable dropdown navigation link item. */
function DropdownLink({ to, icon: Icon, label, onClick, muted = false }) {
  return (
    <Link
      to={to}
      role="menuitem"
      onClick={onClick}
      className={`flex items-center space-x-3 px-2 py-2 rounded-md text-sm transition-colors
                  focus:outline-none focus:ring-2 focus:ring-sky-500 focus:ring-offset-1 focus:ring-offset-navy-800
                  ${muted
                    ? 'text-navy-400 hover:text-sky-200 hover:bg-navy-700'
                    : 'text-sky-100 hover:text-gold-400 hover:bg-navy-700'
                  }`}
    >
      <Icon className="w-4 h-4 flex-shrink-0 text-navy-400" />
      <span>{label}</span>
    </Link>
  );
}

export default UserDropdown;
