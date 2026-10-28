/**
 * Vendor Analytics Dashboard
 * Full analytics suite: sales overview, install trends, discount code performance,
 * per-community drill-down, and CSV export.
 */
import { useEffect, useState, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowDownTrayIcon,
  ArrowTrendingUpIcon,
  ArrowTrendingDownIcon,
  ChevronUpIcon,
  ChevronDownIcon,
  ChevronUpDownIcon,
  ArrowDownCircleIcon,
  ArrowUpCircleIcon,
  CurrencyDollarIcon,
  UserGroupIcon,
  ArrowPathIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CheckCircleIcon,
  XCircleIcon,
  ClockIcon,
} from '@heroicons/react/24/outline';
import api from '../../services/api';

// ─── Constants ────────────────────────────────────────────────────────────────

const PERIODS = [
  { label: 'Today', value: 'today' },
  { label: '7 Days', value: '7d' },
  { label: '30 Days', value: '30d' },
  { label: 'MTD', value: 'mtd' },
  { label: 'YTD', value: 'ytd' },
  { label: 'All Time', value: 'all' },
];

const COMMUNITIES_PAGE_SIZE = 20;

// ─── Helpers ──────────────────────────────────────────────────────────────────

const formatCurrency = (cents) =>
  new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(
    (cents ?? 0) / 100
  );

const formatPct = (val) =>
  val != null ? `${Number(val).toFixed(1)}%` : '—';

const formatNum = (val) =>
  val != null ? Number(val).toLocaleString() : '—';

// ─── Skeleton ─────────────────────────────────────────────────────────────────

function Skeleton({ className = '' }) {
  return (
    <div className={`animate-pulse bg-navy-700 rounded ${className}`} />
  );
}

// ─── StatCard ─────────────────────────────────────────────────────────────────

function StatCard({ label, value, subtitle, trend, color = 'blue', loading }) {
  const colors = {
    blue: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
    green: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
    gold: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
    red: 'bg-red-500/10 text-red-400 border-red-500/20',
  };

  return (
    <div className={`${colors[color]} border rounded-lg p-5`}>
      <p className="text-xs font-medium text-navy-300 uppercase tracking-wide">{label}</p>
      {loading ? (
        <>
          <Skeleton className="h-9 w-24 mt-2" />
          <Skeleton className="h-3 w-32 mt-2" />
        </>
      ) : (
        <>
          <p className="text-3xl font-bold mt-1">{value}</p>
          <div className="flex items-center space-x-2 mt-1">
            {trend != null && (
              <span
                className={`flex items-center text-xs font-medium ${
                  trend >= 0 ? 'text-emerald-400' : 'text-red-400'
                }`}
              >
                {trend >= 0 ? (
                  <ArrowTrendingUpIcon className="w-3 h-3 mr-0.5" />
                ) : (
                  <ArrowTrendingDownIcon className="w-3 h-3 mr-0.5" />
                )}
                {Math.abs(trend).toFixed(1)}%
              </span>
            )}
            {subtitle && (
              <p className="text-xs text-navy-400">{subtitle}</p>
            )}
          </div>
        </>
      )}
    </div>
  );
}

// ─── CSS Bar Chart ────────────────────────────────────────────────────────────

function InstallsChart({ data, loading, error, onRetry }) {
  if (loading) {
    return (
      <div className="h-48 flex items-end space-x-1 px-2">
        {Array.from({ length: 14 }).map((_, i) => (
          <div key={i} className="flex-1 flex flex-col justify-end space-y-0.5">
            <Skeleton className="w-full" style={{ height: `${20 + Math.random() * 60}%` }} />
          </div>
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className="h-48 flex flex-col items-center justify-center text-navy-400">
        <p className="text-sm mb-3">{error}</p>
        <button
          onClick={onRetry}
          className="flex items-center space-x-1 text-sky-400 hover:text-sky-300 text-sm"
        >
          <ArrowPathIcon className="w-4 h-4" />
          <span>Retry</span>
        </button>
      </div>
    );
  }

  if (!data || data.length === 0) {
    return (
      <div className="h-48 flex items-center justify-center border border-dashed border-navy-600 rounded-lg">
        <p className="text-navy-500 text-sm">No installation data yet</p>
      </div>
    );
  }

  const maxVal = Math.max(...data.map((d) => Math.max(d.installs ?? 0, d.uninstalls ?? 0)), 1);

  return (
    <div className="space-y-2">
      {/* Legend */}
      <div className="flex items-center space-x-4 text-xs text-navy-400">
        <span className="flex items-center space-x-1">
          <ArrowDownCircleIcon className="w-3 h-3 text-emerald-400" />
          <span>Installs</span>
        </span>
        <span className="flex items-center space-x-1">
          <ArrowUpCircleIcon className="w-3 h-3 text-red-400" />
          <span>Uninstalls</span>
        </span>
      </div>

      {/* Bars */}
      <div className="flex items-end space-x-1 h-40 overflow-x-auto pb-1">
        {data.map((d, i) => {
          const installH = Math.round(((d.installs ?? 0) / maxVal) * 100);
          const uninstallH = Math.round(((d.uninstalls ?? 0) / maxVal) * 100);
          return (
            <div key={i} className="flex-shrink-0 flex flex-col items-center" style={{ minWidth: '28px' }}>
              <div className="flex items-end space-x-0.5 h-32">
                {/* Install bar */}
                <div
                  className="w-3 bg-emerald-500 rounded-t transition-all duration-300"
                  style={{ height: `${installH}%`, minHeight: installH > 0 ? '2px' : '0' }}
                  title={`Installs: ${d.installs ?? 0}`}
                />
                {/* Uninstall bar */}
                <div
                  className="w-3 bg-red-500 rounded-t transition-all duration-300"
                  style={{ height: `${uninstallH}%`, minHeight: uninstallH > 0 ? '2px' : '0' }}
                  title={`Uninstalls: ${d.uninstalls ?? 0}`}
                />
              </div>
              {/* X-axis label */}
              <p className="text-navy-500 mt-1 text-center" style={{ fontSize: '9px', lineHeight: 1.2 }}>
                {d.label ?? ''}
              </p>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Sort Icon ────────────────────────────────────────────────────────────────

function SortIcon({ field, sortBy, sortDir }) {
  if (sortBy !== field) return <ChevronUpDownIcon className="w-3 h-3 ml-1 opacity-40" />;
  return sortDir === 'asc'
    ? <ChevronUpIcon className="w-3 h-3 ml-1 text-sky-400" />
    : <ChevronDownIcon className="w-3 h-3 ml-1 text-sky-400" />;
}

// ─── Export Dropdown ──────────────────────────────────────────────────────────

function ExportDropdown({ period }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const triggerExport = async (type) => {
    setOpen(false);
    try {
      const response = await api.get('/vendor/analytics/export', {
        params: { type, period },
        responseType: 'blob',
      });
      const url = URL.createObjectURL(new Blob([response.data]));
      const a = document.createElement('a');
      a.href = url;
      a.download = `vendor-${type}-${period}-${Date.now()}.csv`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      // silently ignore — user can retry
    }
  };

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center space-x-2 bg-navy-700 hover:bg-navy-600 border border-navy-600 text-white px-3 py-2 rounded-lg text-sm transition-colors"
      >
        <ArrowDownTrayIcon className="w-4 h-4" />
        <span>Export</span>
        <ChevronDownIcon className="w-3 h-3 opacity-60" />
      </button>
      {open && (
        <div className="absolute right-0 mt-1 w-44 bg-navy-800 border border-navy-700 rounded-lg shadow-xl z-20 py-1">
          <button
            onClick={() => triggerExport('sales')}
            className="w-full text-left px-4 py-2 text-sm text-navy-200 hover:bg-navy-700 hover:text-white transition-colors"
          >
            Export Sales CSV
          </button>
          <button
            onClick={() => triggerExport('usage')}
            className="w-full text-left px-4 py-2 text-sm text-navy-200 hover:bg-navy-700 hover:text-white transition-colors"
          >
            Export Usage CSV
          </button>
        </div>
      )}
    </div>
  );
}

// ─── Discount Code Status Badge ───────────────────────────────────────────────

function StatusBadge({ status }) {
  const map = {
    active: { cls: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20', icon: CheckCircleIcon, label: 'Active' },
    inactive: { cls: 'bg-navy-700 text-navy-400 border-navy-600', icon: XCircleIcon, label: 'Inactive' },
    expired: { cls: 'bg-red-500/10 text-red-400 border-red-500/20', icon: XCircleIcon, label: 'Expired' },
    pending: { cls: 'bg-orange-500/10 text-orange-400 border-orange-500/20', icon: ClockIcon, label: 'Pending' },
  };
  const { cls, icon: Icon, label } = map[status] ?? map.inactive;
  return (
    <span className={`inline-flex items-center space-x-1 px-2 py-0.5 rounded border text-xs font-medium ${cls}`}>
      <Icon className="w-3 h-3" />
      <span>{label}</span>
    </span>
  );
}

// ─── Main Component ───────────────────────────────────────────────────────────

function VendorAnalytics() {
  // Period selector
  const [period, setPeriod] = useState('30d');

  // Sales overview
  const [sales, setSales] = useState(null);
  const [salesLoading, setSalesLoading] = useState(true);
  const [salesError, setSalesError] = useState(null);

  // Install chart
  const [installs, setInstalls] = useState(null);
  const [installsLoading, setInstallsLoading] = useState(true);
  const [installsError, setInstallsError] = useState(null);

  // Discount code summary
  const [discountSummary, setDiscountSummary] = useState(null);
  const [discountLoading, setDiscountLoading] = useState(true);
  const [discountError, setDiscountError] = useState(null);

  // Communities drill-down
  const [communities, setCommunities] = useState(null);
  const [commPage, setCommPage] = useState(1);
  const [commSort, setCommSort] = useState({ field: 'installed_at', dir: 'desc' });
  const [commLoading, setCommLoading] = useState(true);
  const [commError, setCommError] = useState(null);
  const [expandedComm, setExpandedComm] = useState(null);

  // ── Loaders ──────────────────────────────────────────────────────────────

  const loadSales = useCallback(async (p) => {
    setSalesLoading(true);
    setSalesError(null);
    try {
      const res = await api.get('/vendor/analytics/sales', { params: { period: p } });
      setSales(res.data?.data ?? res.data);
    } catch (err) {
      setSalesError(err.response?.data?.error?.message || 'Failed to load sales data');
    } finally {
      setSalesLoading(false);
    }
  }, []);

  const loadInstalls = useCallback(async (p) => {
    setInstallsLoading(true);
    setInstallsError(null);
    try {
      const res = await api.get('/vendor/analytics/installs', {
        params: { period: p, granularity: 'daily' },
      });
      setInstalls(res.data?.data ?? res.data);
    } catch (err) {
      setInstallsError(err.response?.data?.error?.message || 'Failed to load install data');
    } finally {
      setInstallsLoading(false);
    }
  }, []);

  const loadDiscountSummary = useCallback(async () => {
    setDiscountLoading(true);
    setDiscountError(null);
    try {
      const res = await api.get('/vendor/analytics/discount-codes');
      setDiscountSummary(res.data?.data ?? res.data);
    } catch (err) {
      setDiscountError(err.response?.data?.error?.message || 'Failed to load discount data');
    } finally {
      setDiscountLoading(false);
    }
  }, []);

  const loadCommunities = useCallback(async (page, sort) => {
    setCommLoading(true);
    setCommError(null);
    try {
      const res = await api.get('/vendor/analytics/communities', {
        params: {
          page,
          limit: COMMUNITIES_PAGE_SIZE,
          sortBy: sort.field,
          sortDir: sort.dir,
        },
      });
      setCommunities(res.data?.data ?? res.data);
    } catch (err) {
      setCommError(err.response?.data?.error?.message || 'Failed to load community data');
    } finally {
      setCommLoading(false);
    }
  }, []);

  // ── Effects ───────────────────────────────────────────────────────────────

  // Period-dependent: sales + installs
  useEffect(() => {
    loadSales(period);
    loadInstalls(period);
  }, [period, loadSales, loadInstalls]);

  // Period-independent: discount summary (once)
  useEffect(() => {
    loadDiscountSummary();
  }, [loadDiscountSummary]);

  // Communities: re-fetch on page/sort change
  useEffect(() => {
    loadCommunities(commPage, commSort);
  }, [commPage, commSort, loadCommunities]);

  // ── Handlers ──────────────────────────────────────────────────────────────

  const handleCommSort = (field) => {
    setCommSort((prev) =>
      prev.field === field
        ? { field, dir: prev.dir === 'asc' ? 'desc' : 'asc' }
        : { field, dir: 'asc' }
    );
    setCommPage(1);
  };

  const totalCommPages = communities?.total
    ? Math.ceil(communities.total / COMMUNITIES_PAGE_SIZE)
    : 1;

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="space-y-8">

      {/* ── Header ── */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-amber-400">Analytics</h1>
          <p className="text-navy-300 mt-1 text-sm">
            Track installs, revenue, and performance across your modules
          </p>
        </div>

        <div className="flex items-center space-x-3">
          {/* Period tabs */}
          <div className="flex bg-navy-800 border border-navy-700 rounded-lg p-0.5 overflow-x-auto">
            {PERIODS.map((p) => (
              <button
                key={p.value}
                onClick={() => setPeriod(p.value)}
                className={`px-3 py-1.5 text-xs font-medium rounded-md whitespace-nowrap transition-colors ${
                  period === p.value
                    ? 'bg-sky-600 text-white'
                    : 'text-navy-300 hover:text-white'
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>

          <ExportDropdown period={period} />
        </div>
      </div>

      {/* ── Sales Overview Cards ── */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          label="Total Installations"
          value={formatNum(sales?.totalInstallations)}
          trend={sales?.totalInstallationsTrend}
          subtitle="All modules"
          color="blue"
          loading={salesLoading}
        />
        <StatCard
          label="Active Installations"
          value={formatNum(sales?.activeInstallations)}
          trend={sales?.activeInstallationsTrend}
          subtitle="Currently active"
          color="green"
          loading={salesLoading}
        />
        <StatCard
          label="Revenue MTD"
          value={sales ? formatCurrency(sales.revenueMtd) : '—'}
          trend={sales?.revenueMtdTrend}
          subtitle="Month to date"
          color="gold"
          loading={salesLoading}
        />
        <StatCard
          label="Churn Rate"
          value={sales ? formatPct(sales.churnRate) : '—'}
          trend={sales?.churnRateTrend != null ? -sales.churnRateTrend : null}
          subtitle="This period"
          color={sales?.churnRate > 10 ? 'red' : 'blue'}
          loading={salesLoading}
        />
      </div>

      {/* Sales error */}
      {salesError && !salesLoading && (
        <div className="bg-red-500/10 border border-red-500/20 text-red-400 px-4 py-3 rounded-lg flex items-center justify-between text-sm">
          <span>{salesError}</span>
          <button
            onClick={() => loadSales(period)}
            className="flex items-center space-x-1 hover:text-red-300"
          >
            <ArrowPathIcon className="w-4 h-4" />
            <span>Retry</span>
          </button>
        </div>
      )}

      {/* ── Installations Chart ── */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-6">
        <h2 className="text-lg font-bold text-white mb-4">Installation Trends</h2>
        <InstallsChart
          data={installs?.series ?? installs}
          loading={installsLoading}
          error={installsError}
          onRetry={() => loadInstalls(period)}
        />
      </div>

      {/* ── Discount Code Performance ── */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-bold text-white">Discount Code Performance</h2>
          <Link
            to="/vendor/discount-codes"
            className="text-sky-400 hover:text-sky-300 text-sm transition-colors"
          >
            View all →
          </Link>
        </div>

        {discountLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : discountError ? (
          <div className="text-red-400 text-sm flex items-center justify-between">
            <span>{discountError}</span>
            <button
              onClick={loadDiscountSummary}
              className="flex items-center space-x-1 hover:text-red-300"
            >
              <ArrowPathIcon className="w-4 h-4" />
              <span>Retry</span>
            </button>
          </div>
        ) : !discountSummary || (discountSummary?.codes ?? discountSummary)?.length === 0 ? (
          <p className="text-navy-400 text-sm text-center py-6">
            No discount codes yet.{' '}
            <Link to="/vendor/discount-codes" className="text-sky-400 hover:underline">
              Create one
            </Link>
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-navy-700 text-left text-xs text-navy-400 uppercase tracking-wide">
                  <th className="pb-2 pr-4">Code</th>
                  <th className="pb-2 pr-4">Redemptions</th>
                  <th className="pb-2 pr-4">Revenue Impact</th>
                  <th className="pb-2 pr-4">Remaining Uses</th>
                  <th className="pb-2">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-navy-700/50">
                {(discountSummary?.codes ?? discountSummary).slice(0, 5).map((code) => (
                  <tr key={code.id ?? code.code} className="hover:bg-navy-750 transition-colors">
                    <td className="py-3 pr-4 font-mono text-amber-400 font-medium">{code.code}</td>
                    <td className="py-3 pr-4 text-white">{formatNum(code.redemptions)}</td>
                    <td className="py-3 pr-4 text-emerald-400">{formatCurrency(code.revenueImpact ?? 0)}</td>
                    <td className="py-3 pr-4 text-navy-200">
                      {code.remainingUses != null
                        ? code.remainingUses === -1
                          ? '∞'
                          : formatNum(code.remainingUses)
                        : '—'}
                    </td>
                    <td className="py-3">
                      <StatusBadge status={code.status ?? 'active'} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Per-Community Drill-down ── */}
      <div className="bg-navy-800 border border-navy-700 rounded-lg p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-bold text-white">Community Installations</h2>
          <div className="flex items-center space-x-2 text-xs text-navy-400">
            <UserGroupIcon className="w-4 h-4" />
            <span>
              {communities?.total != null
                ? `${communities.total.toLocaleString()} total`
                : ''}
            </span>
          </div>
        </div>

        {commLoading ? (
          <div className="space-y-2">
            {Array.from({ length: 5 }).map((_, i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : commError ? (
          <div className="text-red-400 text-sm flex items-center justify-between">
            <span>{commError}</span>
            <button
              onClick={() => loadCommunities(commPage, commSort)}
              className="flex items-center space-x-1 hover:text-red-300"
            >
              <ArrowPathIcon className="w-4 h-4" />
              <span>Retry</span>
            </button>
          </div>
        ) : !communities || (communities?.items ?? communities)?.length === 0 ? (
          <p className="text-navy-400 text-sm text-center py-6">
            No community installations yet
          </p>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-navy-700 text-left text-xs text-navy-400 uppercase tracking-wide">
                    {[
                      { label: 'Community', field: 'community_name' },
                      { label: 'Module', field: 'module_name' },
                      { label: 'Install Date', field: 'installed_at' },
                      { label: 'Status', field: 'status' },
                      { label: 'Discount Code', field: 'discount_code' },
                      { label: 'Last Active', field: 'last_active_at' },
                    ].map(({ label, field }) => (
                      <th
                        key={field}
                        className="pb-2 pr-4 cursor-pointer hover:text-white transition-colors select-none"
                        onClick={() => handleCommSort(field)}
                      >
                        <span className="inline-flex items-center">
                          {label}
                          <SortIcon
                            field={field}
                            sortBy={commSort.field}
                            sortDir={commSort.dir}
                          />
                        </span>
                      </th>
                    ))}
                    <th className="pb-2 w-8" />
                  </tr>
                </thead>
                <tbody className="divide-y divide-navy-700/50">
                  {(communities?.items ?? communities).map((comm, idx) => {
                    const isExpanded = expandedComm === (comm.id ?? idx);
                    return (
                      <>
                        <tr
                          key={comm.id ?? idx}
                          data-click-delegate
                          className="hover:bg-navy-750 transition-colors cursor-pointer"
                          onClick={() =>
                            setExpandedComm(isExpanded ? null : (comm.id ?? idx))
                          }
                        >
                          <td className="py-3 pr-4 font-medium text-white">
                            <button type="button" aria-expanded={isExpanded} className="text-left font-medium hover:underline focus-visible:underline">{comm.communityName ?? comm.community_name ?? '—'}</button>
                          </td>
                          <td className="py-3 pr-4 text-navy-200">
                            {comm.moduleName ?? comm.module_name ?? '—'}
                          </td>
                          <td className="py-3 pr-4 text-navy-300 whitespace-nowrap">
                            {comm.installedAt ?? comm.installed_at
                              ? new Date(comm.installedAt ?? comm.installed_at).toLocaleDateString()
                              : '—'}
                          </td>
                          <td className="py-3 pr-4">
                            <StatusBadge status={comm.status ?? 'active'} />
                          </td>
                          <td className="py-3 pr-4 font-mono text-amber-400 text-xs">
                            {comm.discountCode ?? comm.discount_code ?? (
                              <span className="text-navy-500 font-sans not-italic">None</span>
                            )}
                          </td>
                          <td className="py-3 pr-4 text-navy-300 whitespace-nowrap">
                            {comm.lastActiveAt ?? comm.last_active_at
                              ? new Date(
                                  comm.lastActiveAt ?? comm.last_active_at
                                ).toLocaleDateString()
                              : '—'}
                          </td>
                          <td className="py-3">
                            {isExpanded ? (
                              <ChevronUpIcon className="w-4 h-4 text-navy-400" />
                            ) : (
                              <ChevronDownIcon className="w-4 h-4 text-navy-400" />
                            )}
                          </td>
                        </tr>

                        {/* Expanded detail row */}
                        {isExpanded && (
                          <tr key={`${comm.id ?? idx}-expanded`} className="bg-navy-900/50">
                            <td colSpan={7} className="py-3 px-4">
                              <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 text-xs text-navy-300">
                                {comm.guildId && (
                                  <div>
                                    <span className="text-navy-500 uppercase tracking-wide text-xxs">Guild ID</span>
                                    <p className="font-mono text-navy-200 mt-0.5">{comm.guildId}</p>
                                  </div>
                                )}
                                {comm.memberCount != null && (
                                  <div>
                                    <span className="text-navy-500 uppercase tracking-wide text-xxs">Members</span>
                                    <p className="text-navy-200 mt-0.5">{formatNum(comm.memberCount)}</p>
                                  </div>
                                )}
                                {comm.plan && (
                                  <div>
                                    <span className="text-navy-500 uppercase tracking-wide text-xxs">Plan</span>
                                    <p className="text-navy-200 capitalize mt-0.5">{comm.plan}</p>
                                  </div>
                                )}
                                {comm.renewalDate && (
                                  <div>
                                    <span className="text-navy-500 uppercase tracking-wide text-xxs">Renewal</span>
                                    <p className="text-navy-200 mt-0.5">
                                      {new Date(comm.renewalDate).toLocaleDateString()}
                                    </p>
                                  </div>
                                )}
                                {comm.revenueTotal != null && (
                                  <div>
                                    <span className="text-navy-500 uppercase tracking-wide text-xxs">Revenue</span>
                                    <p className="text-emerald-400 mt-0.5">{formatCurrency(comm.revenueTotal)}</p>
                                  </div>
                                )}
                                {comm.notes && (
                                  <div className="col-span-2">
                                    <span className="text-navy-500 uppercase tracking-wide text-xxs">Notes</span>
                                    <p className="text-navy-300 mt-0.5">{comm.notes}</p>
                                  </div>
                                )}
                              </div>
                            </td>
                          </tr>
                        )}
                      </>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            {totalCommPages > 1 && (
              <div className="flex items-center justify-between mt-4 pt-4 border-t border-navy-700">
                <p className="text-xs text-navy-400">
                  Page {commPage} of {totalCommPages}
                </p>
                <div className="flex items-center space-x-2">
                  <button
                    aria-label="Previous page"
                    onClick={() => setCommPage((p) => Math.max(1, p - 1))}
                    disabled={commPage <= 1}
                    className="p-1.5 rounded border border-navy-600 text-navy-300 hover:text-white hover:border-navy-500 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                  >
                    <ChevronLeftIcon className="w-4 h-4" />
                  </button>
                  <button
                    aria-label="Next page"
                    onClick={() => setCommPage((p) => Math.min(totalCommPages, p + 1))}
                    disabled={commPage >= totalCommPages}
                    className="p-1.5 rounded border border-navy-600 text-navy-300 hover:text-white hover:border-navy-500 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
                  >
                    <ChevronRightIcon className="w-4 h-4" />
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default VendorAnalytics;
